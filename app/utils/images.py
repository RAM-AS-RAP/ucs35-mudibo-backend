"""Bounded image upload decoding and metadata-stripping JPEG conversion."""

import warnings
from io import BytesIO

from fastapi import UploadFile

from app.core.config import get_settings
from app.core.errors import AppError

_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_READ_CHUNK_SIZE = 64 * 1024


async def process_image(upload: UploadFile) -> bytes:
    """Validate, orient, resize, and re-encode an upload as metadata-free JPEG."""
    if upload.content_type not in {"image/jpeg", "image/png"}:
        raise AppError(422, "invalid_image", "Image must be a JPEG or PNG file.")

    settings = get_settings()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(min(_READ_CHUNK_SIZE, settings.max_image_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_image_bytes:
            raise AppError(413, "image_too_large", "Image exceeds the maximum allowed size.")
        chunks.append(chunk)
    content = b"".join(chunks)

    signature = _JPEG_SIGNATURE if upload.content_type == "image/jpeg" else _PNG_SIGNATURE
    if not content.startswith(signature):
        raise AppError(422, "invalid_image", "Image content does not match its declared type.")

    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow must be installed to process listing images") from exc

    Image.MAX_IMAGE_PIXELS = 25_000_000
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            source = Image.open(BytesIO(content))
            width, height = source.size
            if width < 1 or height < 1 or width * height > Image.MAX_IMAGE_PIXELS:
                raise AppError(422, "invalid_image", "Image dimensions are not allowed.")
            oriented = ImageOps.exif_transpose(source)
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = oriented.convert("RGB")
            image.thumbnail(
                (settings.image_max_dimension, settings.image_max_dimension),
                Image.Resampling.LANCZOS,
            )
            clean_image = Image.new("RGB", image.size, "white")
            clean_image.paste(image)
            output = BytesIO()
            clean_image.save(output, format="JPEG", quality=settings.image_jpeg_quality, optimize=True)
            return output.getvalue()
    except AppError:
        raise
    except Exception as exc:
        raise AppError(422, "invalid_image", "Image could not be safely decoded.") from exc
