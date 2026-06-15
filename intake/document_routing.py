"""Shared file-inbox document routing for process and daemon commands."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RoutedDocument:
    """Result from routing one intake file."""

    processed: bool = False
    personal_finance_event: dict | None = None


def _renamed_suffix(source: Path, dest: Path) -> str:
    return f" (renamed {source.name} -> {dest.name})" if dest.name != source.name else ""


def route_inbox_document(
    pdf: Path,
    *,
    settings: dict,
    receipt_dir: Path,
    conn,
    rules: dict,
    merchant_aliases: dict,
    reserved_paths: set[Path],
    daemon: bool = False,
) -> RoutedDocument:
    """Route one file from the intake inbox into its document store.

    The CLI still owns command setup, dry-run handling, corrupt-file handling,
    and personal-finance batch triggering. This module owns the per-document
    routing flow so the one-shot process command and daemon stay behaviorally
    aligned.
    """
    from intake.classify import classify
    from intake.cli import (
        _backfill_purchaser_from_card,
        _canonical_document_destination,
        _canonical_receipt_destination,
        _card_row_from_card,
        _exhaust_hash_duplicate,
        _file_hash,
        _get_extract_kwargs,
        _letter_row_from_letter,
        _notify_processed_receipt,
        _payment_row_from_payment,
        _receipt_looks_like_identity_document,
        _receipt_row_from_receipt,
        _reroute_identity_receipt,
        _write_receipt_debug_artifacts,
    )
    from intake.db import (
        get_card_by_file_hash,
        get_letter_by_file_hash,
        get_payment_by_file_hash,
        get_receipt_by_file_hash,
        upsert_card,
        upsert_letter,
        upsert_payment,
        upsert_receipt,
    )
    from intake.document_type import classify_document_type
    from intake.extract import extract_card, extract_letter, extract_payment, extract_receipt
    from intake.ocr import extract_text
    from intake.parse_card import classify_card
    from intake.parse_letter import classify_letter
    from intake.parse_payment import classify_payment
    from intake.personal_finance import build_receipt_event
    from intake.sidecar import write_sidecar

    prefix = "  [inbox] " if daemon else "    "

    file_hash = _file_hash(pdf)
    existing = (
        get_receipt_by_file_hash(conn, file_hash)
        or get_letter_by_file_hash(conn, file_hash)
        or get_card_by_file_hash(conn, file_hash)
        or get_payment_by_file_hash(conn, file_hash)
    )
    if existing is not None:
        dest = _exhaust_hash_duplicate(
            receipt_dir,
            conn,
            pdf=pdf,
            existing_row=existing,
            file_hash=file_hash,
            reserved_paths=reserved_paths,
            source="daemon-hash-duplicate" if daemon else "ingest-hash-duplicate",
        )
        print(
            f"{prefix}duplicate by file hash, moved to "
            f"{dest.relative_to(receipt_dir)} (matches {existing['receipt_dir']})"
        )
        return RoutedDocument()

    raw_text = extract_text(pdf)
    doc_type = classify_document_type(raw_text, pdf.name)

    if doc_type == "letter":
        letter, model = extract_letter(
            pdf,
            raw_text,
            openai_api_key=settings.get("openai_api_key") or None,
            openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
        )
        category = classify_letter(letter)
        dest_dir = receipt_dir / category
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _canonical_document_destination(
            dest_dir,
            source_path=pdf,
            document_date=letter.letter_date or letter.scan_date,
            reserved_paths=reserved_paths,
        )
        shutil.move(str(pdf), dest)
        row = _letter_row_from_letter(
            letter,
            filename=dest.name,
            category=category,
            receipt_dir=str(dest.relative_to(receipt_dir)),
            file_hash=file_hash,
            processed_by=model,
        )
        upsert_letter(conn, row)
        write_sidecar(
            dest,
            {
                **{k: v for k, v in row.items() if k != "raw_text"},
                "document_type": "letter",
                "tier": "ok",
            },
        )
        if daemon:
            print(
                f"{prefix}{dest.relative_to(receipt_dir)} "
                f"[letter] sender={letter.sender!r} model={model}"
            )
        else:
            print(
                f"{prefix}-> {category}/{dest.name} [letter]"
                f" sender={letter.sender!r} model={model}{_renamed_suffix(pdf, dest)}"
            )
        return RoutedDocument(processed=True)

    if doc_type == "card":
        card, model = extract_card(
            pdf,
            raw_text,
            openai_api_key=settings.get("openai_api_key") or None,
            openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
        )
        category = classify_card(card)
        dest_dir = receipt_dir / category
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _canonical_document_destination(
            dest_dir,
            source_path=pdf,
            document_date=card.card_date or card.scan_date,
            reserved_paths=reserved_paths,
        )
        shutil.move(str(pdf), dest)
        row = _card_row_from_card(
            card,
            filename=dest.name,
            category=category,
            receipt_dir=str(dest.relative_to(receipt_dir)),
            file_hash=file_hash,
            processed_by=model,
        )
        upsert_card(conn, row)
        write_sidecar(
            dest,
            {
                **{k: v for k, v in row.items() if k != "raw_text"},
                "document_type": "card",
                "tier": "ok",
            },
        )
        if daemon:
            print(
                f"{prefix}{dest.relative_to(receipt_dir)} "
                f"[card] occasion={card.occasion!r} model={model}"
            )
        else:
            print(
                f"{prefix}-> {category}/{dest.name} [card]"
                f" occasion={card.occasion!r} model={model}{_renamed_suffix(pdf, dest)}"
            )
        return RoutedDocument(processed=True)

    if doc_type == "payment":
        payment, model = extract_payment(
            pdf,
            raw_text,
            openai_api_key=settings.get("openai_api_key") or None,
            openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
        )
        category = classify_payment(payment)
        dest_dir = receipt_dir / category
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _canonical_document_destination(
            dest_dir,
            source_path=pdf,
            document_date=payment.payment_date or payment.scan_date,
            reserved_paths=reserved_paths,
        )
        shutil.move(str(pdf), dest)
        row = _payment_row_from_payment(
            payment,
            filename=dest.name,
            category=category,
            receipt_dir=str(dest.relative_to(receipt_dir)),
            file_hash=file_hash,
            processed_by=model,
        )
        upsert_payment(conn, row)
        write_sidecar(
            dest,
            {
                **{k: v for k, v in row.items() if k != "raw_text"},
                "document_type": "payment",
                "tier": "ok",
            },
        )
        if daemon:
            print(
                f"{prefix}{dest.relative_to(receipt_dir)} "
                f"[payment] amount={payment.amount!r} account={payment.account_name!r}"
                f" model={model}"
            )
        else:
            print(
                f"{prefix}-> {category}/{dest.name} [payment]"
                f" amount={payment.amount!r} account={payment.account_name!r}"
                f" model={model}{_renamed_suffix(pdf, dest)}"
            )
        return RoutedDocument(processed=True)

    receipt, model, tier = extract_receipt(
        pdf,
        raw_text=raw_text,
        **_get_extract_kwargs(settings),
        fuzzy_rules=rules,
        merchant_aliases=merchant_aliases,
    )
    if _receipt_looks_like_identity_document(receipt):
        category, dest, row = _reroute_identity_receipt(
            receipt,
            pdf=pdf,
            receipt_dir=receipt_dir,
            conn=conn,
            file_hash=file_hash,
            processed_by=model,
            tier=tier,
            reserved_paths=reserved_paths,
        )
        if daemon:
            print(
                f"{prefix}{dest.relative_to(receipt_dir)} "
                f"[identity] subject={row['subject']!r} model={model}"
            )
        else:
            print(
                f"{prefix}-> {category}/{dest.name} [identity]"
                f" subject={row['subject']!r} model={model}{_renamed_suffix(pdf, dest)}"
            )
        return RoutedDocument(processed=True)

    receipt = _backfill_purchaser_from_card(conn, receipt)
    category = classify(receipt.merchant, rules, receipt.raw_text)
    dest_dir = receipt_dir / category
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _canonical_receipt_destination(
        dest_dir,
        receipt,
        source_path=pdf,
        reserved_paths=reserved_paths,
    )
    shutil.move(str(pdf), dest)
    row = _receipt_row_from_receipt(
        receipt,
        filename=dest.name,
        category=category,
        receipt_dir=str(dest.relative_to(receipt_dir)),
        file_hash=file_hash,
        processed_by=model,
        tier=tier,
    )
    upsert_receipt(conn, row)
    write_sidecar(dest, {k: v for k, v in row.items() if k != "raw_text"})
    _write_receipt_debug_artifacts(dest, receipt, processed_by=model, tier=tier)
    _notify_processed_receipt(
        settings,
        receipt,
        category=category,
        receipt_dir=str(dest.relative_to(receipt_dir)),
        filename=dest.name,
        processed_by=model,
        prefix="  " if daemon else "    ",
    )
    event = build_receipt_event(
        receipt,
        category=category,
        receipt_dir=str(dest.relative_to(receipt_dir)),
        filename=dest.name,
        processed_by=model,
        tier=tier,
    )
    if daemon:
        print(
            f"{prefix}{dest.relative_to(receipt_dir)} "
            f"merchant={receipt.merchant!r} total={receipt.total} model={model}"
        )
    else:
        print(
            f"{prefix}-> {category}/{dest.name} "
            f"(merchant={receipt.merchant!r}, total={receipt.total}, model={model})"
            f"{_renamed_suffix(pdf, dest)}"
        )
    return RoutedDocument(processed=True, personal_finance_event=event)
