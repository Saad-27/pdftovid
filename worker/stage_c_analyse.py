
from __future__ import annotations



import json
import logging
import re
from pathlib import Path
from typing import Any
from worker._image_utils import thumbnail_to_base64

import anthropic


log = logging.getLogger("stage_c")

MODEL = "claude-haiku-4-5"
THUMBNAIL_MAX_DIM = 512  # pixels on the long edge
MAX_OUTPUT_TOKENS = 4096


# -- Custom exceptions so run.py can map them to PRD error codes ----------

class StageCError(Exception):
    """Base for Stage C failures. The error_code attribute is what we
    persist to the jobs table (PRD §4.3)."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message




# -- Prompt construction -------------------------------------------------

def _build_slides_summary_text(extracted: dict) -> str:
    """
    Produce a plain-text rendering of the deck for the prompt. Each slide
    gets its index, title, and bullets (with indent depth marked). The
    model sees this in one block — interleaving with images comes later.
    """
    parts: list[str] = []
    for slide in extracted["slides"]:
        parts.append(f"=== Slide {slide['index']} ===")
        if slide.get("title"):
            parts.append(f"Title: {slide['title']}")
        for block in slide.get("blocks", []):
            indent = "  " * block.get("level", 0)
            bold = " [BOLD]" if block.get("bold") else ""
            parts.append(f"{indent}- {block['text']}{bold}")
        if slide.get("images"):
            parts.append(f"(slide has {len(slide['images'])} image(s) — see attached)")
        parts.append("")  # blank line between slides
    return "\n".join(parts)


SYSTEM_PROMPT = """\
You are analysing a university lecture slide deck to plan a narrated study \
video. You will receive the full text of every slide plus thumbnail images \
of the slides that have them. Your job is to produce a single JSON object \
describing the deck as a whole, so that downstream steps can write a \
narration script per slide with proper context.

Output ONLY a JSON object matching this exact schema, with no prose, no \
markdown, no code fences. The JSON must parse on the first try.

Schema:
{
  "lecture_title": "<short title for the whole lecture>",
  "section_breakdown": [
    {
      "section_title": "<title for this section>",
      "slides": [<list of 1-based slide indices in this section>]
    }
  ],
  "slide_classifications": [
    {
      "slide_index": <1-based int>,
      "is_continuation_of_previous": <bool>,
      "image_role": "key_diagram" | "supplementary" | "decorative" | "none"
    }
  ]
}

Rules:
- Every slide must appear in exactly one section and have exactly one entry \
in slide_classifications.
- section_breakdown should reflect natural topic boundaries, not arbitrary \
splits. A deck may have a single section if it is short or thematically tight.
- is_continuation_of_previous is true when a slide visually or logically \
continues the previous slide (e.g. same title with "(cont.)", or the previous \
slide ended mid-thought).
- image_role is "none" when the slide has no image. For slides with images: \
"key_diagram" if the image is central to understanding (architecture diagram, \
graph, equation figure); "supplementary" if it supports but isn't essential \
(an example screenshot); "decorative" if it's purely visual flourish (logos, \
stock photos).
"""

STRICT_RETRY_REMINDER = (
    "\n\nIMPORTANT: Your previous response could not be parsed as JSON. "
    "Output ONLY the JSON object. No prose before or after. No markdown "
    "code fences. The first character of your response must be { and the "
    "last must be }."
)


def _build_content_blocks(extracted: dict, job_dir: Path) -> list[dict[str, Any]]:
    """
    Build the user message's content blocks. Order: a single text block
    with all slide text, then one image block per slide that has one,
    each preceded by a tiny text marker so the model knows which slide
    the image belongs to.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": _build_slides_summary_text(extracted)}
    ]

    for slide in extracted["slides"]:
        images = slide.get("images", [])
        if not images:
            continue
        # Stage B already capped images to 1 per slide; defensive [0] anyway.
        img_rel = images[0]["path"]
        img_path = job_dir / img_rel
        if not img_path.exists():
            log.warning("Image path %s missing on disk, skipping.", img_path)
            continue
        try:
            data_b64, media_type = thumbnail_to_base64(img_path)
        except Exception as e:
            log.warning("Failed to encode image for slide %d: %s", slide["index"], e)
            continue
        blocks.append({"type": "text", "text": f"(Image for slide {slide['index']}:)"})
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data_b64,
            },
        })

    blocks.append({
        "type": "text",
        "text": (
            "Now produce the JSON plan for this deck. Remember: JSON only, "
            "no prose, no code fences."
        ),
    })
    return blocks


# -- API calls ----------------------------------------------------------

def _liveness_check(client: anthropic.Anthropic) -> None:
    """
    Cheap call (PRD §6.4) — confirms the key works before we run the
    expensive analysis call. Maps 401 to ANTHROPIC_AUTH_FAILED and other
    errors to the same codes the main call uses.
    """
    try:
        client.messages.create(
            model=MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with: ok"}],
        )
    except anthropic.AuthenticationError as e:
        raise StageCError("ANTHROPIC_AUTH_FAILED", "Your API key was rejected.") from e
    except anthropic.RateLimitError as e:
        raise StageCError(
            "ANTHROPIC_RATE_LIMITED",
            "Anthropic is rate-limiting requests. Please try again in a few minutes.",
        ) from e
    except anthropic.BadRequestError as e:
        # Credit / billing problems come through as 400 with a specific message.
        msg = str(e).lower()
        if "credit" in msg or "billing" in msg or "balance" in msg:
            raise StageCError(
                "ANTHROPIC_INSUFFICIENT_CREDIT",
                "Your Anthropic account has no available credit.",
            ) from e
        raise StageCError("ANTHROPIC_AUTH_FAILED", "Anthropic rejected the request.") from e
    except anthropic.APIError as e:
        raise StageCError("MALFORMED_AI_RESPONSE", f"Anthropic error: {e}") from e


def _call_anthropic(client: anthropic.Anthropic, content_blocks: list[dict], strict: bool = False) -> str:
    """Make the analysis call and return the raw text content."""
    system = SYSTEM_PROMPT + (STRICT_RETRY_REMINDER if strict else "")
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content_blocks}],
        )
    except anthropic.AuthenticationError as e:
        raise StageCError("ANTHROPIC_AUTH_FAILED", "Your API key was rejected.") from e
    except anthropic.RateLimitError as e:
        raise StageCError(
            "ANTHROPIC_RATE_LIMITED",
            "Anthropic is rate-limiting requests. Please try again in a few minutes.",
        ) from e
    except anthropic.BadRequestError as e:
        msg = str(e).lower()
        if "credit" in msg or "billing" in msg or "balance" in msg:
            raise StageCError(
                "ANTHROPIC_INSUFFICIENT_CREDIT",
                "Your Anthropic account has no available credit.",
            ) from e
        raise StageCError("MALFORMED_AI_RESPONSE", f"Bad request: {e}") from e

    # Extract text from response — Haiku returns a list of content blocks;
    # for our prompt we expect one text block.
    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(text_parts).strip()


# -- Response parsing ---------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_plan(raw: str) -> dict:
    """
    Try hard to extract a JSON object from the model's response. We're
    not paranoid here — strict prompt should produce clean JSON — but
    we strip code fences and look for the outermost { ... } as a fallback
    before giving up.
    """
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: find the first { and the matching last } and try that.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise StageCError(
        "MALFORMED_AI_RESPONSE",
        "The analysis step failed. Please try again.",
    )


def _validate_plan(plan: dict, slide_count: int) -> None:
    """Light shape check. The downstream stages will lean on these fields."""
    for key in ("lecture_title", "section_breakdown", "slide_classifications"):
        if key not in plan:
            raise StageCError(
                "MALFORMED_AI_RESPONSE",
                f"Plan JSON missing required key: {key}",
            )
    if not isinstance(plan["slide_classifications"], list):
        raise StageCError("MALFORMED_AI_RESPONSE", "slide_classifications must be a list")
    seen_indices = {c.get("slide_index") for c in plan["slide_classifications"]}
    if len(seen_indices) != slide_count:
        log.warning(
            "Plan covers %d slides but deck has %d; downstream may compensate.",
            len(seen_indices), slide_count,
        )


# -- Public entry point -------------------------------------------------

def run(job_dir: Path, api_key: str) -> dict:
    """
    Read extracted.json from job_dir, call Claude Haiku 4.5 to produce
    plan.json. Returns the parsed plan dict.

    Raises StageCError on any failure; run.py maps .code to the DB.
    """
    extracted_path = job_dir / "extracted.json"
    extracted = json.loads(extracted_path.read_text())
    slide_count = len(extracted["slides"])

    client = anthropic.Anthropic(api_key=api_key)

    # 1. Liveness check — fail fast on bad keys before sending images.
    log.info("Stage C: verifying API key…")
    _liveness_check(client)

    # 2. Build the prompt and call.
    content_blocks = _build_content_blocks(extracted, job_dir)
    image_count = sum(1 for b in content_blocks if b.get("type") == "image")
    log.info(
        "Stage C: calling %s on %d slides (%d images)…",
        MODEL, slide_count, image_count,
    )
    raw = _call_anthropic(client, content_blocks, strict=False)

    # 3. Parse — with one strict retry if the model included prose.
    try:
        plan = _parse_plan(raw)
    except StageCError:
        log.warning("Stage C: first response not parseable, retrying with stricter prompt…")
        raw = _call_anthropic(client, content_blocks, strict=True)
        plan = _parse_plan(raw)

    _validate_plan(plan, slide_count)

    (job_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    log.info(
        "Stage C: plan written. Title='%s', %d sections.",
        plan.get("lecture_title", "?"),
        len(plan.get("section_breakdown", [])),
    )
    return plan