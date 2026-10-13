"""Image upload validation and metadata-stripping tests."""

import struct
import zlib
from io import BytesIO

import pytest
from fastapi import UploadFile

from app.core.errors import AppError
from app.utils.images import process_image


class FakeUpload:
    def __init__(self, content: bytes, content_type: str):
        self.content = content
        self.content_type = content_type
        self.bytes_read = 0
        self.read_calls = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        chunk = self.content[:size]
        self.content = self.content[len(chunk) :]
        self.bytes_read += len(chunk)
        return chunk


def fake_upload(content: bytes, content_type: str) -> UploadFile:
    return FakeUpload(content, content_type)  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_rejects_wrong_content_type_before_reading():
    upload = FakeUpload(b"plain text", "text/plain")
    with pytest.raises(AppError) as exc_info:
        await process_image(upload)  # type: ignore[arg-type]
    assert exc_info.value.code == "invalid_image"
    assert upload.read_calls == 0


@pytest.mark.asyncio
async def test_rejects_mismatched_file_signature():
    upload = fake_upload(b"not really a jpeg", "image/jpeg")
    with pytest.raises(AppError) as exc_info:
        await process_image(upload)
    assert exc_info.value.code == "invalid_image"


@pytest.mark.asyncio
async def test_oversize_stream_stops_after_first_over_limit_chunk(monkeypatch):
    import app.utils.images as image_module

    monkeypatch.setattr(image_module, "get_settings", lambda: type("Settings", (), {"max_image_bytes": 8})())
    upload = FakeUpload(b"\xff\xd8\xff" + b"a" * 100_000, "image/jpeg")
    with pytest.raises(AppError) as exc_info:
        await process_image(upload)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 413
    assert exc_info.value.code == "image_too_large"
    assert upload.bytes_read == 9
    assert upload.read_calls == 1


def make_png(width: int = 20, height: int = 10) -> bytes:
    pytest.importorskip("PIL.Image")
    from PIL import Image

    image = Image.new("RGBA", (width, height), (255, 0, 0, 64))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_valid_png_is_reencoded_to_jpeg_without_exif(monkeypatch):
    PILImage = pytest.importorskip("PIL.Image")
    import app.utils.images as image_module

    monkeypatch.setattr(
        image_module,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {"max_image_bytes": 5_242_880, "image_max_dimension": 1600, "image_jpeg_quality": 85},
        )(),
    )
    output = await process_image(fake_upload(make_png(), "image/png"))
    assert output.startswith(b"\xff\xd8\xff")
    with PILImage.open(BytesIO(output)) as result:
        assert result.format == "JPEG"
        assert result.mode == "RGB"
        assert not result.getexif()


@pytest.mark.asyncio
async def test_jpeg_exif_gps_metadata_is_removed(monkeypatch):
    pytest.importorskip("PIL.Image")
    from PIL import ExifTags, Image
    from PIL.TiffImagePlugin import IFDRational

    import app.utils.images as image_module

    monkeypatch.setattr(
        image_module,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {"max_image_bytes": 5_242_880, "image_max_dimension": 1600, "image_jpeg_quality": 85},
        )(),
    )
    source = Image.new("RGB", (20, 12), "white")
    exif = Image.Exif()
    exif[ExifTags.IFD.GPSInfo] = {
        1: "N",
        2: [IFDRational(37, 1), IFDRational(46, 1), IFDRational(30, 1)],
        3: "W",
        4: [IFDRational(122, 1), IFDRational(25, 1), IFDRational(10, 1)],
    }
    stream = BytesIO()
    source.save(stream, format="JPEG", exif=exif)

    result_bytes = await process_image(fake_upload(stream.getvalue(), "image/jpeg"))

    with Image.open(BytesIO(result_bytes)) as sanitized:
        assert not sanitized.getexif()


@pytest.mark.asyncio
async def test_jpeg_exif_metadata_is_removed(monkeypatch):
    PILImage = pytest.importorskip("PIL.Image")
    import app.utils.images as image_module

    monkeypatch.setattr(
        image_module,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {"max_image_bytes": 5_242_880, "image_max_dimension": 1600, "image_jpeg_quality": 85},
        )(),
    )
    source = PILImage.new("RGB", (12, 8), "green")
    exif = PILImage.Exif()
    exif[270] = "GPS coordinates: 37.0, -122.0"
    stream = BytesIO()
    source.save(stream, format="JPEG", exif=exif)

    output = await process_image(fake_upload(stream.getvalue(), "image/jpeg"))

    with PILImage.open(BytesIO(output)) as result:
        assert not result.getexif()


@pytest.mark.asyncio
async def test_large_image_is_downscaled(monkeypatch):
    PILImage = pytest.importorskip("PIL.Image")
    import app.utils.images as image_module

    monkeypatch.setattr(
        image_module,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {"max_image_bytes": 5_242_880, "image_max_dimension": 100, "image_jpeg_quality": 85},
        )(),
    )
    source = PILImage.new("RGB", (250, 125), "blue")
    stream = BytesIO()
    source.save(stream, format="JPEG")
    output = await process_image(fake_upload(stream.getvalue(), "image/jpeg"))
    with PILImage.open(BytesIO(output)) as result:
        assert max(result.size) <= 100


@pytest.mark.asyncio
async def test_oversized_pixel_dimensions_are_rejected_before_decode():
    pytest.importorskip("PIL.Image")
    width, height = 5001, 5000
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk
    with pytest.raises(AppError) as exc_info:
        await process_image(fake_upload(png, "image/png"))
    assert exc_info.value.code == "invalid_image"
