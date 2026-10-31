"""Report pages take their colours from the shared portfolio theme tokens."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = sorted(
    (Path(__file__).resolve().parents[1] / "intake" / "templates" / "report").glob("*.html.j2")
)


def test_there_are_report_templates():
    assert len(TEMPLATES) >= 8


def test_every_page_loads_the_shared_theme_before_its_styles():
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        css = '<link rel="stylesheet" href="/_portfolio/theme.css">'
        js = '<script src="/_portfolio/theme.js"></script>'
        assert css in text and js in text, path.name
        assert text.index(css) < text.index("<style>"), path.name


def test_templates_use_theme_tokens_not_literal_colours():
    for path in TEMPLATES:
        text = re.sub(r"var\([^)]*\)", "", path.read_text(encoding="utf-8"))
        lines = [ln for ln in text.splitlines() if "shadow" not in ln]
        found = re.findall(r"(?<![&\w])#[0-9a-fA-F]{3,8}\b|rgba?\(", "\n".join(lines))
        assert found == [], f"{path.name}: {found[:4]}"
        assert "prefers-color-scheme" not in text, path.name


def test_a_lone_half_width_chart_card_is_not_left_half_empty():
    text = (TEMPLATES[0].parent / "intake-report.html.j2").read_text(encoding="utf-8")
    cards = re.findall(r'<article class="chart-card (half|wide)"', text)
    run = 0
    for kind in [*cards, "wide"]:
        if kind == "half":
            run += 1
        else:
            assert run % 2 == 0, "a half-width chart card has no partner in its row"
            run = 0
