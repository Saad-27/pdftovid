
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from kokoro import KPipeline
from pydub import AudioSegment

log = logging.getLogger("stage_e")

# -- Configuration -------------------------------------------------------

SAMPLE_RATE = 24000           # Kokoro's native output rate
MP3_BITRATE = "64k"           # mono speech compresses very well at 64k
PER_SEGMENT_RETRIES = 2       # PRD §8.2: max 2 retries per segment
LOG_EVERY_N_SEGMENTS = 10


# -- Custom exception ----------------------------------------------------

class StageEError(Exception):
    """Stage E failure. .code is what we persist to the jobs table."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# -- Pipeline cache ------------------------------------------------------

_pipelines: dict[str, KPipeline] = {}


def _lang_code_for_voice(voice: str) -> str:
    """First char of the voice ID encodes language. 'af_bella' -> 'a'."""
    if not voice:
        raise ValueError("Empty voice id")
    return voice[0]


def _get_pipeline(lang_code: str) -> KPipeline:
    if lang_code not in _pipelines:
        log.info(
            "Loading Kokoro pipeline for lang_code='%s' "
            "(first-run model download may take ~30s)...",
            lang_code,
        )
        _pipelines[lang_code] = KPipeline(lang_code=lang_code)
        log.info("Kokoro pipeline '%s' ready.", lang_code)
    return _pipelines[lang_code]


# -- Inference -----------------------------------------------------------

def _synthesise_one(text: str, voice: str) -> tuple[bytes, float]:
    """
    Run one Kokoro inference and encode to MP3. Returns
    (mp3_bytes, duration_seconds). Raises on any failure.
    """
    pipeline = _get_pipeline(_lang_code_for_voice(voice))

    # Strip newlines so kokoro doesn't split on its default \n+ pattern —
    # our narrations are short flowing speech that should produce one chunk.
    clean_text = " ".join(text.split())

    generator = pipeline(clean_text, voice=voice, speed=1.4)

    # Collect chunks defensively: for text near or above kokoro's internal
    # token cap (~510 tokens) it splits. Our 75-word cap shouldn't hit
    # this, but we concatenate anyway in case.
    chunks: list[np.ndarray] = []
    for _graphemes, _phonemes, audio in generator:
        # audio is a torch.FloatTensor at SAMPLE_RATE.
        arr = audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio)
        chunks.append(arr)

    if not chunks:
        raise RuntimeError("Kokoro returned no audio chunks")

    full = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

    # float32 [-1, 1] → int16 PCM. clip() guards against rare values
    # outside the expected range.
    pcm = (np.clip(full, -1.0, 1.0) * 32767).astype(np.int16)

    segment = AudioSegment(
        data=pcm.tobytes(),
        sample_width=2,        # int16 = 2 bytes
        frame_rate=SAMPLE_RATE,
        channels=1,
    )

    buf = io.BytesIO()
    segment.export(buf, format="mp3", bitrate=MP3_BITRATE)
    return buf.getvalue(), segment.duration_seconds


def _warm_load(voice: str) -> None:
    """
    Force the pipeline for this voice's language to load (and torch to
    build its inference graph) before we start the main loop. Without
    this, segment 1 takes much longer than the rest and the progress
    bar appears to hang.
    """
    try:
        _ = _synthesise_one("Loading.", voice)
    except Exception as e:
        # If even the warm-up fails the user has bigger problems (voice
        # ID wrong, model files corrupted, etc). Surface immediately.
        raise StageEError(
            "TTS_FAILED",
            f"Failed to initialise Kokoro for voice '{voice}': {e}",
        )


# -- Per-segment processing ----------------------------------------------

def _process_segment(
    seg: dict,
    voice: str,
    audio_dir: Path,
) -> tuple[bool, float, Optional[str]]:
    """
    Synthesise one segment with retries. Returns
    (success, duration_seconds, error_message_or_none).
    """
    seg_id = seg.get("id", "<no-id>")
    text = (seg.get("narration") or "").strip()

    if not text:
        log.warning("Segment %s has empty narration; skipping.", seg_id)
        return False, 0.0, "Empty narration text"

    last_error: Optional[str] = None
    for attempt in range(PER_SEGMENT_RETRIES + 1):
        try:
            mp3_bytes, duration = _synthesise_one(text, voice)
            out_path = audio_dir / f"seg_{seg_id}.mp3"
            out_path.write_bytes(mp3_bytes)
            return True, duration, None
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning(
                "Segment %s attempt %d/%d failed: %s",
                seg_id, attempt + 1, PER_SEGMENT_RETRIES + 1, last_error,
            )

    return False, 0.0, f"TTS_FAILED: {last_error}"


# -- Public entry point --------------------------------------------------

def run(
    job_dir: Path,
    voice: str,
    on_segment_done: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    Walk scripts/slide_NNN.json, synthesise audio for every segment,
    write audio/seg_<id>.mp3 and audio_durations.json.

    on_segment_done(completed, total) is called after each segment
    (success or skip). Caller is responsible for thread-safety, though
    since Stage E is serial it's only called from one thread.

    Per-segment failures are skipped and recorded in stage_e_skipped.json.
    Raises StageEError only if every segment fails or if setup fails.
    """
    if voice not in (None,) and not isinstance(voice, str):
        raise StageEError("TTS_FAILED", f"Invalid voice value: {voice!r}")

    scripts_dir = job_dir / "scripts"
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(exist_ok=True)

    script_files = sorted(scripts_dir.glob("slide_*.json"))
    if not script_files:
        raise StageEError("TTS_FAILED", "No script files found in scripts/.")

    # Collect every segment, preserving slide order then segment order.
    all_segments: list[dict] = []
    for sf in script_files:
        try:
            script = json.loads(sf.read_text())
        except json.JSONDecodeError as e:
            raise StageEError(
                "TTS_FAILED",
                f"Could not parse {sf.name}: {e}",
            )
        for seg in script.get("segments", []):
            all_segments.append(seg)

    total = len(all_segments)
    if total == 0:
        raise StageEError("TTS_FAILED", "No segments to synthesise.")

    log.info(
        "Stage E: %d segments across %d slides, voice='%s'.",
        total, len(script_files), voice,
    )

    # Warm-load before the loop so segment 1 isn't an outlier.
    _warm_load(voice)

    durations: dict[str, float] = {}
    skipped: list[dict] = []

    callback = on_segment_done if on_segment_done is not None else (lambda _c, _t: None)

    for i, seg in enumerate(all_segments):
        success, duration, error = _process_segment(seg, voice, audio_dir)
        if success:
            durations[seg["id"]] = duration
        else:
            skipped.append({
                "id": seg.get("id", "<no-id>"),
                "reason": error,
                "narration_excerpt": (seg.get("narration") or "")[:80],
            })
        try:
            callback(i + 1, total)
        except Exception:
            log.exception("on_segment_done callback raised")

        # Periodic visibility log so the worker doesn't look frozen.
        completed = i + 1
        if completed % LOG_EVERY_N_SEGMENTS == 0 or completed == total:
            log.info(
                "Stage E: %d/%d segments done (%d skipped so far).",
                completed, total, len(skipped),
            )

    # Persist results.
    (job_dir / "audio_durations.json").write_text(json.dumps(durations, indent=2))

    if skipped:
        (job_dir / "stage_e_skipped.json").write_text(
            json.dumps({"skipped": skipped}, indent=2)
        )
        log.warning(
            "Stage E: %d of %d segments skipped — see stage_e_skipped.json.",
            len(skipped), total,
        )

    if not durations:
        # Every segment failed — surface as a job failure.
        raise StageEError(
            "TTS_FAILED",
            f"All {total} segments failed synthesis. See stage_e_skipped.json.",
        )

    log.info(
        "Stage E: %d/%d segments synthesised successfully.",
        len(durations), total,
    )