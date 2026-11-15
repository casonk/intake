"""One parse of one untrusted file, in its own short-lived process.

    python -m intake.extractor_worker text|png|decode PATH [RESOLUTION]

Run by :mod:`intake.extractor_service` so that a parser that hangs, crashes or eats memory takes down this child and
not the service. CPU time and output size are capped here; memory is capped by the container's limit.

Exit codes: 0 the answer is on stdout, 2 bad usage, 3 the file could not be parsed. On a failure only the
exception *type* goes to stderr, never document content.
"""

from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path

EXIT_OK, EXIT_USAGE, EXIT_UNPARSEABLE = 0, 2, 3
CPU_SECONDS_ENV = "INTAKE_WORKER_CPU_SECONDS"
DEFAULT_CPU_SECONDS = 120
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


def _limit() -> None:
    cpu = int(os.environ.get(CPU_SECONDS_ENV, DEFAULT_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _decodes(path: Path) -> bool:
    """True when Pillow can fully decode the image (``load`` forces every pixel, so truncation raises)."""
    from PIL import Image

    try:
        if path.suffix.lower() in (".heic", ".heif"):
            from intake.ocr import register_heif

            register_heif()
        with Image.open(path) as image:
            image.load()
    except Exception:  # noqa: BLE001  (any decode failure means "not readable")
        return False
    return True


def run(kind: str, path: Path, resolution: int = 150) -> bytes:
    from intake import ocr

    if kind == "text":
        return json.dumps({"text": ocr.extract_text_local(path)}).encode("utf-8")
    if kind == "png":
        return ocr.render_preview_png_local(path, resolution=resolution)
    if kind == "decode":
        return json.dumps({"decodes": _decodes(path)}).encode("utf-8")
    raise ValueError(f"unknown kind {kind!r}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3) or args[0] not in ("text", "png", "decode"):
        print("usage: extractor_worker text|png|decode PATH [RESOLUTION]", file=sys.stderr)
        return EXIT_USAGE
    _limit()
    try:
        output = run(args[0], Path(args[1]), int(args[2]) if len(args) == 3 else 150)
    except Exception as exc:  # noqa: BLE001
        print(type(exc).__name__, file=sys.stderr)
        return EXIT_UNPARSEABLE
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
