"""Translation of non-English receipt OCR: when it runs, what is sent, and what is kept.

Receipt text (and, for the OpenAI artifact path, the receipt image) leaves the machine only when
a provider and key are configured, so the tests pin the cases that must stay local or must do
nothing: disabled providers, a missing key, and any failure. Crew Chief, OpenAI and the image
renderer are fakes; no network, model or real image is involved.
"""

from __future__ import annotations

import builtins
import sys
from base64 import b64decode
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from intake import translation as tr
from intake.parse import Receipt


def receipt(**kw):
    fields = dict(
        filename="r.pdf",
        scan_date=date(2026, 1, 2),
        receipt_date=None,
        receipt_time=None,
        merchant="shop",
        total=1.0,
        raw_text="こんにちは 合計 100円",
        language="ja",
    )
    fields.update(kw)
    return Receipt(**fields)


# --- provider and need ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, "ollama"), ("", "ollama"), ("   ", "ollama"), (" OpenAI ", "openai"), ("NONE", "none")],
)
def test_provider_names_are_normalised_with_a_default(given, expected):
    assert tr.normalize_translation_provider(given) == expected


@pytest.mark.parametrize("value", ["none", "NONE", " off ", "false", "0", "disabled"])
def test_these_values_turn_translation_off(value):
    assert tr.translation_enabled(value) is False


@pytest.mark.parametrize("value", [None, "", "ollama", "openai", "anything-else"])
def test_blank_means_the_local_default_and_other_names_stay_enabled(value):
    assert tr.translation_enabled(value) is True


@pytest.mark.parametrize(
    ("language", "translated", "raw", "expected"),
    [
        ("ja", None, "text", True),
        (" JA ", "", "text", True),
        ("ja", "   ", "text", True),
        ("en", None, "text", False),
        ("EN", None, "text", False),
        (" Und ", None, "text", False),
        ("und", None, "text", False),
        ("", None, "text", False),
        (None, None, "text", False),
        ("ja", "already", "text", False),
        ("ja", None, "", False),
        ("ja", None, "   ", False),
        ("ja", None, None, False),
    ],
)
def test_translation_is_needed_only_for_non_english_text_without_a_translation(
    language, translated, raw, expected
):
    assert (
        tr.needs_translation(language=language, translated_text=translated, raw_text=raw)
        is expected
    )


# --- sanitising model output ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Hello\nWorld", "Hello\nWorld"),
        ("  padded  ", "padded"),
        ("```text\nHello\n```", "Hello"),
        ("```\nHello\n```", "Hello"),
        ("Translation: Hello", "Hello"),
        ("english translation : Hello", "Hello"),
        ("TRANSLATION:Hello", "Hello"),
        ("Translation: Translation: x", "Translation: x"),
        ("The translation: not a prefix", "The translation: not a prefix"),
        ("", None),
        ("   ", None),
        (None, None),
        ("```\n```", None),
        ("Translation:", None),
    ],
)
def test_model_output_is_cleaned_of_fences_and_labels(given, expected):
    assert tr._sanitize_translation_output(given) == expected


def test_the_prompt_carries_the_language_hint_the_rules_and_the_trimmed_text():
    prompt = tr._translation_prompt("  line one\nline two  ", language=" JA ")
    assert "Source language hint: ja" in prompt
    assert prompt.endswith("line one\nline two")
    assert "Return only the translated text" in prompt
    assert "Keep all numbers" in prompt
    assert "Source language hint: unknown" in tr._translation_prompt("x", language=None)
    assert "Source language hint: ja" in tr._openai_artifact_translation_prompt(language="JA")
    assert "hint: unknown" in tr._openai_artifact_translation_prompt(language=None)


# --- fakes ---------------------------------------------------------------------------------


class FakeOpenAI:
    """A fake `openai` module recording its client construction and requests."""

    def __init__(self, content="Hello", error=None):
        self.content = content
        self.error = error
        self.keys = []
        self.requests = []
        outer = self

        class Client:
            def __init__(self, api_key):
                outer.keys.append(api_key)
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def create(self, **kwargs):
                outer.requests.append(kwargs)
                if outer.error:
                    raise outer.error
                message = SimpleNamespace(content=outer.content)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        self.OpenAI = Client


@pytest.fixture
def openai(monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake


@pytest.fixture
def no_openai(monkeypatch):
    class Forbidden:
        def __getattr__(self, name):
            # pytest.fail raises a BaseException, which the module's own `except Exception`
            # cannot swallow, so a stray call is always visible.
            pytest.fail("openai must not be used")

    monkeypatch.setitem(sys.modules, "openai", Forbidden())


class FakeCrewChief:
    instances = []

    def __init__(self, base_url, model, timeout):
        self.args = (base_url, model, timeout)
        self.prompts = []
        FakeCrewChief.instances.append(self)

    def generate(self, prompt):
        self.prompts.append(prompt)
        return FakeCrewChief.reply

    reply = "Hello"
    error = None


@pytest.fixture
def crew(monkeypatch):
    FakeCrewChief.instances = []
    FakeCrewChief.reply = "Hello"

    def generate(self, prompt):
        self.prompts.append(prompt)
        if FakeCrewChief.error:
            raise FakeCrewChief.error
        return FakeCrewChief.reply

    monkeypatch.setattr(FakeCrewChief, "generate", generate)
    FakeCrewChief.error = None
    monkeypatch.setattr(tr, "_load_crew_chief_client_class", lambda: FakeCrewChief)
    return FakeCrewChief


# --- text translation ----------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["none", "off", "false", "0", "disabled"])
def test_a_disabled_provider_translates_nothing_and_contacts_nothing(provider, crew, no_openai):
    assert tr.translate_receipt_text("こんにちは", language="ja", provider=provider) == (None, None)
    assert crew.instances == []


def test_an_unknown_provider_translates_nothing(crew, no_openai):
    assert tr.translate_receipt_text("こんにちは", language="ja", provider="mystery") == (
        None,
        None,
    )
    assert crew.instances == []


def test_the_local_provider_sends_the_prompt_to_crew_chief_and_returns_metadata(crew):
    crew.reply = "```text\nHello, total 100 yen\n```"
    text, meta = tr.translate_receipt_text(
        "こんにちは",
        language="ja",
        provider="OLLAMA",
        model="m1",
        base_url="http://crew:1",
        timeout_seconds=7,
    )
    assert text == "Hello, total 100 yen"
    assert meta == {"provider": "ollama", "model": "m1"}
    client = crew.instances[0]
    assert client.args == ("http://crew:1", "m1", 7)
    assert "こんにちは" in client.prompts[0]


@pytest.mark.parametrize("reply", ["", "   ", None])
def test_an_empty_reply_is_no_translation(crew, reply):
    crew.reply = reply
    assert tr.translate_receipt_text("こんにちは", language="ja") == (None, None)


def test_a_reply_identical_to_the_source_is_no_translation(crew):
    crew.reply = "  already english  "
    assert tr.translate_receipt_text("already english\n", language="ja") == (None, None)


def test_any_failure_means_no_translation_not_an_error(crew):
    crew.error = ConnectionError("crew-chief is down")
    assert tr.translate_receipt_text("こんにちは", language="ja") == (None, None)


def test_the_openai_provider_without_a_key_makes_no_request_and_translates_locally(no_openai, crew):
    crew.reply = "Hello, total 100 yen"
    for key in (None, ""):
        text, meta = tr.translate_receipt_text(
            "こんにちは",
            language="ja",
            provider="openai",
            openai_api_key=key,
            local_model="local-m",
        )
        assert text == "Hello, total 100 yen"
        assert meta == {"provider": "ollama", "model": "local-m", "fallback_from": "openai"}
    assert crew.instances[0].args[1] == "local-m"  # the local model, not the OpenAI model name


def test_the_openai_provider_sends_one_bounded_request(openai):
    openai.content = "Translation: Hello"
    text, meta = tr.translate_receipt_text(
        "こんにちは", language="ja", provider="openai", model="gpt-x", openai_api_key="sk-1"
    )
    assert (text, meta) == ("Hello", {"provider": "openai", "model": "gpt-x"})
    assert openai.keys == ["sk-1"]
    (request,) = openai.requests
    assert request["model"] == "gpt-x"
    assert request["max_tokens"] == 900
    assert request["messages"][0]["role"] == "user"
    assert "こんにちは" in request["messages"][0]["content"]


@pytest.mark.parametrize("failure", ["error", "none", "blank", "identical"])
def test_an_openai_failure_falls_back_to_the_local_model(openai, crew, failure):
    if failure == "error":
        openai.error = RuntimeError("rate limited")
    elif failure == "none":
        openai.content = None
    elif failure == "blank":
        openai.content = "   "
    else:
        openai.content = "こんにちは"  # came back untranslated
    crew.reply = "Hello"
    text, meta = tr.translate_receipt_text(
        "こんにちは", language="ja", provider="openai", openai_api_key="sk", model="gpt-x",
        base_url="http://c:1", timeout_seconds=9, local_model="local-m",
    )  # fmt: skip
    assert (text, meta) == (
        "Hello",
        {"provider": "ollama", "model": "local-m", "fallback_from": "openai"},
    )
    assert crew.instances[0].args == ("http://c:1", "local-m", 9)


def test_a_working_openai_translation_never_touches_the_local_model(openai, crew):
    openai.content = "Hello"
    text, meta = tr.translate_receipt_text(
        "こんにちは", language="ja", provider="openai", openai_api_key="sk"
    )
    assert text == "Hello" and meta["provider"] == "openai" and "fallback_from" not in meta
    assert crew.instances == []


def test_when_the_local_model_fails_too_there_is_no_translation(openai, crew):
    openai.error = RuntimeError("down")
    for failure in (RuntimeError("crew-chief down"), None):
        crew.error = failure
        crew.reply = "" if failure is None else crew.reply
        assert tr.translate_receipt_text(
            "こんにちは", language="ja", provider="openai", openai_api_key="sk"
        ) == (None, None)
    crew.error = None
    crew.reply = "こんにちは"  # untranslated by the local model as well
    assert tr.translate_receipt_text(
        "こんにちは", language="ja", provider="openai", openai_api_key="sk"
    ) == (None, None)


def test_the_local_provider_failing_is_not_retried_or_sent_elsewhere(openai, crew):
    crew.error = RuntimeError("down")
    assert tr.translate_receipt_text("こんにちは", language="ja", provider="ollama") == (None, None)
    assert len(crew.instances) == 1  # one attempt, no second pass
    assert openai.requests == []


@pytest.mark.parametrize("provider", ["none", "off", "disabled", "mystery"])
def test_a_disabled_or_unknown_provider_never_falls_back(provider, no_openai, crew):
    assert tr.translate_receipt_text("こんにちは", language="ja", provider=provider) == (None, None)
    assert crew.instances == []


# --- translating a whole receipt -----------------------------------------------------------


def test_an_english_or_translated_receipt_is_returned_untouched(crew):
    english = receipt(language="en")
    done = receipt(translated_text="already")
    assert tr.maybe_translate_receipt(english) is english
    assert tr.maybe_translate_receipt(done) is done
    assert crew.instances == []


def test_a_failed_translation_returns_the_same_receipt(crew):
    crew.reply = ""
    original = receipt()
    assert tr.maybe_translate_receipt(original) is original


def test_a_translated_copy_is_returned_and_the_original_is_left_alone(crew):
    crew.reply = "Hello, total 100 yen"
    original = receipt()
    updated = tr.maybe_translate_receipt(original, model="m1", base_url="http://c:1")
    assert updated is not original
    assert updated.translated_text == "Hello, total 100 yen"
    assert original.translated_text is None
    assert updated.raw_text == original.raw_text and updated.merchant == original.merchant
    assert updated._debug_translation == {"provider": "ollama", "model": "m1"}


def test_debug_attempts_are_carried_over_as_a_copy(crew):
    original = receipt()
    original._debug_ai_attempts = [{"model": "a"}]
    updated = tr.maybe_translate_receipt(original)
    assert updated._debug_ai_attempts == [{"model": "a"}]
    assert updated._debug_ai_attempts is not original._debug_ai_attempts

    plain = tr.maybe_translate_receipt(receipt())
    assert not hasattr(plain, "_debug_ai_attempts")

    odd = receipt()
    odd._debug_ai_attempts = "not a list"
    assert not hasattr(tr.maybe_translate_receipt(odd), "_debug_ai_attempts")


def test_the_whole_receipt_path_translates_locally_when_openai_is_configured_without_a_key(
    no_openai, crew
):
    crew.reply = "Hello, total 100 yen"
    updated = tr.maybe_translate_receipt(
        receipt(), provider="openai", model="gpt-x", local_model="local-m"
    )
    assert updated.translated_text == "Hello, total 100 yen"
    assert updated._debug_translation == {
        "provider": "ollama",
        "model": "local-m",
        "fallback_from": "openai",
    }
    assert crew.instances[0].args[1] == "local-m"


def test_the_local_model_for_the_whole_receipt_path_defaults_to_the_local_default(no_openai, crew):
    tr.maybe_translate_receipt(receipt(), provider="openai", model="gpt-x")
    assert crew.instances[0].args[1] == tr.DEFAULT_TRANSLATION_MODEL


# --- translating a receipt file ------------------------------------------------------------


class FakeImage:
    def save(self, buf, format):
        assert format == "PNG"
        buf.write(b"\x89PNG-bytes")


@pytest.fixture
def preview(monkeypatch):
    calls = []

    def render(path, *, resolution):
        calls.append((path, resolution))
        return FakeImage()

    monkeypatch.setattr("intake.ocr.render_preview_image", render)
    return calls


def test_a_disabled_provider_does_not_touch_the_file(monkeypatch):
    monkeypatch.setattr("intake.ocr.extract_text", lambda p: pytest.fail("must not OCR"))
    assert tr.translate_receipt_artifact(Path("r.pdf"), provider="none") == (None, None)


def test_the_local_provider_runs_ocr_when_no_text_is_given_then_translates(monkeypatch, crew):
    seen = {}
    monkeypatch.setattr(
        "intake.ocr.extract_text", lambda p: seen.setdefault("p", p) and "こんにちは"
    )
    crew.reply = "Hello"
    text, meta = tr.translate_receipt_artifact(
        Path("r.pdf"), language="ja", model="m1", base_url="http://c:1", timeout_seconds=9
    )
    assert (text, meta) == ("Hello", {"provider": "ollama", "model": "m1"})
    assert seen["p"] == Path("r.pdf")
    assert crew.instances[0].args == ("http://c:1", "m1", 9)


def test_the_local_provider_reuses_text_that_was_already_read(monkeypatch, crew):
    monkeypatch.setattr("intake.ocr.extract_text", lambda p: pytest.fail("must not OCR again"))
    assert tr.translate_receipt_artifact(Path("r.pdf"), raw_text="こんにちは", language="ja")[0]


def test_the_openai_artifact_path_without_a_key_sends_no_image_and_translates_locally(
    no_openai, preview, crew
):
    crew.reply = "Hello"
    text, meta = tr.translate_receipt_artifact(
        Path("r.pdf"), raw_text="こんにちは", language="ja", provider="openai", openai_api_key=None,
        local_model="local-m",
    )  # fmt: skip
    assert text == "Hello"
    assert meta == {
        "provider": "ollama",
        "model": "local-m",
        "fallback_from": "openai",
        "source": "local-ocr",
    }
    assert preview == []  # the image is never rendered or sent


def test_the_openai_artifact_request_carries_the_prompt_bounded_ocr_text_and_image(openai, preview):
    openai.content = "Fully translated"
    text, meta = tr.translate_receipt_artifact(
        Path("r.jpg"),
        raw_text="x" * 3000,
        language="ja",
        provider="openai",
        model="gpt-y",
        openai_api_key="sk-2",
    )
    assert text == "Fully translated"
    assert meta == {"provider": "openai", "model": "gpt-y", "source": "artifact"}
    assert preview == [(Path("r.jpg"), 150)]
    (request,) = openai.requests
    assert request["model"] == "gpt-y" and request["max_tokens"] == 1200
    parts = request["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text", "text", "image_url"]
    assert "Translate this receipt fully into English" in parts[0]["text"]
    assert parts[1]["text"] == "OCR text (may contain noise):\n" + "x" * 2000
    url = parts[2]["image_url"]
    assert url["detail"] == "high"
    assert url["url"].startswith("data:image/png;base64,")
    assert b64decode(url["url"].split(",", 1)[1]) == b"\x89PNG-bytes"


def test_blank_ocr_text_is_not_added_to_the_artifact_request(openai, preview):
    tr.translate_receipt_artifact(
        Path("r.jpg"), raw_text="   ", provider="openai", openai_api_key="sk"
    )
    tr.translate_receipt_artifact(Path("r.jpg"), provider="openai", openai_api_key="sk")
    for request in openai.requests:
        assert [p["type"] for p in request["messages"][0]["content"]] == ["text", "image_url"]


@pytest.mark.parametrize("failure", ["error", "blank"])
def test_an_openai_artifact_failure_falls_back_to_local_ocr_and_the_local_model(
    openai, preview, crew, monkeypatch, failure
):
    if failure == "error":
        openai.error = RuntimeError("boom")
    else:
        openai.content = "  "
    read = []
    monkeypatch.setattr("intake.ocr.extract_text", lambda p: read.append(p) or "こんにちは")
    crew.reply = "Hello"
    text, meta = tr.translate_receipt_artifact(
        Path("r.jpg"), provider="openai", openai_api_key="sk", language="ja", local_model="local-m"
    )
    assert text == "Hello"
    assert meta == {
        "provider": "ollama",
        "model": "local-m",
        "fallback_from": "openai",
        "source": "local-ocr",
    }
    assert read == [Path("r.jpg")]  # the receipt was read again with local OCR


def test_text_that_was_already_read_is_not_read_again_for_the_fallback(
    openai, preview, crew, monkeypatch
):
    openai.error = RuntimeError("boom")
    monkeypatch.setattr("intake.ocr.extract_text", lambda p: pytest.fail("must not OCR again"))
    crew.reply = "Hello"
    text, _ = tr.translate_receipt_artifact(
        Path("r.jpg"), raw_text="こんにちは", provider="openai", openai_api_key="sk"
    )
    assert text == "Hello"


def test_a_working_artifact_translation_never_reads_or_translates_locally(
    openai, preview, crew, monkeypatch
):
    openai.content = "Fully translated"
    monkeypatch.setattr("intake.ocr.extract_text", lambda p: pytest.fail("must not OCR"))
    text, meta = tr.translate_receipt_artifact(
        Path("r.jpg"), provider="openai", openai_api_key="sk"
    )
    assert (
        text == "Fully translated" and meta["source"] == "artifact" and "fallback_from" not in meta
    )
    assert crew.instances == []


def test_with_no_local_text_or_no_local_translation_the_artifact_is_left_untranslated(
    openai, preview, crew, monkeypatch
):
    openai.error = RuntimeError("boom")
    monkeypatch.setattr("intake.ocr.extract_text", lambda p: "   ")
    assert tr.translate_receipt_artifact(Path("r.jpg"), provider="openai", openai_api_key="sk") == (
        None,
        None,
    )

    def broken(path):
        raise ValueError("unsupported")

    monkeypatch.setattr("intake.ocr.extract_text", broken)
    assert tr.translate_receipt_artifact(Path("r.jpg"), provider="openai", openai_api_key="sk") == (
        None,
        None,
    )

    monkeypatch.setattr("intake.ocr.extract_text", lambda p: "こんにちは")
    crew.error = RuntimeError("crew-chief down")
    assert tr.translate_receipt_artifact(Path("r.jpg"), provider="openai", openai_api_key="sk") == (
        None,
        None,
    )
    assert crew.instances and openai.requests  # both were tried


# --- finding the Crew Chief client ---------------------------------------------------------


@pytest.fixture
def clean_crew_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "crew_chief", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield
    sys.modules.pop("crew_chief", None)


def block_import(monkeypatch, name, raises):
    real = builtins.__import__

    def fake(modname, *args, **kwargs):
        if modname == name:
            raise raises
        return real(modname, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_an_installed_crew_chief_is_used(monkeypatch, clean_crew_import):
    module = SimpleNamespace(CrewChiefClient=FakeCrewChief)
    monkeypatch.setitem(sys.modules, "crew_chief", module)
    assert tr._load_crew_chief_client_class() is FakeCrewChief


def test_a_missing_crew_chief_with_no_sibling_checkout_is_reported(
    monkeypatch, tmp_path, clean_crew_import
):
    monkeypatch.setattr(tr, "__file__", str(tmp_path / "a" / "b" / "translation.py"))
    block_import(monkeypatch, "crew_chief", ModuleNotFoundError("no", name="crew_chief"))
    with pytest.raises(ModuleNotFoundError) as info:
        tr._load_crew_chief_client_class()
    assert str(info.value) == "crew_chief"


def test_a_sibling_crew_chief_checkout_is_added_to_the_path_and_used(
    monkeypatch, tmp_path, clean_crew_import
):
    package = tmp_path / "crew-chief" / "src" / "crew_chief"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("class CrewChiefClient:\n    marker = 'sibling'\n")
    (tmp_path / "a" / "b").mkdir(parents=True)
    monkeypatch.setattr(tr, "__file__", str(tmp_path / "a" / "b" / "translation.py"))

    real = builtins.__import__
    attempts = []

    def first_attempt_fails(modname, *args, **kwargs):
        if modname == "crew_chief" and not attempts:
            attempts.append(1)
            raise ModuleNotFoundError("no", name="crew_chief")
        return real(modname, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", first_attempt_fails)
    client_class = tr._load_crew_chief_client_class()
    assert client_class.marker == "sibling"
    assert str(tmp_path / "crew-chief" / "src") in sys.path


def test_a_different_missing_module_is_not_hidden(monkeypatch, clean_crew_import):
    block_import(monkeypatch, "crew_chief", ModuleNotFoundError("no", name="requests"))
    with pytest.raises(ModuleNotFoundError) as info:
        tr._load_crew_chief_client_class()
    assert info.value.name == "requests"
