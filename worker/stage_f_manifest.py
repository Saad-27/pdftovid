
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("stage_f")

# -- Timing constants (PRD §4.6) -----------------------------------------

START_PAD_SECONDS = 0.2
END_PAD_SECONDS = 0.2
VISUAL_TO_AUDIO_LEAD_SECONDS = 0.2
INTER_SEGMENT_GAP_SECONDS = 0.1
TITLE_CARD_DURATION_SECONDS = 2.0   # fallback when every segment is skipped

# Video output spec (PRD §4.7).
VIDEO_RESOLUTION = [1920, 1080]
VIDEO_FRAMERATE = 30


# -- Custom exception ----------------------------------------------------

class StageFError(Exception):
    """Stage F failure. PRD §4.6 says these are always code bugs."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# -- Helpers -------------------------------------------------------------

def _round_ms(seconds: float) -> float:
    """Round to millisecond precision so manifest diffs stay sane and
    Remotion's frame math doesn't accumulate float fuzz."""
    return round(seconds, 3)


def _slide_image_path(slide: dict) -> Optional[str]:
    """Return the first image's path, or None. Stage B already keeps only
    the first image per slide (PRD §9.2)."""
    images = slide.get("images") or []
    if images:
        return images[0].get("path")
    return None


def _derive_image_layout(
    segments: list[dict],
    image_role: str,
    has_image: bool,
) -> str:
    """
    Resolve the per-slide image_layout for Stage G's renderer.

    Policy (2026-05-21): if the slide has a usable image and at least
    one segment surfaces it, always use right_split. The full_image,
    left_split, and text_with_inline_image cases are intentionally
    unreachable — text_with_inline_image triggered a flexbox-overflow
    bug where accumulated visual_text pushed the image off-screen, and
    layout variety isn't worth re-introducing that class of edge case.
    The renderer still understands the other layouts; they're kept as
    dead code for cheap re-enablement later.

    Rules:
      - No image, or image_role=none/decorative → "text_only".
      - Image present and at least one segment has show_image=True → "right_split".
      - Otherwise (image on disk but no segment surfaces it) → "text_only".
    """
    if not has_image or image_role in (None, "", "none", "decorative"):
        return "text_only"
    if any(seg.get("show_image") for seg in segments):
        return "right_split"
    return "text_only"


def _build_slide_entry(
    slide: dict,
    script: dict,
    classification: dict,
    durations: dict[str, float],
    slide_start_seconds: float,
) -> dict:
    """
    Build one slide's manifest entry, including per-segment timings.
    slide_start_seconds is this slide's offset on the global timeline.
    """
    slide_index = slide["index"]
    image_path = _slide_image_path(slide)
    image_role = (classification or {}).get("image_role", "none")

    raw_segments = script.get("segments") or []
    # Filter out segments Stage E couldn't synthesise.
    usable_segments: list[dict] = []
    skipped_in_slide = 0
    for seg in raw_segments:
        seg_id = seg.get("id")
        if not seg_id:
            log.warning("Slide %d has a segment with no id; skipping.", slide_index)
            skipped_in_slide += 1
            continue
        if seg_id not in durations:
            log.warning(
                "Slide %d segment %s has no audio (skipped at Stage E); "
                "omitting from manifest.", slide_index, seg_id,
            )
            skipped_in_slide += 1
            continue
        usable_segments.append(seg)

    # All segments skipped → 3-second title card.
    if not usable_segments:
        log.warning(
            "Slide %d has no usable segments; emitting %.1fs title card.",
            slide_index, TITLE_CARD_DURATION_SECONDS,
        )
        return {
            "slide_index": slide_index,
            "start_seconds": _round_ms(slide_start_seconds),
            "duration_seconds": TITLE_CARD_DURATION_SECONDS,
            "title": slide.get("title", ""),
            "image_layout": "text_only" if not image_path else "right_split",
            "image_path": image_path,
            "segments": [],
        }

    # Walk segments and compute timings (local to slide_start_seconds=0;
    # we'll add slide_start_seconds at the end).
    manifest_segments: list[dict] = []
    cursor = START_PAD_SECONDS
    for i, seg in enumerate(usable_segments):
        if i > 0:
            # cursor was left at previous narration's END; advance by gap.
            cursor += INTER_SEGMENT_GAP_SECONDS
        visual_start_local = cursor
        audio_start_local = visual_start_local + VISUAL_TO_AUDIO_LEAD_SECONDS
        audio_duration = durations[seg["id"]]
        audio_end_local = audio_start_local + audio_duration

        manifest_segments.append({
            "id": seg["id"],
            "visual_text": seg.get("visual_text", ""),
            "show_image": bool(seg.get("show_image", False)),
            "audio_file": f"audio/seg_{seg['id']}.mp3",
            "audio_duration_seconds": _round_ms(audio_duration),
            "visual_start_seconds": _round_ms(slide_start_seconds + visual_start_local),
            "audio_start_seconds": _round_ms(slide_start_seconds + audio_start_local),
        })
        cursor = audio_end_local  # next iteration adds the gap

    slide_duration = cursor + END_PAD_SECONDS

    return {
        "slide_index": slide_index,
        "start_seconds": _round_ms(slide_start_seconds),
        "duration_seconds": _round_ms(slide_duration),
        "title": slide.get("title", ""),
        "image_layout": _derive_image_layout(
            usable_segments, image_role, has_image=bool(image_path),
        ),
        "image_path": image_path,
        "segments": manifest_segments,
    }


# -- Public entry point --------------------------------------------------

def run(job_dir: Path) -> None:
    """
    Read extracted.json, plan.json, audio_durations.json, and all
    scripts/slide_NNN.json files. Write manifest.json. Raises
    StageFError on any structural issue; never raises on per-slide or
    per-segment skips (those are warnings).
    """
    try:
        extracted = json.loads((job_dir / "extracted.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise StageFError("MANIFEST_FAILED", f"Could not read extracted.json: {e}")

    try:
        plan = json.loads((job_dir / "plan.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise StageFError("MANIFEST_FAILED", f"Could not read plan.json: {e}")

    try:
        durations = json.loads((job_dir / "audio_durations.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise StageFError("MANIFEST_FAILED", f"Could not read audio_durations.json: {e}")

    slides_by_idx = {s["index"]: s for s in extracted.get("slides", [])}
    classifications_by_idx = {
        c["slide_index"]: c
        for c in plan.get("slide_classifications", [])
        if "slide_index" in c
    }

    scripts_dir = job_dir / "scripts"
    if not scripts_dir.is_dir():
        raise StageFError("MANIFEST_FAILED", "scripts/ directory missing.")
    script_files = sorted(scripts_dir.glob("slide_*.json"))
    if not script_files:
        raise StageFError("MANIFEST_FAILED", "No script files in scripts/.")

    # Iterate slides in slide_index order. Scripts are zero-padded so
    # filename sort == index sort, but we re-key by index defensively.
    scripts_by_idx: dict[int, dict] = {}
    for sf in script_files:
        try:
            script = json.loads(sf.read_text())
        except json.JSONDecodeError as e:
            raise StageFError(
                "MANIFEST_FAILED", f"Could not parse {sf.name}: {e}"
            )
        idx = script.get("slide_index")
        if not isinstance(idx, int):
            raise StageFError(
                "MANIFEST_FAILED",
                f"{sf.name} has no integer slide_index.",
            )
        scripts_by_idx[idx] = script

    sorted_indices = sorted(scripts_by_idx.keys())

    log.info(
        "Stage F: building manifest for %d slides (%d audio durations on disk).",
        len(sorted_indices), len(durations),
    )

    manifest_slides: list[dict] = []
    cursor_seconds = 0.0
    for idx in sorted_indices:
        slide = slides_by_idx.get(idx)
        if slide is None:
            # Script references a slide that isn't in extracted.json.
            # Shouldn't happen, but don't crash — synthesise a minimal stub.
            log.warning("Slide %d in script but missing from extracted.json.", idx)
            slide = {"index": idx, "title": "", "blocks": [], "images": []}

        entry = _build_slide_entry(
            slide=slide,
            script=scripts_by_idx[idx],
            classification=classifications_by_idx.get(idx) or {"image_role": "none"},
            durations=durations,
            slide_start_seconds=cursor_seconds,
        )
        manifest_slides.append(entry)
        cursor_seconds += entry["duration_seconds"]

    manifest = {
        "video": {
            "total_duration_seconds": _round_ms(cursor_seconds),
            "resolution": VIDEO_RESOLUTION,
            "framerate": VIDEO_FRAMERATE,
        },
        "lecture_title": plan.get("lecture_title", ""),
        "lecture_filename": extracted.get("lecture_filename", ""),
        "slides": manifest_slides,
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    log.info(
        "Stage F: manifest written. %d slides, total duration %.1fs.",
        len(manifest_slides), cursor_seconds,
    )