
from __future__ import annotations

import json
import re
from pathlib import Path

import fitz  # PyMuPDF


_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*$")          # "2", "12"
_PAGE_OF_RE = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")        # "5/30"
_DATE_RANGE_RE = re.compile(r"^\s*\d{4}\s*[/\-]\s*\d{2,4}\s*$")  # "2025/2026"


def _is_chrome(text: str) -> bool:
    """Heuristic: is this line slide chrome rather than content?"""
    t = text.strip()
    if not t:
        return True
    if _PAGE_NUMBER_RE.match(t):
        return True
    if _PAGE_OF_RE.match(t):
        return True
    if _DATE_RANGE_RE.match(t):
        return True
    return False


def _pick_title(lines: list[dict], page_height: float) -> tuple[str, float]:
    """
    Pick the title from a list of {text, size, bbox} dicts.

    A line is the title only if it's structurally distinct from body
    text — either notably larger, or notably top-positioned and at
    least somewhat larger. Slides where all content is roughly one
    size (e.g. a bullet-only slide, or a quote slide) return no title.

    Strategy:
      1. Drop chrome lines (giant page numbers etc.).
      2. Find the top size-tier (lines within 0.5pt of the largest).
      3. If only one tier exists across all remaining content, no
         title — there's no size signal to tell title from body.
      4. If the top tier has 1-2 lines AND is >=1.15x the median size
         of lines below it → title is the topmost line in the tier.
      5. Else if the top tier's topmost line sits in the top quartile
         of the page AND is >=1.10x median below → title.
      6. Else no title.

    The 1.15x / 1.10x ratios are deliberately conservative. We'd
    rather miss a borderline title (slide renders with no header
    bar) than fabricate one (bullet text re-spoken as a title).
    """
    if not lines:
        return "", 0.0

    candidates = [l for l in lines if not _is_chrome(l["text"])]
    if len(candidates) <= 1:
        # Single content line on its own isn't a title in the
        # title-vs-body sense — render it as body and skip the header.
        return "", 0.0

    max_size = max(l["size"] for l in candidates)
    top_tier = [l for l in candidates if abs(l["size"] - max_size) < 0.5]
    rest = [l for l in candidates if abs(l["size"] - max_size) >= 0.5]

    # Everything is one font size → no title.
    if not rest:
        return "", 0.0

    rest_sizes = sorted(l["size"] for l in rest)
    median_rest = rest_sizes[len(rest_sizes) // 2]
    if median_rest <= 0:
        return "", 0.0

    ratio = max_size / median_rest
    top_tier_sorted = sorted(top_tier, key=lambda l: l["bbox"][1])
    candidate = top_tier_sorted[0]
    candidate_y = candidate["bbox"][1]

    # Rule 4: small top tier, notably larger.
    if len(top_tier) <= 2 and ratio >= 1.15:
        return candidate["text"], candidate["size"]

    # Rule 5: top-quartile position, somewhat larger.
    if candidate_y < page_height * 0.25 and ratio >= 1.10:
        return candidate["text"], candidate["size"]

    return "", 0.0


def _bucket_levels(left_xs: list[float]) -> dict[float, int]:
    """
    Given the left-x of every content line on a slide, return a map from
    each x to a discrete bullet level (0, 1, 2, ...). The smallest x is
    level 0; subsequent levels are anything ≥15pt further in.

    This is per-slide rather than absolute so different templates calibrate
    on themselves, not on PowerPoint defaults.
    """
    if not left_xs:
        return {}
    sorted_xs = sorted(set(left_xs))
    levels: dict[float, int] = {sorted_xs[0]: 0}
    current_level = 0
    current_anchor = sorted_xs[0]
    for x in sorted_xs[1:]:
        if x - current_anchor >= 15:  
            current_level = min(current_level + 1, 3)
            current_anchor = x
        levels[x] = current_level
    return levels


def _bbox_overlap(a: tuple[float, float, float, float],
                  b: tuple[float, float, float, float]) -> float:
    """
    Intersection area between two bboxes given as (x, y, w, h).
    Returns 0 if they don't overlap. Used in Stage B to detect
    text-over-image so we can drop background-style images.
    """
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy

def run(job_dir: Path) -> dict:
    """
    Read input.pdf from job_dir, write extracted.json and extracted/*.png.
    Returns the parsed extracted.json content for the caller's convenience.

    Raises:
        ValueError("NO_TEXT_EXTRACTED") if no text was found at all.
        Other exceptions bubble up as EXTRACTION_FAILED.
    """
    pdf_path = job_dir / "input.pdf"
    out_img_dir = job_dir / "extracted"
    out_img_dir.mkdir(exist_ok=True)

    doc = fitz.open(pdf_path)
    slides: list[dict] = []
    any_text = False

    for page_index, page in enumerate(doc, start=1):
        # `dict` mode gives us blocks → lines → spans with font/size/flags.
        # We do one pass that gathers every line, then decide the title and
        # bucket bullet levels from that list.
        raw = page.get_text("dict")

        # Collect every line on the page as a flat list of dicts.
        # We merge spans within a line so "Concurrent " + "programming" reads
        # as one entry, but keep lines separate so list items don't fuse.
        lines: list[dict] = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # 0 = text block
                continue
            for line in block.get("lines", []):
                parts: list[str] = []
                size = 0.0
                bold = False
                for span in line.get("spans", []):
                    span_text = span.get("text", "")
                    if span_text:
                        parts.append(span_text)
                        size = max(size, span.get("size", 0))
                        # PyMuPDF: flags bit 4 (value 16) = bold
                        if span.get("flags", 0) & 16:
                            bold = True
                text = "".join(parts).strip()
                if not text:
                    continue
                bbox = line.get("bbox", [0, 0, 0, 0])
                lines.append({
                    "text": text,
                    "size": size,
                    "bold": bold,
                    "bbox": list(bbox),
                })

        if lines:
            any_text = True

        # Pick the title using the helper (handles "biggest-font is a page
        # number" cases).
        title, title_size = _pick_title(lines, page.rect.height)

        # Filter the content lines: drop chrome, drop the title itself.
        content_lines = [
            l for l in lines
            if not _is_chrome(l["text"])
            and not (l["text"] == title and abs(l["size"] - title_size) < 0.5)
        ]

        # Bucket bullet levels relative to this slide's own leftmost text.
        level_map = _bucket_levels([l["bbox"][0] for l in content_lines])

        blocks_out = [
            {
                "text": l["text"],
                "level": level_map.get(l["bbox"][0], 0),
                "bold": l["bold"],
                "bbox": [
                    l["bbox"][0],
                    l["bbox"][1],
                    l["bbox"][2] - l["bbox"][0],
                    l["bbox"][3] - l["bbox"][1],
                ],
            }
            for l in content_lines
        ]

        # Extract embedded images. Per PRD §9.2 we keep one image per
        # slide. We iterate all images and pick the first one that
        # passes the background-image filter (added 2026-05-21).
        #
        # Two filters, both cheap:
        #   - Tier 1: image bbox covers >70% of page area → background.
        #   - Tier 2: text bboxes overlap >30% of image area →
        #     text-on-background decoration.
        # `lines` (not `content_lines`) is used for overlap so chrome
        # text on a background also counts as a "text on image" signal.
        page_area = page.rect.width * page.rect.height
        images_out: list[dict] = []
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                rects = page.get_image_rects(xref)
                if not rects:
                    # Orphan XObject with no placement — can't reason
                    # about it; skip.
                    continue
                place = rects[0]
                img_x, img_y = place[0], place[1]
                img_w = place[2] - place[0]
                img_h = place[3] - place[1]
                img_area = img_w * img_h
                if img_area <= 0:
                    continue

                # Tier 1: covers most of the page → background.
                if page_area > 0 and img_area / page_area > 0.7:
                    continue

                # Tier 2: text overlaps image significantly → decoration.
                image_bbox = (img_x, img_y, img_w, img_h)
                overlap = 0.0
                for l in lines:
                    lx0, ly0, lx1, ly1 = l["bbox"]
                    overlap += _bbox_overlap(
                        (lx0, ly0, lx1 - lx0, ly1 - ly0),
                        image_bbox,
                    )
                if overlap / img_area > 0.3:
                    continue

                # Passed both filters — save and stop.
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha > 3:  # CMYK → RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_filename = f"page_{page_index:03d}_image_001.png"
                pix.save(out_img_dir / img_filename)
                images_out.append({
                    "path": f"extracted/{img_filename}",
                    "bbox": [img_x, img_y, img_w, img_h],
                })
                pix = None
                break
            except Exception:
                # Unreadable image — skip, don't kill the page.
                continue

        slides.append({
            "index": page_index,
            "title": title,
            "blocks": blocks_out,
            "images": images_out,
        })

    doc.close()

    if not any_text:
        raise ValueError("NO_TEXT_EXTRACTED")

    extracted = {
        "lecture_filename": pdf_path.name,
        "slides": slides,
    }
    (job_dir / "extracted.json").write_text(json.dumps(extracted, indent=2))
    return extracted