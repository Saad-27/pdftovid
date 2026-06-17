
from __future__ import annotations

import json
import os
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, NamedTuple

log = logging.getLogger("stage_g")

# Renderer directory, sibling of `worker/` and `backend/`. The npm-installed
# Remotion CLI lives in renderer/node_modules/.bin/remotion.
RENDERER_DIR = Path(__file__).resolve().parent.parent / "renderer"

# Frame-progress lines from Remotion look roughly like:
#   "Rendered frame 142/8856"
# or with carriage returns instead of newlines:
#   "Encoding... 50% ETA 00:02:15"
# We match the first form because it gives us exact counts. The second
# form (the encoder pass) is short anyway so we don't bother parsing it.
_FRAME_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


class StageGError(Exception):
    """Stage G failure. .code is what we persist to the jobs table."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_remotion_bin() -> Path:
    """
    Locate the Remotion CLI binary inside renderer/node_modules.

    Why not just `npx remotion`? Two reasons:
      1. npx adds 0.5-2s of startup overhead per invocation.
      2. In CI / containers, npx sometimes re-downloads packages if the
         registry cache is missing, leading to surprise network calls.

    Falling back to `npx remotion` is fine if the direct path is missing.
    """
    direct = RENDERER_DIR / "node_modules" / ".bin" / "remotion"
    if direct.exists():
        return direct
    # On Windows the entry is `remotion.cmd`. We accept either.
    direct_cmd = RENDERER_DIR / "node_modules" / ".bin" / "remotion.cmd"
    if direct_cmd.exists():
        return direct_cmd
    raise StageGError(
        "RENDER_FAILED",
        f"Could not find Remotion CLI at {direct}. "
        f"Did you `cd renderer && npm install`?",
    )


def _resolve_node_bin() -> str:
    """
    Return 'node' or raise. We don't actually need to validate the version
    here — Remotion's own startup will complain if Node is too old.
    """
    import shutil

    found = shutil.which("node")
    if not found:
        raise StageGError(
            "RENDER_FAILED",
            "node executable not found on PATH. Install Node 18+ and retry.",
        )
    return found


def _link_job_into_public(job_dir: Path) -> Path:
    """
    Symlink the job dir into `renderer/public/<job_uuid>/` so that
    Remotion's stock static-file resolution (anchored at renderer/public)
    can find the audio and image assets.

    Returns the symlink path so the caller can clean it up afterwards.
    """
    public_dir = RENDERER_DIR / "public"
    public_dir.mkdir(parents=True, exist_ok=True)

    job_public_dir = public_dir / job_dir.name

    # An existing symlink (or any leftover entry) at this path should be
    # cleared first. `.exists()` returns False for a dangling symlink,
    # so we also check `is_symlink()`.
    if job_public_dir.is_symlink() or job_public_dir.exists():
        try:
            job_public_dir.unlink()
        except IsADirectoryError:
            # Defensive: someone created a real directory here. Don't
            # silently nuke it; surface the problem.
            raise StageGError(
                "RENDER_FAILED",
                f"{job_public_dir} exists and is a directory, not a symlink. "
                f"Remove it manually and retry.",
            )

    job_public_dir.symlink_to(job_dir, target_is_directory=True)
    log.info("Stage G: symlinked %s -> %s", job_public_dir, job_dir)
    return job_public_dir


def _unlink_job_from_public(job_public_dir: Path) -> None:
    """Idempotent cleanup of the per-job symlink created above."""
    if job_public_dir.is_symlink():
        try:
            job_public_dir.unlink()
            log.info("Stage G: removed symlink %s", job_public_dir)
        except OSError as e:
            # Cleanup failure is annoying but not fatal — the next render
            # for this same job_id will overwrite it.
            log.warning("Could not remove symlink %s: %s", job_public_dir, e)


def _faststart_mp4(path: Path) -> None:
    """
    Re-multiplex the MP4 to move the `moov` atom to the start of the file.

    Why this is necessary: Remotion (via ffmpeg) writes MP4s with the moov
    atom at the END of the file by default — because the encoder only
    knows the final byte offsets after all frames are written, putting
    the atom up front would require an extra pass.

    Local players read the whole file before playing, so they find the
    moov atom fine. Browsers serving the file progressively over HTTP
    Range requests need the moov atom at the START to know where audio
    packets live. Without faststart, browsers commonly play video but
    drop audio (the symptom we hit: audio works on download, not in <video>).

    `-c copy -movflags +faststart` does a stream-copy remux (no
    re-encoding), so this completes in 2-5 seconds for a 45 MB file.
    """
    import shutil

    if not path.exists():
        raise StageGError("RENDER_FAILED", f"Cannot faststart, {path} missing.")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise StageGError(
            "RENDER_FAILED",
            "ffmpeg not found on PATH. It's needed for the faststart pass "
            "(and Remotion itself uses it internally).",
        )

    tmp = path.with_name(path.stem + ".faststart.mp4")
    if tmp.exists():
        tmp.unlink()

    cmd = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", str(path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(tmp),
    ]
    log.info("Stage G: running faststart pass…")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Don't leave a half-written file lying around.
        if tmp.exists():
            tmp.unlink()
        raise StageGError(
            "RENDER_FAILED",
            f"ffmpeg faststart pass failed (rc={result.returncode}):\n"
            f"{result.stderr[-500:]}",
        )

    # Atomic-ish swap: rename the faststarted file over the original.
    tmp.replace(path)
    log.info("Stage G: faststart complete (moov atom moved to file head).")


class RenderResult(NamedTuple):
    """What a successful Stage G render hands back to the worker."""
    video_url: str
    size_bytes: int


def _upload_to_r2(local_path: Path, object_key: str) -> str:
    """
    Upload the finished MP4 to R2 (public-read); return its public URL.
    Config comes from backend/config.py (env-driven). boto3 >= 1.36 adds an
    upload checksum R2 rejects, so we disable it via the Config flags below
    (Cloudflare's documented mitigation).
    """
    import sys

    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import config  # loads .env and exposes R2_* settings

    missing = [
        name for name in (
            "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET", "R2_PUBLIC_BASE_URL",
        )
        if not getattr(config, name, "")
    ]
    if missing:
        raise StageGError(
            "UPLOAD_FAILED", f"R2 is not configured (missing: {', '.join(missing)})."
        )

    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=config.R2_ACCESS_KEY_ID,
            aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        client.upload_file(
            str(local_path), config.R2_BUCKET, object_key,
            ExtraArgs={
                "ContentType": "video/mp4",
                # Force a download instead of inline playback. The frontend's
                # <a download> attribute is ignored for cross-origin URLs (R2 is
                # a different origin), so the header must live on the object.
                "ContentDisposition": 'attachment; filename="lecture.mp4"',
            },
        )
    except StageGError:
        raise
    except Exception as e:
        raise StageGError("UPLOAD_FAILED", f"R2 upload failed: {e}")

    return f"{config.R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"

def run(
    job_dir: Path,
    on_frame_done: Optional[Callable[[int, int], None]] = None,
) -> "RenderResult":
    """
    Render `<job_dir>/manifest.json` to `<job_dir>/output.mp4`.

    Args:
        job_dir: directory containing manifest.json + audio/ + extracted/.
        on_frame_done: optional callback (rendered, total) called whenever
            Remotion reports a frame-progress line. The job processor uses
            this to bump progress_percent during the long render.
    """
    manifest_path = job_dir / "manifest.json"
    if not manifest_path.exists():
        raise StageGError(
            "RENDER_FAILED", f"manifest.json not found at {manifest_path}"
        )

    # Validate the manifest is parseable before spawning Node. Cheap and
    # gives a much better error message than Remotion's own parse failure.
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        raise StageGError("RENDER_FAILED", f"manifest.json is not valid JSON: {e}")

    if not manifest.get("slides"):
        raise StageGError("RENDER_FAILED", "manifest.json has no slides — nothing to render.")

    output_path = job_dir / "output.mp4"
    # Remove a prior render if one exists; otherwise Remotion's "overwrite"
    # prompt can stall a non-interactive process. --overwrite handles this
    # but defence in depth is fine.
    if output_path.exists():
        output_path.unlink()

    # Make the job dir reachable from renderer/public/<uuid> so that
    # staticFile("<uuid>/audio/seg_x.mp3") resolves via Remotion's
    # internal HTTP server. This is the workaround for the silent-audio
    # bug we hit pointing --public-dir at an arbitrary external dir.
    job_public_dir = _link_job_into_public(job_dir)

    try:
        # Prefix relative manifest paths with the job UUID so they resolve
        # under renderer/public/. Stage F emits paths like
        # "audio/seg_1-1.mp3" and "extracted/page_001_image_001.png" —
        # we turn those into "<uuid>/audio/seg_1-1.mp3" etc.
        prefix = job_dir.name + "/"
        for slide in manifest.get("slides", []):
            if slide.get("image_path"):
                slide["image_path"] = prefix + slide["image_path"]
            for seg in slide.get("segments", []):
                if seg.get("audio_file"):
                    seg["audio_file"] = prefix + seg["audio_file"]

        # Write the composition props to a file rather than passing the
        # whole manifest on the command line. argv has limits (ARG_MAX)
        # and on macOS it's only 256KB — a 50-slide manifest can flirt
        # with that. Remotion accepts --props=path/to/file.json and
        # reads the JSON itself.
        props_path = job_dir / "_remotion_props.json"
        props_path.write_text(json.dumps({"manifest": manifest}))

        remotion_bin = _resolve_remotion_bin()
        _resolve_node_bin()  # raise early if missing

        # Remotion `render` arguments:
        #   <entry>         the file that calls registerRoot()
        #   <composition>   id from <Composition id="LectureVideo" .../>
        #   <output>        destination MP4
        #   --props=PATH    JSON file with composition props (our wrapped manifest)
        #   --log=info      so frame-progress lines show up on stdout
        #   --overwrite     skip the interactive "file exists?" prompt
        # Note: NO --public-dir here. Remotion uses the default
        # `renderer/public/` and our symlink puts the job assets where
        # staticFile() expects them.
        cmd = [
            str(remotion_bin),
            "render",
            "src/index.tsx",
            "LectureVideo",
            str(output_path),
            f"--props={props_path}",
            "--log=info",
            "--overwrite",
        ]
        # Render concurrency is hardware-dependent. Locally (real cores, no
        # throttling) we pass nothing so Remotion auto-picks from CPU count
        # (remotion.config.ts's setConcurrency(null)) — this is what makes a
        # local render fast. On a constrained host, pin REMOTION_CONCURRENCY=1
        # to avoid the OOM we hit on shared-cpu Fly machines.
        concurrency = os.environ.get("REMOTION_CONCURRENCY")
        if concurrency:
            cmd.append(f"--concurrency={concurrency}")

        total_frames = _expected_total_frames(manifest)
        log.info(
            "Stage G: spawning Remotion. Expected %d frames (%.1fs at %dfps).",
            total_frames,
            manifest["video"]["total_duration_seconds"],
            manifest["video"]["framerate"],
        )

        # Run as a subprocess, streaming stdout so we can tail frame counters.
        proc = subprocess.Popen(
            cmd,
            cwd=str(RENDERER_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # interleave; easier to debug
            text=True,
            bufsize=1,  # line-buffered
        )

        last_reported = -1
        captured: list[str] = []
        try:
            # Defensive: stdout could be None on some platforms if redirection fails.
            # bufsize=1 with text=True almost always gives us a real iterator.
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                captured.append(line)
                # Keep the captured log to a reasonable size for error messages.
                if len(captured) > 500:
                    captured.pop(0)

                match = _FRAME_RE.search(line)
                if match and on_frame_done is not None:
                    try:
                        done = int(match.group(1))
                        total = int(match.group(2))
                    except ValueError:
                        continue
                    # Remotion can print decreasing/duplicate counters during the
                    # encoder pass; ignore non-monotonic updates.
                    if done > last_reported:
                        last_reported = done
                        try:
                            on_frame_done(done, total)
                        except Exception:
                            log.exception("on_frame_done callback raised")

                # Surface every ~50th line for diagnostics, plus anything that
                # looks like an error.
                if "error" in line.lower() or "fatal" in line.lower():
                    log.warning("Remotion: %s", line)
        finally:
            rc = proc.wait()

        if rc != 0:
            tail = "\n".join(captured[-30:])
            raise StageGError(
                "RENDER_FAILED",
                f"Remotion exited with code {rc}. Last output:\n{tail}",
            )

        if not output_path.exists():
            raise StageGError(
                "RENDER_FAILED",
                f"Remotion reported success but {output_path} is missing.",
            )

        # Move the moov atom to the front of the file so browsers can play
        # audio via progressive HTTP. See _faststart_mp4 for why.
        _faststart_mp4(output_path)

        size_bytes = output_path.stat().st_size
        log.info(
            "Stage G: render complete. %s (%.2f MB).",
            output_path,
            size_bytes / (1024 * 1024),
        )
        video_url = _upload_to_r2(output_path, job_dir.name + ".mp4")
        log.info("Stage G: uploaded to R2 -> %s", video_url)
        return RenderResult(video_url=video_url, size_bytes=size_bytes)
    finally:
        _unlink_job_from_public(job_public_dir)


def _expected_total_frames(manifest: dict) -> int:
    """Total frames the render will produce, used for progress %."""
    video = manifest.get("video", {})
    secs = float(video.get("total_duration_seconds", 0))
    fps = int(video.get("framerate", 30))
    return max(1, int(secs * fps))


if __name__ == "__main__":
    # Convenience entry point for ad-hoc testing:
    #   python -m worker.stage_g_render <job_dir>
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if len(sys.argv) != 2:
        print("Usage: python -m worker.stage_g_render <job_dir>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1]).resolve()
    if not target.is_dir():
        print(f"Not a directory: {target}", file=sys.stderr)
        sys.exit(2)

    def _progress(done: int, total: int) -> None:
        pct = 100.0 * done / max(1, total)
        # \r so the line refreshes in place when run interactively
        sys.stdout.write(f"\rRendered {done:>6}/{total} frames ({pct:5.1f}%)")
        sys.stdout.flush()

    try:
        run(target, on_frame_done=_progress)
        print()
    except StageGError as e:
        print(f"\nStage G FAILED ({e.code}): {e.message}", file=sys.stderr)
        sys.exit(1)