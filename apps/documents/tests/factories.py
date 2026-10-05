"""Test-only synthetic image factory (never a real photograph)."""

from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile


def make_test_photo(
    *, width: int = 300, height: int = 300, image_format: str = "PNG"
) -> SimpleUploadedFile:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(120, 120, 120)).save(buffer, format=image_format)
    content_type = "image/png" if image_format == "PNG" else "image/jpeg"
    extension = "png" if image_format == "PNG" else "jpg"
    return SimpleUploadedFile(f"photo.{extension}", buffer.getvalue(), content_type=content_type)
