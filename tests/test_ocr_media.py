"""Text extraction from PDFs, images and videos, with the third-party tools replaced.

``pdfplumber``, ``pytesseract``, Pillow and ``ffmpeg`` are all faked (injected into
``sys.modules`` or replaced at ``subprocess.run``), so these tests behave the same whether or not
the real tools are installed, and no image is rendered, OCR is run, or process started.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from intake import ocr


class FakePage:
    def __init__(self, text, image="page-image"):
        self.text = text
        self.image = image
        self.resolutions = []

    def extract_text(self):
        return self.text

    def to_image(self, resolution):
        self.resolutions.append(resolution)
        return SimpleNamespace(original=f"{self.image}@{resolution}")


class FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def tools(monkeypatch):
    """Inject fake pdfplumber and pytesseract; return the shared recording state."""
    state = SimpleNamespace(pdf=FakePdf([]), opened=[], ocr_inputs=[], ocr_result="OCR TEXT")

    def open_pdf(path):
        state.opened.append(path)
        return state.pdf

    monkeypatch.setitem(sys.modules, "pdfplumber", SimpleNamespace(open=open_pdf))

    def image_to_string(image):
        state.ocr_inputs.append(image)
        return state.ocr_result

    monkeypatch.setitem(
        sys.modules, "pytesseract", SimpleNamespace(image_to_string=image_to_string)
    )
    return state


# --- dispatch -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["x.pdf", "X.PDF"])
def test_pdfs_use_pdf_extraction_regardless_of_case(monkeypatch, name):
    monkeypatch.setattr(ocr, "_extract_pdf_text", lambda p: f"pdf:{p.name}")
    assert ocr.extract_text(Path(name)) == f"pdf:{name}"


@pytest.mark.parametrize("name", ["a.jpg", "a.JPEG", "a.png", "a.tiff", "a.webp", "a.bmp"])
def test_images_are_loaded_then_ocred(monkeypatch, name):
    monkeypatch.setattr(ocr, "_load_image", lambda p: f"loaded:{p.name}")
    monkeypatch.setattr(ocr, "_ocr_image", lambda image: f"ocr({image})")
    assert ocr.extract_text(Path(name)) == f"ocr(loaded:{name})"


@pytest.mark.parametrize("name", ["v.mov", "v.MP4", "v.m4v"])
def test_videos_are_ocred_from_a_thumbnail(monkeypatch, name):
    monkeypatch.setattr(ocr, "_video_thumbnail_image", lambda p: f"thumb:{p.name}")
    monkeypatch.setattr(ocr, "_ocr_image", lambda image: f"ocr({image})")
    assert ocr.extract_text(Path(name)) == f"ocr(thumb:{name})"


@pytest.mark.parametrize("name", ["notes.txt", "archive.zip", "noextension", "x.pdf.bak"])
def test_other_file_types_are_refused_with_the_path(name):
    with pytest.raises(ValueError, match="unsupported receipt media type") as info:
        ocr.extract_text(Path(name))
    assert name in str(info.value)
    with pytest.raises(ValueError, match="unsupported receipt media type"):
        ocr.render_preview_image(Path(name))


def test_preview_dispatches_by_media_type_and_passes_the_resolution(monkeypatch):
    monkeypatch.setattr(ocr, "_render_pdf_page", lambda p, *, resolution: ("pdf", resolution))
    monkeypatch.setattr(ocr, "_load_image", lambda p: ("image", p.name))
    monkeypatch.setattr(ocr, "_video_thumbnail_image", lambda p: ("video", p.name))
    assert ocr.render_preview_image(Path("a.pdf")) == ("pdf", 150)
    assert ocr.render_preview_image(Path("a.pdf"), resolution=72) == ("pdf", 72)
    assert ocr.render_preview_image(Path("a.png")) == ("image", "a.png")
    assert ocr.render_preview_image(Path("a.mov")) == ("video", "a.mov")


# --- PDFs ---------------------------------------------------------------------------------


def test_a_page_with_real_text_is_read_natively_and_not_ocred(tools):
    tools.pdf = FakePdf([FakePage("This receipt has plenty of native text on it")])
    assert ocr.extract_text(Path("r.pdf")) == "This receipt has plenty of native text on it"
    assert tools.ocr_inputs == []
    assert tools.opened == [Path("r.pdf")]


def test_native_text_has_doubled_bold_glyphs_collapsed(tools):
    tools.pdf = FakePdf([FakePage("IInnvvooiiccee dated 2026-01-01 total due")])
    assert ocr.extract_text(Path("r.pdf")).startswith("Invoice dated")


def test_a_page_with_almost_no_text_falls_back_to_ocr_at_300dpi(tools):
    page = FakePage("   short   ", image="scan")
    tools.pdf = FakePdf([page])
    tools.ocr_result = "recognised text"
    assert ocr.extract_text(Path("r.pdf")) == "recognised text"
    assert page.resolutions == [300]
    assert tools.ocr_inputs == ["scan@300"]


def test_a_page_with_no_text_at_all_is_ocred(tools):
    tools.pdf = FakePdf([FakePage(None)])
    assert ocr.extract_text(Path("r.pdf")) == "OCR TEXT"


def test_the_native_text_threshold_is_twenty_characters(tools):
    nineteen = "abcdefghijklmnopqrs"
    twenty = "abcdefghijklmnopqrst"
    tools.pdf = FakePdf([FakePage(nineteen), FakePage(twenty)])
    tools.ocr_result = "OCR"
    assert ocr.extract_text(Path("r.pdf")) == "OCR\n" + twenty
    assert len(tools.ocr_inputs) == 1


def test_pages_are_joined_in_order_mixing_native_and_ocr(tools):
    tools.pdf = FakePdf(
        [
            FakePage("first page has enough native text here"),
            FakePage(""),
            FakePage("third page also has enough native text"),
        ]
    )
    tools.ocr_result = "second page by ocr"
    assert ocr.extract_text(Path("r.pdf")).split("\n") == [
        "first page has enough native text here",
        "second page by ocr",
        "third page also has enough native text",
    ]


def test_a_pdf_with_no_pages_reads_as_empty_text(tools):
    assert ocr.extract_text(Path("r.pdf")) == ""


def test_the_first_page_renders_as_a_preview(tools):
    first, second = FakePage("a", image="one"), FakePage("b", image="two")
    tools.pdf = FakePdf([first, second])
    assert ocr._render_pdf_page(Path("r.pdf"), resolution=96) == "one@96"
    assert first.resolutions == [96] and second.resolutions == []


def test_a_pdf_without_pages_cannot_be_previewed(tools):
    with pytest.raises(ValueError, match="No pages in r.pdf"):
        ocr._render_pdf_page(Path("r.pdf"), resolution=150)


def test_ocr_helpers_pass_the_image_straight_to_tesseract(tools):
    assert ocr._ocr_image("img") == "OCR TEXT"
    assert tools.ocr_inputs == ["img"]


# --- images -------------------------------------------------------------------------------


class FakeImage:
    def __init__(self, name="original"):
        self.name = name
        self.closed = False
        self.converted_to = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def convert(self, mode):
        self.converted_to = mode
        return f"{self.name}-{mode}"


def test_an_image_is_rotated_by_its_exif_tag_and_converted_to_rgb(monkeypatch, tmp_path):
    image = FakeImage()
    seen = {}

    def exif_transpose(img):
        seen["input"] = img
        return FakeImage("rotated")

    pil = SimpleNamespace(
        Image=SimpleNamespace(open=lambda path: (seen.setdefault("path", path), image)[1]),
        ImageOps=SimpleNamespace(exif_transpose=exif_transpose),
    )
    monkeypatch.setitem(sys.modules, "PIL", pil)

    got = ocr._load_image(tmp_path / "a.jpg")

    assert got == "rotated-RGB"
    assert seen["input"] is image
    assert seen["path"] == tmp_path / "a.jpg"
    assert image.closed  # the file handle is released


# --- video --------------------------------------------------------------------------------


@pytest.fixture
def ffmpeg(monkeypatch):
    state = SimpleNamespace(calls=[], fail=None, tmp_names=[])

    def run(cmd, **kwargs):
        state.calls.append((cmd, kwargs))
        if state.fail is not None:
            raise state.fail
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ocr.subprocess, "run", run)
    monkeypatch.setattr(ocr, "_load_image", lambda p: ("thumbnail", p.suffix, p.exists()))
    return state


def test_a_video_thumbnail_is_made_with_ffmpeg_into_a_temporary_png(ffmpeg, tmp_path):
    got = ocr._video_thumbnail_image(tmp_path / "clip.mov")

    cmd, kwargs = ffmpeg.calls[0]
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-i") + 1] == str(tmp_path / "clip.mov")
    assert cmd[cmd.index("-vf") + 1] == "thumbnail"
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert "-y" in cmd
    assert cmd[-1].endswith(".png")
    assert kwargs["check"] is True
    assert got[:2] == ("thumbnail", ".png")


def test_the_temporary_thumbnail_is_removed_afterwards(ffmpeg, tmp_path):
    ocr._video_thumbnail_image(tmp_path / "clip.mov")
    produced = Path(ffmpeg.calls[0][0][-1])
    assert not produced.exists()


def test_an_ffmpeg_failure_propagates_and_leaves_no_temp_file(ffmpeg, tmp_path):
    ffmpeg.fail = subprocess.CalledProcessError(1, "ffmpeg", stderr="bad codec")
    with pytest.raises(subprocess.CalledProcessError):
        ocr._video_thumbnail_image(tmp_path / "clip.mov")
    assert not Path(ffmpeg.calls[0][0][-1]).exists()
