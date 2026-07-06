"""Adversarial probes for the outbound sensitive-content filter.

``filter_outbound`` is the last line of defence before LLM output reaches a
connector (Telegram). The threat model: the model echoes a secret /
PII verbatim. Each test asserts the security invariant — the raw secret must NOT
survive in the output — across token shapes, multi-secret messages, separators,
and the entropy/Luhn gates. Failures here are real leakage bugs.
"""

from __future__ import annotations

import pytest

from akana_server.connectors.egress_filter import REDACTION, filter_outbound


def _leaks(secret: str, text: str) -> bool:
    """True if any non-trivial run of the secret survived in the filtered output."""
    return secret in filter_outbound(text).text


# --------------------------------------------------------------------------- #
# Provider token shapes — historically "confirmed leak" cases (see source).   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "secret, expected_id",
    [
        ("sk-" + "A" * 32, "credential.token_shape"),
        ("ghp_" + "B" * 36, "credential.token_shape"),
        ("gho_" + "C" * 36, "credential.token_shape"),
        ("xoxb-123456789012-abcdEFGH", "credential.token_shape"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "credential.token_shape",
        ),
        ("AKIA" + "Q" * 16, "credential.aws_access_key_id"),
    ],
)
def test_provider_token_shapes_redacted(secret: str, expected_id: str) -> None:
    # Neutral carrier: avoid credential keywords (token/secret/key...) so we exercise
    # the SHAPE rule in isolation, not the broader assignment rule.
    res = filter_outbound(f"oops {secret} done")
    assert secret not in res.text
    assert REDACTION in res.text
    assert expected_id in res.matched


# --------------------------------------------------------------------------- #
# Multi-secret in one message — every secret must be removed + reported.       #
# --------------------------------------------------------------------------- #


def test_multiple_secrets_all_redacted() -> None:
    text = "api_key=FIRSTsecretValue\npassword=SECONDsecretValue"
    res = filter_outbound(text)
    assert "FIRSTsecretValue" not in res.text
    assert "SECONDsecretValue" not in res.text


def test_mixed_pii_card_and_iban_both_removed() -> None:
    text = "Kart 4111 1111 1111 1111 ve IBAN TR33 0006 1005 1978 6457 8413 26"
    res = filter_outbound(text)
    # no 4-digit card group and no IBAN digit group should survive
    assert "4111" not in res.text
    assert "6457" not in res.text
    assert {"payment.card", "payment.iban"} <= set(res.matched)


# --------------------------------------------------------------------------- #
# IBAN — must be redacted in FULL (spaced form), not partially.               #
# --------------------------------------------------------------------------- #


def test_spaced_iban_fully_redacted() -> None:
    res = filter_outbound("Hesap: TR33 0006 1005 1978 6457 8413 26 buyrun")
    for group in ("TR33", "0006", "1005", "1978", "6457", "8413"):
        assert group not in res.text
    assert "payment.iban" in res.matched


# --------------------------------------------------------------------------- #
# Card — separators + Luhn gate (positive) and non-card passthrough (gate).    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "card",
    [
        "4111111111111111",       # contiguous
        "4111-1111-1111-1111",    # hyphen
        "4111.1111.1111.1111",    # dot
        "4111 1111-1111.1111",    # mixed separators
    ],
)
def test_luhn_valid_card_redacted_regardless_of_separator(card: str) -> None:
    res = filter_outbound(f"ödeme {card} ile")
    assert "1111" not in res.text
    assert "payment.card" in res.matched


def test_non_luhn_digit_run_is_left_intact() -> None:
    """Documented design: the Luhn gate keeps random long digit strings (order ids,
    tracking numbers) from being masked. A non-card 16-digit run survives."""
    res = filter_outbound("Sipariş no 1111 1111 1111 1111 teşekkürler")
    assert "payment.card" not in res.matched


# --------------------------------------------------------------------------- #
# Generic high-entropy gate — true secrets in, ordinary text out.             #
# --------------------------------------------------------------------------- #


def test_sha256_hex_digest_redacted() -> None:
    digest = "a" * 31 + "b1c2d3e4f5"  # 41 hex chars, mixed → looks random
    res = filter_outbound(f"hash {digest} bitti")
    assert digest not in res.text
    assert "credential.high_entropy" in res.matched


@pytest.mark.parametrize(
    "innocuous",
    [
        "z" * 50,                                     # single repeated char
        "thequickbrownfoxjumpsoverthelazydoghello",   # 40 alpha, no digit, non-hex
        "550e8400-e29b-41d4-a716-446655440000",        # a UUID (36 chars, hyphen-split)
    ],
)
def test_high_entropy_gate_does_not_mask_ordinary_text(innocuous: str) -> None:
    res = filter_outbound(f"değer: {innocuous} son")
    assert innocuous in res.text
    assert "credential.high_entropy" not in res.matched


@pytest.mark.parametrize(
    "url",
    [
        # A GitHub commit URL: the slash-joined host+path is > 40 chars.
        "https://github.com/MuhittinYilmazer/akana/commit/9a58188f1ebffc3014d0a5e614d9",
        # A Google Docs URL whose document id alone is a long mixed-case token.
        "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
        # A long POSIX file path.
        "/home/user/projects/akana/some/very/long/path/to/a/file/name.txt",
    ],
)
def test_high_entropy_gate_does_not_mask_urls_or_paths(url: str) -> None:
    """Regression: the base64 alternation used to include ``/``, so ``\\b`` started the
    match at a URL host and the whole slash-joined path was redacted — corrupting the
    sent message AND (via the router's persisted archive) the web-UI record. Ordinary
    links/paths must survive filter_outbound untouched."""
    res = filter_outbound(f"see {url} thanks")
    assert url in res.text
    assert "credential.high_entropy" not in res.matched


# --------------------------------------------------------------------------- #
# Assignment / label rules — case-insensitive, end-of-line, next-line value.   #
# --------------------------------------------------------------------------- #


def test_assignment_is_case_insensitive() -> None:
    res = filter_outbound("API_KEY = hunter2LongSecretValue")
    assert "hunter2LongSecretValue" not in res.text


def test_multiword_secret_value_fully_masked() -> None:
    """Regression: a multi-word value must be masked to end-of-line, not just token 1."""
    res = filter_outbound("parola: correct horse battery staple")
    assert "horse" not in res.text
    assert "staple" not in res.text


def test_label_over_next_line_value_masked() -> None:
    res = filter_outbound("Your password is:\nABCdef1234567890longvalue\nNormal next line.")
    assert "ABCdef1234567890longvalue" not in res.text
    assert "Normal next line." in res.text  # only the value line is crossed


def test_authorization_header_scheme_and_token_masked() -> None:
    res = filter_outbound("Authorization: Bearer abcDEF123456ghiJKL")
    assert "abcDEF123456ghiJKL" not in res.text


def test_bare_bearer_token_masked_but_short_word_kept() -> None:
    masked = filter_outbound("token is Bearer abcDEF123456ghiJKL789")
    assert "abcDEF123456ghiJKL789" not in masked.text
    # the short English word "bearer" in prose is not a secret
    kept = filter_outbound("he is the bearer of good news")
    assert "good news" in kept.text


def test_otp_code_redacted() -> None:
    res = filter_outbound("Your verification code: 482913 expires soon")
    assert "482913" not in res.text
    assert "otp.code" in res.matched


# --------------------------------------------------------------------------- #
# Private key — truncated (no END marker) must mask through end-of-string.     #
# --------------------------------------------------------------------------- #


def test_truncated_private_key_masked_to_eos() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAsecretKeyMaterialLine1\n"
        "moreSecretKeyMaterialLine2andLine3plus"
    )
    res = filter_outbound(text)
    assert "secretKeyMaterial" not in res.text
    assert "moreSecretKeyMaterial" not in res.text
    assert "credential.private_key" in res.matched


# --------------------------------------------------------------------------- #
# Robustness — no crash on None/empty/clean input; clean text untouched.       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [None, "", "   ", "Yarın saat 14:00 buluşalım."])
def test_clean_or_empty_input_is_safe(value) -> None:
    res = filter_outbound(value)
    assert res.matched == ()
    if value:
        assert res.text == value


def test_redaction_marker_itself_is_not_re_redacted() -> None:
    # feeding already-redacted text must be stable (idempotent-ish)
    once = filter_outbound("password: secretvalue123").text
    twice = filter_outbound(once).text
    assert once == twice


# --------------------------------------------------------------------------- #
# Batch A regressions — JSON-quoted creds, long OTP, Telegram bot token.        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "blob, secret",
    [
        ('{"password": "S3cretAccountPass"}', "S3cretAccountPass"),
        ('{"api_key":"sk_live_ABCdef123456"}', "sk_live_ABCdef123456"),
        ('{ "api_key" : "TokenValueABC123" }', "TokenValueABC123"),
        ("{'password': 'singleQuoted99'}", "singleQuoted99"),
    ],
)
def test_json_quoted_credentials_redacted(blob: str, secret: str) -> None:
    """Models commonly echo secrets as JSON/dict; the closing key-quote used to break the
    keyword→separator anchor so the value leaked verbatim."""
    res = filter_outbound(blob)
    assert secret not in res.text
    assert "credential.assignment" in res.matched


@pytest.mark.parametrize("code", ["123456789", "123456789012"])
def test_long_otp_fully_masked(code: str) -> None:
    """OTP rule used to cap at 8 digits, leaking the trailing digits of 9+ digit codes."""
    res = filter_outbound(f"your verification code is {code} thanks")
    assert not any(seg and seg in res.text for seg in (code, code[:8], code[-4:]))
    assert "otp.code" in res.matched


def test_telegram_bot_token_redacted() -> None:
    """The Telegram connector's most critical credential; the colon-split shape escaped
    every prior rule."""
    token = "8123456789:AAH" + "x" * 32  # 10-digit bot id + ':' + 35-char secret
    res = filter_outbound(f"the bot is {token} now")
    assert token not in res.text
    assert "credential.telegram_bot_token" in res.matched
