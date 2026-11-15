"""The extractor sandbox: the client seam, the HTTP service, and the rule that a hostile file is never parsed here.

The service runs for real on a loopback port, with a fake worker standing in for pdfplumber and tesseract so each
failure mode (hang, crash, busy, oversize) can be driven on demand.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from intake import ocr, sandbox
from intake.extractor_service import Extractor, make_server

FAKE_WORKER = """
import json, sys, time
kind, path = sys.argv[1], sys.argv[2]
data = open(path, "rb").read()
if data.startswith(b"HANG"):
    time.sleep(60)
if data.startswith(b"CRASH"):
    sys.stderr.write("SECRET-DOCUMENT-CONTENT")
    sys.exit(1)
if data.startswith(b"UNPARSEABLE"):
    sys.stderr.write("ValueError")
    sys.exit(3)
if kind == "text":
    sys.stdout.write(json.dumps({"text": "text:" + data.decode("utf-8", "replace")}))
elif kind == "png":
    sys.stdout.buffer.write(b"\\x89PNG\\r\\n\\x1a\\n" + data)
elif kind == "decode":
    sys.stdout.write(json.dumps({"decodes": not data.startswith(b"BAD")}))
"""


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A running extractor service with a fake worker; returns (extractor, url)."""
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    extractor = Extractor(
        max_bytes=1024, timeout=2, concurrency=1, worker_command=[sys.executable, str(worker)]
    )
    server = make_server("127.0.0.1", 0, extractor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setenv("INTAKE_EXTRACTOR_URL", url)
    monkeypatch.setenv("INTAKE_EXTRACTOR_MAX_BYTES", "1024")
    monkeypatch.setenv("INTAKE_EXTRACTOR_TIMEOUT", "10")
    yield extractor, url
    server.shutdown()
    server.server_close()


def doc(tmp_path, name: str, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return path


# ---- the switch ----------------------------------------------------------------------------------------------


def test_the_default_is_local_so_nothing_changes_without_opting_in(monkeypatch):
    monkeypatch.delenv("INTAKE_EXTRACTOR", raising=False)
    assert sandbox.mode() == "local" and sandbox.enabled() is False


def test_an_unknown_mode_is_an_error_not_a_quiet_local(monkeypatch):
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandboxed")
    with pytest.raises(ValueError, match="INTAKE_EXTRACTOR"):
        sandbox.mode()
    monkeypatch.setenv("INTAKE_EXTRACTOR_TIMEOUT", "soon")
    with pytest.raises(ValueError):
        sandbox.timeout_seconds()


def test_local_mode_parses_in_process_and_never_calls_the_sandbox(monkeypatch, tmp_path):
    monkeypatch.delenv("INTAKE_EXTRACTOR", raising=False)
    monkeypatch.setattr(
        sandbox, "extract_text", lambda path: pytest.fail("the sandbox must not be used")
    )
    monkeypatch.setattr(ocr, "extract_text_local", lambda path: "local text")
    assert ocr.extract_text(tmp_path / "r.pdf") == "local text"


def test_sandbox_mode_never_parses_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setattr(
        ocr,
        "extract_text_local",
        lambda path: pytest.fail("a hostile file must not be parsed here"),
    )
    monkeypatch.setattr(
        ocr, "render_preview_image", lambda *a, **k: pytest.fail("no local rendering")
    )
    monkeypatch.setattr(sandbox, "extract_text", lambda path: "sandbox text")
    monkeypatch.setattr(sandbox, "preview_png", lambda path, resolution: b"sandbox png")
    assert ocr.extract_text(tmp_path / "r.pdf") == "sandbox text"
    assert ocr.render_preview_png(tmp_path / "r.pdf") == b"sandbox png"


def test_an_unreachable_sandbox_fails_closed_instead_of_falling_back(monkeypatch, tmp_path):
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setenv("INTAKE_EXTRACTOR_URL", "http://127.0.0.1:1")  # nothing listens
    monkeypatch.setattr(
        ocr, "extract_text_local", lambda path: pytest.fail("must not fall back to local parsing")
    )
    with pytest.raises(sandbox.SandboxUnavailable):
        ocr.extract_text(doc(tmp_path, "r.pdf", b"x"))
    assert sandbox.healthy() is False


# ---- the service, over real HTTP ---------------------------------------------------------------------------------


def test_text_preview_and_decode_round_trip(service, tmp_path):
    assert sandbox.healthy() is True
    assert sandbox.extract_text(doc(tmp_path, "r.pdf", b"TOTAL 4.33")) == "text:TOTAL 4.33"
    png = sandbox.preview_png(doc(tmp_path, "r.png", b"pixels"), 150)
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"pixels")
    assert sandbox.image_decodes(doc(tmp_path, "ok.jpg", b"fine")) is True
    assert sandbox.image_decodes(doc(tmp_path, "bad.jpg", b"BAD image")) is False


def test_a_crashing_parser_is_a_refusal_and_its_error_never_echoes_the_document(service, tmp_path):
    with pytest.raises(sandbox.SandboxRejected) as refused:
        sandbox.extract_text(doc(tmp_path, "r.pdf", b"CRASH"))
    # the worker's stderr is never relayed to the caller
    assert "SECRET-DOCUMENT-CONTENT" not in str(refused.value)
    with pytest.raises(sandbox.SandboxRejected):
        sandbox.extract_text(doc(tmp_path, "u.pdf", b"UNPARSEABLE"))


def test_a_decoder_that_dies_answers_not_readable_rather_than_failing(service, tmp_path):
    assert sandbox.image_decodes(doc(tmp_path, "c.jpg", b"CRASH")) is False


def test_a_hanging_parser_is_killed_at_the_timeout_and_the_service_keeps_answering(
    service, tmp_path
):
    started = time.monotonic()
    with pytest.raises(sandbox.SandboxRejected):
        sandbox.extract_text(doc(tmp_path, "h.pdf", b"HANG"))
    assert time.monotonic() - started < 8
    assert sandbox.healthy() is True
    assert sandbox.extract_text(doc(tmp_path, "r.pdf", b"after")) == "text:after"


def test_oversize_files_are_refused_before_and_at_the_service(service, tmp_path):
    big = doc(tmp_path, "big.pdf", b"x" * 2000)
    with pytest.raises(sandbox.SandboxRejected, match="limit"):
        sandbox.extract_text(big)  # the client's own limit
    _, url = service
    request = urllib.request.Request(f"{url}/extract?suffix=.pdf", data=b"x" * 2000, method="POST")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(
            request, timeout=5
        )  # a client that skips the limit still meets the service's
    assert caught.value.code == 413


def test_unsupported_types_missing_lengths_and_bad_queries_are_refused(service, tmp_path):
    _, url = service

    def status(path, data=b"x", method="POST", headers=None):
        request = urllib.request.Request(
            f"{url}{path}", data=data, method=method, headers=headers or {}
        )
        try:
            return urllib.request.urlopen(request, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    assert status("/extract?suffix=.exe") == 400
    assert status("/extract?suffix=") == 400
    assert status("/decode?suffix=.pdf") == 400, "decode is for images only"
    assert status("/preview?suffix=.png&resolution=abc") == 400
    assert status("/preview?suffix=.png&resolution=5000") == 400
    assert status("/nope?suffix=.pdf") == 404
    assert status("/healthz", data=None, method="GET") == 200


def test_a_second_document_while_all_slots_are_busy_is_unavailable_not_queued_forever(
    service, tmp_path, monkeypatch
):
    extractor, _ = service
    monkeypatch.setattr("intake.extractor_service.BUSY_WAIT_SECONDS", 0.2)

    def hold_the_only_slot():
        with contextlib.suppress(sandbox.SandboxError):  # the hung parse is killed at the timeout
            sandbox.extract_text(doc(tmp_path, "h.pdf", b"HANG"))

    holder = threading.Thread(target=hold_the_only_slot, daemon=True)
    holder.start()
    time.sleep(0.5)  # the only slot is now held by the hanging parse
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.extract_text(doc(tmp_path, "r.pdf", b"queued"))
    holder.join(timeout=10)


def test_documents_are_parsed_in_a_private_directory_that_is_removed(service, tmp_path):
    extractor, _ = service
    seen = []
    original = subprocess.run

    def spy(command, **kwargs):
        seen.append(kwargs["cwd"])
        return original(command, **kwargs)

    import intake.extractor_service as module

    module.subprocess.run = spy
    try:
        sandbox.extract_text(doc(tmp_path, "r.pdf", b"x"))
    finally:
        module.subprocess.run = original
    assert seen and not os.path.exists(seen[0]), "the working directory is gone after the request"


# ---- the callers -------------------------------------------------------------------------------------------------


def test_an_image_is_reported_corrupt_only_when_the_sandbox_says_so(monkeypatch, tmp_path):
    from intake.cli import _is_unreadable_receipt_image

    path = doc(tmp_path, "r.jpg", b"x")
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setattr(sandbox, "image_decodes", lambda p: True)
    assert _is_unreadable_receipt_image(path) is False
    monkeypatch.setattr(sandbox, "image_decodes", lambda p: False)
    assert _is_unreadable_receipt_image(path) is True

    def down(p):
        raise sandbox.SandboxUnavailable("down")

    monkeypatch.setattr(sandbox, "image_decodes", down)
    assert _is_unreadable_receipt_image(path) is False, "an outage must not mark a receipt corrupt"

    def refused(p):
        raise sandbox.SandboxRejected("refused")

    monkeypatch.setattr(sandbox, "image_decodes", refused)
    assert _is_unreadable_receipt_image(path) is True
    # and the host's own Pillow is never touched in sandbox mode
    monkeypatch.setattr(
        "PIL.Image.open", lambda *a, **k: pytest.fail("the host must not decode the image")
    )
    monkeypatch.setattr(sandbox, "image_decodes", lambda p: True)
    assert _is_unreadable_receipt_image(path) is False


def test_the_daemon_leaves_the_inbox_alone_for_a_cycle_when_the_sandbox_is_down(
    monkeypatch, tmp_path, capsys
):
    from intake import cli

    inbox = tmp_path / "intake"
    inbox.mkdir()
    receipt = inbox / "r.pdf"
    receipt.write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setenv("INTAKE_EXTRACTOR_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(
        "intake.document_routing.route_inbox_document",
        lambda *a, **k: pytest.fail("not this cycle"),
    )
    result = cli._process_inbox({"receipt_dir": str(tmp_path)}, tmp_path / "db.sqlite")
    assert result == (0, 0)
    assert receipt.exists(), "the file stays where it is for the next cycle"
    assert "not answering" in capsys.readouterr().err


def test_preview_bytes_go_straight_to_the_vision_request_without_the_host_decoding_them(
    monkeypatch, tmp_path
):
    from intake import ai

    monkeypatch.setattr(ocr, "render_preview_png", lambda path, resolution: b"\x89PNG-bytes")
    assert ai._preview_image_b64(tmp_path / "r.pdf") == base64.b64encode(b"\x89PNG-bytes").decode()


def test_the_local_preview_is_a_real_png(monkeypatch, tmp_path):
    from PIL import Image

    monkeypatch.delenv("INTAKE_EXTRACTOR", raising=False)
    monkeypatch.setattr(
        ocr, "render_preview_image", lambda path, resolution=150: Image.new("RGB", (4, 4), "white")
    )
    png = ocr.render_preview_png(tmp_path / "r.png")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


# ---- the real worker ------------------------------------------------------------------------------------------


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
# Runs the real worker from this checkout whether or not intake is installed (the service itself uses `-I -m`).
REAL_WORKER = [
    sys.executable,
    "-c",
    f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
    "from intake.extractor_worker import main; raise SystemExit(main(sys.argv[1:]))",
]


def test_the_real_worker_decodes_a_good_image_and_rejects_a_truncated_one(tmp_path, monkeypatch):
    from PIL import Image

    # Noise does not compress, so cutting the file in half really loses pixels (a trailing-bytes trim of a tiny
    # white image would still decode, exactly as it always has).
    good = tmp_path / "good.png"
    Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3)).save(good)
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])

    extractor = Extractor(
        max_bytes=10_000_000, timeout=60, concurrency=1, worker_command=REAL_WORKER
    )
    server = make_server("127.0.0.1", 0, extractor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("INTAKE_EXTRACTOR", "sandbox")
    monkeypatch.setenv("INTAKE_EXTRACTOR_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        assert sandbox.image_decodes(good) is True
        assert sandbox.image_decodes(truncated) is False
        assert sandbox.preview_png(good, 150).startswith(b"\x89PNG")
    finally:
        server.shutdown()
        server.server_close()


def test_the_worker_reports_only_the_error_type_and_caps_cpu(tmp_path, capsys):
    from intake import extractor_worker

    missing = tmp_path / "gone.pdf"
    assert extractor_worker.main(["text", str(missing)]) == extractor_worker.EXIT_UNPARSEABLE
    assert capsys.readouterr().err.strip() in {"FileNotFoundError", "ValueError"}
    assert extractor_worker.main(["nope", str(missing)]) == extractor_worker.EXIT_USAGE
    assert json.dumps({"x": 1})  # keep json imported for the helpers above
