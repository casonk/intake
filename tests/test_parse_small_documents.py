"""Greeting cards, payment confirmations and letters: what is pulled out of the OCR text.

These parsers feed the filing category and the database row, so the tests pin the extraction
rules (what wins when several things match, what is trimmed, what is left empty rather than
guessed) and the category each result is filed under. Pure text in, values out: no files, OCR
or network.
"""

from __future__ import annotations

from datetime import date

import pytest

from intake.parse_card import Card, classify_card, parse_card
from intake.parse_letter import Letter, classify_letter, parse_letter
from intake.parse_payment import Payment, classify_payment, parse_payment

# --- cards --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "occasion"),
    [
        ("HAPPY  BIRTHDAY to you", "birthday"),
        ("Congratulation on the new job", "congratulations"),
        ("Congratulations!", "congratulations"),
        ("Merry Christmas", "holiday"),
        ("Happy Holiday", "holiday"),
        ("Season's Greetings", "holiday"),
        ("Happy New Year", "holiday"),
        ("Happy Anniversary", "anniversary"),
        ("Get well soon", "get-well"),
        ("With sympathy", "sympathy"),
        ("Thinking of you", "sympathy"),
        ("Thank you so much", "thank-you"),
        ("Bon Voyage", "farewell"),
        ("Good luck!", "farewell"),
    ],
)
def test_card_occasion_is_recognised(text, occasion):
    assert parse_card("c.pdf", text).occasion == occasion


def test_the_first_matching_occasion_wins_over_later_ones():
    assert parse_card("c.pdf", "Thank you! Happy birthday!").occasion == "birthday"


def test_a_card_with_no_known_occasion_has_none_and_files_as_general():
    card = parse_card("c.pdf", "Just saying hello")
    assert card.occasion is None
    assert classify_card(card) == "cards/general"
    assert classify_card(parse_card("c.pdf", "Happy Birthday")) == "cards/birthday"


@pytest.mark.parametrize(
    ("text", "sender"),
    [
        ("Happy Birthday\nFrom: Alex", "Alex"),
        ("Happy Birthday\nLove, Alex", "Alex"),
        ("Happy Birthday\nLove from Grandma", "Grandma"),
        ("Happy Birthday\nSigned: Pat", "Pat"),
    ],
)
def test_the_sender_comes_from_a_signature_line(text, sender):
    assert parse_card("c.pdf", text).sender == sender


def test_without_a_signature_line_the_last_short_undated_line_is_the_sender():
    text = "Happy Birthday\nWishing you a very long and wonderful year ahead of you now\nJamie\n"
    assert parse_card("c.pdf", text).sender == "Jamie"


def test_the_sender_guess_skips_dates_numbers_and_long_lines():
    text = "Happy Birthday\nSam\nMarch 3, 2026\n12345\n" + "x" * 45
    assert parse_card("c.pdf", text).sender == "Sam"


def test_a_card_with_nothing_signature_like_has_no_sender():
    assert parse_card("c.pdf", "2026\n12345").sender is None
    assert parse_card("c.pdf", "").sender is None


def test_the_recipient_comes_from_a_to_or_dear_line():
    assert parse_card("c.pdf", "To: Sam\nHappy Birthday").recipient == "Sam"
    assert parse_card("c.pdf", "Dear Sam\nHappy Birthday").recipient == "Sam"
    assert parse_card("c.pdf", "Happy Birthday").recipient is None


def test_a_card_keeps_its_date_and_a_bounded_excerpt():
    card = parse_card("scan_20260102.pdf", "  Happy Birthday 2026-03-03\n" + "y" * 400 + "  ")
    assert card.card_date == date(2026, 3, 3)
    assert card.scan_date == date(2026, 1, 2)
    assert len(card.message_excerpt) == 300
    assert card.message_excerpt.startswith("Happy Birthday")
    assert card.raw_text.endswith("  ")  # the raw text itself is untouched
    assert isinstance(card, Card)


# --- payments -----------------------------------------------------------------------------


def test_the_payment_amount_is_the_largest_dollar_amount_with_cents():
    text = "Fee $5.00\nPayment received $1,200.50\nBalance $99.99\nPoints 5000 $7"
    assert parse_payment("p.pdf", text).amount == 1200.5


def test_a_payment_without_a_dollar_amount_has_none():
    assert parse_payment("p.pdf", "no money here 12.50").amount is None
    assert parse_payment("p.pdf", "").amount is None


@pytest.mark.parametrize(
    ("text", "last4"),
    [
        ("account ending 4321", "4321"),
        ("account ending in 4321", "4321"),
        ("xxxx1234", "1234"),
        ("Card 9876", "9876"),
        ("nothing useful", None),
    ],
)
def test_the_account_last4(text, last4):
    assert parse_payment("p.pdf", text).account_last4 == last4


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("Confirmation #: ABC12345", "ABC12345"),
        ("Reference no. 99887766", "99887766"),
        ("conf: A1-B2-C3-D4", "A1-B2-C3-D4"),
        ("Authorization number ZZ99XX", "ZZ99XX"),
        ("Confirmation 123", None),
        ("Reference", None),
        ("Payment Confirmation 99", None),
        ("Ref#12345", "12345"),
        ("nothing", None),
    ],
)
def test_the_confirmation_number(text, number):
    assert parse_payment("p.pdf", text).confirmation_number == number


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Your mortgage payment", "mortgage"),
        ("Escrow shortage", "mortgage"),
        ("car payment due", "auto"),
        ("Vehicle Payment", "auto"),
        ("student loan installment", "loan"),
        ("Electric bill", "utility"),
        ("Internet bill", "utility"),
        ("Visa statement balance", "credit-card"),
        ("minimum payment due", "credit-card"),
        ("a gift", "other"),
    ],
)
def test_the_payment_type(text, kind):
    assert parse_payment("p.pdf", text).payment_type == kind


def test_the_payment_type_follows_a_fixed_priority_when_several_terms_appear():
    assert parse_payment("p.pdf", "mortgage and auto loan and visa").payment_type == "mortgage"
    assert parse_payment("p.pdf", "mortgage and car payment").payment_type == "mortgage"
    assert parse_payment("p.pdf", "car payment on a visa").payment_type == "auto"
    assert parse_payment("p.pdf", "auto loan through electric co").payment_type == "loan"
    assert parse_payment("p.pdf", "electric bill by visa").payment_type == "utility"


def test_a_payment_keeps_its_dates_and_leaves_unknown_fields_empty():
    payment = parse_payment("scan_20260102.pdf", "Paid on 2026-03-03 $10.00")
    assert payment.payment_date == date(2026, 3, 3)
    assert payment.scan_date == date(2026, 1, 2)
    assert payment.account_name is None and payment.payer is None
    assert isinstance(payment, Payment)
    assert parse_payment("p.pdf", "no date").payment_date is None


@pytest.mark.parametrize(
    ("kind", "category"),
    [
        ("mortgage", "payments/mortgage"),
        ("credit-card", "payments/credit-card"),
        (None, "payments/other"),
    ],
)
def test_payments_are_filed_by_type(kind, category):
    payment = Payment("p.pdf", date(2026, 1, 1), None, None, None, None, kind, None, None, "")
    assert classify_payment(payment) == category


# --- letters ------------------------------------------------------------------------------


def test_the_header_fields_are_read_from_their_lines():
    text = (
        "From: Acme Bank\nTo: Sam Smith\nSubject: Your account\nDear Sam,\n"
        "We are writing about your account."
    )
    letter = parse_letter("l.pdf", text)
    assert letter.sender == "Acme Bank"
    assert letter.recipient == "Sam Smith"
    assert letter.subject == "Your account"
    assert letter.salutation == "Dear Sam,"
    assert isinstance(letter, Letter)


@pytest.mark.parametrize("label", ["Re", "Subject", "Subj"])
def test_a_subject_can_be_labelled_three_ways(label):
    assert parse_letter("l.pdf", f"{label}: Hello there").subject == "Hello there"


@pytest.mark.parametrize("label", ["Sender", "From"])
def test_a_sender_can_be_labelled_two_ways(label):
    assert parse_letter("l.pdf", f"{label}: Acme").sender == "Acme"
    assert parse_letter("l.pdf", f"{label.lower()}: Acme").recipient is None


def test_a_recipient_can_be_labelled_two_ways():
    assert parse_letter("l.pdf", "Recipient: Sam").recipient == "Sam"
    assert parse_letter("l.pdf", "to: Sam").recipient == "Sam"


def test_long_header_values_are_cut_to_their_limits():
    long = "z" * 200
    letter = parse_letter("l.pdf", f"From: {long}\nTo: {long}\nSubject: {long}")
    assert (len(letter.sender), len(letter.recipient), len(letter.subject)) == (80, 80, 120)


def test_missing_or_blank_headers_are_none():
    letter = parse_letter("l.pdf", "some body text\nFrom:    ")
    assert letter.sender is None
    assert letter.recipient is None and letter.subject is None and letter.salutation is None


def test_the_salutation_may_be_to_whom_it_may_concern():
    assert parse_letter("l.pdf", "To Whom It May Concern:\nbody").salutation == (
        "To Whom It May Concern:"
    )


def test_the_body_excerpt_drops_header_and_closing_lines_and_is_bounded():
    text = (
        "From: A\nTo: B\nDate: 2026-01-01\nSubject: S\nDear B,\n"
        "First line.\nSecond line.\nSincerely,\nA\nBest regards\nYours truly\nRegards\n"
    )
    assert parse_letter("l.pdf", text).body_excerpt == "First line. Second line. A"
    assert len(parse_letter("l.pdf", "w " * 600).body_excerpt) == 500


def test_a_letter_keeps_its_dates():
    letter = parse_letter("scan_20260102.pdf", "Dated 2026-03-03\nbody")
    assert letter.letter_date == date(2026, 3, 3)
    assert letter.scan_date == date(2026, 1, 2)
    assert parse_letter("l.pdf", "no date").letter_date is None


def make_letter(text):
    return Letter("l.pdf", date(2026, 1, 1), None, None, None, None, None, text, text)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Your Residence Card is enclosed", "letters/legal/identity"),
        ("my number notification", "letters/legal/identity"),
        ("在留カード", "letters/legal/identity"),
        ("the plaintiff and defendant", "letters/legal"),
        ("Your prescription is ready, patient", "letters/medical"),
        ("Your account balance and tax refund", "letters/financial"),
        ("Thanks for dinner, see you soon", "letters/personal"),
        ("", "letters/personal"),
    ],
)
def test_letters_are_filed_by_content(text, category):
    assert classify_letter(make_letter(text)) == category


def test_letter_categories_follow_a_fixed_priority():
    assert classify_letter(make_letter("residence card and court")) == "letters/legal/identity"
    assert classify_letter(make_letter("court hospital account")) == "letters/legal"
    assert classify_letter(make_letter("hospital account")) == "letters/medical"


def test_category_words_match_through_punctuation():
    assert classify_letter(make_letter("The court, today.")) == "letters/legal"
    assert classify_letter(make_letter("(hospital)")) == "letters/medical"
    assert classify_letter(make_letter("balance: due")) == "letters/financial"


def test_category_words_must_match_whole_words():
    assert classify_letter(make_letter("accounting and courtesy")) == "letters/personal"
