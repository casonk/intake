"""HEIC/HEIF receipts (iPhone photos): supported where they can be decoded, and never mistaken for corrupt where not."""

from __future__ import annotations

import io
import threading

import pytest

from intake import media, ocr, sandbox
from intake.cli import _is_unreadable_receipt_image
from intake.extractor_service import Extractor, make_server


def _heic_bytes(size=(64, 64), noise=False) -> bytes:
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    pillow_heif.register_heif_opener()
    if noise:
        import os

        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, "white")
    buffer = io.BytesIO()
    image.save(buffer, format="HEIF")
    return buffer.getvalue()


def test_heic_and_heif_are_image_receipt_types():
    assert {".heic", ".heif"} <= media.IMAGE_RECEIPT_SUFFIXES
    assert media.is_image_receipt("IMG_0001.HEIC") and media.is_image_receipt("photo.heif")
    assert media.receipt_suffix("IMG_0001.HEIC") == ".heic"


def test_heic_is_unsupported_not_corrupt_where_nothing_can_decode_it(monkeypatch, tmp_path):
    monkeypatch.delenv("INTAKE_EXTRACTOR", raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    photo = tmp_path / "IMG_0001.HEIC"
    photo.write_bytes(b"not decodable here")
    other = tmp_path / "receipt.jpg"
    other.write_bytes(b"x")
    assert media.heic_decodable() is False
    assert media.is_supported_receipt(photo) is False
    # the HEIC stays in the folder, ignored
    assert media.iter_supported_receipt_files(tmp_path) == [other]
    # and nobody is told it is a corrupt upload
    assert _is_unreadable_receipt_image(photo) is False


def test_heic_is_supported_where_pillow_heif_is_installed(monkeypatch, tmp_path):
    monkeypatch.delenv("INTAKE_EXTRACTOR", raising=False)
    monkeypatch.setattr(
        "importlib.util.find_spec", lambda name: object() if name == "pillow_heif" else None
    )
    photo = tmp_path / "IMG_0001.HEIC"
    photo.write_bytes(b"x")
    assert media.heic_decodable() is True and media.is_supported_receipt(photo) is True
    assert media.iter_supported_receipt_files(tmp_path) == [photo]


def test_heic_is_supported_in_sandbox_mode_without_any_local_decoder(monkeypatch, tmp_path):
    # The sandbox image carries the decoder; the daemon never decodes the file, so it needs nothing installed.
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    photo = tmp_path / "IMG_0001.heic"
    photo.write_bytes(b"x")
    assert media.heic_decodable() is True and media.is_supported_receipt(photo) is True


def test_loading_a_heic_registers_the_decoder_and_says_what_to_install_when_it_is_missing(
    monkeypatch, tmp_path
):
    photo = tmp_path / "IMG_0001.heic"
    photo.write_bytes(b"x")
    registered = []
    monkeypatch.setattr(ocr, "register_heif", lambda: registered.append(True))
    from PIL import UnidentifiedImageError

    # the fake bytes are not an image, so decoding fails after the opener was registered, which is all this checks
    with pytest.raises(UnidentifiedImageError):
        ocr._load_image(photo)
    assert registered == [True]

    monkeypatch.undo()
    monkeypatch.setitem(
        __import__("sys").modules, "pillow_heif", None
    )  # makes `import pillow_heif` fail
    with pytest.raises(RuntimeError, match="pillow-heif"):
        ocr.register_heif()


def test_other_image_types_do_not_load_the_heic_decoder(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "register_heif", lambda: pytest.fail("not for a PNG"))
    from PIL import Image

    png = tmp_path / "r.png"
    Image.new("RGB", (8, 8), "white").save(png)
    assert ocr._load_image(png).size == (8, 8)


def test_the_sandbox_service_accepts_heic_for_every_endpoint():
    from intake.extractor_service import ENDPOINTS
    from intake.media import IMAGE_RECEIPT_SUFFIXES, SUPPORTED_RECEIPT_SUFFIXES

    assert ".heic" in SUPPORTED_RECEIPT_SUFFIXES and ".heic" in IMAGE_RECEIPT_SUFFIXES
    assert set(ENDPOINTS.values()) == {"text", "png", "decode"}


# ---- real HEIC files: these run wherever pillow-heif is installed (CI installs it from pyproject) -------------


def test_a_real_heic_loads_normalized_to_rgb(tmp_path):
    photo = tmp_path / "IMG_0001.heic"
    photo.write_bytes(_heic_bytes((48, 32)))
    image = ocr._load_image(photo)
    assert image.mode == "RGB" and image.size == (48, 32)


def test_a_real_heic_gets_a_png_preview_and_a_truncated_one_does_not_decode(tmp_path):
    good = tmp_path / "good.heic"
    data = _heic_bytes((96, 96), noise=True)
    good.write_bytes(data)
    assert ocr.render_preview_png_local(good, resolution=100).startswith(b"\x89PNG")

    from intake import extractor_worker

    assert extractor_worker._decodes(good) is True
    truncated = tmp_path / "truncated.heic"
    truncated.write_bytes(data[: len(data) // 2])
    assert extractor_worker._decodes(truncated) is False
    assert _is_unreadable_receipt_image(truncated) is True
    assert _is_unreadable_receipt_image(good) is False


def test_a_real_heic_goes_through_the_sandbox_service_end_to_end(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    worker = [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, {str(root)!r}); "
        "from intake.extractor_worker import main; raise SystemExit(main(sys.argv[1:]))",
    ]
    photo = tmp_path / "IMG_0001.heic"
    photo.write_bytes(_heic_bytes((64, 64), noise=True))
    server = make_server(
        "127.0.0.1",
        0,
        Extractor(max_bytes=50_000_000, timeout=60, concurrency=1, worker_command=worker),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setenv("INTAKE_EXTRACTOR_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        assert sandbox.image_decodes(photo) is True
        assert sandbox.preview_png(photo, 100).startswith(b"\x89PNG")
        assert _is_unreadable_receipt_image(photo) is False
    finally:
        server.shutdown()
        server.server_close()
