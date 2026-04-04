"""Generate Markdown and HTML summary reports from the intake database."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from pathlib import Path

from jinja2 import BaseLoader, Environment

_MD_TEMPLATE = """\
# Intake Receipt Report

## Summary

| Field | Value |
|---|---|
| Total Receipts | {{ data.total_receipts }} |
| Total Spend | {{ data.total_spend_display }} |
| Date Range | {{ data.date_min or "—" }} → {{ data.date_max or "—" }} |

## Spending by Category

| Category | Count | Total Spend | % of Total | Chart |
|---|---|---|---|---|
{% for row in data.by_category -%}
| {{ row.category_markdown }} | {{ row.count }} | {{ row.total_spend_display }} | {{ row.share_display }} | {{ row.chart_bar or "—" }} |
{% endfor %}

## Spending by Month

| Month | Count | Total Spend | Chart |
|---|---|---|---|
{% for row in data.by_month -%}
| {{ row.month_label }} | {{ row.count }} | {{ row.total_spend_display }} | {{ row.chart_bar or "—" }} |
{% endfor %}

## Top 10 Merchants

| Merchant | Count | Total Spend | % of Total | Chart |
|---|---|---|---|---|
{% for row in data.top_merchants -%}
| {{ row.merchant_label }} | {{ row.count }} | {{ row.total_spend_display }} | {{ row.share_display }} | {{ row.chart_bar or "—" }} |
{% endfor %}

## Extraction Model Breakdown

| Model | Receipt Count | Share | Chart |
|---|---|---|---|
{% for row in data.by_model -%}
| {{ row.model_label }} | {{ row.count }} | {{ row.share_display }} | {{ row.chart_bar or "—" }} |
{% endfor %}

## Recent Receipts (last 50)

| Date | Merchant | Category | Total | Purchaser | Card | File | Folder | Model | Processed At |
|---|---|---|---|---|---|---|---|---|---|
{% for row in data.recent -%}
| {{ row.scan_date }} | {{ row.merchant or "Unknown" }} | {{ row.category_markdown }} | {% if row.total is not none %}{{ row.total_display }}{% else %}—{% endif %} | {{ row.purchaser or "—" }} | {{ row.card_last4 or "—" }} | {{ row.file_markdown }} | {{ row.folder_markdown }} | {{ row.processed_by or "tesseract" }} | {{ row.processed_at_display }} |
{% endfor %}
"""

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intake Receipt Report</title>
<style>
  :root {
    --ink: #18212f;
    --muted: #667085;
    --paper: #f3efe6;
    --panel: rgba(255, 255, 255, 0.9);
    --line: rgba(24, 33, 47, 0.12);
    --shadow: 0 20px 45px rgba(24, 33, 47, 0.09);
    --teal: #0f766e;
    --teal-soft: #d3f1ec;
    --amber: #c77d2b;
    --amber-soft: #f7dfc4;
    --blue: #375a9e;
    --blue-soft: #dce7ff;
    --rose: #b5475d;
    --rose-soft: #f7d9e0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(circle at top left, rgba(15, 118, 110, 0.16), transparent 34%),
      radial-gradient(circle at top right, rgba(199, 125, 43, 0.14), transparent 30%),
      linear-gradient(180deg, #f8f5ef 0%, #f2eee6 100%);
  }
  .page {
    max-width: 1240px;
    margin: 0 auto;
    padding: 32px 20px 56px;
  }
  .hero {
    background: linear-gradient(135deg, rgba(255, 255, 255, 0.96), rgba(248, 248, 244, 0.92));
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 26px;
    box-shadow: var(--shadow);
    padding: 28px;
  }
  .eyebrow {
    font-size: 12px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--teal);
    margin: 0 0 8px;
  }
  h1 {
    margin: 0;
    font-size: clamp(2rem, 5vw, 3.8rem);
    line-height: 0.95;
    letter-spacing: -0.04em;
  }
  .hero p {
    margin: 14px 0 0;
    color: var(--muted);
    max-width: 50rem;
    line-height: 1.55;
  }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 16px;
    margin-top: 22px;
  }
  .summary-card {
    background: var(--panel);
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 20px;
    padding: 18px 18px 16px;
  }
  .summary-card .label {
    display: block;
    font-size: 12px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .summary-card .value {
    display: block;
    font-size: clamp(1.5rem, 4vw, 2.1rem);
    font-weight: 700;
    letter-spacing: -0.04em;
  }
  .summary-card .subtext {
    display: block;
    margin-top: 6px;
    color: var(--muted);
    font-size: 0.92rem;
  }
  h2 {
    margin: 34px 0 14px;
    font-size: 1.32rem;
    letter-spacing: -0.03em;
  }
  .chart-grid {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 18px;
  }
  .chart-card {
    grid-column: span 12;
    background: var(--panel);
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 24px;
    padding: 18px 18px 20px;
    box-shadow: var(--shadow);
  }
  .chart-card.wide { grid-column: span 12; }
  .chart-card.half { grid-column: span 12; }
  @media (min-width: 980px) {
    .chart-card.half { grid-column: span 6; }
  }
  .chart-head {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    align-items: baseline;
    margin-bottom: 14px;
  }
  .chart-head h3 {
    margin: 0;
    font-size: 1.05rem;
    letter-spacing: -0.02em;
  }
  .chart-head p {
    margin: 0;
    color: var(--muted);
    font-size: 0.92rem;
  }
  .empty-state {
    padding: 20px;
    border-radius: 18px;
    background: rgba(255, 255, 255, 0.72);
    color: var(--muted);
    border: 1px dashed rgba(24, 33, 47, 0.16);
  }
  .line-chart {
    width: 100%;
    height: auto;
    display: block;
    overflow: visible;
  }
  .line-chart .grid { stroke: rgba(24, 33, 47, 0.10); stroke-width: 1; }
  .line-chart .axis-label {
    fill: var(--muted);
    font-size: 11px;
  }
  .line-chart .area { fill: rgba(15, 118, 110, 0.14); }
  .line-chart .trend {
    fill: none;
    stroke: var(--teal);
    stroke-width: 3;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .line-chart .point {
    fill: white;
    stroke: var(--teal);
    stroke-width: 2.5;
  }
  .line-chart .point-value {
    fill: var(--ink);
    font-size: 11px;
    font-weight: 600;
  }
  .bar-list {
    display: grid;
    gap: 12px;
  }
  .bar-row {
    padding: 10px 12px;
    border-radius: 16px;
    background: rgba(255, 255, 255, 0.72);
    border: 1px solid rgba(24, 33, 47, 0.05);
  }
  .bar-meta,
  .bar-foot {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: baseline;
  }
  .bar-meta {
    font-size: 0.95rem;
    font-weight: 600;
  }
  .bar-meta .value,
  .bar-foot {
    color: var(--muted);
    font-size: 0.88rem;
  }
  .bar-track {
    height: 12px;
    border-radius: 999px;
    overflow: hidden;
    background: rgba(24, 33, 47, 0.08);
    margin: 8px 0 6px;
  }
  .bar-fill {
    height: 100%;
    border-radius: 999px;
  }
  .fill-category { background: linear-gradient(90deg, var(--teal), #35a69f); }
  .fill-merchant { background: linear-gradient(90deg, var(--amber), #e4a44c); }
  .fill-model { background: linear-gradient(90deg, var(--blue), #6f8fe3); }
  .detail-table {
    width: 100%;
    border-collapse: collapse;
    background: rgba(255, 255, 255, 0.86);
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 18px;
    overflow: hidden;
    box-shadow: var(--shadow);
  }
  .detail-table th,
  .detail-table td {
    padding: 11px 12px;
    text-align: left;
    border-bottom: 1px solid rgba(24, 33, 47, 0.08);
    vertical-align: top;
  }
  .detail-table th {
    font-size: 12px;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    color: var(--muted);
    background: rgba(243, 239, 230, 0.95);
  }
  .detail-table tbody tr:nth-child(even) td {
    background: rgba(248, 247, 243, 0.7);
  }
  .detail-table tbody tr:last-child td {
    border-bottom: none;
  }
  a {
    color: var(--blue);
    text-decoration: none;
    font-weight: 600;
  }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<main class="page">
  <section class="hero">
    <p class="eyebrow">Intake Dashboard</p>
    <h1>Receipt activity at a glance.</h1>
    <p>Daily totals, dominant categories, merchant concentration, and model usage from the current intake database.</p>
    <div class="summary-grid">
      <article class="summary-card">
        <span class="label">Total Receipts</span>
        <span class="value">{{ data.total_receipts }}</span>
        <span class="subtext">{{ data.date_min or "—" }} to {{ data.date_max or "—" }}</span>
      </article>
      <article class="summary-card">
        <span class="label">Total Spend</span>
        <span class="value">{{ data.total_spend_display }}</span>
        <span class="subtext">Across all processed receipts</span>
      </article>
      <article class="summary-card">
        <span class="label">Top Category</span>
        <span class="value">{{ data.top_category_name }}</span>
        <span class="subtext">{{ data.top_category_total_display }}</span>
      </article>
      <article class="summary-card">
        <span class="label">Top Merchant</span>
        <span class="value">{{ data.top_merchant_name }}</span>
        <span class="subtext">{{ data.top_merchant_total_display }}</span>
      </article>
    </div>
  </section>

  <h2>Visual Overview</h2>
  <section class="chart-grid">
    <article class="chart-card wide">
      <div class="chart-head">
        <div>
          <h3>Monthly Spend Trend</h3>
          <p>Receipt totals grouped by scan month.</p>
        </div>
        <p>{{ data.by_month|length }} month(s)</p>
      </div>
      {% if data.charts.monthly.has_data %}
      <svg class="line-chart" viewBox="0 0 {{ data.charts.monthly.width }} {{ data.charts.monthly.height }}" role="img" aria-label="Monthly spend trend">
        {% for tick in data.charts.monthly.y_ticks %}
        <line class="grid" x1="{{ data.charts.monthly.plot_left }}" y1="{{ tick.y }}" x2="{{ data.charts.monthly.plot_right }}" y2="{{ tick.y }}"></line>
        <text class="axis-label" x="{{ data.charts.monthly.plot_left - 10 }}" y="{{ tick.y + 4 }}" text-anchor="end">{{ tick.label }}</text>
        {% endfor %}
        <polygon class="area" points="{{ data.charts.monthly.area_points }}"></polygon>
        <polyline class="trend" points="{{ data.charts.monthly.line_points }}"></polyline>
        {% for point in data.charts.monthly.points %}
        <circle class="point" cx="{{ point.x }}" cy="{{ point.y }}" r="5"></circle>
        {% if point.show_value %}
        <text class="point-value" x="{{ point.x }}" y="{{ point.y - 12 }}" text-anchor="middle">{{ point.value_display }}</text>
        {% endif %}
        {% if point.show_label %}
        <text class="axis-label" x="{{ point.x }}" y="{{ data.charts.monthly.label_y }}" text-anchor="middle">{{ point.label }}</text>
        {% endif %}
        {% endfor %}
      </svg>
      {% else %}
      <div class="empty-state">No spend data yet.</div>
      {% endif %}
    </article>

    <article class="chart-card half">
      <div class="chart-head">
        <div>
          <h3>Top Categories by Spend</h3>
          <p>Where the largest share of money is going.</p>
        </div>
        <p>{{ data.charts.categories.rows|length }} shown</p>
      </div>
      {% if data.charts.categories.rows %}
      <div class="bar-list">
        {% for row in data.charts.categories.rows %}
        <div class="bar-row">
          <div class="bar-meta">
            <span>{{ row.label }}</span>
            <span class="value">{{ row.value_display }}</span>
          </div>
          <div class="bar-track"><div class="bar-fill fill-category" style="width: {{ row.bar_width }}%"></div></div>
          <div class="bar-foot">
            <span>{{ row.count }} receipt(s)</span>
            <span>{{ row.share_display }}</span>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty-state">No category data yet.</div>
      {% endif %}
    </article>

    <article class="chart-card half">
      <div class="chart-head">
        <div>
          <h3>Top Merchants by Spend</h3>
          <p>The heaviest merchants in the dataset.</p>
        </div>
        <p>{{ data.charts.merchants.rows|length }} shown</p>
      </div>
      {% if data.charts.merchants.rows %}
      <div class="bar-list">
        {% for row in data.charts.merchants.rows %}
        <div class="bar-row">
          <div class="bar-meta">
            <span>{{ row.label }}</span>
            <span class="value">{{ row.value_display }}</span>
          </div>
          <div class="bar-track"><div class="bar-fill fill-merchant" style="width: {{ row.bar_width }}%"></div></div>
          <div class="bar-foot">
            <span>{{ row.count }} receipt(s)</span>
            <span>{{ row.share_display }}</span>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty-state">No merchant data yet.</div>
      {% endif %}
    </article>

    <article class="chart-card half">
      <div class="chart-head">
        <div>
          <h3>Extraction Model Mix</h3>
          <p>How many receipts each extraction path handled.</p>
        </div>
        <p>{{ data.charts.models.rows|length }} model(s)</p>
      </div>
      {% if data.charts.models.rows %}
      <div class="bar-list">
        {% for row in data.charts.models.rows %}
        <div class="bar-row">
          <div class="bar-meta">
            <span>{{ row.label }}</span>
            <span class="value">{{ row.value_display }}</span>
          </div>
          <div class="bar-track"><div class="bar-fill fill-model" style="width: {{ row.bar_width }}%"></div></div>
          <div class="bar-foot">
            <span>{{ row.share_display }}</span>
            <span>{{ row.count }} receipt(s)</span>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty-state">No extraction data yet.</div>
      {% endif %}
    </article>
  </section>

  <h2>Detail Tables</h2>

  <h3>Spending by Category</h3>
  <table class="detail-table">
    <thead><tr><th>Category</th><th>Count</th><th>Total Spend</th><th>% of Total</th></tr></thead>
    <tbody>
    {% for row in data.by_category %}
    <tr>
      <td>{% if row.folder_uri %}<a href="{{ row.folder_uri }}">{{ row.category }}</a>{% else %}{{ row.category }}{% endif %}</td>
      <td>{{ row.count }}</td>
      <td>{{ row.total_spend_display }}</td>
      <td>{{ row.share_display }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>

  <h3>Spending by Month</h3>
  <table class="detail-table">
    <thead><tr><th>Month</th><th>Count</th><th>Total Spend</th></tr></thead>
    <tbody>
    {% for row in data.by_month %}
    <tr><td>{{ row.month_label }}</td><td>{{ row.count }}</td><td>{{ row.total_spend_display }}</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h3>Top 10 Merchants</h3>
  <table class="detail-table">
    <thead><tr><th>Merchant</th><th>Count</th><th>Total Spend</th><th>% of Total</th></tr></thead>
    <tbody>
    {% for row in data.top_merchants %}
    <tr><td>{{ row.merchant_label }}</td><td>{{ row.count }}</td><td>{{ row.total_spend_display }}</td><td>{{ row.share_display }}</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h3>Extraction Model Breakdown</h3>
  <table class="detail-table">
    <thead><tr><th>Model</th><th>Receipt Count</th><th>Share</th></tr></thead>
    <tbody>
    {% for row in data.by_model %}
    <tr><td>{{ row.model_label }}</td><td>{{ row.count }}</td><td>{{ row.share_display }}</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h3>Recent Receipts (last 50)</h3>
  <table class="detail-table">
    <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Total</th><th>Purchaser</th><th>Card</th><th>File</th><th>Folder</th><th>Model</th><th>Processed At</th></tr></thead>
    <tbody>
    {% for row in data.recent %}
    <tr>
      <td>{{ row.scan_date }}</td>
      <td>{{ row.merchant or "Unknown" }}</td>
      <td>{% if row.category_uri %}<a href="{{ row.category_uri }}">{{ row.category }}</a>{% else %}{{ row.category }}{% endif %}</td>
      <td>{% if row.total is not none %}{{ row.total_display }}{% else %}—{% endif %}</td>
      <td>{{ row.purchaser or "—" }}</td>
      <td>{{ "···· " + row.card_last4 if row.card_last4 else "—" }}</td>
      <td>{% if row.file_link_uri %}<a href="{{ row.file_link_uri }}">{{ row.filename }}</a>{% else %}{{ row.filename }}{% endif %}</td>
      <td>{% if row.folder_uri %}<a href="{{ row.folder_uri }}">{{ row.folder_name }}</a>{% else %}—{% endif %}</td>
      <td>{{ row.processed_by or "tesseract" }}</td>
      <td>{{ row.processed_at_display }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</main>
</body>
</html>
"""


def _format_currency(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def _format_percent(value: float) -> str:
    return f"{value:.1f}%"


def _format_compact_currency(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


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


def _path_uri(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        return path.resolve().as_uri()
    except ValueError:
        return None


def _markdown_link(label: str, uri: str | None) -> str:
    return f"[{label}]({uri})" if uri else label


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

    if receipt_dir:
        relative = Path(receipt_dir)
        if relative.suffix.lower() == ".pdf":
            candidate = receipt_root / relative
            if candidate.exists():
                return candidate
        elif filename:
            candidate = receipt_root / relative / filename
            if candidate.exists():
                return candidate

    if filename and filename.lower().endswith(".pdf") and category:
        candidate = receipt_root / Path(category) / filename
        if candidate.exists():
            return candidate

    if not filename or not filename.lower().endswith(".pdf"):
        return None
    if search_cache is not None and filename in search_cache:
        return search_cache[filename]

    matches = sorted(
        path for path in receipt_root.rglob(filename) if path.is_file() and path.suffix.lower() == ".pdf"
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
        share_pct = (value / total_for_share * 100) if total_for_share else 0.0
        if kind == "currency":
            value_display = _format_currency(value)
        else:
            value_display = str(int(round(value)))
        item[label_name] = label
        item["share_pct"] = share_pct
        item["share_display"] = _format_percent(share_pct)
        item["value_display"] = value_display
        item["chart_bar"] = _ascii_bar(value, max_value)
        prepared.append(item)
    return prepared


def _prepare_recent(rows: list[dict], *, receipt_root: Path | None) -> list[dict]:
    prepared: list[dict] = []
    search_cache: dict[str, Path | None] = {}
    for row in rows:
        item = dict(row)
        item["total_display"] = _format_currency(item.get("total"))
        processed_at = item.get("processed_at")
        item["processed_at_display"] = processed_at[:19] if processed_at else "—"
        category = _safe_label(item.get("category"), "General")
        category_path = _category_folder_path(receipt_root, category)
        category_uri = _path_uri(category_path)
        folder_path = _receipt_folder_path(
            receipt_root,
            item.get("receipt_dir"),
            filename=item.get("filename"),
            category=category,
            category_path=category_path,
            search_cache=search_cache,
        )
        folder_uri = _path_uri(folder_path)
        file_path = _receipt_file_path(
            receipt_root,
            item.get("receipt_dir"),
            filename=item.get("filename"),
            category=category,
            search_cache=search_cache,
        )
        file_uri = _path_uri(file_path)
        file_link_uri = file_uri or folder_uri
        item["folder_name"] = folder_path.name if folder_path else "—"
        item["category_uri"] = category_uri
        item["folder_uri"] = folder_uri
        item["file_uri"] = file_uri
        item["file_link_uri"] = file_link_uri
        item["category_markdown"] = _markdown_link(category, category_uri)
        item["file_markdown"] = (
            _markdown_link(item["filename"], file_link_uri) if file_link_uri else item["filename"]
        )
        item["folder_markdown"] = _markdown_link(item["folder_name"], folder_uri) if folder_uri else "—"
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
) -> list[dict]:
    selected = rows[:limit] if limit is not None else rows
    max_value = max((float(row.get(value_key) or 0) for row in selected), default=0.0)
    chart_rows: list[dict] = []
    for row in selected:
        value = float(row.get(value_key) or 0)
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
                "bar_width": round((value / max_value * 100) if max_value else 0.0, 1),
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


def _build_report_view(data: dict, *, receipt_root: Path | None) -> dict:
    view = dict(data)
    view["total_spend_display"] = _format_currency(view["total_spend"])

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
    top_merchants = _prepare_rows(
        view["top_merchants"],
        label_key="merchant",
        label_name="merchant_label",
        value_key="total_spend",
        total_for_share=float(view["total_spend"] or 0),
        default_label="Unknown",
        kind="currency",
    )
    by_model = _prepare_rows(
        view["by_model"],
        label_key="model",
        label_name="model_label",
        value_key="count",
        total_for_share=float(view["total_receipts"] or 0),
        default_label="tesseract",
        kind="count",
    )

    for row in by_category:
        row["total_spend_display"] = row["value_display"]
        folder_uri = _path_uri(_category_folder_path(receipt_root, row["category"]))
        row["folder_uri"] = folder_uri
        row["category_markdown"] = _markdown_link(row["category"], folder_uri)
    for row in by_month:
        row["total_spend_display"] = row["value_display"]
    for row in top_merchants:
        row["total_spend_display"] = row["value_display"]

    recent = _prepare_recent(view["recent"], receipt_root=receipt_root)

    top_category = by_category[0] if by_category else None
    top_merchant = top_merchants[0] if top_merchants else None

    view.update(
        {
            "by_category": by_category,
            "by_month": by_month,
            "top_merchants": top_merchants,
            "by_model": by_model,
            "recent": recent,
            "top_category_name": top_category["category"] if top_category else "—",
            "top_category_total_display": (
                top_category["total_spend_display"] if top_category else "No category data yet"
            ),
            "top_merchant_name": top_merchant["merchant_label"] if top_merchant else "—",
            "top_merchant_total_display": (
                top_merchant["total_spend_display"] if top_merchant else "No merchant data yet"
            ),
            "charts": {
                "monthly": _build_monthly_chart(by_month),
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


def generate_reports(
    conn: sqlite3.Connection,
    output_dir: Path,
    fmt: str = "both",
    *,
    receipt_root: Path | None = None,
) -> list[Path]:
    """Write Markdown and/or HTML reports to *output_dir*.

    *fmt* is one of ``"md"``, ``"html"``, or ``"both"``.
    Returns the list of paths written.
    """
    from intake.db import query_summary

    output_dir.mkdir(parents=True, exist_ok=True)
    data = _build_report_view(query_summary(conn), receipt_root=receipt_root)

    md_env = Environment(loader=BaseLoader(), autoescape=False)
    html_env = Environment(loader=BaseLoader(), autoescape=True)
    written: list[Path] = []

    if fmt in ("md", "both"):
        md_path = output_dir / "intake-report.md"
        md_path.write_text(md_env.from_string(_MD_TEMPLATE).render(data=data), encoding="utf-8")
        written.append(md_path)

    if fmt in ("html", "both"):
        html_path = output_dir / "intake-report.html"
        html_path.write_text(html_env.from_string(_HTML_TEMPLATE).render(data=data), encoding="utf-8")
        written.append(html_path)

    return written
