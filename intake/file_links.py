"""Shared helpers for Snowbridge/File Browser receipt links."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from intake.media import is_supported_receipt

DEFAULT_FILEBROWSER_BASE_URL = "https://files.snowbridge.internal"
DEFAULT_FILEBROWSER_RECEIPT_ROOT = "receipt"


def configured_filebrowser_base_url(settings: dict) -> str | None:
    """Return the configured File Browser base URL."""
    report_links_cfg = settings.get("report_links", {})
    if isinstance(report_links_cfg, dict) and "filebrowser_base_url" in report_links_cfg:
        configured = str(report_links_cfg.get("filebrowser_base_url") or "").strip()
        return configured or None
    return DEFAULT_FILEBROWSER_BASE_URL


def configured_filebrowser_receipt_root(settings: dict) -> str:
    """Return the configured receipt-root path exposed by File Browser."""
    report_links_cfg = settings.get("report_links", {})
    if isinstance(report_links_cfg, dict) and report_links_cfg.get("filebrowser_receipt_root"):
        return str(report_links_cfg["filebrowser_receipt_root"]).strip()
    return DEFAULT_FILEBROWSER_RECEIPT_ROOT


def _relative_parts(path: Path | str | None) -> list[str] | None:
    if path is None:
        return None
    value = str(path).replace("\\", "/").strip("/")
    if not value:
        return []
    return [part for part in value.split("/") if part and part != "."]


def filebrowser_uri(
    relative_path: Path | str | None,
    *,
    base_url: str | None,
    root_path: str,
    is_dir: bool = False,
) -> str | None:
    """Return a File Browser URL for a path relative to the receipt root."""
    if not base_url:
        return None
    parsed = urlsplit(base_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None

    root_parts = _relative_parts(root_path) or []
    relative_parts = _relative_parts(relative_path) or []
    encoded = "/".join(quote(part, safe="") for part in ["files", *root_parts, *relative_parts])
    final_path = (
        f"{parsed.path.rstrip('/')}/{encoded}" if parsed.path.rstrip("/") else f"/{encoded}"
    )
    if is_dir and not final_path.endswith("/"):
        final_path += "/"
    return urlunsplit((parsed.scheme, parsed.netloc, final_path, "", ""))


def filebrowser_uri_for_local_path(
    path: Path | None,
    *,
    receipt_root: Path | None,
    base_url: str | None,
    root_path: str,
) -> str | None:
    """Return a File Browser URL for a local path under the receipt root."""
    if path is None or receipt_root is None or not path.exists():
        return None
    try:
        relative = path.relative_to(receipt_root)
    except ValueError:
        try:
            relative = path.resolve().relative_to(receipt_root.resolve())
        except ValueError:
            return None
    return filebrowser_uri(
        relative,
        base_url=base_url,
        root_path=root_path,
        is_dir=path.is_dir(),
    )


def configured_receipt_root(settings: dict) -> Path | None:
    """Return the configured local receipt root, if present."""
    value = str(settings.get("receipt_dir") or "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def resolve_receipt_file_path(
    receipt_row: dict,
    *,
    receipt_root: Path | None,
    search_cache: dict[str, Path | None] | None = None,
) -> Path | None:
    """Resolve the local receipt artifact path for a DB row."""
    if receipt_root is None:
        return None

    receipt_dir = str(receipt_row.get("receipt_dir") or "").strip()
    filename = str(receipt_row.get("filename") or "").strip()
    category = str(receipt_row.get("category") or "").strip()

    if receipt_dir:
        relative = Path(receipt_dir)
        if relative.suffix and is_supported_receipt(relative):
            candidate = receipt_root / relative
            if candidate.exists():
                return candidate
        elif filename:
            candidate = receipt_root / relative / filename
            if candidate.exists():
                return candidate

    if filename and category and is_supported_receipt(filename):
        candidate = receipt_root / Path(category) / filename
        if candidate.exists():
            return candidate

    if not filename or not is_supported_receipt(filename):
        return None
    if search_cache is not None and filename in search_cache:
        return search_cache[filename]

    matches = sorted(
        path
        for path in receipt_root.rglob(filename)
        if path.is_file() and is_supported_receipt(path)
    )
    resolved = matches[0] if len(matches) == 1 else None
    if search_cache is not None:
        search_cache[filename] = resolved
    return resolved


def build_receipt_filebrowser_link(
    settings: dict,
    receipt_row: dict,
    *,
    search_cache: dict[str, Path | None] | None = None,
) -> str | None:
    """Return the Snowbridge/File Browser URL for a tracked receipt row."""
    receipt_root = configured_receipt_root(settings)
    if receipt_root is None or not receipt_root.exists():
        return None
    file_path = resolve_receipt_file_path(
        receipt_row,
        receipt_root=receipt_root,
        search_cache=search_cache,
    )
    return filebrowser_uri_for_local_path(
        file_path,
        receipt_root=receipt_root,
        base_url=configured_filebrowser_base_url(settings),
        root_path=configured_filebrowser_receipt_root(settings),
    )
