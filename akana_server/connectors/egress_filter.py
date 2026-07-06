"""ConnectorEngine F0 — outbound message sensitive-content filter.

F0 implementation of the sensitive content filter (F4.3 in the vision plan):
patterns are **data** (``SENSITIVE_EGRESS_PATTERNS``) — adding a new pattern
is a list-append, not a code change. Matched spans are replaced with
:data:`REDACTION`; the rest of the message continues to be sent. Detections
are returned to the caller (which writes them to the audit log).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "REDACTION",
    "SENSITIVE_EGRESS_PATTERNS",
    "EgressFilterResult",
    "filter_outbound",
]

REDACTION = "[REDACTED]"

#: Pattern set — each entry is ``{"id", "pattern", "reason"}``; searched case-insensitively.
SENSITIVE_EGRESS_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "id": "credential.private_key",
        # If END is absent (truncated / streaming key) mask through to end-of-string (\Z).
        # The old pattern made the END part OPTIONAL → `?` matched zero times and only
        # the BEGIN header was masked, leaving ALL lines of key material exposed
        # (confirmed: "[REDACTED]\nMIIEowIBAAKCAQEA...secret"). The security filter is
        # fail-closed: hide everything from a suspicious BEGIN through to END OR end-of-string.
        "pattern": r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        "reason": "Private key material",
    },
    {
        "id": "credential.assignment",
        # Value is masked through to END-OF-LINE (``[^\n]+``), not just the first token.
        # The old pattern used ``\S+`` and hid only the FIRST token → a multi-word
        # secret/password (confirmed: "parola: correct horse battery staple") had only
        # "correct" masked and the rest leaked to the channel. Fail-closed:
        # when ``key/token/password = ...`` is seen, hide the ENTIRE remainder of that
        # line. After ``[:=]`` an OPTIONAL single newline is allowed before the value
        # (``\n?\s*``) so a label-over-value layout — "Your password is:\n<secret>"
        # (confirmed leak: the value sat on the NEXT line and escaped the SAME-line
        # ``[^\n]+``) — is also masked. Only one newline is crossed (the value line);
        # subsequent lines are untouched.
        "pattern": (
            r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|"
            # Allow an optional quote between the keyword and the separator so the JSON/dict
            # form ``{"password": "..."}`` is caught (the closing quote sat between the
            # keyword and ``:`` and broke the old anchor). \x22=" \x27=' (avoids delimiter
            # clash in this raw string). The value is still masked through end-of-line.
            r"password|parola|şifre|bearer)\b[\x22\x27]?\s*[:=]\s*[\x22\x27]?\n?[^\S\n]*[^\n]+"
        ),
        "reason": "Credential assignment (key/token/password = ...)",
    },
    {
        # Label OVER value, possibly with words between the label and the colon:
        # ``Your password is:\n<secret>`` (confirmed leak — the value is on the NEXT
        # line and the label carries connective words "is", so the SAME-line
        # ``credential.assignment`` above does not catch it). Matches a credential
        # label, up to 3 short connective words, an optional ``:``/``=``, then a
        # single newline and a high-entropy value token on the next line. To avoid
        # masking innocent prose the next-line token must look secret-ish: it
        # contains a digit OR is at least 12 characters long. The whole span
        # (label + value) is redacted (fail-closed); only the value line is crossed.
        "id": "credential.label_value",
        "pattern": (
            r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|"
            r"password|parola|şifre|passphrase|bearer)\b"
            r"(?:[^\S\n]+\w+){0,3}[^\S\n]*[:=]?[^\S\n]*\n[^\S\n]*"
            r"(?:[^\s]*\d[^\s]*|[^\s]{12,})"
        ),
        "reason": "Credential label over a high-entropy value on the next line",
    },
    {
        # ``Authorization: Bearer <token>`` / ``Proxy-Authorization: Basic ...``
        # — the value is carried by a space, not ``[:=]``, so the assignment pattern
        # does not catch ``Authorization``. Mask from the header name through to
        # end-of-line (scheme + token are both hidden).
        "id": "credential.auth_header",
        "pattern": r"\b(?:Proxy-)?Authorization\b\s*[:=]?\s*[^\n]+",
        "reason": "Authorization header (scheme + token)",
    },
    {
        # A bare ``Bearer <token>`` outside a header (in LLM response prose) —
        # the assignment pattern requires ``[:=]`` and misses this. Mask the token
        # body together with the scheme when it is at least 12 characters long
        # (the short word "bearer" in prose could be innocent; the threshold
        # reduces false positives).
        "id": "credential.bearer_token",
        "pattern": r"\bBearer\s+[A-Za-z0-9._\-]{12,}",
        "reason": "Bare Bearer token",
    },
    {
        # Provider-specific high-entropy token shapes — masked even WITHOUT an
        # assignment / header context (in case an LLM echoes a secret in plain text):
        # OpenAI ``sk-...``, GitHub ``ghp_/gho_/ghu_/ghs_/ghr_...``, Slack
        # ``xoxb-/xoxa-/xoxp-...``, and JWT ``eyJ...<.>...<.>...``.
        "id": "credential.token_shape",
        "pattern": (
            r"\bsk-[A-Za-z0-9_\-]{16,}"
            r"|\bgh[opusr]_[A-Za-z0-9]{20,}"
            r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
            r"|\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
        ),
        "reason": "High-entropy provider token shape",
    },
    {
        # Telegram bot token — ``<8-10 digit bot id>:<35-char secret>``. The most
        # security-critical credential for the Telegram connector; the colon-split shape
        # escapes the high-entropy rule. Masked unconditionally (the shape is specific).
        "id": "credential.telegram_bot_token",
        "pattern": r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b",
        "reason": "Telegram bot token",
    },
    {
        # AWS Access Key ID — a fixed, unambiguous shape: ``AKIA`` (or the ASIA/AGPA/
        # AIDA/AROA/ANPA… IAM-entity prefixes) followed by EXACTLY 16 upper/digit
        # chars. Confirmed leak: an LLM echoing ``AKIA<16>`` passed straight through
        # (no assignment/header context). Masked unconditionally — the shape is so
        # specific that false positives are effectively impossible.
        "id": "credential.aws_access_key_id",
        "pattern": r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|A3T[A-Z0-9])[A-Z0-9]{16}\b",
        "reason": "AWS access key id",
    },
    {
        # AWS Secret Access Key — 40-char base64 (``[A-Za-z0-9/+]``) carried by an
        # ``aws_secret_access_key = ...`` style assignment OR an explicit "secret"
        # label. A bare 40-char base64 blob is also covered by the gated
        # ``credential.high_entropy`` rule below; this entry catches the labelled
        # form where the value contains ``/`` or ``+`` (not a hex string).
        "id": "credential.aws_secret_key",
        "pattern": (
            r"\baws.{0,20}?(?:secret|key)\b[^\n]{0,20}?[:=]\s*[A-Za-z0-9/+]{40}\b"
            r"|\b[A-Za-z0-9/+]{40}\b(?=[^\n]{0,30}\baws\b)"
        ),
        "reason": "AWS secret access key",
    },
    {
        # Slack incoming-webhook URL — ``https://hooks.slack.com/services/T.../B.../<secret>``.
        # The trailing path segment is the secret; possession of the full URL lets
        # anyone post to the workspace. Confirmed leak: the URL passed through
        # untouched. Masked unconditionally (the host + path shape is unambiguous).
        "id": "credential.slack_webhook",
        "pattern": (
            r"https?://hooks\.slack\.com/services/"
            r"[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+"
        ),
        "reason": "Slack incoming-webhook URL",
    },
    {
        # Generic high-entropy blob — a bare ≥32-char hex OR ≥40-char base64 token
        # with NO surrounding context (an LLM echoing a raw secret/hash/key in
        # prose). This is the broadest rule, so it is gated by the
        # :func:`_high_entropy_ok` validator: the candidate must actually look
        # random (mixed case / digits, not a single repeated char, not all letters)
        # to keep ordinary long identifiers and repeated-character strings from
        # being masked. Hex secrets (32/40/64 chars: MD5/SHA1/SHA256, many API
        # secrets) and 40+ base64 blobs (e.g. AWS secret keys) are the target.
        "id": "credential.high_entropy",
        # A base64 secret blob (incl. AWS secret keys with `/`+`+`) starts with an
        # alnum/`+`/`_` char and is a single unbroken run. The OLD pattern
        # `\b[A-Za-z0-9/+_\-]{40,}\b` let `/` be a LEADING/joining char, so `\b`
        # started the match at a URL host and swallowed the whole slash-joined path
        # (github.com/owner/repo/commit/<sha>, docs.google.com/document/d/<id>/edit,
        # a POSIX path) as one ≥40-char "token" — redacting ordinary links in the sent
        # message AND (via the router's persisted archive) the web-UI record. The fix:
        # (1) the base64 run must START on an alnum/`+`/`_` (never `/`), so a URL path
        # segment beginning right after a `/` cannot anchor the match; (2) the leading
        # `(?<![./:@\w-])` refuses a candidate glued to a preceding `.`/`/`/`:`/`@`/word
        # char — i.e. a URL host label, path segment, scheme, or userinfo. Every alnum
        # segment inside a URL path is preceded by `/` or `.`, so all of them are
        # rejected, while a real secret after `=`/`{`/space still matches.
        "pattern": (
            r"(?<![./:@\w-])"
            r"(?:[0-9a-fA-F]{32,}|[A-Za-z0-9+_][A-Za-z0-9/+_\-]{39,})\b"
        ),
        "reason": "Bare high-entropy secret (hex/base64)",
        "validate": "high_entropy",
    },
    {
        "id": "otp.code",
        "pattern": (
            r"\b(?:otp|doğrulama kodu|onay kodu|verification code|one[- ]time|"
            r"tek kullanımlık)\b[^\d\n]{0,40}\d{4,}"
        ),
        "reason": "One-time code (OTP) pattern",
    },
    # IBAN must come BEFORE the card pattern: if the card pattern swallows the inner
    # 16 digits of a space-separated IBAN, partial leakage remains (IBAN prefix and
    # last group exposed). The most specific pattern must match first (confirmed by the
    # spaced-IBAN test). Pattern order IS data; moving these lines changes behaviour.
    {
        "id": "payment.iban",
        "pattern": r"\bTR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}\b|\bTR\d{24}\b",
        "reason": "IBAN pattern",
    },
    {
        "id": "payment.card",
        # The old pattern only caught 16-digit sequences separated by spaces/hyphens
        # → escapable with dots/tabs, missed 14/15/19-digit PANs, and also swallowed
        # every non-secret 16-digit sequence. Now the separator is ANY non-alphanumeric
        # character (``[^0-9A-Za-z]``), length 13–19 digits (card range). To reduce
        # false positives (random long digit strings) a Luhn mod-10 check via
        # ``validate`` is applied — candidates that fail are not masked.
        "pattern": r"\b\d(?:[^0-9A-Za-z]?\d){12,18}\b",
        "reason": "Card number pattern (13–19 digits, Luhn)",
        "validate": "luhn",
    },
)


def _luhn_ok(digits: str) -> bool:
    """Luhn (mod-10) check — validates a card number candidate.

    ``digits`` must contain only decimal digits (separators are stripped before
    calling). Returns ``False`` for sequences outside 13–19 digits or whose
    checksum does not match (eliminates false positives)."""
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    # Right to left; every digit at an even position (1-indexed) is doubled.
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _high_entropy_ok(candidate: str) -> bool:
    """Heuristic gate for the generic ≥32-char secret rule (reduce false positives).

    The pattern alone would mask long ordinary identifiers / repeated-character
    runs. A blob is treated as a secret only when it actually looks random:

    * a clean hex string of >=32 chars (MD5/SHA1/SHA256 + many API secrets) is
      accepted on its own — hex of this length in prose is almost always a digest
      or key; AND it must not be a single repeated character.
    * otherwise the blob must mix character classes — contain BOTH a letter and a
      digit (a typical base64/token secret) — and not be a single repeated char.

    Pure-alphabetic long words (no digits) are NOT masked, so ordinary prose and
    long identifiers like ``aaaaaaaa…`` survive."""
    s = candidate.strip()
    if len(s) < 32:
        return False
    if len(set(s)) <= 2:  # "aaaa…", "abab…" — not random
        return False
    is_hex = all(c in "0123456789abcdefABCDEF" for c in s)
    if is_hex:
        return True
    has_alpha = any(c.isalpha() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return has_alpha and has_digit


#: ``validate`` key → validator; if it does not approve the match, the span is not masked.
_VALIDATORS = {
    "luhn": lambda m: _luhn_ok("".join(ch for ch in m.group(0) if ch.isdigit())),
    "high_entropy": lambda m: _high_entropy_ok(m.group(0)),
}

#: ``(id, compiled, validator|None)`` — when a validator is present, a match is masked
#: only if the validator approves it (e.g. Luhn for cards). None means every match is masked.
_COMPILED: tuple[tuple[str, re.Pattern[str], Any], ...] = tuple(
    (
        str(p["id"]),
        re.compile(str(p["pattern"]), re.IGNORECASE),
        _VALIDATORS.get(str(p["validate"])) if p.get("validate") else None,
    )
    for p in SENSITIVE_EGRESS_PATTERNS
)


@dataclass(frozen=True, slots=True)
class EgressFilterResult:
    """Filter output: redacted text + triggered pattern ids."""

    text: str
    matched: tuple[str, ...] = ()

    @property
    def redacted(self) -> bool:
        return bool(self.matched)


def filter_outbound(text: str) -> EgressFilterResult:
    """Scan outbound text; mask sensitive spans and return the list of detections."""
    out = text or ""
    matched: list[str] = []
    for pattern_id, pattern, validator in _COMPILED:
        if validator is None:
            out, count = pattern.subn(REDACTION, out)
        else:
            # Pattern with a validator: only approved matches are masked; rejected
            # candidates are left as-is (no false-positive leakage).
            hits = 0

            def _sub(m: re.Match[str], _v: Any = validator) -> str:
                nonlocal hits
                if _v(m):
                    hits += 1
                    return REDACTION
                return m.group(0)

            out, _ = pattern.subn(_sub, out)
            count = hits
        if count:
            matched.append(pattern_id)
    return EgressFilterResult(text=out, matched=tuple(matched))
