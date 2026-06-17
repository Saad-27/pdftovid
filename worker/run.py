
from __future__ import annotations

import logging
import os
import shutil
import socket
import sys
import time
from worker import stage_b_extract, stage_c_analyse, stage_d_script, stage_e_synthesise, stage_f_manifest, stage_g_render
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402
import keystore  # noqa: E402
import r2  # noqa: E402
from config import JOBS_DIR, R2_INPUT_BUCKET, LOCAL_INPUT_MODE   # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s worker: %(message)s")
log = logging.getLogger("worker")

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL_SECONDS = 2


def _set_stage(job_id: str, stage: str, percent: float) -> None:
    db.update_job(job_id, current_stage=stage, progress_percent=percent)


def process_job(job: dict) -> None:
    job_id = str(job["id"])
    job_dir = JOBS_DIR / job_id
    log.info("Picked up job %s (file=%s, voice=%s)", job_id, job["filename"], job["voice"])

    try:

        db.update_job(job_id, state="validated", current_stage="A", progress_percent=1)
        try:
            keystore.touch_key(job_id)
        except Exception:
            log.warning("[%s] Could not refresh key TTL on claim", job_id)

        job_dir.mkdir(parents=True, exist_ok=True)
        if not (LOCAL_INPUT_MODE and (job_dir / "input.pdf").exists()):
            try:
                r2.download_file(R2_INPUT_BUCKET, f"{job_id}.pdf", job_dir / "input.pdf")
            except Exception:
                log.exception("[%s] Could not download input PDF from R2", job_id)
                db.fail_job(
                    job_id,
                    "EXTRACTION_FAILED",
                    "We couldn't retrieve your uploaded PDF. Please upload again to retry.",
                )
                return
        # Stage B: Extraction
        _set_stage(job_id, "B", 5)
        log.info("[%s] Stage B: extracting text and images…", job_id)
        try:
            extracted = stage_b_extract.run(job_dir)
        except ValueError as e:
            if str(e) == "NO_TEXT_EXTRACTED":
                db.fail_job(
                    job_id,
                    "NO_TEXT_EXTRACTED",
                    "We couldn't find any text in this PDF. It may be a scanned document — OCR is not supported in v1.",
                )
                return
            raise

        slide_count = len(extracted["slides"])
        log.info("[%s] Stage B done: %d slides extracted", job_id, slide_count)
        db.update_job(job_id, state="extracted", current_stage="B", progress_percent=10)

        api_key = keystore.pop_key(job_id)
        if api_key is None:
            db.fail_job(
                job_id,
                "KEY_EXPIRED",
                "This job expired before it could be processed. Your API key was "
                "discarded for security and never stored. Please upload again to retry.",
            )
            return
        api_key = api_key.strip()

        # Stage C: Global analysis
        _set_stage(job_id, "C", 12)
        log.info("[%s] Stage C: global analysis…", job_id)
        try:
            stage_c_analyse.run(job_dir, api_key)
        except stage_c_analyse.StageCError as e:
            db.fail_job(job_id, e.code, e.message)
            return
        db.update_job(job_id, state="analysed", current_stage="C", progress_percent=20)
        log.info("[%s] Stage C done.", job_id)

        # Stage D: Per-slide scripting
        _set_stage(job_id, "D", 20)
        log.info("[%s] Stage D: per-slide scripting…", job_id)

        slide_count = len(extracted["slides"])

        def on_slide_done(slide_idx: int, _job_id: str = job_id, _total: int = slide_count) -> None:

            current = db.get_job(_job_id)
            if not current:
                return
            new_pct = min(45.0, max(current.get("progress_percent", 20.0), 20.0) + 25.0 / _total)
            db.update_job(_job_id, progress_percent=new_pct)

        try:
            stage_d_script.run(job_dir, api_key, on_slide_done=on_slide_done)
        except stage_d_script.StageDError as e:
            db.fail_job(job_id, e.code, e.message)
            return
        db.update_job(job_id, state="scripted", current_stage="D", progress_percent=45)
        log.info("[%s] Stage D done.", job_id)

        # Stage E: Speech synthesis (Kokoro TTS)
        _set_stage(job_id, "E", 45)
        log.info("[%s] Stage E: speech synthesis…", job_id)

        def on_segment_done(completed: int, total: int, _job_id: str = job_id) -> None:

            new_pct = 45.0 + 25.0 * (completed / total)
            db.update_job(_job_id, progress_percent=min(70.0, new_pct))

        try:
            stage_e_synthesise.run(job_dir, job["voice"], on_segment_done=on_segment_done)
        except stage_e_synthesise.StageEError as e:
            db.fail_job(job_id, e.code, e.message)
            return
        db.update_job(job_id, state="synthesised", current_stage="E", progress_percent=70)
        log.info("[%s] Stage E done.", job_id)

        # Stage F: Manifest construction (deterministic, no external deps)
        _set_stage(job_id, "F", 70)
        log.info("[%s] Stage F: building manifest…", job_id)
        try:
            stage_f_manifest.run(job_dir)
        except stage_f_manifest.StageFError as e:
            db.fail_job(job_id, e.code, e.message)
            return
        db.update_job(job_id, state="timed", current_stage="F", progress_percent=75)
        log.info("[%s] Stage F done.", job_id)

        _set_stage(job_id, "G", 75)
        log.info("[%s] Stage G: rendering MP4 with Remotion…", job_id)
 
        def on_frame_done(rendered: int, total: int, _job_id: str = job_id) -> None:
            # Stage G occupies 75% → 99%
            if total <= 0:
                return
            new_pct = 75.0 + 24.0 * (rendered / total)
            db.update_job(_job_id, progress_percent=min(99.0, new_pct))
 
        try:
            result = stage_g_render.run(job_dir, on_frame_done=on_frame_done)
        except stage_g_render.StageGError as e:
            db.fail_job(job_id, e.code, e.message)
            return
 
        # Stage G 
        db.update_job(
            job_id,
            state="done",
            current_stage="G",
            progress_percent=100,
            video_url=result.video_url,
            video_size_bytes=result.size_bytes,
        )
        log.info(
            "[%s] Stage G done. Video: %.2f MB -> %s",
            job_id, result.size_bytes / (1024 * 1024), result.video_url,
        )


    except Exception as e:
        log.exception("[%s] Unexpected failure", job_id)
        db.fail_job(job_id, "EXTRACTION_FAILED", str(e)[:500])
    finally:
        # Release the lock so the row isn't stuck claimed.
        db.update_job(job_id, locked_until=None, worker_id=None)

        try:
            keystore.delete_key(job_id)
        except Exception:
            log.warning("[%s] Could not delete key from store", job_id)

        shutil.rmtree(job_dir, ignore_errors=True)

        # Log the final state so we don't need Adminer to see what happened.
        final = db.get_job(job_id)
        if final:
            state = final.get("state")
            if state == "failed":
                log.error(
                    "[%s] FINAL: state=failed stage=%s code=%s message=%s",
                    job_id,
                    final.get("current_stage"),
                    final.get("error_code"),
                    final.get("error_message"),
                )
            else:
                log.info(
                    "[%s] FINAL: state=%s stage=%s progress=%.0f%%",
                    job_id,
                    state,
                    final.get("current_stage"),
                    final.get("progress_percent") or 0,
                )


def main() -> None:
    log.info("Worker %s starting. Polling every %ds.", WORKER_ID, POLL_INTERVAL_SECONDS)
    try:
        while True:
            job = db.claim_next_job(WORKER_ID)
            if job is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            process_job(job)
    except KeyboardInterrupt:
        log.info("Worker shutting down (KeyboardInterrupt).")


if __name__ == "__main__":
    main()
