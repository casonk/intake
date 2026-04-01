"""Generate Markdown and HTML summary reports from the intake database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jinja2 import Environment, BaseLoader

_MD_TEMPLATE = """\
# Intake Receipt Report

## Summary

| Field | Value |
|---|---|
| Total Receipts | {{ data.total_receipts }} |
| Total Spend | ${{ "%.2f"|format(data.total_spend) }} |
| Date Range | {{ data.date_min or "—" }} → {{ data.date_max or "—" }} |

## Spending by Category

| Category | Count | Total Spend | % of Total |
|---|---|---|---|
{% for row in data.by_category -%}
| {{ row.category }} | {{ row.count }} | ${{ "%.2f"|format(row.total_spend) }} | {{ "%.1f"|format(row.total_spend / data.total_spend * 100 if data.total_spend else 0) }}% |
{% endfor %}
## Spending by Month

| Month | Count | Total Spend |
|---|---|---|
{% for row in data.by_month -%}
| {{ row.month }} | {{ row.count }} | ${{ "%.2f"|format(row.total_spend) }} |
{% endfor %}
## Top 10 Merchants

| Merchant | Count | Total Spend |
|---|---|---|
{% for row in data.top_merchants -%}
| {{ row.merchant or "Unknown" }} | {{ row.count }} | ${{ "%.2f"|format(row.total_spend) }} |
{% endfor %}
## Extraction Model Breakdown

| Model | Receipt Count |
|---|---|
{% for row in data.by_model -%}
| {{ row.model }} | {{ row.count }} |
{% endfor %}
## Recent Receipts (last 20)

| Date | Merchant | Category | Total | Purchaser | Card | Filename | Model | Processed At |
|---|---|---|---|---|---|---|---|---|
{% for row in data.recent -%}
| {{ row.scan_date }} | {{ row.merchant or "Unknown" }} | {{ row.category }} | {% if row.total is not none %}${{ "%.2f"|format(row.total) }}{% else %}—{% endif %} | {{ row.purchaser or "—" }} | {{ row.card_last4 or "—" }} | {{ row.filename }} | {{ row.processed_by or "tesseract" }} | {{ row.processed_at[:19] if row.processed_at else "—" }} |
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
  body { font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { border-bottom: 2px solid #555; padding-bottom: .4rem; }
  h2 { border-bottom: 1px solid #ccc; padding-bottom: .2rem; margin-top: 2rem; }
  table { border-collapse: collapse; width: 100%; margin-top: .5rem; }
  th, td { border: 1px solid #ddd; padding: .4rem .7rem; text-align: left; }
  th { background: #f4f4f4; }
  tr:nth-child(even) { background: #fafafa; }
  .summary-table td:first-child { font-weight: bold; width: 14rem; }
</style>
</head>
<body>
<h1>Intake Receipt Report</h1>

<h2>Summary</h2>
<table class="summary-table">
  <tr><td>Total Receipts</td><td>{{ data.total_receipts }}</td></tr>
  <tr><td>Total Spend</td><td>${{ "%.2f"|format(data.total_spend) }}</td></tr>
  <tr><td>Date Range</td><td>{{ data.date_min or "—" }} → {{ data.date_max or "—" }}</td></tr>
</table>

<h2>Spending by Category</h2>
<table>
  <thead><tr><th>Category</th><th>Count</th><th>Total Spend</th><th>% of Total</th></tr></thead>
  <tbody>
  {% for row in data.by_category %}
  <tr>
    <td>{{ row.category }}</td>
    <td>{{ row.count }}</td>
    <td>${{ "%.2f"|format(row.total_spend) }}</td>
    <td>{{ "%.1f"|format(row.total_spend / data.total_spend * 100 if data.total_spend else 0) }}%</td>
  </tr>
  {% endfor %}
  </tbody>
</table>

<h2>Spending by Month</h2>
<table>
  <thead><tr><th>Month</th><th>Count</th><th>Total Spend</th></tr></thead>
  <tbody>
  {% for row in data.by_month %}
  <tr><td>{{ row.month }}</td><td>{{ row.count }}</td><td>${{ "%.2f"|format(row.total_spend) }}</td></tr>
  {% endfor %}
  </tbody>
</table>

<h2>Top 10 Merchants</h2>
<table>
  <thead><tr><th>Merchant</th><th>Count</th><th>Total Spend</th></tr></thead>
  <tbody>
  {% for row in data.top_merchants %}
  <tr><td>{{ row.merchant or "Unknown" }}</td><td>{{ row.count }}</td><td>${{ "%.2f"|format(row.total_spend) }}</td></tr>
  {% endfor %}
  </tbody>
</table>

<h2>Extraction Model Breakdown</h2>
<table>
  <thead><tr><th>Model</th><th>Receipt Count</th></tr></thead>
  <tbody>
  {% for row in data.by_model %}
  <tr><td>{{ row.model }}</td><td>{{ row.count }}</td></tr>
  {% endfor %}
  </tbody>
</table>

<h2>Recent Receipts (last 20)</h2>
<table>
  <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Total</th><th>Purchaser</th><th>Card</th><th>Filename</th><th>Model</th><th>Processed At</th></tr></thead>
  <tbody>
  {% for row in data.recent %}
  <tr>
    <td>{{ row.scan_date }}</td>
    <td>{{ row.merchant or "Unknown" }}</td>
    <td>{{ row.category }}</td>
    <td>{% if row.total is not none %}${{ "%.2f"|format(row.total) }}{% else %}—{% endif %}</td>
    <td>{{ row.purchaser or "—" }}</td>
    <td>{{ "···· " + row.card_last4 if row.card_last4 else "—" }}</td>
    <td>{{ row.filename }}</td>
    <td>{{ row.processed_by or "tesseract" }}</td>
    <td>{{ row.processed_at[:19] if row.processed_at else "—" }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</body>
</html>
"""


def generate_reports(
    conn: sqlite3.Connection, output_dir: Path, fmt: str = "both"
) -> list[Path]:
    """Write Markdown and/or HTML reports to *output_dir*.

    *fmt* is one of ``"md"``, ``"html"``, or ``"both"``.
    Returns the list of paths written.
    """
    from intake.db import query_summary

    output_dir.mkdir(parents=True, exist_ok=True)
    data = query_summary(conn)

    env = Environment(loader=BaseLoader(), autoescape=False)
    written: list[Path] = []

    if fmt in ("md", "both"):
        md_path = output_dir / "intake-report.md"
        md_path.write_text(
            env.from_string(_MD_TEMPLATE).render(data=data), encoding="utf-8"
        )
        written.append(md_path)

    if fmt in ("html", "both"):
        html_path = output_dir / "intake-report.html"
        html_path.write_text(
            env.from_string(_HTML_TEMPLATE).render(data=data), encoding="utf-8"
        )
        written.append(html_path)

    return written
