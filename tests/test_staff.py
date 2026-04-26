"""Tests for receipt staff extraction."""

from intake.staff import extract_staff, receipt_staff_from_dicts, receipt_staff_to_dicts


def test_extract_staff_from_server_line():
    staff = extract_staff("Server: Isabel J\nTotal 9.20")

    assert receipt_staff_to_dicts(staff) == [
        {"role": "server", "name": "isabel j", "source_text": "Server: Isabel J"}
    ]


def test_extract_staff_from_waiter_line_with_number():
    staff = extract_staff("Waiter 23 Cailly\nVisa 12.34")

    assert receipt_staff_to_dicts(staff) == [
        {"role": "server", "name": "cailly", "source_text": "Waiter 23 Cailly"}
    ]


def test_extract_staff_from_kroger_cashier_code():
    staff = extract_staff("Your cashier was CHEC 510\nTOTAL 82.70")

    assert receipt_staff_to_dicts(staff) == [
        {"role": "cashier", "name": "chec 510", "source_text": "Your cashier was CHEC 510"}
    ]


def test_extract_staff_ignores_pay_server_footer():
    assert extract_staff("PLEASE PAY SERVER**\nTOTAL 23.96") == []


def test_extract_staff_ignores_windows_server_product_text():
    assert extract_staff("Microsoft Windows Server and Microsoft Windows Server") == []


def test_extract_staff_ignores_unlabeled_counter_noise():
    assert extract_staff("Counter df: dans KE iy") == []


def test_extract_staff_prefers_more_complete_duplicate_name():
    staff = extract_staff("Server: isabe]\nServer: isabel j")

    assert receipt_staff_to_dicts(staff) == [
        {"role": "server", "name": "isabel j", "source_text": "Server: isabel j"}
    ]


def test_receipt_staff_from_dicts_normalizes_payloads():
    staff = receipt_staff_from_dicts(
        [{"role": "Waitress", "name": "  Patricia W  ", "source_text": "server: Patricia W"}]
    )

    assert receipt_staff_to_dicts(staff) == [
        {"role": "server", "name": "patricia w", "source_text": "server: Patricia W"}
    ]
