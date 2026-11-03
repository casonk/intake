"""Routing one scanned file from the inbox into its store.

Routing moves the user's only copy of a scan, so the properties that matter are: an exact
duplicate is never re-processed or lost, each document type lands under its own category with a
database row keyed by the file hash and a sidecar that omits the raw OCR text, nothing is left
behind in the inbox, and two files that map to the same name never overwrite each other.
OCR and the language-model extractors are stubbed; hashing, naming, the file moves, the database
and the sidecars are real and live under ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from intake import document_routing as dr
from intake.db import (
    get_card_by_file_hash,
    get_document_by_file_hash,
    get_letter_by_file_hash,
    get_payment_by_file_hash,
    get_receipt_by_file_hash,
    init_db,
)
from intake.fingerprint import sha256_file
from intake.parse import Receipt
from intake.parse_card import parse_card
from intake.parse_letter import parse_letter
from intake.parse_payment import parse_payment

LETTER_TEXT = "Dear Sam,\nThank you for dinner on Saturday. See you soon.\nLove, Alex"
CARD_TEXT = "Happy Birthday!\nWishing you a wonderful year.\nFrom Alex"
PAYMENT_TEXT = "Payment received $1,200.00 mortgage payment confirmation ABC12345 ending 4321"
DOCUMENT_TEXT = "Installation Manual\nModel: XJ-100\nSerial: 998877\nKeep this guide."
IDENTITY_TEXT = "Residence Card\nPeriod of stay: 3 years\nNationality / Region: Example"


@pytest.fixture
def env(tmp_path, monkeypatch):
    receipt_dir = tmp_path / "receipts"
    inbox = receipt_dir / "inbox"
    inbox.mkdir(parents=True)
    conn = init_db(tmp_path / "test.db")

    state = SimpleNamespace(
        text="",
        doc_type="receipt",
        receipt=None,
        calls=[],
        notified=[],
        debug=[],
        category="groceries",
        receipt_dir=receipt_dir,
        inbox=inbox,
        conn=conn,
        reserved=set(),
    )

    def extract_text(path):
        state.calls.append(("ocr", path.name))
        return state.text

    monkeypatch.setattr("intake.ocr.extract_text", extract_text)
    monkeypatch.setattr("intake.document_type.classify_document_type", lambda t, n: state.doc_type)

    def extractor(parse, name):
        def run(pdf, raw_text, *, openai_api_key, openai_model):
            state.calls.append((name, openai_api_key, openai_model))
            return parse(pdf.name, raw_text, pdf_path=pdf), "model-x"

        return run

    monkeypatch.setattr("intake.extract.extract_letter", extractor(parse_letter, "letter"))
    monkeypatch.setattr("intake.extract.extract_card", extractor(parse_card, "card"))
    monkeypatch.setattr("intake.extract.extract_payment", extractor(parse_payment, "payment"))

    def extract_receipt(pdf, *, raw_text, fuzzy_rules, merchant_aliases, **kwargs):
        state.calls.append(("receipt", dict(kwargs), fuzzy_rules, merchant_aliases))
        return state.receipt or make_receipt(pdf.name, raw_text), "model-x", "ok"

    monkeypatch.setattr("intake.extract.extract_receipt", extract_receipt)
    monkeypatch.setattr("intake.classify.classify", lambda merchant, rules, raw: state.category)
    monkeypatch.setattr(
        "intake.cli._notify_processed_receipt",
        lambda settings, receipt, **kw: state.notified.append(kw),
    )
    monkeypatch.setattr(
        "intake.cli._write_receipt_debug_artifacts",
        lambda dest, receipt, **kw: state.debug.append((dest.name, kw)),
    )
    yield state
    conn.close()


def make_receipt(filename="scan.pdf", raw_text="TOTAL 12.50", **kw):
    fields = dict(
        filename=filename,
        scan_date=date(2026, 1, 2),
        receipt_date=date(2026, 1, 1),
        receipt_time=None,
        merchant="costco",
        total=12.5,
        raw_text=raw_text,
    )
    fields.update(kw)
    return Receipt(**fields)


def put(env, name="scan_20260102.pdf", content=b"%PDF fake contents"):
    path = env.inbox / name
    path.write_bytes(content)
    return path


def route(env, pdf, *, daemon=False, settings=None, rules=None, aliases=None):
    return dr.route_inbox_document(
        pdf,
        settings=settings or {},
        receipt_dir=env.receipt_dir,
        conn=env.conn,
        rules=rules if rules is not None else {},
        merchant_aliases=aliases if aliases is not None else {},
        reserved_paths=env.reserved,
        daemon=daemon,
    )


def stored_files(env, category):
    folder = env.receipt_dir / category
    return sorted(p for p in folder.glob("*") if not p.name.endswith(".json"))


def sidecar(path):
    return json.loads(path.with_suffix(".intake.json").read_text())


# --- document types -----------------------------------------------------------------------


def test_a_receipt_is_filed_recorded_and_reported(env, capsys):
    env.text = "TOTAL 12.50"
    pdf = put(env, content=b"receipt bytes")
    digest = sha256_file(pdf)

    result = route(env, pdf)

    (dest,) = stored_files(env, "groceries")
    assert not pdf.exists()
    assert dest.read_bytes() == b"receipt bytes"
    assert result.processed is True
    assert result.personal_finance_event is not None
    row = get_receipt_by_file_hash(env.conn, digest)
    assert row["filename"] == dest.name
    assert row["receipt_dir"] == f"groceries/{dest.name}"
    assert row["merchant"] == "costco"
    assert "raw_text" not in sidecar(dest)
    assert sidecar(dest)["merchant"] == "costco"
    assert env.notified[0]["category"] == "groceries"
    assert env.notified[0]["prefix"] == "    "
    assert env.debug == [(dest.name, {"processed_by": "model-x", "tier": "ok"})]
    out = capsys.readouterr().out
    assert f"-> groceries/{dest.name} (merchant='costco', total=12.5, model=model-x)" in out


def test_the_daemon_reports_a_receipt_in_its_own_format_and_prefix(env, capsys):
    pdf = put(env)
    route(env, pdf, daemon=True)
    (dest,) = stored_files(env, "groceries")
    assert env.notified[0]["prefix"] == "  "
    assert f"  [inbox] groceries/{dest.name} merchant='costco' total=12.5 model=model-x" in (
        capsys.readouterr().out
    )


def test_extraction_receives_the_configured_rules_aliases_and_keys(env):
    pdf = put(env)
    route(
        env,
        pdf,
        settings={
            "openai_api_key": "sk-test",
            "openai_model_low": "small",
            "openai_model_mid": "mid",
        },
        rules={"groceries": ["costco"]},
        aliases={"cstco": "costco"},
    )
    _, kwargs, rules, aliases = next(c for c in env.calls if c[0] == "receipt")
    assert kwargs["openai_api_key"] == "sk-test"
    assert kwargs["openai_model_low"] == "small"
    assert kwargs["openai_model_mid"] == "mid"
    assert rules == {"groceries": ["costco"]}
    assert aliases == {"cstco": "costco"}


def test_a_document_is_filed_by_its_kind_without_the_ocr_text_in_its_sidecar(env, capsys):
    env.text = DOCUMENT_TEXT
    env.doc_type = "document"
    pdf = put(env, content=b"manual bytes")
    digest = sha256_file(pdf)

    result = route(env, pdf)

    row = get_document_by_file_hash(env.conn, digest)
    category = row["category"]
    (dest,) = stored_files(env, category)
    assert dest.read_bytes() == b"manual bytes"
    assert not pdf.exists()
    assert result == dr.RoutedDocument(processed=True)
    assert row["receipt_dir"] == f"{category}/{dest.name}"
    data = sidecar(dest)
    assert "raw_text" not in data
    assert data["document_type"] == "document" and data["tier"] == "ok"
    assert f"-> {category}/{dest.name} [document]" in capsys.readouterr().out


def test_a_letter_is_filed_by_category_with_the_extractor_model_recorded(env, capsys):
    env.text = LETTER_TEXT
    env.doc_type = "letter"
    pdf = put(env)
    digest = sha256_file(pdf)

    result = route(env, pdf, settings={"openai_api_key": "", "openai_model_low": ""})

    row = get_letter_by_file_hash(env.conn, digest)
    (dest,) = stored_files(env, row["category"])
    assert row["processed_by"] == "model-x"
    assert result.personal_finance_event is None
    assert sidecar(dest)["document_type"] == "letter"
    assert "raw_text" not in sidecar(dest)
    # blank settings become "no key" and the default small model, never an empty string
    assert ("letter", None, "gpt-4o-mini") in env.calls
    assert f"[letter] sender={row['sender']!r} model=model-x" in capsys.readouterr().out


def test_a_card_is_filed_under_its_occasion(env, capsys):
    env.text = CARD_TEXT
    env.doc_type = "card"
    pdf = put(env)
    digest = sha256_file(pdf)

    route(env, pdf)

    row = get_card_by_file_hash(env.conn, digest)
    assert row["category"] == "cards/birthday"
    (dest,) = stored_files(env, "cards/birthday")
    assert sidecar(dest)["document_type"] == "card"
    assert "raw_text" not in sidecar(dest)
    assert "[card] occasion='birthday' model=model-x" in capsys.readouterr().out


def test_a_payment_is_filed_by_its_payment_type(env, capsys):
    env.text = PAYMENT_TEXT
    env.doc_type = "payment"
    pdf = put(env)
    digest = sha256_file(pdf)

    route(env, pdf)

    row = get_payment_by_file_hash(env.conn, digest)
    assert row["category"].startswith("payments/")
    (dest,) = stored_files(env, row["category"])
    assert sidecar(dest)["document_type"] == "payment"
    assert "raw_text" not in sidecar(dest)
    assert "[payment] amount=1200.0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("doc_type", "text", "marker"),
    [
        ("document", DOCUMENT_TEXT, "title="),
        ("letter", LETTER_TEXT, "sender="),
        ("card", CARD_TEXT, "occasion="),
        ("payment", PAYMENT_TEXT, "amount="),
    ],
)
def test_the_daemon_reports_each_type_with_the_inbox_prefix(env, capsys, doc_type, text, marker):
    env.text, env.doc_type = text, doc_type
    route(env, put(env), daemon=True)
    out = capsys.readouterr().out
    assert out.startswith("  ")
    assert f"[{doc_type}]" in out and marker in out
    assert "->" not in out  # the daemon format has no arrow


def test_an_identity_document_read_as_a_receipt_is_filed_as_a_legal_letter(env, capsys):
    env.receipt = make_receipt(raw_text=IDENTITY_TEXT, merchant="")
    pdf = put(env, content=b"id card bytes")
    digest = sha256_file(pdf)

    result = route(env, pdf)

    row = get_letter_by_file_hash(env.conn, digest)
    assert row["category"] == "letters/legal/identity"
    (dest,) = stored_files(env, "letters/legal/identity")
    assert dest.read_bytes() == b"id card bytes"
    assert get_receipt_by_file_hash(env.conn, digest) is None  # not also stored as spend
    assert sidecar(dest)["identity_document"] is True
    assert result.personal_finance_event is None
    assert env.notified == []  # no "receipt processed" notice for an identity card
    assert "[identity] subject='Japan residence card'" in capsys.readouterr().out


def test_the_daemon_reports_an_identity_document(env, capsys):
    env.receipt = make_receipt(raw_text=IDENTITY_TEXT, merchant="")
    route(env, put(env), daemon=True)
    assert "[identity] subject=" in capsys.readouterr().out


# --- duplicates ---------------------------------------------------------------------------


def test_an_exact_duplicate_is_set_aside_and_never_re_processed(env, capsys):
    first = put(env, "first.pdf", b"same bytes")
    route(env, first)
    env.calls.clear()
    (original,) = stored_files(env, "groceries")

    second = put(env, "second.pdf", b"same bytes")
    result = route(env, second)

    assert result == dr.RoutedDocument()  # not processed
    assert env.calls == []  # no OCR and no extraction for a file already seen
    assert not second.exists()
    assert original.exists() and original.read_bytes() == b"same bytes"
    dupes = list((env.receipt_dir / "exhaust" / "duplicates" / "groceries").glob("*.pdf"))
    assert len(dupes) == 1 and dupes[0].read_bytes() == b"same bytes"
    out = capsys.readouterr().out
    assert "duplicate by file hash, moved to exhaust/duplicates/groceries/" in out
    assert f"(matches groceries/{original.name})" in out


@pytest.mark.parametrize(
    ("doc_type", "text"),
    [
        ("receipt", "TOTAL 12.50"),
        ("document", DOCUMENT_TEXT),
        ("letter", LETTER_TEXT),
        ("card", CARD_TEXT),
        ("payment", PAYMENT_TEXT),
    ],
)
def test_a_duplicate_of_any_document_type_is_recognised(env, doc_type, text):
    env.text, env.doc_type = text, doc_type
    route(env, put(env, "a.pdf", b"same bytes"))
    env.calls.clear()
    second = put(env, "b.pdf", b"same bytes")
    result = route(env, second)
    assert result.processed is False
    assert env.calls == []  # never OCRed or extracted again
    assert not second.exists()


@pytest.mark.parametrize(
    ("doc_type", "text", "name"),
    [
        ("letter", LETTER_TEXT, "letter"),
        ("card", CARD_TEXT, "card"),
        ("payment", PAYMENT_TEXT, "payment"),
    ],
)
def test_extractors_get_the_configured_key_and_model_or_the_safe_defaults(
    env, doc_type, text, name
):
    env.text, env.doc_type = text, doc_type
    route(env, put(env, "a.pdf", b"one"), settings={})
    route(
        env,
        put(env, "b.pdf", b"two"),
        settings={"openai_api_key": "sk-test", "openai_model_low": "small"},
    )
    route(env, put(env, "c.pdf", b"three"), settings={"openai_api_key": "", "openai_model_low": ""})
    calls = [c for c in env.calls if c[0] == name]
    assert calls == [
        (name, None, "gpt-4o-mini"),
        (name, "sk-test", "small"),
        (name, None, "gpt-4o-mini"),  # blank settings are "not configured", never an empty key
    ]


def test_the_daemon_labels_a_duplicate_in_the_audit_trail(env):
    route(env, put(env, "a.pdf", b"x"))
    route(env, put(env, "b.pdf", b"x"), daemon=True)
    row = env.conn.execute("SELECT source FROM receipt_updates ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "daemon-hash-duplicate"


def test_the_one_shot_process_labels_a_duplicate_differently(env):
    route(env, put(env, "a.pdf", b"x"))
    route(env, put(env, "b.pdf", b"x"))
    row = env.conn.execute("SELECT source FROM receipt_updates ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "ingest-hash-duplicate"


# --- names never collide ------------------------------------------------------------------


def test_two_different_files_with_the_same_date_never_overwrite_each_other(env):
    first = put(env, "scan_20260102.pdf", b"first receipt")
    second = put(env, "scan_20260102_b.pdf", b"second receipt")
    route(env, first)
    route(env, second)

    files = stored_files(env, "groceries")
    assert len(files) == 2
    assert {f.read_bytes() for f in files} == {b"first receipt", b"second receipt"}


def test_a_name_reserved_earlier_in_the_run_is_not_reused(env):
    first = put(env, "a.pdf", b"one")
    route(env, first)
    assert len(env.reserved) == 1
    reserved_before = set(env.reserved)

    second = put(env, "b.pdf", b"two")
    route(env, second)
    assert len(env.reserved) == 2
    assert reserved_before < env.reserved


def test_the_renamed_hint_appears_only_when_the_name_changed(env, capsys):
    pdf = put(env, "scan_20260102.pdf", b"x")
    route(env, pdf)
    out = capsys.readouterr().out
    assert "(renamed scan_20260102.pdf ->" in out


def test_no_rename_hint_when_the_file_already_has_its_canonical_name():
    source = SimpleNamespace(name="20260101_0000.pdf")
    same = SimpleNamespace(name="20260101_0000.pdf")
    other = SimpleNamespace(name="20260101_0001.pdf")
    assert dr._renamed_suffix(source, same) == ""
    assert dr._renamed_suffix(source, other) == " (renamed 20260101_0000.pdf -> 20260101_0001.pdf)"
