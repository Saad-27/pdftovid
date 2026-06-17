
from __future__ import annotations

import concurrent.futures as futures
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import anthropic

from worker._image_utils import thumbnail_to_base64

log = logging.getLogger("stage_d")

MODEL = "claude-haiku-4-5"
MAX_OUTPUT_TOKENS = 2048      # per-slide responses are small
MAX_CONCURRENT_CHAINS = 2     # PRD §4.4
PARSE_RETRIES = 2             # MALFORMED_AI_RESPONSE retries per slide (PRD §8.2)


# -- Custom exception ----------------------------------------------------

class StageDError(Exception):
    """Stage D failure. .code is what we persist to the jobs table."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# -- Prompt ---------------------------------------------------------------

SYSTEM_PROMPT = """\
You are writing the spoken narration for one slide of a study video. The \
video reveals the slide's points one at a time on screen while your \
narration plays over them. Your job: produce the segment-by-segment \
breakdown for this single slide.

Output ONLY a JSON object matching this exact schema. No prose, no markdown, \
no code fences. First character must be { and last must be }.

Schema:
{
  "slide_index": <int, 1-based, matches the slide you were given>,
  "segments": [
    {
      "id": "<slide_index>-<segment_number_starting_at_1>",
      "visual_text": "<short on-screen text for this segment, one bullet or phrase>",
      "narration": "<conversational spoken text, will be read aloud>",
      "show_image": <bool>
    }
  ],
  "summary_for_next_slide": "<ONE short sentence summarising what this slide established, used as context for the next slide's narration>"
}

Rules for narration:
- stiff academic prose.
- Each narration field must be brief \
- do NOT introduce facts, \
numbers, or claims that are not in the source slide.
- If the slide is marked as a continuation of the previous slide, the FIRST \
segment's narration should briefly reference what came before (e.g. "So \
building on that…", "Coming back to…"). But most of the narration should be whats on the slide
- Avoid filler openings ("essentially", "basically", "what this means \
is", "so let's talk about", "in other words"). Get straight to the substance.

Rules for segments:
- If the source slide has NO extractable text (a blank slide, end-of-deck \
"Questions?" slide, etc.), still produce exactly ONE segment. Set \
visual_text to the slide title if there is one, otherwise to a short \
placeholder like "End of lecture" or "—". Narration should be a brief \
transitional remark. NEVER leave visual_text as an empty string.
- Maximum 8 segments per slide. If the slide has more bullets, consolidate \
related ones into single segments.
- Minimum 1 segment, even for very sparse slides.
- visual_text should be SHORT — one phrase or bullet. The narration carries \
the detail.

Rules for show_image:
- If image_role from the plan is "decorative" or "none", set show_image \
false on all segments.
- If image_role is "key_diagram", at least one segment should set \
show_image true. Pick the segment whose narration most directly references \
the image.
- If image_role is "supplementary", you may set show_image true on the \
segment that most benefits from it, or leave it false throughout.
- The image, when shown, will appear on the right half of the slide. \
You do not control position — only whether the image is visible during \
each segment.
"""

STRICT_RETRY_REMINDER = (
    "\n\nIMPORTANT: Your previous response could not be parsed as JSON. "
    "Output ONLY the JSON object. No prose before or after. No markdown "
    "code fences. The first character of your response must be { and "
    "the last must be }."
)


# -- Prompt construction --------------------------------------------------

def _slide_text(slide: dict) -> str:
    """Render this slide's title + bullets as plain text for the prompt."""
    lines: list[str] = []
    if slide.get("title"):
        lines.append(f"Title: {slide['title']}")
    for block in slide.get("blocks", []):
        indent = "  " * block.get("level", 0)
        bold = " [BOLD]" if block.get("bold") else ""
        lines.append(f"{indent}- {block['text']}{bold}")
    if not lines:
        lines.append("(slide has no extractable text)")
    return "\n".join(lines)


def _build_user_content(
    slide: dict,
    classification: dict,
    section_title: str,
    previous_summary: Optional[str],
    job_dir: Path,
) -> list[dict[str, Any]]:
    """
    Build the per-slide user message. Order: textual context, then the
    image if any, then the explicit ask. The image goes near the end so
    the model has read the text by the time it sees the picture.
    """
    parts: list[str] = []
    parts.append(f"Slide index: {slide['index']}")
    parts.append(f"Section: {section_title}")
    parts.append(f"Image role: {classification.get('image_role', 'none')}")
    parts.append(f"Continuation of previous slide: {classification.get('is_continuation_of_previous', False)}")
    if previous_summary:
        parts.append(f"Previous slide's narration summary: {previous_summary}")
    parts.append("")
    parts.append("Slide content:")
    parts.append(_slide_text(slide))

    blocks: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(parts)}]

    # Attach the image if there is one and it's not marked decorative/none.
    image_role = classification.get("image_role", "none")
    images = slide.get("images", [])
    if images and image_role not in ("none",):
        img_path = job_dir / images[0]["path"]
        if img_path.exists():
            try:
                data_b64, media_type = thumbnail_to_base64(img_path)
                blocks.append({"type": "text", "text": "(slide image attached:)"})
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data_b64},
                })
            except Exception as e:
                log.warning("Stage D: failed to encode image for slide %d: %s", slide["index"], e)

    blocks.append({"type": "text", "text": "Now produce the JSON for this slide. JSON only."})
    return blocks


# -- API call + parsing ---------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _call_anthropic(
    client: anthropic.Anthropic,
    content_blocks: list[dict],
    strict: bool,
) -> str:
    system = SYSTEM_PROMPT + (STRICT_RETRY_REMINDER if strict else "")
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content_blocks}],
        )
    except anthropic.AuthenticationError as e:
        raise StageDError("ANTHROPIC_AUTH_FAILED", "Your API key was rejected.") from e
    except anthropic.RateLimitError as e:
        raise StageDError(
            "ANTHROPIC_RATE_LIMITED",
            "Anthropic is rate-limiting requests. Please try again in a few minutes.",
        ) from e
    except anthropic.BadRequestError as e:
        msg = str(e).lower()
        if "credit" in msg or "billing" in msg or "balance" in msg:
            raise StageDError(
                "ANTHROPIC_INSUFFICIENT_CREDIT",
                "Your Anthropic account has no available credit.",
            ) from e
        raise StageDError("MALFORMED_AI_RESPONSE", f"Bad request: {e}") from e

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(text_parts).strip()


def _parse_script(raw: str) -> dict:
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise StageDError(
        "MALFORMED_AI_RESPONSE",
        "The scripting step failed for one slide. Please try again.",
    )


def _validate_script(script: dict, slide_index: int, slide_title: str = "") -> None:
    """Light shape check before we trust this for the manifest stage."""
    if script.get("slide_index") != slide_index:
        # Soft-correct rather than fail: model occasionally echoes the wrong index.
        script["slide_index"] = slide_index

    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        raise StageDError(
            "MALFORMED_AI_RESPONSE",
            f"Slide {slide_index}: no segments produced.",
        )
    if len(segments) > 8:
        log.warning("Slide %d: model returned %d segments, truncating to 8.",
                    slide_index, len(segments))
        script["segments"] = segments[:8]

    for i, seg in enumerate(script["segments"], start=1):
        # narration is required — without it there's no audio source.
        if not seg.get("narration"):
            raise StageDError(
                "MALFORMED_AI_RESPONSE",
                f"Slide {slide_index} segment {i}: missing narration.",
            )
        # visual_text can legitimately be empty for blank slides. Fall back
        # to a placeholder rather than failing the whole job.
        if not seg.get("visual_text"):
            fallback = slide_title or "(no on-screen content)"
            log.warning(
                "Slide %d segment %d: empty visual_text, using fallback %r",
                slide_index, i, fallback,
            )
            seg["visual_text"] = fallback
        # Make sure the id is canonical even if the model deviated.
        seg["id"] = f"{slide_index}-{i}"
        seg.setdefault("show_image", False)

    script.setdefault("summary_for_next_slide", "")

# -- Per-slide processing -------------------------------------------------

def _process_slide(client, slide, classification, section_title, previous_summary, job_dir):
    content = _build_user_content(slide, classification, section_title, previous_summary, job_dir)
    last_error = None
    raw = None
    for attempt in range(PARSE_RETRIES + 1):
        strict = attempt > 0
        try:
            raw = _call_anthropic(client, content, strict=strict)
            script = _parse_script(raw)
            _validate_script(script, slide["index"], slide.get("title", ""))
            return script
        except StageDError as e:
            if e.code in ("ANTHROPIC_AUTH_FAILED", "ANTHROPIC_INSUFFICIENT_CREDIT"):
                raise
            last_error = e
            log.warning(
                "Slide %d attempt %d/%d failed (%s).",
                slide["index"], attempt + 1, PARSE_RETRIES + 1, e.code,
            )
            if raw is not None and e.code == "MALFORMED_AI_RESPONSE":
                log.warning("  raw (first 300 chars): %r", raw[:300])
    assert last_error is not None
    raise last_error

# -- Chain construction & execution --------------------------------------

def _build_chains(slide_classifications: list[dict]) -> list[list[int]]:
    """
    Group slide indices into chains. A chain starts at a slide marked
    is_continuation_of_previous=false (or the very first slide) and
    extends through any consecutive continuations.
    """
    # Sort by slide_index defensively — plan.json should already be ordered.
    classifications_by_idx = {
        c["slide_index"]: c for c in slide_classifications if "slide_index" in c
    }
    sorted_indices = sorted(classifications_by_idx.keys())

    chains: list[list[int]] = []
    current: list[int] = []
    for i, idx in enumerate(sorted_indices):
        is_cont = classifications_by_idx[idx].get("is_continuation_of_previous", False)
        # First slide can't continue anything; force chain-start.
        if i == 0 or not is_cont:
            if current:
                chains.append(current)
            current = [idx]
        else:
            current.append(idx)
    if current:
        chains.append(current)
    return chains


def _section_title_for(slide_index: int, sections: list[dict]) -> str:
    for sec in sections:
        if slide_index in sec.get("slides", []):
            return sec.get("section_title", "")
    return ""


def _process_chain(
    chain: list[int],
    slides_by_idx: dict[int, dict],
    classifications_by_idx: dict[int, dict],
    sections: list[dict],
    job_dir: Path,
    client: anthropic.Anthropic,
    on_slide_done: Callable[[int], None],
) -> None:
    """Run one chain of slides sequentially, threading the previous-slide
    summary forward. Writes scripts/slide_NNN.json for each."""
    scripts_dir = job_dir / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    previous_summary: Optional[str] = None
    for idx in chain:
        slide = slides_by_idx[idx]
        classification = classifications_by_idx[idx]
        section_title = _section_title_for(idx, sections)

        script = _process_slide(
            client=client,
            slide=slide,
            classification=classification,
            section_title=section_title,
            previous_summary=previous_summary,
            job_dir=job_dir,
        )
        out_path = scripts_dir / f"slide_{idx:03d}.json"
        out_path.write_text(json.dumps(script, indent=2))
        previous_summary = script.get("summary_for_next_slide") or None
        on_slide_done(idx)


# -- Public entry point --------------------------------------------------

def run(
    job_dir: Path,
    api_key: str,
    on_slide_done: Optional[Callable[[int], None]] = None,
) -> None:
    """
    Read extracted.json + plan.json. Produce scripts/slide_NNN.json for
    every slide. Raises StageDError on any failure.

    on_slide_done is called (in a worker thread) with the slide index each
    time a slide's script is written. Caller is responsible for thread-safety.
    """
    extracted = json.loads((job_dir / "extracted.json").read_text())
    plan = json.loads((job_dir / "plan.json").read_text())

    slides_by_idx = {s["index"]: s for s in extracted["slides"]}
    classifications_by_idx = {
        c["slide_index"]: c for c in plan.get("slide_classifications", [])
    }
    sections = plan.get("section_breakdown", [])

    # Sanity: every slide needs a classification. If the plan missed any,
    # synthesise a benign default rather than fail the whole job.
    for idx in slides_by_idx:
        if idx not in classifications_by_idx:
            log.warning("Slide %d missing from plan; defaulting classification.", idx)
            classifications_by_idx[idx] = {
                "slide_index": idx,
                "is_continuation_of_previous": False,
                "image_role": "none",
            }

    chains = _build_chains(list(classifications_by_idx.values()))
    log.info(
        "Stage D: %d slides → %d chains, running up to %d in parallel.",
        len(slides_by_idx), len(chains), MAX_CONCURRENT_CHAINS,
    )

    # max_retries on the SDK covers HTTP transients (429/5xx) automatically.
    client = anthropic.Anthropic(api_key=api_key, max_retries=2)

    callback = on_slide_done if on_slide_done is not None else (lambda _idx: None)
    # Wrap callback in a lock so concurrent slide completions don't trample
    # each other's progress writes.
    cb_lock = threading.Lock()
    def safe_cb(idx: int) -> None:
        with cb_lock:
            try:
                callback(idx)
            except Exception:
                log.exception("on_slide_done callback raised")

    errors: list[StageDError] = []
    with futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHAINS) as pool:
        fut_to_chain = {
            pool.submit(
                _process_chain,
                chain, slides_by_idx, classifications_by_idx,
                sections, job_dir, client, safe_cb,
            ): chain
            for chain in chains
        }
        for fut in futures.as_completed(fut_to_chain):
            try:
                fut.result()
            except StageDError as e:
                errors.append(e)
                # Cancel any chain that hasn't started yet. In-flight chains
                # finish their current API call before noticing.
                for other in fut_to_chain:
                    other.cancel()
            except Exception as e:
                errors.append(StageDError("MALFORMED_AI_RESPONSE", f"Unexpected: {e}"))
                for other in fut_to_chain:
                    other.cancel()

    if errors:
        # Prefer the most specific (non-malformed) error if multiple chains failed.
        ranked = sorted(errors, key=lambda e: e.code == "MALFORMED_AI_RESPONSE")
        raise ranked[0]

    log.info("Stage D: all %d slides scripted.", len(slides_by_idx))