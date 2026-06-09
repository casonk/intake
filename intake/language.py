"""Receipt language and locale hints derived from OCR text."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReceiptLocaleHint:
    language: str = "und"
    currency: str | None = None
    confidence: float = 0.0
    reasons: tuple[str, ...] = ()


_JAPANESE_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
_GERMAN_CHAR_RE = re.compile(r"[ÄÖÜäöüß]")
_FRENCH_CHAR_RE = re.compile(r"[ÀÂÆÇÉÈÊËÎÏÔŒÙÛÜŸàâæçéèêëîïôœùûüÿ]")
_JAPANESE_RECEIPT_TERMS = (
    "領収書",
    "レシート",
    "合計",
    "小計",
    "消費税",
    "税込",
    "税抜",
    "お預り",
    "お預かり",
    "お釣り",
    "釣銭",
    "現計",
    "円",
)
_GERMAN_RECEIPT_TERMS = (
    "bäckerei",
    "backerei",
    "gesamt",
    "gesamtsumme",
    "kassenbon",
    "mwst",
    "netto",
    "rechnung",
    "rückgeld",
    "ruckgeld",
    "summe",
    "ust",
    "zahlbetrag",
    "zwischensumme",
)
_FRENCH_RECEIPT_TERMS = (
    "boulangerie",
    "espèces",
    "especes",
    "montant",
    "paiement",
    "reçu",
    "recu",
    "rendu",
    "sous total",
    "sous-total",
    "total ttc",
    "ttc",
    "tva",
)


def _term_hits(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(term for term in terms if term in text)


def _latin_locale_candidate(
    *,
    language: str,
    accent_count: int,
    term_hits: tuple[str, ...],
    reasons: list[str],
) -> ReceiptLocaleHint | None:
    if len(term_hits) >= 2:
        confidence = 0.84 if accent_count else 0.76
    elif len(term_hits) == 1 and accent_count:
        confidence = 0.68
    else:
        return None

    if accent_count:
        reasons.append(f"{language}_chars={accent_count}")
    reasons.append(f"{language}_terms=" + ",".join(term_hits[:4]))
    return ReceiptLocaleHint(
        language=language,
        confidence=confidence,
        reasons=tuple(reasons),
    )


def detect_receipt_locale(raw_text: str) -> ReceiptLocaleHint:
    """Return a best-effort language/currency hint for receipt OCR text."""
    text = str(raw_text or "")
    if not text.strip():
        return ReceiptLocaleHint()

    japanese_chars = len(_JAPANESE_CHAR_RE.findall(text))
    latin_chars = len(_LATIN_CHAR_RE.findall(text))
    term_hits = _term_hits(text, _JAPANESE_RECEIPT_TERMS)

    reasons: list[str] = []
    if japanese_chars:
        reasons.append(f"japanese_chars={japanese_chars}")
    if term_hits:
        reasons.append("terms=" + ",".join(term_hits[:4]))

    if japanese_chars >= 4 or len(term_hits) >= 2 or ("円" in text and japanese_chars):
        signal = min(1.0, (japanese_chars / max(japanese_chars + latin_chars, 1)) + 0.25)
        confidence = max(signal, 0.8 if len(term_hits) >= 2 else 0.65)
        return ReceiptLocaleHint(
            language="ja",
            currency="JPY",
            confidence=round(min(confidence, 1.0), 2),
            reasons=tuple(reasons),
        )

    lower_text = text.casefold()
    german_candidate = _latin_locale_candidate(
        language="de",
        accent_count=len(_GERMAN_CHAR_RE.findall(text)),
        term_hits=_term_hits(lower_text, _GERMAN_RECEIPT_TERMS),
        reasons=[],
    )
    french_candidate = _latin_locale_candidate(
        language="fr",
        accent_count=len(_FRENCH_CHAR_RE.findall(text)),
        term_hits=_term_hits(lower_text, _FRENCH_RECEIPT_TERMS),
        reasons=[],
    )
    candidates = [candidate for candidate in (german_candidate, french_candidate) if candidate]
    if candidates:
        best = max(
            candidates,
            key=lambda candidate: (
                candidate.confidence,
                len(candidate.reasons),
                candidate.language,
            ),
        )
        return best

    if latin_chars and japanese_chars == 0:
        return ReceiptLocaleHint(language="en", currency=None, confidence=0.55, reasons=("latin",))

    return ReceiptLocaleHint(reasons=tuple(reasons))


def is_japanese_locale(hint: ReceiptLocaleHint | None) -> bool:
    return bool(hint and hint.language == "ja")
