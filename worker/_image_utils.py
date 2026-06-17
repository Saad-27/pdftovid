"""
Small shared utilities for the AI stages. Currently only image
preparation, but anything else that gets duplicated between stages
belongs here.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

THUMBNAIL_MAX_DIM = 512  # pixels on the long edge


def thumbnail_to_base64(image_path: Path) -> tuple[str, str]:

    with Image.open(image_path) as img:
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")

        img.thumbnail((THUMBNAIL_MAX_DIM, THUMBNAIL_MAX_DIM), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        return data, "image/png"