"""Extract cross-document reference identifiers (EFT/transaction/confirmation/
order/invoice numbers) from receipt text.

Two receipts sharing one of these — even under completely different merchant
names or document types (a payment-confirmation email and a saved PDF of the
same confirmation page, say) — are strong evidence of the same real-world
transaction recorded twice. Matching still isn't proof on its own (an IRS
penalty notice mailed twice, say, is two real letters, not one), so this is
report-only: see query_reference_duplicate_groups in db.py.
"""

from __future__ import annotations

import re

# Every captured ID must contain at least one digit (via the trailing
# lookahead) — otherwise an optional label like "Order" or "Invoice" with
# nothing but a plain English word after it (e.g. "Order Total", "Invoice
# attached") would get misread as an identifier.
_HAS_DIGIT = r"(?=[A-Za-z0-9]*\d)"
_HAS_DIGIT_WITH_DASH = r"(?=[A-Za-z0-9-]*\d)"
_REFERENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eft_number", re.compile(r"\beft\s*(?:#|number)?\s*:?\s*(\d{6,})\b", re.IGNORECASE)),
    (
        "transaction_id",
        re.compile(rf"\btransaction\s*id\s*:?\s*{_HAS_DIGIT}([A-Za-z0-9]{{8,}})\b", re.IGNORECASE),
    ),
    (
        "confirmation_number",
        re.compile(
            rf"\bconfirmation\s*(?:number|#|code)\s*:?\s*{_HAS_DIGIT}([A-Za-z0-9]{{8,}})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "order_number",
        re.compile(
            rf"\border\s*(?:#|number)?\s*:?\s*{_HAS_DIGIT}([A-Za-z0-9]{{6,}})\b", re.IGNORECASE
        ),
    ),
    (
        # Real invoice numbers often include hyphens (e.g. "S-PK944EGZGJ-2606").
        "invoice_number",
        re.compile(
            rf"\binvoice\s*(?:#|no\.?|number)?\s*:?\s*{_HAS_DIGIT_WITH_DASH}([A-Za-z0-9-]{{5,}})\b",
            re.IGNORECASE,
        ),
    ),
)


def extract_reference_ids(raw_text: str) -> dict[str, str]:
    """Return {kind: normalized_id} for each reference-number pattern found.

    Only the first match per kind is kept — a single receipt mentioning the
    same order number twice shouldn't create two entries.
    """
    if not raw_text:
        return {}
    found: dict[str, str] = {}
    for kind, pattern in _REFERENCE_PATTERNS:
        match = pattern.search(raw_text)
        if match:
            found[kind] = match.group(1).upper()
    return found
