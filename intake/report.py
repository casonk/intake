"""Generate Markdown and HTML summary reports from the intake database."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from jinja2 import BaseLoader, Environment

from intake.certification import receipt_certifications_from_dicts, receipt_certifications_to_dicts
from intake.notify import format_money
from intake.parse import receipt_items_from_dicts, receipt_items_to_dicts
from intake.staff import receipt_staff_from_dicts, receipt_staff_to_dicts

_DEFAULT_FILEBROWSER_BASE_URL = "https://files.snowbridge.internal"
_DEFAULT_FILEBROWSER_RECEIPT_ROOT = "receipt"

_MD_TEMPLATE = """\
# Intake Receipt Report

[Summary](#summary) | [Visual Overview](#visual-overview) | [Merchant Directory]({{ data.merchant_directory_md_path }}) | [Merchant Trends](#merchant-spend-over-time) | [Merchant Months](#merchant-month-breakdown) | [Recent Receipts](#recent-receipts-last-50)

## Summary

| Field | Value |
|---|---|
| Total Receipts | {{ data.total_receipts }} |
| Total Spend | {{ data.total_spend_display }} |
| Date Range | {{ data.date_min or "—" }} → {{ data.date_max or "—" }} |
| Itemization Attempted | {{ data.itemization_attempted }} / {{ data.total_receipts }} |
| Itemized Receipts | {{ data.itemized_receipts }} / {{ data.total_receipts }} ({{ data.itemized_share_display }}) |
| Total Line Items | {{ data.total_line_items }} |
{% if data.receipt_root_markdown -%}
| Receipt Root | {{ data.receipt_root_markdown }} |
{% endif -%}
{% if data.intake_markdown -%}
| Intake Inbox | {{ data.intake_markdown }} |
{% endif %}

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
| {{ row.merchant_markdown }} | {{ row.count }} | {{ row.total_spend_display }} | {{ row.share_display }} | {{ row.chart_bar or "—" }} |
{% endfor %}

## Merchant Spend Over Time

| Merchant | Active Months | Total Spend | Monthly Breakdown |
|---|---|---|---|
{% for row in data.merchant_trends -%}
| {{ row.merchant_markdown }} | {{ row.active_months }} | {{ row.total_spend_display }} | {{ row.breakdown_markdown or "—" }} |
{% endfor %}

## Extraction Model Breakdown

| Model | Receipt Count | Share | Chart |
|---|---|---|---|
{% for row in data.by_model -%}
| {{ row.model_label }} | {{ row.count }} | {{ row.share_display }} | {{ row.chart_bar or "—" }} |
{% endfor %}

## Merchant Month Breakdown

| Merchant | Month | Receipt Count | Total Spend |
|---|---|---|---|
{% for row in data.merchant_monthly -%}
| {{ row.merchant_markdown }} | {{ row.month_label }} | {{ row.count }} | {{ row.total_spend_display }} |
{% endfor %}

## Recent Receipts (last 50)

| Date | Merchant | Category | Total | Purchaser | Card | Staff | Items | File | Folder | Model | Processed At |
|---|---|---|---|---|---|---|---|---|---|---|---|
{% for row in data.recent -%}
| {{ row.scan_date }} | {{ row.merchant_markdown }} | {{ row.category_markdown }} | {% if row.total is not none %}{{ row.total_display }}{% else %}—{% endif %} | {{ row.purchaser or "—" }} | {{ row.card_last4 or "—" }} | {{ row.staff_markdown }} | {{ row.items_markdown }} | {{ row.file_markdown }} | {{ row.folder_markdown }} | {{ row.processed_by or "tesseract" }} | {{ row.processed_at_display }} |
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
    width: 100%;
    max-width: none;
    margin: 0;
    padding: 32px clamp(12px, 1.5vw, 28px) 56px;
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
  .page-nav {
    position: sticky;
    top: 16px;
    z-index: 20;
    margin: 18px 0 22px;
    padding: 12px 14px;
    border-radius: 20px;
    background: rgba(255, 255, 255, 0.88);
    border: 1px solid rgba(24, 33, 47, 0.08);
    box-shadow: var(--shadow);
    backdrop-filter: blur(10px);
  }
  .page-nav-label {
    display: block;
    margin-bottom: 10px;
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .page-nav-links {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  .page-nav-links a {
    display: inline-flex;
    align-items: center;
    padding: 8px 12px;
    border-radius: 999px;
    background: rgba(220, 231, 255, 0.78);
    border: 1px solid rgba(55, 90, 158, 0.18);
    font-size: 0.92rem;
  }
  .hero-links {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 18px;
  }
  .hero-links a {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    border-radius: 999px;
    background: rgba(220, 231, 255, 0.78);
    border: 1px solid rgba(55, 90, 158, 0.18);
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
    max-width: min(100%, 15rem);
  }
  .bar-fill {
    height: 100%;
    border-radius: 999px;
  }
  .fill-category { background: linear-gradient(90deg, var(--teal), #35a69f); }
  .fill-merchant { background: linear-gradient(90deg, var(--amber), #e4a44c); }
  .fill-model { background: linear-gradient(90deg, var(--blue), #6f8fe3); }
  .trend-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 14px;
  }
  .trend-card {
    padding: 14px 14px 12px;
    border-radius: 18px;
    background: rgba(255, 255, 255, 0.72);
    border: 1px solid rgba(24, 33, 47, 0.05);
  }
  .trend-meta {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: baseline;
    margin-bottom: 10px;
  }
  .trend-meta strong {
    display: block;
    font-size: 0.98rem;
  }
  .trend-meta span {
    color: var(--muted);
    font-size: 0.82rem;
  }
  .trend-value {
    color: var(--ink);
    font-size: 0.95rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .sparkline {
    width: 100%;
    height: auto;
    display: block;
  }
  .sparkline .grid {
    stroke: rgba(24, 33, 47, 0.10);
    stroke-width: 1;
  }
  .sparkline .area {
    fill: rgba(199, 125, 43, 0.14);
  }
  .sparkline .trend {
    fill: none;
    stroke: var(--amber);
    stroke-width: 2.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .trend-foot {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    margin-top: 8px;
    color: var(--muted);
    font-size: 0.82rem;
  }
  .trend-breakdown {
    margin-top: 8px;
    color: var(--muted);
    font-size: 0.82rem;
    line-height: 1.45;
  }
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
  .detail-table .items-cell {
    width: clamp(12rem, 22vw, 20rem);
    max-width: 20rem;
  }
  .item-summary {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
    white-space: normal;
    overflow-wrap: anywhere;
    line-height: 1.35;
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
  section[id], article[id], h2[id], h3[id] {
    scroll-margin-top: 92px;
  }
  a:hover { text-decoration: underline; }
  /* ── column resize ── */
  th { position: relative; }
  .col-resizer { position:absolute;right:0;top:0;bottom:0;width:4px;cursor:col-resize;user-select:none; }
  .col-resizer:hover,.col-resizer.rsz-active { background:rgba(24,33,47,.18); }
  table.resizing { user-select:none;cursor:col-resize; }
  /* ── cell expand ── */
  td.expanded { white-space:normal!important;max-width:none!important;overflow:visible!important; }
  td.expanded .item-summary { -webkit-line-clamp:unset!important;display:block!important; }
</style>
</head>
<body>
<main class="page">
  <section class="hero" id="summary">
    <p class="eyebrow">Intake Dashboard</p>
    <h1>Receipt activity at a glance.</h1>
    <p>Daily totals, dominant categories, merchant concentration, and model usage from the current intake database.</p>
    {% if data.receipt_root_uri or data.intake_uri or data.merchant_directory_html_path %}
    <div class="hero-links">
      {% if data.receipt_root_uri %}<a href="{{ data.receipt_root_uri }}">Open receipt root</a>{% endif %}
      {% if data.intake_uri %}<a href="{{ data.intake_uri }}">Open intake inbox</a>{% endif %}
      {% if data.merchant_directory_html_path %}<a href="{{ data.merchant_directory_html_path }}">Merchant directory</a>{% endif %}
    </div>
    {% endif %}
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
      <article class="summary-card">
        <span class="label">Itemized Receipts</span>
        <span class="value">{{ data.itemized_coverage_display }}</span>
        <span class="subtext">{{ data.itemized_share_display }} with at least one line item</span>
      </article>
      <article class="summary-card">
        <span class="label">Total Line Items</span>
        <span class="value">{{ data.total_line_items }}</span>
        <span class="subtext">{{ data.itemization_attempted }} receipt(s) attempted</span>
      </article>
    </div>
  </section>

  <nav class="page-nav" aria-label="Section menu">
    <span class="page-nav-label">Jump To</span>
    <div class="page-nav-links">
      <a href="#summary">Summary</a>
      <a href="#visual-overview">Visual Overview</a>
      <a href="{{ data.merchant_directory_html_path }}">Merchant Directory</a>
      <a href="#merchant-spend-over-time">Merchant Trends</a>
      <a href="#merchant-month-breakdown">Merchant Months</a>
      <a href="#recent-receipts">Recent Receipts</a>
      <a href="audit.html">Audit</a>
    </div>
  </nav>

  <h2 id="visual-overview">Visual Overview</h2>
  <section class="chart-grid">
    <article class="chart-card wide" id="monthly-spend-trend">
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
            <span>{% if row.href %}<a href="{{ row.href }}">{{ row.label }}</a>{% else %}{{ row.label }}{% endif %}</span>
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

    <article class="chart-card wide" id="merchant-spend-over-time">
      <div class="chart-head">
        <div>
          <h3>Merchant Spend Over Time</h3>
          <p>Monthly spend history for the top merchants.</p>
        </div>
        <p>{{ data.merchant_trends|length }} shown</p>
      </div>
      {% if data.merchant_trends %}
      <div class="trend-grid">
        {% for row in data.merchant_trends %}
        <div class="trend-card">
          <div class="trend-meta">
            <div>
              <strong>{% if row.merchant_href_html %}<a href="{{ row.merchant_href_html }}">{{ row.merchant_label }}</a>{% else %}{{ row.merchant_label }}{% endif %}</strong>
              <span>{{ row.active_months }} active month(s)</span>
            </div>
            <div class="trend-value">{{ row.total_spend_display }}</div>
          </div>
          {% if row.sparkline.has_data %}
          <svg class="sparkline" viewBox="0 0 {{ row.sparkline.width }} {{ row.sparkline.height }}" role="img" aria-label="{{ row.merchant_label }} monthly spend trend">
            {% for tick in row.sparkline.y_ticks %}
            <line class="grid" x1="{{ row.sparkline.plot_left }}" y1="{{ tick.y }}" x2="{{ row.sparkline.plot_right }}" y2="{{ tick.y }}"></line>
            {% endfor %}
            <polygon class="area" points="{{ row.sparkline.area_points }}"></polygon>
            <polyline class="trend" points="{{ row.sparkline.line_points }}"></polyline>
          </svg>
          {% else %}
          <div class="empty-state">No merchant history yet.</div>
          {% endif %}
          <div class="trend-foot">
            <span>{{ row.range_start_label }}</span>
            <span>{{ row.range_end_label }}</span>
          </div>
          {% if row.breakdown_html %}
          <div class="trend-breakdown">{{ row.breakdown_html|safe }}</div>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty-state">No merchant history yet.</div>
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

  <h2 id="detail-tables">Detail Tables</h2>

  <h3 id="spending-by-category">Spending by Category</h3>
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

  <h3 id="spending-by-month">Spending by Month</h3>
  <table class="detail-table">
    <thead><tr><th>Month</th><th>Count</th><th>Total Spend</th></tr></thead>
    <tbody>
    {% for row in data.by_month %}
    <tr><td>{{ row.month_label }}</td><td>{{ row.count }}</td><td>{{ row.total_spend_display }}</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h3 id="top-merchants">Top 10 Merchants</h3>
  <table class="detail-table">
    <thead><tr><th>Merchant</th><th>Count</th><th>Total Spend</th><th>% of Total</th></tr></thead>
    <tbody>
    {% for row in data.top_merchants %}
    <tr><td>{% if row.merchant_href_html %}<a href="{{ row.merchant_href_html }}">{{ row.merchant_label }}</a>{% else %}{{ row.merchant_label }}{% endif %}</td><td>{{ row.count }}</td><td>{{ row.total_spend_display }}</td><td>{{ row.share_display }}</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h3 id="merchant-month-breakdown">Merchant Month Breakdown</h3>
  <table class="detail-table">
    <thead><tr><th>Merchant</th><th>Month</th><th>Receipt Count</th><th>Total Spend</th></tr></thead>
    <tbody>
    {% for row in data.merchant_monthly %}
    <tr><td>{% if row.merchant_href_html %}<a href="{{ row.merchant_href_html }}">{{ row.merchant_label }}</a>{% else %}{{ row.merchant_label }}{% endif %}</td><td>{{ row.month_label }}</td><td>{{ row.count }}</td><td>{{ row.total_spend_display }}</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h3 id="extraction-model-breakdown">Extraction Model Breakdown</h3>
  <table class="detail-table">
    <thead><tr><th>Model</th><th>Receipt Count</th><th>Share</th></tr></thead>
    <tbody>
    {% for row in data.by_model %}
    <tr><td>{{ row.model_label }}</td><td>{{ row.count }}</td><td>{{ row.share_display }}</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h3 id="recent-receipts">Recent Receipts (last 50)</h3>
  <table class="detail-table">
    <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Total</th><th>Purchaser</th><th>Card</th><th>Staff</th><th>Items</th><th>File</th><th>Folder</th><th>Model</th><th>Processed At</th></tr></thead>
    <tbody>
    {% for row in data.recent %}
    <tr>
      <td>{{ row.scan_date }}</td>
      <td>{% if row.merchant_href_html %}<a href="{{ row.merchant_href_html }}">{{ row.merchant_label }}</a>{% else %}{{ row.merchant_label }}{% endif %}</td>
      <td>{% if row.category_uri %}<a href="{{ row.category_uri }}">{{ row.category }}</a>{% else %}{{ row.category }}{% endif %}</td>
      <td>{% if row.total is not none %}{{ row.total_display }}{% else %}—{% endif %}</td>
      <td>{{ row.purchaser or "—" }}</td>
      <td>{{ "···· " + row.card_last4 if row.card_last4 else "—" }}</td>
      <td>{{ row.staff_label }}</td>
      <td class="items-cell"><span class="item-summary" title="{{ row.items_full_label }}">{{ row.items_label }}</span></td>
      <td>{% if row.file_link_uri %}<a href="{{ row.file_link_uri }}">{{ row.filename }}</a>{% else %}{{ row.filename }}{% endif %}</td>
      <td>{% if row.folder_uri %}<a href="{{ row.folder_uri }}">{{ row.folder_name }}</a>{% else %}—{% endif %}</td>
      <td>{{ row.processed_by or "tesseract" }}</td>
      <td>{{ row.processed_at_display }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</main>
<script>
(function () {
  function initColResize(tbl) {
    tbl.querySelectorAll('th').forEach(function (th) {
      var r = document.createElement('div');
      r.className = 'col-resizer';
      th.appendChild(r);
      r.addEventListener('mousedown', function (e) {
        var x0 = e.clientX, w0 = th.offsetWidth;
        tbl.classList.add('resizing');
        r.classList.add('rsz-active');
        var mv = function (ev) { th.style.width = th.style.minWidth = Math.max(40, w0 + ev.clientX - x0) + 'px'; };
        var up = function () { tbl.classList.remove('resizing'); r.classList.remove('rsz-active'); document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); };
        document.addEventListener('mousemove', mv);
        document.addEventListener('mouseup', up);
        e.preventDefault();
      });
    });
  }
  function initCellExpand(tbl) {
    tbl.addEventListener('click', function (e) {
      var td = e.target.closest('td');
      if (td) { td.classList.toggle('expanded'); }
    });
  }
  document.querySelectorAll('.detail-table').forEach(function (t) { initColResize(t); initCellExpand(t); });
}());
</script>
</body>
</html>
"""

_MERCHANT_DIRECTORY_MD_TEMPLATE = """\
# Merchant Directory

[Main Report]({{ data.main_report_md_href }})

{% if data.merchants -%}
| Merchant | Receipts | Total Spend | Active Months | Date Range |
|---|---|---|---|---|
{% for row in data.merchants -%}
| {{ row.merchant_markdown }} | {{ row.count }} | {{ row.total_spend_display }} | {{ row.active_months }} | {{ row.date_min or "—" }} → {{ row.date_max or "—" }} |
{% endfor %}
{% else -%}
No merchant data yet.
{% endif %}
"""

_MERCHANT_DIRECTORY_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Merchant Directory</title>
<style>
  :root {
    --ink: #18212f;
    --muted: #667085;
    --panel: rgba(255, 255, 255, 0.92);
    --line: rgba(24, 33, 47, 0.12);
    --shadow: 0 18px 40px rgba(24, 33, 47, 0.09);
    --blue: #375a9e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 32%),
      linear-gradient(180deg, #f8f5ef 0%, #f2eee6 100%);
  }
  .page {
    width: 100%;
    max-width: none;
    margin: 0;
    padding: 32px clamp(12px, 1.5vw, 28px) 56px;
  }
  .hero, .panel {
    background: var(--panel);
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 24px;
    box-shadow: var(--shadow);
    padding: 24px;
  }
  .hero + .panel { margin-top: 18px; }
  .eyebrow {
    font-size: 12px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 8px;
  }
  h1, h2 {
    margin: 0;
    letter-spacing: -0.03em;
  }
  .hero p, .panel p {
    color: var(--muted);
    line-height: 1.55;
  }
  .hero-links, .summary-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 18px;
  }
  .hero-links a {
    display: inline-flex;
    padding: 10px 14px;
    border-radius: 999px;
    background: rgba(220, 231, 255, 0.78);
    border: 1px solid rgba(55, 90, 158, 0.18);
    text-decoration: none;
  }
  .summary-grid { margin-top: 20px; }
  .summary-card {
    flex: 1 1 180px;
    min-width: 180px;
    padding: 16px;
    border-radius: 18px;
    background: rgba(248, 247, 243, 0.88);
    border: 1px solid rgba(24, 33, 47, 0.06);
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
    font-size: 1.8rem;
    font-weight: 700;
    letter-spacing: -0.04em;
  }
  .summary-card .subtext {
    display: block;
    margin-top: 6px;
    color: var(--muted);
    font-size: 0.92rem;
  }
  label {
    display: block;
    margin-bottom: 8px;
    font-size: 12px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
  }
  input {
    width: 100%;
    padding: 12px 14px;
    border-radius: 14px;
    border: 1px solid rgba(24, 33, 47, 0.14);
    font: inherit;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 16px;
    background: rgba(255, 255, 255, 0.86);
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 18px;
    overflow: hidden;
  }
  th, td {
    padding: 12px;
    text-align: left;
    border-bottom: 1px solid rgba(24, 33, 47, 0.08);
    vertical-align: top;
  }
  .items-cell {
    width: clamp(12rem, 22vw, 20rem);
    max-width: 20rem;
  }
  .item-summary {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
    white-space: normal;
    overflow-wrap: anywhere;
    line-height: 1.35;
  }
  th {
    font-size: 12px;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    color: var(--muted);
    background: rgba(243, 239, 230, 0.95);
  }
  tbody tr:nth-child(even) td {
    background: rgba(248, 247, 243, 0.7);
  }
  tbody tr:last-child td { border-bottom: none; }
  a {
    color: var(--blue);
    text-decoration: none;
    font-weight: 600;
  }
  a:hover { text-decoration: underline; }
  .empty-state {
    padding: 18px;
    border-radius: 16px;
    background: rgba(248, 247, 243, 0.85);
    border: 1px dashed rgba(24, 33, 47, 0.16);
    color: var(--muted);
  }
  /* ── column resize ── */
  th { position: relative; }
  .col-resizer { position:absolute;right:0;top:0;bottom:0;width:4px;cursor:col-resize;user-select:none; }
  .col-resizer:hover,.col-resizer.rsz-active { background:rgba(24,33,47,.18); }
  table.resizing { user-select:none;cursor:col-resize; }
  /* ── cell expand ── */
  td.expanded { white-space:normal!important;max-width:none!important;overflow:visible!important; }
  td.expanded .item-summary { -webkit-line-clamp:unset!important;display:block!important; }
  /* ── auto-size filter input ── */
  #merchant-filter { field-sizing:content;min-width:180px;width:auto; }
</style>
</head>
<body>
<main class="page">
  <section class="hero">
    <p class="eyebrow">Merchant Directory</p>
    <h1>Browse every tracked merchant.</h1>
    <p>Use this page to step beyond the default top-merchant view and jump into a dedicated history page for any vendor in the receipt corpus.</p>
    <div class="hero-links">
      <a href="{{ data.main_report_html_href }}">Back to main report</a>
    </div>
    <div class="summary-grid">
      <article class="summary-card">
        <span class="label">Merchants</span>
        <span class="value">{{ data.merchant_count }}</span>
        <span class="subtext">Tracked merchant buckets</span>
      </article>
      <article class="summary-card">
        <span class="label">Receipts</span>
        <span class="value">{{ data.total_receipts }}</span>
        <span class="subtext">Across the full database</span>
      </article>
      <article class="summary-card">
        <span class="label">Total Spend</span>
        <span class="value">{{ data.total_spend_display }}</span>
        <span class="subtext">All merchant buckets combined</span>
      </article>
    </div>
  </section>

  <section class="panel">
    <h2>Directory</h2>
    <p>Filter by merchant name, then open a dedicated page with monthly history and receipt-level detail.</p>
    {% if data.merchants %}
    <label for="merchant-filter">Filter merchants</label>
    <input id="merchant-filter" type="search" placeholder="Search merchants">
    <table>
      <thead>
        <tr><th>Merchant</th><th>Receipts</th><th>Total Spend</th><th>Active Months</th><th>Date Range</th></tr>
      </thead>
      <tbody>
        {% for row in data.merchants %}
        <tr data-merchant-row data-search="{{ row.filter_text }}">
          <td><a href="{{ row.detail_html_href }}">{{ row.merchant_label }}</a></td>
          <td>{{ row.count }}</td>
          <td>{{ row.total_spend_display }}</td>
          <td>{{ row.active_months }}</td>
          <td>{{ row.date_min or "—" }} → {{ row.date_max or "—" }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty-state">No merchant data yet.</div>
    {% endif %}
  </section>
</main>
<script>
(function () {
  function initColResize(tbl) {
    tbl.querySelectorAll('th').forEach(function (th) {
      var r = document.createElement('div');
      r.className = 'col-resizer';
      th.appendChild(r);
      r.addEventListener('mousedown', function (e) {
        var x0 = e.clientX, w0 = th.offsetWidth;
        tbl.classList.add('resizing');
        r.classList.add('rsz-active');
        var mv = function (ev) { th.style.width = th.style.minWidth = Math.max(40, w0 + ev.clientX - x0) + 'px'; };
        var up = function () { tbl.classList.remove('resizing'); r.classList.remove('rsz-active'); document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); };
        document.addEventListener('mousemove', mv);
        document.addEventListener('mouseup', up);
        e.preventDefault();
      });
    });
  }
  function initCellExpand(tbl) {
    tbl.addEventListener('click', function (e) {
      var td = e.target.closest('td');
      if (td) { td.classList.toggle('expanded'); }
    });
  }
  document.querySelectorAll('table').forEach(function (t) { initColResize(t); initCellExpand(t); });

  var merchantFilter = document.getElementById("merchant-filter");
  if (merchantFilter) {
    var rows = Array.from(document.querySelectorAll("[data-merchant-row]"));
    merchantFilter.addEventListener("input", function () {
      var query = merchantFilter.value.trim().toLowerCase();
      for (var i = 0; i < rows.length; i++) {
        rows[i].hidden = query !== "" && !rows[i].dataset.search.includes(query);
      }
    });
  }
}());
</script>
</body>
</html>
"""

_MERCHANT_DETAIL_MD_TEMPLATE = """\
# Merchant: {{ data.merchant_label }}

[Main Report]({{ data.main_report_md_href }}) | [Merchant Directory]({{ data.directory_md_href }})

| Field | Value |
|---|---|
| Total Receipts | {{ data.count }} |
| Total Spend | {{ data.total_spend_display }} |
| Active Months | {{ data.active_months }} |
| Date Range | {{ data.date_min or "—" }} → {{ data.date_max or "—" }} |
| Itemization Attempted | {{ data.itemization_attempted }} / {{ data.count }} |
| Itemized Receipts | {{ data.itemized_receipts }} / {{ data.count }} |
| Total Line Items | {{ data.total_line_items }} |

## Monthly Spend

{% if data.by_month -%}
| Month | Count | Total Spend |
|---|---|---|
{% for row in data.by_month -%}
| {{ row.month_label }} | {{ row.count }} | {{ row.total_spend_display }} |
{% endfor %}
{% else -%}
No monthly history yet.
{% endif %}

## Category Mix

{% if data.by_category -%}
| Category | Count | Total Spend | % of Merchant |
|---|---|---|---|
{% for row in data.by_category -%}
| {{ row.category_markdown }} | {{ row.count }} | {{ row.total_spend_display }} | {{ row.share_display }} |
{% endfor %}
{% else -%}
No category data yet.
{% endif %}

## Certifications

{% if data.certifications -%}
| Certification | Issuer | Receipt Count | Evidence |
|---|---|---|---|
{% for row in data.certifications -%}
| {{ row.label }} | {{ row.issuer }} | {{ row.receipt_count }} | {{ row.evidence_markdown }} |
{% endfor %}
{% else -%}
No certifications recorded yet.
{% endif %}

## Receipts

{% if data.receipts -%}
| Date | Category | Total | Purchaser | Card | Staff | Items | Certifications | File | Folder | Model | Processed At |
|---|---|---|---|---|---|---|---|---|---|---|---|
{% for row in data.receipts -%}
| {{ row.scan_date }} | {{ row.category_markdown }} | {{ row.total_display }} | {{ row.purchaser or "—" }} | {{ row.card_last4 or "—" }} | {{ row.staff_markdown }} | {{ row.items_markdown }} | {{ row.certifications_markdown }} | {{ row.file_markdown }} | {{ row.folder_markdown }} | {{ row.processed_by or "tesseract" }} | {{ row.processed_at_display }} |
{% endfor %}
{% else -%}
No receipt rows yet.
{% endif %}
"""

_MERCHANT_DETAIL_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ data.merchant_label }} · Intake Merchant History</title>
<style>
  :root {
    --ink: #18212f;
    --muted: #667085;
    --panel: rgba(255, 255, 255, 0.92);
    --line: rgba(24, 33, 47, 0.12);
    --shadow: 0 18px 40px rgba(24, 33, 47, 0.09);
    --blue: #375a9e;
    --amber: #c77d2b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(circle at top left, rgba(199, 125, 43, 0.12), transparent 30%),
      linear-gradient(180deg, #f8f5ef 0%, #f2eee6 100%);
  }
  .page {
    width: 100%;
    max-width: none;
    margin: 0;
    padding: 32px clamp(12px, 1.5vw, 28px) 56px;
  }
  .hero, .panel {
    background: var(--panel);
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 24px;
    box-shadow: var(--shadow);
    padding: 24px;
  }
  .panel { margin-top: 18px; }
  .eyebrow {
    font-size: 12px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 8px;
  }
  h1, h2 {
    margin: 0;
    letter-spacing: -0.03em;
  }
  .hero p, .panel p {
    color: var(--muted);
    line-height: 1.55;
  }
  .hero-links, .page-nav, .summary-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 18px;
  }
  .hero-links a, .page-nav a {
    display: inline-flex;
    padding: 10px 14px;
    border-radius: 999px;
    background: rgba(220, 231, 255, 0.78);
    border: 1px solid rgba(55, 90, 158, 0.18);
    text-decoration: none;
  }
  .summary-grid { margin-top: 20px; }
  .summary-card {
    flex: 1 1 180px;
    min-width: 180px;
    padding: 16px;
    border-radius: 18px;
    background: rgba(248, 247, 243, 0.88);
    border: 1px solid rgba(24, 33, 47, 0.06);
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
    font-size: 1.8rem;
    font-weight: 700;
    letter-spacing: -0.04em;
  }
  .summary-card .subtext {
    display: block;
    margin-top: 6px;
    color: var(--muted);
    font-size: 0.92rem;
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
  .line-chart .area { fill: rgba(199, 125, 43, 0.14); }
  .line-chart .trend {
    fill: none;
    stroke: var(--amber);
    stroke-width: 3;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .line-chart .point {
    fill: white;
    stroke: var(--amber);
    stroke-width: 2.5;
  }
  .line-chart .point-value {
    fill: var(--ink);
    font-size: 11px;
    font-weight: 600;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 16px;
    background: rgba(255, 255, 255, 0.86);
    border: 1px solid rgba(24, 33, 47, 0.08);
    border-radius: 18px;
    overflow: hidden;
  }
  th, td {
    padding: 12px;
    text-align: left;
    border-bottom: 1px solid rgba(24, 33, 47, 0.08);
    vertical-align: top;
  }
  th {
    font-size: 12px;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    color: var(--muted);
    background: rgba(243, 239, 230, 0.95);
  }
  tbody tr:nth-child(even) td {
    background: rgba(248, 247, 243, 0.7);
  }
  tbody tr:last-child td { border-bottom: none; }
  a {
    color: var(--blue);
    text-decoration: none;
    font-weight: 600;
  }
  a:hover { text-decoration: underline; }
  .empty-state {
    padding: 18px;
    border-radius: 16px;
    background: rgba(248, 247, 243, 0.85);
    border: 1px dashed rgba(24, 33, 47, 0.16);
    color: var(--muted);
  }
  /* ── column resize ── */
  th { position: relative; }
  .col-resizer { position:absolute;right:0;top:0;bottom:0;width:4px;cursor:col-resize;user-select:none; }
  .col-resizer:hover,.col-resizer.rsz-active { background:rgba(24,33,47,.18); }
  table.resizing { user-select:none;cursor:col-resize; }
  /* ── cell expand ── */
  td.expanded { white-space:normal!important;max-width:none!important;overflow:visible!important; }
  td.expanded .item-summary { -webkit-line-clamp:unset!important;display:block!important; }
</style>
</head>
<body>
<main class="page">
  <section class="hero" id="summary">
    <p class="eyebrow">Merchant History</p>
    <h1>{{ data.merchant_label }}</h1>
    <p>Receipt-level drill-down for a single merchant bucket, including monthly totals, category mix, and the underlying files.</p>
    <div class="hero-links">
      <a href="{{ data.main_report_html_href }}">Back to main report</a>
      <a href="{{ data.directory_html_href }}">Merchant directory</a>
    </div>
    <div class="summary-grid">
      <article class="summary-card">
        <span class="label">Receipts</span>
        <span class="value">{{ data.count }}</span>
        <span class="subtext">{{ data.date_min or "—" }} to {{ data.date_max or "—" }}</span>
      </article>
      <article class="summary-card">
        <span class="label">Total Spend</span>
        <span class="value">{{ data.total_spend_display }}</span>
        <span class="subtext">{{ data.active_months }} active month(s)</span>
      </article>
      <article class="summary-card">
        <span class="label">Itemized Receipts</span>
        <span class="value">{{ data.itemized_receipts }}/{{ data.count }}</span>
        <span class="subtext">{{ data.total_line_items }} stored line item(s)</span>
      </article>
    </div>
  </section>

  <nav class="page-nav" aria-label="Merchant page menu">
    <a href="#monthly-spend">Monthly Spend</a>
    <a href="#category-mix">Category Mix</a>
    <a href="#merchant-certifications">Certifications</a>
    <a href="#merchant-receipts">Receipts</a>
  </nav>

  <section class="panel" id="monthly-spend">
    <h2>Monthly Spend</h2>
    <p>Exact spend history for this merchant across the full receipt corpus.</p>
    {% if data.charts.monthly.has_data %}
    <svg class="line-chart" viewBox="0 0 {{ data.charts.monthly.width }} {{ data.charts.monthly.height }}" role="img" aria-label="{{ data.merchant_label }} monthly spend trend">
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
    <table>
      <thead><tr><th>Month</th><th>Count</th><th>Total Spend</th></tr></thead>
      <tbody>
        {% for row in data.by_month %}
        <tr><td>{{ row.month_label }}</td><td>{{ row.count }}</td><td>{{ row.total_spend_display }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty-state">No monthly history yet.</div>
    {% endif %}
  </section>

  <section class="panel" id="category-mix">
    <h2>Category Mix</h2>
    <p>Where this merchant currently lands in the intake taxonomy.</p>
    {% if data.by_category %}
    <table>
      <thead><tr><th>Category</th><th>Count</th><th>Total Spend</th><th>% of Merchant</th></tr></thead>
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
    {% else %}
    <div class="empty-state">No category data yet.</div>
    {% endif %}
  </section>

  <section class="panel" id="merchant-certifications">
    <h2>Certifications</h2>
    <p>Structured merchant certifications and claims captured from receipt evidence.</p>
    {% if data.certifications %}
    <table>
      <thead><tr><th>Certification</th><th>Issuer</th><th>Receipt Count</th><th>Evidence</th></tr></thead>
      <tbody>
        {% for row in data.certifications %}
        <tr>
          <td>{{ row.label }}</td>
          <td>{{ row.issuer }}</td>
          <td>{{ row.receipt_count }}</td>
          <td>{{ row.evidence_html|safe }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty-state">No certifications recorded yet.</div>
    {% endif %}
  </section>

  <section class="panel" id="merchant-receipts">
    <h2>Receipts</h2>
    <p>Every tracked receipt for this merchant bucket.</p>
    {% if data.receipts %}
    <table>
      <thead><tr><th>Date</th><th>Category</th><th>Total</th><th>Purchaser</th><th>Card</th><th>Staff</th><th>Items</th><th>Certifications</th><th>File</th><th>Folder</th><th>Model</th><th>Processed At</th></tr></thead>
      <tbody>
        {% for row in data.receipts %}
        <tr>
          <td>{{ row.scan_date }}</td>
          <td>{% if row.category_uri %}<a href="{{ row.category_uri }}">{{ row.category }}</a>{% else %}{{ row.category }}{% endif %}</td>
          <td>{{ row.total_display }}</td>
          <td>{{ row.purchaser or "—" }}</td>
          <td>{{ "···· " + row.card_last4 if row.card_last4 else "—" }}</td>
          <td>{{ row.staff_label }}</td>
          <td class="items-cell"><span class="item-summary" title="{{ row.items_full_label }}">{{ row.items_label }}</span></td>
          <td>{{ row.certifications_label }}</td>
          <td>{% if row.file_link_uri %}<a href="{{ row.file_link_uri }}">{{ row.filename }}</a>{% else %}{{ row.filename }}{% endif %}</td>
          <td>{% if row.folder_uri %}<a href="{{ row.folder_uri }}">{{ row.folder_name }}</a>{% else %}—{% endif %}</td>
          <td>{{ row.processed_by or "tesseract" }}</td>
          <td>{{ row.processed_at_display }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty-state">No receipt rows yet.</div>
    {% endif %}
  </section>
</main>
<script>
(function () {
  function initColResize(tbl) {
    tbl.querySelectorAll('th').forEach(function (th) {
      var r = document.createElement('div');
      r.className = 'col-resizer';
      th.appendChild(r);
      r.addEventListener('mousedown', function (e) {
        var x0 = e.clientX, w0 = th.offsetWidth;
        tbl.classList.add('resizing');
        r.classList.add('rsz-active');
        var mv = function (ev) { th.style.width = th.style.minWidth = Math.max(40, w0 + ev.clientX - x0) + 'px'; };
        var up = function () { tbl.classList.remove('resizing'); r.classList.remove('rsz-active'); document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); };
        document.addEventListener('mousemove', mv);
        document.addEventListener('mouseup', up);
        e.preventDefault();
      });
    });
  }
  function initCellExpand(tbl) {
    tbl.addEventListener('click', function (e) {
      var td = e.target.closest('td');
      if (td) { td.classList.toggle('expanded'); }
    });
  }
  document.querySelectorAll('table').forEach(function (t) { initColResize(t); initCellExpand(t); });
}());
</script>
</body>
</html>
"""

_AUDIT_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intake Audit</title>
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
  .page { width: 100%; max-width: none; margin: 0; padding: 32px clamp(12px, 1.5vw, 28px) 56px; }
  .hero {
    background: linear-gradient(135deg, rgba(255,255,255,0.96), rgba(248,248,244,0.92));
    border: 1px solid rgba(24,33,47,0.08);
    border-radius: 26px;
    box-shadow: var(--shadow);
    padding: 28px;
  }
  .eyebrow { font-size:12px; letter-spacing:.18em; text-transform:uppercase; color:var(--teal); margin:0 0 8px; }
  h1 { margin:0; font-size:clamp(1.8rem,4vw,3rem); letter-spacing:-0.04em; }
  .hero p { margin:10px 0 0; color:var(--muted); line-height:1.55; }
  .hero-links { display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }
  .hero-links a {
    display:inline-flex; align-items:center; gap:8px; padding:10px 14px;
    border-radius:999px; background:rgba(220,231,255,0.78);
    border:1px solid rgba(55,90,158,0.18); color:var(--blue);
    text-decoration:none; font-size:.92rem; font-weight:600;
  }
  .hero-links a:hover { text-decoration:underline; }
  .filter-bar {
    margin: 18px 0 14px;
    padding: 18px 18px 14px;
    border-radius: 20px;
    background: rgba(255,255,255,0.88);
    border: 1px solid rgba(24,33,47,0.08);
    box-shadow: var(--shadow);
  }
  .filter-row { display:flex; flex-wrap:wrap; gap:12px; align-items:flex-end; }
  .filter-field { display:flex; flex-direction:column; gap:4px; }
  .filter-field--grow { flex:0 0 auto; }
  .filter-field label { font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); }
  .filter-field input,
  .filter-field select {
    padding:8px 10px; border-radius:10px;
    border:1px solid rgba(24,33,47,0.16);
    background:rgba(243,239,230,0.7);
    font-size:.92rem; color:var(--ink);
    font-family:inherit;
  }
  .filter-field input:focus,
  .filter-field select:focus { outline:2px solid var(--teal); outline-offset:1px; }
  .filter-field--btn { justify-content:flex-end; }
  .filter-field--btn button {
    padding:8px 16px; border-radius:10px; border:none;
    background:rgba(24,33,47,0.1); color:var(--ink);
    font-size:.92rem; font-family:inherit; cursor:pointer;
  }
  .filter-field--btn button:hover { background:rgba(24,33,47,0.18); }
  .stat-bar {
    margin-top:12px; font-size:.92rem; color:var(--muted);
  }
  .stat-bar strong { color:var(--ink); }
  .table-wrap {
    overflow-x:auto;
    border-radius:18px;
    box-shadow:var(--shadow);
  }
  .detail-table {
    width:100%; border-collapse:collapse;
    background:rgba(255,255,255,0.86);
    border:1px solid rgba(24,33,47,0.08);
    border-radius:18px; overflow:hidden;
  }
  .detail-table th, .detail-table td {
    padding:10px 12px; text-align:left;
    border-bottom:1px solid rgba(24,33,47,0.08); vertical-align:top;
    white-space:nowrap;
  }
  .detail-table .items-cell {
    width: clamp(12rem, 22vw, 20rem);
    max-width: 20rem;
    white-space: normal;
  }
  .item-summary {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
    white-space: normal;
    overflow-wrap: anywhere;
    line-height: 1.35;
  }
  .detail-table th {
    font-size:11px; letter-spacing:.10em; text-transform:uppercase;
    color:var(--muted); background:rgba(243,239,230,0.95);
    position:sticky; top:0; z-index:2;
  }
  .detail-table th.sortable { cursor:pointer; user-select:none; }
  .detail-table th.sortable:hover { color:var(--ink); }
  .detail-table th .sort-arrow { margin-left:4px; opacity:.4; }
  .detail-table th.sort-asc .sort-arrow::after { content:"▲"; opacity:1; }
  .detail-table th.sort-desc .sort-arrow::after { content:"▼"; opacity:1; }
  .detail-table tbody tr:nth-child(even) td { background:rgba(248,247,243,0.7); }
  .detail-table tbody tr:last-child td { border-bottom:none; }
  .detail-table tbody tr:hover td { background:rgba(220,231,255,0.3); }
  .detail-table td.merchant { max-width:200px; overflow:hidden; text-overflow:ellipsis; }
  a { color:var(--blue); text-decoration:none; font-weight:600; }
  a:hover { text-decoration:underline; }
  .badge {
    display:inline-block; padding:2px 8px; border-radius:999px;
    font-size:.8rem; font-weight:600;
  }
  .badge--ok { background:var(--teal-soft); color:var(--teal); }
  .empty-state {
    padding:32px; text-align:center; color:var(--muted);
    background:rgba(255,255,255,0.72); border-radius:18px;
    border:1px dashed rgba(24,33,47,0.16);
  }
  section[id], h2[id] { scroll-margin-top:80px; }
  /* ── column resize ── */
  .detail-table th { position: relative; }
  .col-resizer { position:absolute;right:0;top:0;bottom:0;width:4px;cursor:col-resize;user-select:none; }
  .col-resizer:hover,.col-resizer.rsz-active { background:rgba(24,33,47,.18); }
  .detail-table.resizing { user-select:none;cursor:col-resize; }
  /* ── cell expand ── */
  .detail-table td.expanded { white-space:normal!important;max-width:none!important;overflow:visible!important; }
  .detail-table td.expanded .item-summary { -webkit-line-clamp:unset!important;display:block!important; }
</style>
</head>
<body>
<main class="page">

  <section class="hero">
    <p class="eyebrow">Intake Dashboard</p>
    <h1>Receipt Audit</h1>
    <p>Filter by date range, category, merchant, or purchaser. {{ total_count }} receipts total.</p>
    <div class="hero-links">
      <a href="intake-report.html">← Dashboard</a>
    </div>
  </section>

  <div class="filter-bar">
    <div class="filter-row">
      <div class="filter-field">
        <label for="f-from">From</label>
        <input type="date" id="f-from">
      </div>
      <div class="filter-field">
        <label for="f-to">To</label>
        <input type="date" id="f-to">
      </div>
      <div class="filter-field">
        <label for="f-cat">Category</label>
        <select id="f-cat">
          <option value="">All categories</option>
          {% for cat in categories %}<option value="{{ cat }}">{{ cat }}</option>{% endfor %}
        </select>
      </div>
      <div class="filter-field filter-field--grow">
        <label for="f-merchant">Merchant</label>
        <input type="text" id="f-merchant" placeholder="Search merchants…" autocomplete="off">
      </div>
      <div class="filter-field filter-field--grow">
        <label for="f-purchaser">Purchaser</label>
        <input type="text" id="f-purchaser" placeholder="Search purchasers…" autocomplete="off">
      </div>
      <div class="filter-field filter-field--btn">
        <label>&nbsp;</label>
        <button id="f-clear">Clear filters</button>
      </div>
    </div>
    <div class="stat-bar">
      <strong id="stat-count">{{ total_count }}</strong> receipt(s) &nbsp;·&nbsp;
      total spend: <strong id="stat-total">—</strong>
    </div>
  </div>

  <div id="audit-empty" class="empty-state" style="display:none">No receipts match the current filters.</div>
  <div class="table-wrap" id="audit-wrap">
    <table class="detail-table" id="audit-table">
      <thead>
        <tr>
          <th class="sortable" data-col="scan_date">Date<span class="sort-arrow"></span></th>
          <th>Merchant</th>
          <th class="sortable" data-col="category">Category<span class="sort-arrow"></span></th>
          <th class="sortable" data-col="total">Total<span class="sort-arrow"></span></th>
          <th>Purchaser</th>
          <th>Card</th>
          <th class="sortable" data-col="processed_by">Model<span class="sort-arrow"></span></th>
          <th>Items</th>
          <th>File</th>
          <th>Reviewed</th>
        </tr>
      </thead>
      <tbody id="audit-tbody"></tbody>
    </table>
  </div>

</main>
<script>
(function () {
  const R = {{ receipts_json|safe }};
  let sortCol = 'scan_date', sortAsc = false;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function link(href, label) {
    return href ? `<a href="${esc(href)}">${esc(label)}</a>` : esc(label);
  }

  function fmt(rows) {
    if (!rows.length) return '';
    return rows.map(r => {
      const rev = r.reviewed ? '<span class="badge badge--ok">Reviewed</span>' : '';
      return [
        '<tr>',
        `<td>${esc(r.scan_date)}</td>`,
        `<td class="merchant">${link(r.merchant_href, r.merchant_label||r.merchant||'—')}</td>`,
        `<td>${link(r.folder_uri, r.category||'—')}</td>`,
        `<td>${esc(r.total_display)}</td>`,
        `<td>${esc(r.purchaser||'—')}</td>`,
        `<td>${r.card_last4 ? '···· '+esc(r.card_last4) : '—'}</td>`,
        `<td>${esc(r.processed_by||'tesseract')}</td>`,
        `<td class="items-cell"><span class="item-summary" title="${esc(r.items_full_label||r.items_label||'')}">${esc(r.items_label||'—')}</span></td>`,
        `<td>${link(r.file_link_uri, r.filename||'—')}</td>`,
        `<td>${rev}</td>`,
        '</tr>',
      ].join('');
    }).join('');
  }

  function sumTotal(rows) {
    let t = 0, n = 0;
    for (const r of rows) { if (r.total != null) { t += r.total; n++; } }
    return n ? '$' + t.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
  }

  function cmpVal(r, col) {
    const v = r[col];
    if (v == null) return sortAsc ? '￿' : '';
    return v;
  }

  function applySort(rows) {
    return rows.slice().sort((a, b) => {
      const av = cmpVal(a, sortCol), bv = cmpVal(b, sortCol);
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortAsc ? cmp : -cmp;
    });
  }

  function apply() {
    const from = document.getElementById('f-from').value;
    const to   = document.getElementById('f-to').value;
    const cat  = document.getElementById('f-cat').value;
    const merch = document.getElementById('f-merchant').value.toLowerCase();
    const purch = document.getElementById('f-purchaser').value.toLowerCase();

    let out = R.filter(r => {
      if (from && r.scan_date < from) return false;
      if (to   && r.scan_date > to)   return false;
      if (cat  && !r.category.startsWith(cat)) return false;
      if (merch && !(r.merchant||'').toLowerCase().includes(merch)) return false;
      if (purch && !(r.purchaser||'').toLowerCase().includes(purch)) return false;
      return true;
    });

    out = applySort(out);

    document.getElementById('audit-tbody').innerHTML = fmt(out);
    document.getElementById('stat-count').textContent = out.length;
    document.getElementById('stat-total').textContent = sumTotal(out);
    document.getElementById('audit-empty').style.display = out.length ? 'none' : 'block';
    document.getElementById('audit-wrap').style.display = out.length ? '' : 'none';
  }

  ['f-from','f-to','f-cat','f-merchant','f-purchaser'].forEach(id => {
    document.getElementById(id).addEventListener('input', apply);
  });

  document.getElementById('f-clear').addEventListener('click', () => {
    ['f-from','f-to','f-merchant','f-purchaser'].forEach(id => {
      document.getElementById(id).value = '';
    });
    document.getElementById('f-cat').value = '';
    apply();
  });

  document.querySelectorAll('th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (sortCol === col) { sortAsc = !sortAsc; }
      else { sortCol = col; sortAsc = col !== 'scan_date'; }
      document.querySelectorAll('th.sortable').forEach(t => {
        t.classList.remove('sort-asc','sort-desc');
      });
      th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      apply();
    });
  });

  // Default sort indicator
  document.querySelector('th[data-col="scan_date"]').classList.add('sort-desc');

  apply();

  // ── col resize ──
  function initColResize(tbl) {
    tbl.querySelectorAll('th').forEach(function (th) {
      var r = document.createElement('div');
      r.className = 'col-resizer';
      th.appendChild(r);
      r.addEventListener('mousedown', function (e) {
        var x0 = e.clientX, w0 = th.offsetWidth;
        tbl.classList.add('resizing');
        r.classList.add('rsz-active');
        var mv = function (ev) { th.style.width = th.style.minWidth = Math.max(40, w0 + ev.clientX - x0) + 'px'; };
        var up = function () { tbl.classList.remove('resizing'); r.classList.remove('rsz-active'); document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); };
        document.addEventListener('mousemove', mv);
        document.addEventListener('mouseup', up);
        e.preventDefault();
      });
    });
  }
  // ── cell expand (event delegation — works after tbody is rebuilt) ──
  function initCellExpand(el) {
    el.addEventListener('click', function (e) {
      var td = e.target.closest('td');
      if (td) { td.classList.toggle('expanded'); }
    });
  }
  // ── auto-size text inputs ──
  function initAutosize(el) {
    var s = document.createElement('span');
    var cs = window.getComputedStyle(el);
    s.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;pointer-events:none;'
      + 'font:' + cs.font + ';padding:0 ' + cs.paddingRight + ';';
    document.body.appendChild(s);
    var extra = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + 4;
    var sync = function () {
      s.textContent = el.value || el.placeholder || '';
      el.style.width = Math.max(120, s.offsetWidth + extra) + 'px';
    };
    el.addEventListener('input', sync);
    sync();
  }

  var auditTable = document.getElementById('audit-table');
  if (auditTable) { initColResize(auditTable); }
  var auditTbody = document.getElementById('audit-tbody');
  if (auditTbody) { initCellExpand(auditTbody); }
  ['f-merchant', 'f-purchaser'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) { initAutosize(el); }
  });
}());
</script>
</body>
</html>
"""


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


def _build_merchant_detail_view(
    merchant_row: dict,
    receipt_rows: list[dict],
    month_rows: list[dict],
    *,
    receipt_root: Path | None,
    filebrowser_base_url: str | None,
    filebrowser_receipt_root: str,
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
    )
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

    prepared = _prepare_recent(
        raw_rows,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
        merchant_lookup=merchant_page_lookup,
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
                "card_last4": r.get("card_last4") or "",
                "processed_by": r.get("processed_by") or "tesseract",
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
            ),
        )
        for merchant_row in data.get("all_merchants", [])
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
        audit_path = generate_audit_report(
            conn,
            output_dir,
            receipt_root=receipt_root,
            filebrowser_base_url=filebrowser_base_url,
            filebrowser_receipt_root=filebrowser_receipt_root,
        )
        written.append(audit_path)

    return written
