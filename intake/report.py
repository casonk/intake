"""Generate Markdown and HTML summary reports from the intake database."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from jinja2 import BaseLoader, Environment

from intake.certification import receipt_certifications_from_dicts, receipt_certifications_to_dicts
from intake.notify import format_money
from intake.parse import receipt_items_from_dicts, receipt_items_to_dicts
from intake.staff import receipt_staff_from_dicts, receipt_staff_to_dicts

_DEFAULT_FILEBROWSER_BASE_URL = "https://files.snowbridge.internal"
_DEFAULT_FILEBROWSER_RECEIPT_ROOT = "receipt"

_REPORT_TEMPLATE_ROOT = files("intake.templates.report")


def _load_report_template(name: str) -> str:
    return (_REPORT_TEMPLATE_ROOT / name).read_text(encoding="utf-8")


_MD_TEMPLATE = _load_report_template("intake-report.md.j2")

_HTML_TEMPLATE = _load_report_template("intake-report.html.j2")

_MERCHANT_DIRECTORY_MD_TEMPLATE = _load_report_template("merchant-directory.md.j2")

_MERCHANT_DIRECTORY_HTML_TEMPLATE = _load_report_template("merchant-directory.html.j2")

_MERCHANT_DETAIL_MD_TEMPLATE = _load_report_template("merchant-detail.md.j2")

_MERCHANT_DETAIL_HTML_TEMPLATE = _load_report_template("merchant-detail.html.j2")

_MODEL_DIRECTORY_MD_TEMPLATE = _load_report_template("model-directory.md.j2")

_MODEL_DIRECTORY_HTML_TEMPLATE = _load_report_template("model-directory.html.j2")

_MODEL_DETAIL_MD_TEMPLATE = _load_report_template("model-detail.md.j2")

_MODEL_DETAIL_HTML_TEMPLATE = _load_report_template("model-detail.html.j2")

_PURCHASER_DIRECTORY_MD_TEMPLATE = _load_report_template("purchaser-directory.md.j2")

_PURCHASER_DIRECTORY_HTML_TEMPLATE = _load_report_template("purchaser-directory.html.j2")

_PURCHASER_DETAIL_MD_TEMPLATE = _load_report_template("purchaser-detail.md.j2")

_PURCHASER_DETAIL_HTML_TEMPLATE = _load_report_template("purchaser-detail.html.j2")

_AUDIT_HTML_TEMPLATE = _load_report_template("audit.html.j2")


def _format_currency(value: float | int | None) -> str:
    if value is None:
        return "—"
    return format_money(value, "USD")


def _format_receipt_total(value: float | int | None, currency: str | None) -> str:
    if value is None:
        return "—"
    return format_money(value, currency)


def _format_percent(value: float) -> str:
    return f"{value:.1f}%"


def _format_compact_currency(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _has_known_total(row: dict) -> bool:
    return int(row.get("known_total_count") or 0) > 0


def _ascii_bar(value: float, max_value: float, *, width: int = 24) -> str:
    if value <= 0 or max_value <= 0:
        return ""
    filled = max(1, round((value / max_value) * width))
    return "#" * filled


def _safe_label(value: str | None, default: str) -> str:
    if value is None:
        return default
    label = str(value).strip()
    return label or default


def _month_label(value: str | None) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.strptime(value, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return value


def _nice_axis_max(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10
    return nice * magnitude


def _markdown_link(label: str, uri: str | None) -> str:
    return f"[{label}]({uri})" if uri else label


def _relative_parts(path: Path | str | None) -> list[str] | None:
    if path is None:
        return None
    value = str(path).replace("\\", "/").strip("/")
    if not value:
        return []
    return [part for part in value.split("/") if part and part != "."]


def _filebrowser_uri(
    relative_path: Path | str | None,
    *,
    base_url: str | None,
    root_path: str,
    is_dir: bool = False,
) -> str | None:
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


def _filebrowser_uri_for_local_path(
    path: Path | None,
    *,
    receipt_root: Path | None,
    base_url: str | None,
    root_path: str,
) -> str | None:
    if path is None or receipt_root is None or not path.exists():
        return None
    try:
        relative = path.relative_to(receipt_root)
    except ValueError:
        try:
            relative = path.resolve().relative_to(receipt_root.resolve())
        except ValueError:
            return None
    return _filebrowser_uri(
        relative,
        base_url=base_url,
        root_path=root_path,
        is_dir=path.is_dir(),
    )


def _category_folder_path(receipt_root: Path | None, category: str) -> Path | None:
    if receipt_root is None or not category:
        return None
    return receipt_root / Path(category)


def _receipt_file_path(
    receipt_root: Path | None,
    receipt_dir: str | None,
    *,
    filename: str | None = None,
    category: str | None = None,
    search_cache: dict[str, Path | None] | None = None,
) -> Path | None:
    if receipt_root is None:
        return None
    from intake.media import is_supported_receipt

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

    if filename and is_supported_receipt(filename) and category:
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


def _receipt_folder_path(
    receipt_root: Path | None,
    receipt_dir: str | None,
    *,
    filename: str | None = None,
    category: str | None = None,
    category_path: Path | None = None,
    search_cache: dict[str, Path | None] | None = None,
) -> Path | None:
    file_path = _receipt_file_path(
        receipt_root,
        receipt_dir,
        filename=filename,
        category=category,
        search_cache=search_cache,
    )
    if file_path is None:
        if receipt_root is not None and receipt_dir:
            relative = Path(receipt_dir)
            candidate = receipt_root / relative
            if candidate.exists() and candidate.is_dir():
                return candidate
        return category_path
    return file_path.parent


def _prepare_rows(
    rows: list[dict],
    *,
    label_key: str,
    label_name: str,
    value_key: str,
    total_for_share: float,
    limit: int | None = None,
    default_label: str,
    kind: str,
) -> list[dict]:
    selected = rows[:limit] if limit is not None else rows
    max_value = max((float(row.get(value_key) or 0) for row in selected), default=0.0)
    prepared: list[dict] = []
    for row in selected:
        item = dict(row)
        label = _safe_label(row.get(label_key), default_label)
        value = float(row.get(value_key) or 0)
        has_known_total = kind != "currency" or _has_known_total(item)
        share_pct = (value / total_for_share * 100) if total_for_share and has_known_total else None
        if kind == "currency":
            value_display = _format_currency(value) if has_known_total else "—"
        else:
            value_display = str(int(round(value)))
        item[label_name] = label
        item["has_known_total"] = has_known_total
        item["share_pct"] = share_pct
        item["share_display"] = _format_percent(share_pct) if share_pct is not None else "—"
        item["value_display"] = value_display
        item["chart_bar"] = _ascii_bar(value, max_value) if has_known_total else ""
        prepared.append(item)
    return prepared


def _merchant_key(value: str | None) -> str:
    label = _safe_label(value, "Unknown")
    return label.casefold()


def _merchant_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    return slug or "unknown"


def _attach_category_links(
    rows: list[dict],
    *,
    receipt_root: Path | None,
    filebrowser_base_url: str | None,
    filebrowser_receipt_root: str,
) -> list[dict]:
    for row in rows:
        folder_uri = _filebrowser_uri_for_local_path(
            _category_folder_path(receipt_root, row["category"]),
            receipt_root=receipt_root,
            base_url=filebrowser_base_url,
            root_path=filebrowser_receipt_root,
        )
        row["folder_uri"] = folder_uri
        row["category_markdown"] = _markdown_link(row["category"], folder_uri)
    return rows


def _prepare_all_merchants(rows: list[dict], *, total_for_share: float) -> list[dict]:
    prepared = _prepare_rows(
        rows,
        label_key="merchant",
        label_name="merchant_label",
        value_key="total_spend",
        total_for_share=total_for_share,
        default_label="Unknown",
        kind="currency",
    )
    used_slugs: dict[str, int] = {}
    for row in prepared:
        row["total_spend_display"] = row["value_display"]
        base_slug = _merchant_slug(row["merchant_label"])
        slug_count = used_slugs.get(base_slug, 0) + 1
        used_slugs[base_slug] = slug_count
        row["merchant_slug"] = base_slug if slug_count == 1 else f"{base_slug}-{slug_count}"
        row["merchant_page_html"] = f"merchants/{row['merchant_slug']}.html"
        row["merchant_page_md"] = f"merchants/{row['merchant_slug']}.md"
        row["merchant_href_html"] = row["merchant_page_html"]
        row["merchant_href_md"] = row["merchant_page_md"]
        row["merchant_markdown"] = _markdown_link(row["merchant_label"], row["merchant_href_md"])
    return prepared


def _prepare_merchant_monthly(
    rows: list[dict],
    *,
    merchant_lookup: dict[str, dict] | None = None,
) -> list[dict]:
    prepared: list[dict] = []
    for row in rows:
        item = dict(row)
        item["merchant_label"] = _safe_label(item.get("merchant"), "Unknown")
        item["month_label"] = _month_label(item.get("month"))
        item["has_known_total"] = _has_known_total(item)
        item["total_spend_display"] = (
            _format_currency(item.get("total_spend")) if item["has_known_total"] else "—"
        )
        merchant_page = (
            merchant_lookup.get(_merchant_key(item.get("merchant"))) if merchant_lookup else None
        )
        item["merchant_href_html"] = (
            merchant_page.get("merchant_page_html") if merchant_page else None
        )
        item["merchant_href_md"] = merchant_page.get("merchant_page_md") if merchant_page else None
        item["merchant_markdown"] = _markdown_link(item["merchant_label"], item["merchant_href_md"])
        prepared.append(item)
    return prepared


def _build_sparkline(points: list[dict]) -> dict:
    width = 280
    height = 74
    plot_left = 8
    plot_right = width - 8
    plot_top = 8
    plot_bottom = height - 14

    if not points:
        return {
            "has_data": False,
            "width": width,
            "height": height,
            "plot_left": plot_left,
            "plot_right": plot_right,
            "y_ticks": [],
            "line_points": "",
            "area_points": "",
        }

    max_value = max((float(point.get("value") or 0) for point in points), default=0.0)
    scale_max = _nice_axis_max(max_value)
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    step = plot_width / (len(points) - 1) if len(points) > 1 else 0

    coords: list[tuple[float, float]] = []
    for idx, point in enumerate(points):
        value = float(point.get("value") or 0)
        x = plot_left + (step * idx if len(points) > 1 else plot_width / 2)
        y = plot_bottom - ((value / scale_max) * plot_height if scale_max else 0)
        coords.append((round(x, 2), round(y, 2)))

    line_points = " ".join(f"{x},{y}" for x, y in coords)
    first_x = coords[0][0]
    last_x = coords[-1][0]
    area_points = f"{line_points} {last_x},{plot_bottom} {first_x},{plot_bottom}"
    y_ticks = []
    for step_idx in range(3):
        y = plot_bottom - (plot_height * step_idx / 2)
        y_ticks.append({"y": round(y, 2)})

    return {
        "has_data": True,
        "width": width,
        "height": height,
        "plot_left": plot_left,
        "plot_right": plot_right,
        "y_ticks": y_ticks,
        "line_points": line_points,
        "area_points": area_points,
    }


def _build_merchant_trends(
    top_merchants: list[dict],
    merchant_monthly: list[dict],
    by_month: list[dict],
    *,
    limit: int = 8,
) -> list[dict]:
    months = [row.get("month") for row in by_month if row.get("month")]
    if not months:
        return []

    history_lookup: dict[str, dict[str, dict]] = {}
    for row in merchant_monthly:
        history_lookup.setdefault(_merchant_key(row.get("merchant")), {})[row["month"]] = row

    trends: list[dict] = []
    for merchant_row in top_merchants[:limit]:
        merchant_key = _merchant_key(merchant_row.get("merchant"))
        month_rows = history_lookup.get(merchant_key, {})
        points = []
        active_rows: list[dict] = []
        for month in months:
            current = month_rows.get(month)
            value = float(current.get("total_spend") or 0) if current else 0.0
            points.append({"month": month, "value": value})
            if current and value > 0:
                active_rows.append(current)

        breakdown_parts = [
            f"{_month_label(row.get('month'))} {_format_currency(row.get('total_spend'))} ({row.get('count', 0)})"
            for row in active_rows
        ]
        trends.append(
            {
                "merchant_label": merchant_row.get(
                    "merchant_label", _safe_label(merchant_row.get("merchant"), "Unknown")
                ),
                "merchant_markdown": merchant_row.get(
                    "merchant_markdown",
                    _markdown_link(
                        _safe_label(merchant_row.get("merchant"), "Unknown"),
                        merchant_row.get("merchant_href_md"),
                    ),
                ),
                "merchant_href_html": merchant_row.get("merchant_href_html"),
                "active_months": len(active_rows),
                "total_spend_display": merchant_row.get(
                    "total_spend_display", _format_currency(merchant_row.get("total_spend"))
                ),
                "breakdown_markdown": "; ".join(breakdown_parts),
                "breakdown_html": "<br>".join(breakdown_parts),
                "range_start_label": _month_label(months[0]),
                "range_end_label": _month_label(months[-1]),
                "sparkline": _build_sparkline(points),
            }
        )
    return trends


def _decode_recent_items(items_json: object) -> list[dict] | None:
    if items_json is None:
        return None
    try:
        payload = json.loads(items_json) if isinstance(items_json, str) else items_json
    except json.JSONDecodeError:
        return []
    return receipt_items_to_dicts(receipt_items_from_dicts(payload))


def _decode_recent_certifications(certifications_json: object) -> list[dict] | None:
    if certifications_json is None:
        return None
    try:
        payload = (
            json.loads(certifications_json)
            if isinstance(certifications_json, str)
            else certifications_json
        )
    except json.JSONDecodeError:
        return []
    return receipt_certifications_to_dicts(receipt_certifications_from_dicts(payload))


def _decode_recent_staff(staff_json: object) -> list[dict] | None:
    if staff_json is None:
        return None
    try:
        payload = json.loads(staff_json) if isinstance(staff_json, str) else staff_json
    except json.JSONDecodeError:
        return []
    return receipt_staff_to_dicts(receipt_staff_from_dicts(payload))


_ITEM_SUMMARY_PREVIEW_COUNT = 2
_ITEM_SUMMARY_DESCRIPTION_LIMIT = 34
_ITEM_SUMMARY_LABEL_LIMIT = 90
_ITEM_SUMMARY_FULL_LABEL_LIMIT = 240


def _squash_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _format_item_summary(items: list[dict] | None) -> tuple[str, str, str]:
    if items is None:
        return "—", "—", "—"
    if not items:
        return "0", "0", "0"

    descriptions = [_squash_text(item.get("description")) for item in items]
    descriptions = [description for description in descriptions if description]
    preview = ", ".join(
        _truncate_text(description, _ITEM_SUMMARY_DESCRIPTION_LIMIT)
        for description in descriptions[:_ITEM_SUMMARY_PREVIEW_COUNT]
    )
    if len(items) > _ITEM_SUMMARY_PREVIEW_COUNT:
        preview += ", ..."
    full_preview = ", ".join(descriptions)
    label = _truncate_text(
        f"{len(items)}: {preview}" if preview else str(len(items)), _ITEM_SUMMARY_LABEL_LIMIT
    )
    full_label = _truncate_text(
        f"{len(items)}: {full_preview}" if full_preview else str(len(items)),
        _ITEM_SUMMARY_FULL_LABEL_LIMIT,
    )
    return label, label.replace("|", "/"), full_label


def _format_certification_summary(certifications: list[dict] | None) -> tuple[str, str]:
    if certifications is None:
        return "—", "—"
    if not certifications:
        return "—", "—"
    labels = ", ".join(cert["label"] for cert in certifications if cert.get("label"))
    summary = labels or str(len(certifications))
    return summary, summary.replace("|", "/")


def _format_staff_summary(staff: list[dict] | None) -> tuple[str, str]:
    if staff is None:
        return "—", "—"
    if not staff:
        return "—", "—"
    labels = []
    for entry in staff[:3]:
        role = str(entry.get("role") or "staff").strip()
        name = str(entry.get("name") or "").strip()
        labels.append(f"{role}: {name}" if name else role)
    if len(staff) > 3:
        labels.append("...")
    summary = ", ".join(labels)
    return summary, summary.replace("|", "/")


def _prepare_recent(
    rows: list[dict],
    *,
    receipt_root: Path | None,
    filebrowser_base_url: str | None,
    filebrowser_receipt_root: str,
    merchant_lookup: dict[str, dict] | None = None,
    purchaser_lookup: dict[str, dict] | None = None,
) -> list[dict]:
    prepared: list[dict] = []
    search_cache: dict[str, Path | None] = {}
    for row in rows:
        item = dict(row)
        item["merchant_label"] = _safe_label(item.get("merchant"), "Unknown")
        merchant_page = (
            merchant_lookup.get(_merchant_key(item.get("merchant"))) if merchant_lookup else None
        )
        item["merchant_href_html"] = (
            merchant_page.get("merchant_page_html") if merchant_page else None
        )
        item["merchant_href_md"] = merchant_page.get("merchant_page_md") if merchant_page else None
        item["merchant_markdown"] = _markdown_link(item["merchant_label"], item["merchant_href_md"])
        purchaser_page = (
            purchaser_lookup.get(_purchaser_key(item.get("purchaser")))
            if purchaser_lookup and item.get("purchaser")
            else None
        )
        item["purchaser_href_html"] = purchaser_page.get("html") if purchaser_page else None
        item["purchaser_href_md"] = purchaser_page.get("md") if purchaser_page else None
        item["purchaser_markdown"] = (
            _markdown_link(item["purchaser"], item["purchaser_href_md"])
            if item.get("purchaser")
            else "—"
        )
        item["total_display"] = _format_receipt_total(item.get("total"), item.get("currency"))
        processed_at = item.get("processed_at")
        item["processed_at_display"] = processed_at[:19] if processed_at else "—"
        items = _decode_recent_items(item.get("items_json"))
        item["items"] = items
        item["item_count"] = len(items) if items is not None else None
        item["items_label"], item["items_markdown"], item["items_full_label"] = (
            _format_item_summary(items)
        )
        certifications = _decode_recent_certifications(item.get("certifications_json"))
        item["certifications"] = certifications
        item["certifications_label"], item["certifications_markdown"] = (
            _format_certification_summary(certifications)
        )
        staff = _decode_recent_staff(item.get("staff_json"))
        item["staff"] = staff
        item["staff_label"], item["staff_markdown"] = _format_staff_summary(staff)
        category = _safe_label(item.get("category"), "General")
        category_path = _category_folder_path(receipt_root, category)
        category_uri = _filebrowser_uri_for_local_path(
            category_path,
            receipt_root=receipt_root,
            base_url=filebrowser_base_url,
            root_path=filebrowser_receipt_root,
        )
        folder_path = _receipt_folder_path(
            receipt_root,
            item.get("receipt_dir"),
            filename=item.get("filename"),
            category=category,
            category_path=category_path,
            search_cache=search_cache,
        )
        folder_uri = _filebrowser_uri_for_local_path(
            folder_path,
            receipt_root=receipt_root,
            base_url=filebrowser_base_url,
            root_path=filebrowser_receipt_root,
        )
        file_path = _receipt_file_path(
            receipt_root,
            item.get("receipt_dir"),
            filename=item.get("filename"),
            category=category,
            search_cache=search_cache,
        )
        file_uri = _filebrowser_uri_for_local_path(
            file_path,
            receipt_root=receipt_root,
            base_url=filebrowser_base_url,
            root_path=filebrowser_receipt_root,
        )
        file_link_uri = file_uri
        item["folder_name"] = folder_path.name if folder_path else "—"
        item["category_uri"] = category_uri
        item["folder_uri"] = folder_uri
        item["file_uri"] = file_uri
        item["file_link_uri"] = file_link_uri
        item["category_markdown"] = _markdown_link(category, category_uri)
        item["file_markdown"] = (
            _markdown_link(item["filename"], file_link_uri) if file_link_uri else item["filename"]
        )
        item["folder_markdown"] = (
            _markdown_link(item["folder_name"], folder_uri) if folder_uri else "—"
        )
        prepared.append(item)
    return prepared


def _bar_chart_rows(
    rows: list[dict],
    *,
    label_key: str,
    value_key: str,
    default_label: str,
    kind: str,
    limit: int | None = None,
    href_key: str | None = None,
) -> list[dict]:
    selected = rows[:limit] if limit is not None else rows
    values = [float(row.get(value_key) or 0) for row in selected]
    mean_value = sum(values) / len(values) if values else 0.0
    # Scale so a bar at the mean shows at 50%; bars at 2× mean reach 100%.
    # This keeps small bars visible when one outlier would otherwise dwarf them.
    scale = mean_value * 2 if mean_value > 0 else 1.0
    chart_rows: list[dict] = []
    for row, value in zip(selected, values, strict=True):
        if kind == "currency":
            value_display = _format_currency(value)
        else:
            value_display = str(int(round(value)))
        chart_rows.append(
            {
                "label": _safe_label(row.get(label_key), default_label),
                "count": row.get("count", 0),
                "value_display": value_display,
                "share_display": row.get("share_display", _format_percent(0.0)),
                "bar_width": round(min(100.0, value / scale * 100), 1),
                "href": row.get(href_key) if href_key else None,
            }
        )
    return chart_rows


def _build_monthly_chart(rows: list[dict]) -> dict:
    width = 1000
    height = 320
    plot_left = 64
    plot_right = width - 24
    plot_top = 22
    plot_bottom = height - 56
    label_y = height - 22

    if not rows:
        return {
            "has_data": False,
            "width": width,
            "height": height,
            "plot_left": plot_left,
            "plot_right": plot_right,
            "label_y": label_y,
            "y_ticks": [],
            "line_points": "",
            "area_points": "",
            "points": [],
        }

    max_spend = max((float(row.get("total_spend") or 0) for row in rows), default=0.0)
    scale_max = _nice_axis_max(max_spend)
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    count = len(rows)
    step = plot_width / (count - 1) if count > 1 else 0
    label_stride = max(1, math.ceil(count / 6))
    points: list[dict] = []

    for idx, row in enumerate(rows):
        spend = float(row.get("total_spend") or 0)
        x = plot_left + (step * idx if count > 1 else plot_width / 2)
        y = plot_bottom - ((spend / scale_max) * plot_height if scale_max else 0)
        points.append(
            {
                "x": round(x, 2),
                "y": round(y, 2),
                "label": row.get("month_label", row.get("month", "")),
                "value_display": _format_compact_currency(spend),
                "show_label": idx % label_stride == 0 or idx == count - 1,
                "show_value": count <= 8,
            }
        )

    line_points = " ".join(f"{point['x']},{point['y']}" for point in points)
    first_x = points[0]["x"]
    last_x = points[-1]["x"]
    area_points = f"{line_points} {last_x},{plot_bottom} {first_x},{plot_bottom}"

    y_ticks = []
    for step_idx in range(5):
        value = scale_max * step_idx / 4
        y = plot_bottom - (plot_height * step_idx / 4)
        y_ticks.append({"y": round(y, 2), "label": _format_compact_currency(value)})

    return {
        "has_data": True,
        "width": width,
        "height": height,
        "plot_left": plot_left,
        "plot_right": plot_right,
        "label_y": label_y,
        "y_ticks": y_ticks,
        "line_points": line_points,
        "area_points": area_points,
        "points": points,
    }


def _aggregate_categories(rows: list[dict]) -> list[dict]:
    categories: dict[str, dict] = {}
    for row in rows:
        category = _safe_label(row.get("category"), "General")
        entry = categories.setdefault(
            category,
            {"category": category, "count": 0, "known_total_count": 0, "total_spend": 0.0},
        )
        entry["count"] += 1
        if row.get("total") is not None:
            entry["known_total_count"] += 1
            entry["total_spend"] += float(row.get("total") or 0.0)
    return sorted(
        categories.values(),
        key=lambda item: (-float(item["total_spend"]), -int(item["count"]), item["category"]),
    )


def _aggregate_certifications(rows: list[dict]) -> list[dict]:
    certifications: dict[tuple[str, str], dict] = {}
    for row in rows:
        for cert in row.get("certifications") or []:
            issuer = str(cert.get("issuer") or "").strip()
            label = str(cert.get("label") or "").strip()
            if not issuer or not label:
                continue
            key = (issuer, label)
            entry = certifications.setdefault(
                key,
                {
                    "issuer": issuer,
                    "label": label,
                    "receipt_count": 0,
                    "evidence_entries_md": [],
                    "evidence_entries_html": [],
                    "filenames": set(),
                },
            )
            filename = row.get("filename")
            if filename and filename not in entry["filenames"]:
                entry["filenames"].add(filename)
                entry["receipt_count"] += 1
            evidence_text = cert.get("source_text") or label
            evidence_label_md = row.get("file_markdown") or str(filename or "receipt")
            evidence_label_html = row.get("filename") or "receipt"
            file_href = row.get("file_link_uri")
            evidence_md = f"{evidence_text} on {evidence_label_md}"
            evidence_html = (
                f'{evidence_text} on <a href="{file_href}">{evidence_label_html}</a>'
                if file_href
                else f"{evidence_text} on {evidence_label_html}"
            )
            if evidence_md not in entry["evidence_entries_md"]:
                entry["evidence_entries_md"].append(evidence_md)
            if evidence_html not in entry["evidence_entries_html"]:
                entry["evidence_entries_html"].append(evidence_html)

    prepared: list[dict] = []
    for entry in certifications.values():
        evidence_md = "; ".join(entry["evidence_entries_md"][:3]) or "—"
        evidence_html = "<br>".join(entry["evidence_entries_html"][:3]) or "—"
        prepared.append(
            {
                "issuer": entry["issuer"],
                "label": entry["label"],
                "receipt_count": entry["receipt_count"],
                "evidence_markdown": evidence_md,
                "evidence_html": evidence_html,
            }
        )
    return sorted(
        prepared,
        key=lambda item: (-int(item["receipt_count"]), item["issuer"], item["label"]),
    )


def _build_merchant_directory_view(data: dict) -> dict:
    merchants: list[dict] = []
    for row in data.get("all_merchants", []):
        item = dict(row)
        item["detail_html_href"] = Path(item["merchant_page_html"]).name
        item["detail_md_href"] = Path(item["merchant_page_md"]).name
        item["merchant_markdown"] = _markdown_link(item["merchant_label"], item["detail_md_href"])
        item["filter_text"] = " ".join(
            part
            for part in (
                item["merchant_label"].casefold(),
                str(item.get("date_min") or "").casefold(),
                str(item.get("date_max") or "").casefold(),
            )
            if part
        )
        merchants.append(item)
    return {
        "main_report_html_href": "../intake-report.html",
        "main_report_md_href": "../intake-report.md",
        "merchant_count": len(merchants),
        "total_receipts": data.get("total_receipts", 0),
        "total_spend_display": data.get("total_spend_display", _format_currency(0)),
        "merchants": merchants,
    }


def _relativize_recent_links(receipts: list[dict], *, prefix: str = "../") -> None:
    """Rewrite merchant/purchaser page links produced by _prepare_recent to
    account for pages one level deeper than the report root (models/,
    merchants/, purchasers/)."""
    for row in receipts:
        if row.get("merchant_href_html"):
            row["merchant_href_html"] = f"{prefix}{row['merchant_href_html']}"
        if row.get("merchant_href_md"):
            row["merchant_markdown"] = _markdown_link(
                row["merchant_label"], f"{prefix}{row['merchant_href_md']}"
            )
        if row.get("purchaser_href_html"):
            row["purchaser_href_html"] = f"{prefix}{row['purchaser_href_html']}"
        if row.get("purchaser_href_md"):
            row["purchaser_markdown"] = _markdown_link(
                row["purchaser"], f"{prefix}{row['purchaser_href_md']}"
            )


def _build_merchant_detail_view(
    merchant_row: dict,
    receipt_rows: list[dict],
    month_rows: list[dict],
    *,
    receipt_root: Path | None,
    filebrowser_base_url: str | None,
    filebrowser_receipt_root: str,
    purchaser_lookup: dict[str, dict] | None = None,
) -> dict:
    by_category = _attach_category_links(
        _prepare_rows(
            _aggregate_categories(receipt_rows),
            label_key="category",
            label_name="category",
            value_key="total_spend",
            total_for_share=float(merchant_row.get("total_spend") or 0.0),
            default_label="General",
            kind="currency",
        ),
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
    )
    for row in by_category:
        row["total_spend_display"] = row["value_display"]

    receipts = _prepare_recent(
        receipt_rows,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
        purchaser_lookup=purchaser_lookup,
    )
    _relativize_recent_links(receipts)
    certifications = _aggregate_certifications(receipts)
    month_rows_with_totals = [row for row in month_rows if row.get("has_known_total", True)]

    itemization_attempted = 0
    itemized_receipts = 0
    total_line_items = 0
    for row in receipts:
        items = row.get("items")
        if items is None:
            continue
        itemization_attempted += 1
        if items:
            itemized_receipts += 1
            total_line_items += len(items)

    return {
        "merchant_label": merchant_row["merchant_label"],
        "count": merchant_row["count"],
        "total_spend_display": merchant_row["total_spend_display"],
        "active_months": merchant_row.get("active_months", len(month_rows)),
        "date_min": merchant_row.get("date_min"),
        "date_max": merchant_row.get("date_max"),
        "itemization_attempted": itemization_attempted,
        "itemized_receipts": itemized_receipts,
        "total_line_items": total_line_items,
        "by_month": month_rows,
        "by_category": by_category,
        "certifications": certifications,
        "receipts": receipts,
        "main_report_html_href": "../intake-report.html",
        "main_report_md_href": "../intake-report.md",
        "directory_html_href": "index.html",
        "directory_md_href": "index.md",
        "charts": {
            "monthly": _build_monthly_chart(month_rows_with_totals),
        },
    }


def _model_key(value: str | None) -> str:
    label = _safe_label(value, "tesseract")
    return label.casefold()


def _model_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    return slug or "unknown"


def _build_model_directory_view(data: dict) -> dict:
    models: list[dict] = []
    for row in data.get("by_model", []):
        item = dict(row)
        item["detail_html_href"] = f"{item['model_slug']}.html"
        item["detail_md_href"] = f"{item['model_slug']}.md"
        item["model_markdown"] = _markdown_link(item["model_label"], item["detail_md_href"])
        models.append(item)
    return {
        "main_report_html_href": "../intake-report.html",
        "main_report_md_href": "../intake-report.md",
        "model_count": len(models),
        "total_receipts": data.get("total_receipts", 0),
        "models": models,
    }


def _build_model_detail_view(
    model_row: dict,
    receipt_rows: list[dict],
    *,
    receipt_root: Path | None,
    filebrowser_base_url: str | None,
    filebrowser_receipt_root: str,
    merchant_lookup: dict[str, dict] | None = None,
    purchaser_lookup: dict[str, dict] | None = None,
) -> dict:
    """Build the drill-down page for one extraction model (see _build_merchant_detail_view)."""
    total_spend = sum(
        float(row.get("total") or 0.0)
        for row in receipt_rows
        if row.get("total") is not None and str(row.get("currency") or "USD").upper() == "USD"
    )
    by_category = _attach_category_links(
        _prepare_rows(
            _aggregate_categories(receipt_rows),
            label_key="category",
            label_name="category",
            value_key="total_spend",
            total_for_share=total_spend,
            default_label="General",
            kind="currency",
        ),
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
    )
    for row in by_category:
        row["total_spend_display"] = row["value_display"]

    receipts = _prepare_recent(
        receipt_rows,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
        merchant_lookup=merchant_lookup,
        purchaser_lookup=purchaser_lookup,
    )
    # Recent's merchant/purchaser links are relative to the report root; this
    # page lives one level down, under reports/models/.
    _relativize_recent_links(receipts)

    dates = [row.get("scan_date") for row in receipt_rows if row.get("scan_date")]

    return {
        "model_label": model_row["model_label"],
        "count": model_row["count"],
        "share_display": model_row["share_display"],
        "total_spend_display": _format_currency(total_spend),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "by_category": by_category,
        "receipts": receipts,
        "main_report_html_href": "../intake-report.html",
        "main_report_md_href": "../intake-report.md",
        "directory_html_href": "index.html",
        "directory_md_href": "index.md",
    }


def _purchaser_key(value: str | None) -> str:
    label = _safe_label(value, "Unknown")
    return label.casefold()


def _purchaser_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    return slug or "unknown"


def _build_purchaser_directory_view(data: dict) -> dict:
    purchasers: list[dict] = []
    for row in data.get("by_purchaser", []):
        item = dict(row)
        item["detail_html_href"] = f"{item['purchaser_slug']}.html"
        item["detail_md_href"] = f"{item['purchaser_slug']}.md"
        item["purchaser_markdown"] = _markdown_link(item["purchaser_label"], item["detail_md_href"])
        purchasers.append(item)
    return {
        "main_report_html_href": "../intake-report.html",
        "main_report_md_href": "../intake-report.md",
        "purchaser_count": len(purchasers),
        "total_receipts": data.get("total_receipts", 0),
        "total_spend_display": data.get("total_spend_display", _format_currency(0)),
        "purchasers": purchasers,
    }


def _build_purchaser_detail_view(
    purchaser_row: dict,
    receipt_rows: list[dict],
    *,
    receipt_root: Path | None,
    filebrowser_base_url: str | None,
    filebrowser_receipt_root: str,
    merchant_lookup: dict[str, dict] | None = None,
) -> dict:
    """Build the drill-down page for one purchaser (see _build_merchant_detail_view)."""
    by_category = _attach_category_links(
        _prepare_rows(
            _aggregate_categories(receipt_rows),
            label_key="category",
            label_name="category",
            value_key="total_spend",
            total_for_share=float(purchaser_row.get("total_spend") or 0.0),
            default_label="General",
            kind="currency",
        ),
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
    )
    for row in by_category:
        row["total_spend_display"] = row["value_display"]

    receipts = _prepare_recent(
        receipt_rows,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
        merchant_lookup=merchant_lookup,
    )
    # Recent's merchant links are relative to the report root; this page
    # lives one level down, under reports/purchasers/.
    _relativize_recent_links(receipts)

    dates = [row.get("scan_date") for row in receipt_rows if row.get("scan_date")]

    return {
        "purchaser_label": purchaser_row["purchaser_label"],
        "count": purchaser_row["count"],
        "share_display": purchaser_row["share_display"],
        "total_spend_display": purchaser_row["total_spend_display"],
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "by_category": by_category,
        "receipts": receipts,
        "main_report_html_href": "../intake-report.html",
        "main_report_md_href": "../intake-report.md",
        "directory_html_href": "index.html",
        "directory_md_href": "index.md",
    }


def _build_report_view(
    data: dict,
    *,
    receipt_root: Path | None,
    filebrowser_base_url: str | None,
    filebrowser_receipt_root: str,
) -> dict:
    view = dict(data)
    view["total_spend_display"] = _format_currency(view["total_spend"])
    total_receipts = int(view["total_receipts"] or 0)
    itemized_receipts = int(view.get("itemized_receipts") or 0)
    itemization_attempted = int(view.get("itemization_attempted") or 0)
    itemized_share = (itemized_receipts / total_receipts * 100) if total_receipts else 0.0
    view["itemized_share_display"] = _format_percent(itemized_share)
    view["itemized_coverage_display"] = (
        f"{itemized_receipts}/{total_receipts}" if total_receipts else "0/0"
    )

    by_category = _prepare_rows(
        view["by_category"],
        label_key="category",
        label_name="category",
        value_key="total_spend",
        total_for_share=float(view["total_spend"] or 0),
        default_label="General",
        kind="currency",
    )
    by_month = _prepare_rows(
        view["by_month"],
        label_key="month",
        label_name="month",
        value_key="total_spend",
        total_for_share=float(view["total_spend"] or 0),
        default_label="Unknown",
        kind="currency",
    )
    for row in by_month:
        row["month_label"] = _month_label(row.get("month"))
    by_month_with_totals = [row for row in by_month if row.get("has_known_total", True)]
    all_merchants = _prepare_all_merchants(
        view.get("all_merchants", []),
        total_for_share=float(view["total_spend"] or 0),
    )
    top_merchants = [dict(row) for row in all_merchants[:10]]
    merchant_lookup = {_merchant_key(row.get("merchant")): row for row in all_merchants}
    by_model = _prepare_rows(
        view["by_model"],
        label_key="model",
        label_name="model_label",
        value_key="count",
        total_for_share=float(view["total_receipts"] or 0),
        default_label="tesseract",
        kind="count",
    )
    used_model_slugs: dict[str, int] = {}
    for row in by_model:
        base_slug = _model_slug(row["model_label"])
        slug_count = used_model_slugs.get(base_slug, 0) + 1
        used_model_slugs[base_slug] = slug_count
        row["model_slug"] = base_slug if slug_count == 1 else f"{base_slug}-{slug_count}"
        row["model_href_html"] = f"models/{row['model_slug']}.html"
        row["model_href_md"] = f"models/{row['model_slug']}.md"
        row["model_markdown"] = _markdown_link(row["model_label"], row["model_href_md"])
    by_purchaser = _prepare_rows(
        view.get("by_purchaser", []),
        label_key="purchaser",
        label_name="purchaser_label",
        value_key="total_spend",
        total_for_share=float(view["total_spend"] or 0),
        default_label="Unknown",
        kind="currency",
    )
    used_purchaser_slugs: dict[str, int] = {}
    for row in by_purchaser:
        row["total_spend_display"] = row["value_display"]
        base_slug = _purchaser_slug(row["purchaser_label"])
        slug_count = used_purchaser_slugs.get(base_slug, 0) + 1
        used_purchaser_slugs[base_slug] = slug_count
        row["purchaser_slug"] = base_slug if slug_count == 1 else f"{base_slug}-{slug_count}"
        row["purchaser_href_html"] = f"purchasers/{row['purchaser_slug']}.html"
        row["purchaser_href_md"] = f"purchasers/{row['purchaser_slug']}.md"
        row["purchaser_markdown"] = _markdown_link(row["purchaser_label"], row["purchaser_href_md"])
    purchaser_lookup = {
        _purchaser_key(row["purchaser_label"]): {
            "html": row["purchaser_href_html"],
            "md": row["purchaser_href_md"],
        }
        for row in by_purchaser
    }
    merchant_monthly = _prepare_merchant_monthly(
        view.get("merchant_by_month", []),
        merchant_lookup=merchant_lookup,
    )

    by_category = _attach_category_links(
        by_category,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
    )
    for row in by_category:
        row["total_spend_display"] = row["value_display"]
    for row in by_month:
        row["total_spend_display"] = row["value_display"]
    merchant_trends = _build_merchant_trends(top_merchants, merchant_monthly, by_month_with_totals)

    recent = _prepare_recent(
        view["recent"],
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
        merchant_lookup=merchant_lookup,
        purchaser_lookup=purchaser_lookup,
    )

    top_category = by_category[0] if by_category else None
    top_merchant = top_merchants[0] if top_merchants else None
    receipt_root_uri = _filebrowser_uri(
        None,
        base_url=filebrowser_base_url,
        root_path=filebrowser_receipt_root,
        is_dir=True,
    )
    intake_uri = _filebrowser_uri(
        "intake",
        base_url=filebrowser_base_url,
        root_path=filebrowser_receipt_root,
        is_dir=True,
    )

    view.update(
        {
            "by_category": by_category,
            "by_month": by_month,
            "all_merchants": all_merchants,
            "top_merchants": top_merchants,
            "merchant_monthly": merchant_monthly,
            "merchant_trends": merchant_trends,
            "by_model": by_model,
            "by_purchaser": by_purchaser,
            "recent": recent,
            "top_category_name": top_category["category"] if top_category else "—",
            "top_category_total_display": (
                top_category["total_spend_display"] if top_category else "No category data yet"
            ),
            "top_merchant_name": top_merchant["merchant_label"] if top_merchant else "—",
            "top_merchant_total_display": (
                top_merchant["total_spend_display"] if top_merchant else "No merchant data yet"
            ),
            "itemization_attempted": itemization_attempted,
            "receipt_root_uri": receipt_root_uri,
            "receipt_root_markdown": _markdown_link("receipt/", receipt_root_uri),
            "intake_uri": intake_uri,
            "intake_markdown": _markdown_link("receipt/intake/", intake_uri),
            "merchant_directory_html_path": "merchants/index.html",
            "merchant_directory_md_path": "merchants/index.md",
            "model_directory_html_path": "models/index.html",
            "model_directory_md_path": "models/index.md",
            "purchaser_directory_html_path": "purchasers/index.html",
            "purchaser_directory_md_path": "purchasers/index.md",
            "charts": {
                "monthly": _build_monthly_chart(by_month_with_totals),
                "categories": {
                    "rows": _bar_chart_rows(
                        by_category,
                        label_key="category",
                        value_key="total_spend",
                        default_label="General",
                        kind="currency",
                        limit=8,
                    )
                },
                "merchants": {
                    "rows": _bar_chart_rows(
                        top_merchants,
                        label_key="merchant_label",
                        value_key="total_spend",
                        default_label="Unknown",
                        kind="currency",
                        href_key="merchant_href_html",
                    )
                },
                "models": {
                    "rows": _bar_chart_rows(
                        by_model,
                        label_key="model_label",
                        value_key="count",
                        default_label="tesseract",
                        kind="count",
                    )
                },
            },
        }
    )
    return view


def generate_audit_report(
    conn: sqlite3.Connection,
    output_dir: Path,
    *,
    receipt_root: Path | None = None,
    filebrowser_base_url: str | None = _DEFAULT_FILEBROWSER_BASE_URL,
    filebrowser_receipt_root: str = _DEFAULT_FILEBROWSER_RECEIPT_ROOT,
) -> Path:
    """Write reports/audit.html — filterable receipt audit page."""
    from intake.db import query_all_receipts

    raw_rows = query_all_receipts(conn)

    # Reuse _prepare_recent to get merchant page links and file URIs.
    # Build a lightweight merchant→page-href lookup from available merchant data.
    merchant_page_lookup: dict[str, dict] = {}
    for row in raw_rows:
        key = _merchant_key(row.get("merchant"))
        if key and key not in merchant_page_lookup:
            slug = _merchant_slug(row.get("merchant") or "")
            if slug:
                merchant_page_lookup[key] = {
                    "merchant_page_html": f"merchants/{slug}.html",
                    "merchant_page_md": f"merchants/{slug}.md",
                }

    model_href_lookup: dict[str, str] = {}
    for row in raw_rows:
        key = _model_key(row.get("processed_by"))
        if key not in model_href_lookup:
            slug = _model_slug(_safe_label(row.get("processed_by"), "tesseract"))
            model_href_lookup[key] = f"models/{slug}.html"

    purchaser_page_lookup: dict[str, dict] = {}
    for row in raw_rows:
        if not row.get("purchaser"):
            continue
        key = _purchaser_key(row.get("purchaser"))
        if key not in purchaser_page_lookup:
            slug = _purchaser_slug(row.get("purchaser") or "")
            if slug:
                purchaser_page_lookup[key] = {
                    "html": f"purchasers/{slug}.html",
                    "md": f"purchasers/{slug}.md",
                }

    prepared = _prepare_recent(
        raw_rows,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
        merchant_lookup=merchant_page_lookup,
        purchaser_lookup=purchaser_page_lookup,
    )

    receipts_for_js: list[dict] = []
    for r in prepared:
        receipts_for_js.append(
            {
                "scan_date": r.get("scan_date") or "",
                "merchant": r.get("merchant") or "",
                "merchant_label": r.get("merchant_label") or "",
                "merchant_href": r.get("merchant_href_html") or "",
                "category": r.get("category") or "",
                "total": r.get("total"),
                "total_display": r.get("total_display") or "—",
                "purchaser": r.get("purchaser") or "",
                "purchaser_href": r.get("purchaser_href_html") or "",
                "card_last4": r.get("card_last4") or "",
                "processed_by": r.get("processed_by") or "tesseract",
                "model_href": model_href_lookup.get(_model_key(r.get("processed_by")), ""),
                "items_label": r.get("items_label") or "—",
                "items_full_label": r.get("items_full_label") or r.get("items_label") or "—",
                "file_link_uri": r.get("file_link_uri") or "",
                "folder_uri": r.get("folder_uri") or "",
                "filename": r.get("filename") or "",
                "reviewed": bool(r.get("reviewed_at")),
            }
        )

    categories = sorted({r["category"] for r in receipts_for_js if r["category"]})

    html_env = Environment(loader=BaseLoader(), autoescape=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "audit.html"
    audit_path.write_text(
        html_env.from_string(_AUDIT_HTML_TEMPLATE).render(
            receipts_json=json.dumps(receipts_for_js, ensure_ascii=True),
            categories=categories,
            total_count=len(receipts_for_js),
        ),
        encoding="utf-8",
    )
    return audit_path


def generate_reports(
    conn: sqlite3.Connection,
    output_dir: Path,
    fmt: str = "both",
    *,
    receipt_root: Path | None = None,
    filebrowser_base_url: str | None = _DEFAULT_FILEBROWSER_BASE_URL,
    filebrowser_receipt_root: str = _DEFAULT_FILEBROWSER_RECEIPT_ROOT,
) -> list[Path]:
    """Write Markdown and/or HTML reports to *output_dir*.

    *fmt* is one of ``"md"``, ``"html"``, or ``"both"``.
    Returns the list of paths written.
    """
    from intake.db import query_summary

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_data = query_summary(conn)
    data = _build_report_view(
        raw_data,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
    )

    md_env = Environment(loader=BaseLoader(), autoescape=False)
    html_env = Environment(loader=BaseLoader(), autoescape=True)
    written: list[Path] = []
    merchant_lookup = {
        _merchant_key(row.get("merchant")): row for row in data.get("all_merchants", [])
    }
    purchaser_lookup = {
        _purchaser_key(row.get("purchaser_label")): {
            "html": row.get("purchaser_href_html"),
            "md": row.get("purchaser_href_md"),
        }
        for row in data.get("by_purchaser", [])
    }

    merchant_dir = output_dir / "merchants"
    merchant_receipts_lookup: dict[str, list[dict]] = {}
    for row in raw_data.get("merchant_receipts", []):
        merchant_receipts_lookup.setdefault(_merchant_key(row.get("merchant")), []).append(
            dict(row)
        )
    merchant_month_lookup: dict[str, list[dict]] = {}
    for row in data.get("merchant_monthly", []):
        merchant_month_lookup.setdefault(_merchant_key(row.get("merchant")), []).append(dict(row))
    merchant_directory_view = _build_merchant_directory_view(data)
    merchant_detail_views = [
        (
            merchant_row,
            _build_merchant_detail_view(
                merchant_row,
                merchant_receipts_lookup.get(_merchant_key(merchant_row.get("merchant")), []),
                merchant_month_lookup.get(_merchant_key(merchant_row.get("merchant")), []),
                receipt_root=receipt_root,
                filebrowser_base_url=filebrowser_base_url,
                filebrowser_receipt_root=filebrowser_receipt_root,
                purchaser_lookup=purchaser_lookup,
            ),
        )
        for merchant_row in data.get("all_merchants", [])
    ]

    model_dir = output_dir / "models"
    model_receipts_lookup: dict[str, list[dict]] = {}
    for row in raw_data.get("merchant_receipts", []):
        model_receipts_lookup.setdefault(_model_key(row.get("processed_by")), []).append(dict(row))
    model_directory_view = _build_model_directory_view(data)
    model_detail_views = [
        (
            model_row,
            _build_model_detail_view(
                model_row,
                model_receipts_lookup.get(_model_key(model_row.get("model_label")), []),
                receipt_root=receipt_root,
                filebrowser_base_url=filebrowser_base_url,
                filebrowser_receipt_root=filebrowser_receipt_root,
                merchant_lookup=merchant_lookup,
                purchaser_lookup=purchaser_lookup,
            ),
        )
        for model_row in data.get("by_model", [])
    ]

    purchaser_dir = output_dir / "purchasers"
    purchaser_receipts_lookup: dict[str, list[dict]] = {}
    for row in raw_data.get("merchant_receipts", []):
        purchaser_receipts_lookup.setdefault(_purchaser_key(row.get("purchaser")), []).append(
            dict(row)
        )
    purchaser_directory_view = _build_purchaser_directory_view(data)
    purchaser_detail_views = [
        (
            purchaser_row,
            _build_purchaser_detail_view(
                purchaser_row,
                purchaser_receipts_lookup.get(
                    _purchaser_key(purchaser_row.get("purchaser_label")), []
                ),
                receipt_root=receipt_root,
                filebrowser_base_url=filebrowser_base_url,
                filebrowser_receipt_root=filebrowser_receipt_root,
                merchant_lookup=merchant_lookup,
            ),
        )
        for purchaser_row in data.get("by_purchaser", [])
    ]

    if fmt in ("md", "both"):
        md_path = output_dir / "intake-report.md"
        md_path.write_text(md_env.from_string(_MD_TEMPLATE).render(data=data), encoding="utf-8")
        written.append(md_path)
        merchant_dir.mkdir(parents=True, exist_ok=True)
        merchant_index_md_path = merchant_dir / "index.md"
        merchant_index_md_path.write_text(
            md_env.from_string(_MERCHANT_DIRECTORY_MD_TEMPLATE).render(
                data=merchant_directory_view
            ),
            encoding="utf-8",
        )
        written.append(merchant_index_md_path)
        for merchant_row, merchant_view in merchant_detail_views:
            merchant_md_path = merchant_dir / f"{merchant_row['merchant_slug']}.md"
            merchant_md_path.write_text(
                md_env.from_string(_MERCHANT_DETAIL_MD_TEMPLATE).render(data=merchant_view),
                encoding="utf-8",
            )
            written.append(merchant_md_path)
        model_dir.mkdir(parents=True, exist_ok=True)
        model_index_md_path = model_dir / "index.md"
        model_index_md_path.write_text(
            md_env.from_string(_MODEL_DIRECTORY_MD_TEMPLATE).render(data=model_directory_view),
            encoding="utf-8",
        )
        written.append(model_index_md_path)
        for model_row, model_view in model_detail_views:
            model_md_path = model_dir / f"{model_row['model_slug']}.md"
            model_md_path.write_text(
                md_env.from_string(_MODEL_DETAIL_MD_TEMPLATE).render(data=model_view),
                encoding="utf-8",
            )
            written.append(model_md_path)
        purchaser_dir.mkdir(parents=True, exist_ok=True)
        purchaser_index_md_path = purchaser_dir / "index.md"
        purchaser_index_md_path.write_text(
            md_env.from_string(_PURCHASER_DIRECTORY_MD_TEMPLATE).render(
                data=purchaser_directory_view
            ),
            encoding="utf-8",
        )
        written.append(purchaser_index_md_path)
        for purchaser_row, purchaser_view in purchaser_detail_views:
            purchaser_md_path = purchaser_dir / f"{purchaser_row['purchaser_slug']}.md"
            purchaser_md_path.write_text(
                md_env.from_string(_PURCHASER_DETAIL_MD_TEMPLATE).render(data=purchaser_view),
                encoding="utf-8",
            )
            written.append(purchaser_md_path)

    if fmt in ("html", "both"):
        html_path = output_dir / "intake-report.html"
        html_path.write_text(
            html_env.from_string(_HTML_TEMPLATE).render(data=data), encoding="utf-8"
        )
        written.append(html_path)
        merchant_dir.mkdir(parents=True, exist_ok=True)
        merchant_index_html_path = merchant_dir / "index.html"
        merchant_index_html_path.write_text(
            html_env.from_string(_MERCHANT_DIRECTORY_HTML_TEMPLATE).render(
                data=merchant_directory_view
            ),
            encoding="utf-8",
        )
        written.append(merchant_index_html_path)
        for merchant_row, merchant_view in merchant_detail_views:
            merchant_html_path = merchant_dir / f"{merchant_row['merchant_slug']}.html"
            merchant_html_path.write_text(
                html_env.from_string(_MERCHANT_DETAIL_HTML_TEMPLATE).render(data=merchant_view),
                encoding="utf-8",
            )
            written.append(merchant_html_path)
        model_dir.mkdir(parents=True, exist_ok=True)
        model_index_html_path = model_dir / "index.html"
        model_index_html_path.write_text(
            html_env.from_string(_MODEL_DIRECTORY_HTML_TEMPLATE).render(data=model_directory_view),
            encoding="utf-8",
        )
        written.append(model_index_html_path)
        for model_row, model_view in model_detail_views:
            model_html_path = model_dir / f"{model_row['model_slug']}.html"
            model_html_path.write_text(
                html_env.from_string(_MODEL_DETAIL_HTML_TEMPLATE).render(data=model_view),
                encoding="utf-8",
            )
            written.append(model_html_path)
        purchaser_dir.mkdir(parents=True, exist_ok=True)
        purchaser_index_html_path = purchaser_dir / "index.html"
        purchaser_index_html_path.write_text(
            html_env.from_string(_PURCHASER_DIRECTORY_HTML_TEMPLATE).render(
                data=purchaser_directory_view
            ),
            encoding="utf-8",
        )
        written.append(purchaser_index_html_path)
        for purchaser_row, purchaser_view in purchaser_detail_views:
            purchaser_html_path = purchaser_dir / f"{purchaser_row['purchaser_slug']}.html"
            purchaser_html_path.write_text(
                html_env.from_string(_PURCHASER_DETAIL_HTML_TEMPLATE).render(data=purchaser_view),
                encoding="utf-8",
            )
            written.append(purchaser_html_path)
        audit_path = generate_audit_report(
            conn,
            output_dir,
            receipt_root=receipt_root,
            filebrowser_base_url=filebrowser_base_url,
            filebrowser_receipt_root=filebrowser_receipt_root,
        )
        written.append(audit_path)

    return written
