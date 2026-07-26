"""Turkish-aware recall tokenizer — expand a question into SQL search terms.

Ported verbatim-in-spirit from ``akana_server.memory.recall_format``
(the chat-reply formatter is dropped — only the proven term expansion survives).
Pure ``re``; no project imports. Both :class:`SemanticStore.search` and the
:class:`Recall` engine tokenize through here so episodic and semantic search
agree on what a query "means".

The expansion is deliberately generous (recall favours recall): it keeps the raw
query, a boilerplate-stripped form, a rough possessive stem (``adım`` → ``ad``,
meaning "my name" → "name"), the subject of "… ne?" ("what is …?") questions,
and a few hand-tuned key aliases (``ad`` ↔ ``isim`` ↔ ``name``).

It is also the home of the shared bilingual :data:`STOPWORDS` set — vocabulary
belongs in one module, and the episodic FTS query builder needs the same list.
"""

from __future__ import annotations

import re
import unicodedata

_RECALL_PREFIXES = (
    "ne hatırlıyorsun",
    "ne hatırlıyorsunuz",
    "hatırlıyor musun",
    "hatırlıyor musunuz",
    "hafızanda ne var",
    "hafızanda neler",
    "dün ne konuş",
    "dün ne demiş",
    "geçen hafta ne",
    "what do you remember",
    "do you remember",
)
_STOP = frozenset(
    {
        "ne", "mi", "mı", "mu", "mü", "bir", "bu", "şu", "benim", "ben",
        "the", "about", "for", "ile", "için", "hakkında",
    }
)

#: Function words that carry no retrieval signal, in BOTH shipped languages.
#: Kept separate from ``_STOP`` (which shapes the LIKE term expansion and is
#: calibrated against the recall gold-set) because it serves a different job:
#: episodic FTS ORs its tokens together, so a single stopword in the query
#: matches every turn that happens to contain "how"/"do"/"my" — an off-topic
#: question then recalls unrelated private turns. Content words only here; an
#: over-wide set silently deletes recall, which is the worse failure.
STOPWORDS = _STOP | frozenset(
    {
        # English
        "a", "an", "am", "and", "any", "are", "as", "at", "be", "been", "being",
        "but", "by", "can", "could", "did", "do", "does", "from", "had", "has",
        "have", "he", "her", "hers", "him", "his", "how", "i", "if", "in", "into",
        "is", "it", "its", "may", "me", "might", "must", "my", "no", "not", "of",
        "on", "or", "our", "ours", "shall", "she", "should", "so", "some", "than",
        "that", "the", "their", "theirs", "them", "then", "there", "these", "they",
        "this", "those", "to", "was", "we", "were", "what", "when", "where",
        "which", "while", "who", "whom", "why", "will", "with", "would", "you",
        "your", "yours",
        # Turkish
        "ama", "bana", "bize", "biz", "da", "de", "daha", "den", "dan", "gibi",
        "hangi", "ise", "ki", "kim", "kime", "kimi", "nasil", "nasıl", "neden",
        "nedir", "neydi", "niye", "o", "onlar", "onu", "sen", "siz", "ve", "veya",
        "ya", "çok", "cok",
    }
)

# Longest suffixes first (Turkish possessive / plural / accusative — must be ordered longest-first).
_POSSESSIVE_SUFFIXES = (
    "larımız", "lerimiz", "larım", "lerim", "ları", "leri",
    "ımız", "imiz", "umuz", "ümüz", "ınız", "iniz", "unuz", "ünüz",
    "ım", "im", "um", "üm", "ın", "in", "un", "ün",
    # 3rd-person-singular possessive / accusative — after a vowel (arabası→araba meaning "his/her car", arabayı→araba)
    "sı", "si", "su", "sü", "yı", "yi", "yu", "yü",
    # accusative / post-consonant possessive + vowel-stem possessive (editörü→editör "editor",
    # takımı→takım "team", arabam→araba "my car", kedim→kedi "my cat"). Single letter: stem ≥2 (len guard) +
    # the raw term is always kept → over-stemming yields at worst a harmless extra term.
    "ı", "i", "u", "ü", "m",
)

# Extra semantic keys to try when the user asks about a subject (adım → ad → isim).
_SUBJECT_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "ad": ("ad", "isim", "name", "adı", "adım", "ismim"),
    "adı": ("ad", "isim", "name", "adı"),
    "adım": ("ad", "isim", "name", "adı"),
    "isim": ("isim", "name", "ad", "ismim"),
    "ismim": ("isim", "name", "ad"),
    "name": ("name", "ad", "isim"),
}

_KNOWN_STEMS: dict[str, str] = {
    "adım": "ad",
    "adim": "ad",
    "ismim": "isim",
    "isim": "isim",
}

_WORD_RE = re.compile(r"[\wğüşöçıİĞÜŞÖÇ]+", flags=re.IGNORECASE)
_SUBJECT_NE_RE = re.compile(
    r"(?:benim\s+)?([\wğüşöçıİĞÜŞÖÇ]+)\s+ne\s*\??\s*$", flags=re.IGNORECASE
)


def turkish_possessive_stem(word: str) -> str:
    """Rough stem for recall keys: ``adım`` → ``ad`` ("my name" → "name"), ``ismim`` → ``isim``."""
    w = (word or "").strip().lower()
    if w in _KNOWN_STEMS:
        return _KNOWN_STEMS[w]
    if len(w) < 3:
        return w
    for suf in _POSSESSIVE_SUFFIXES:
        if w.endswith(suf) and len(w) > len(suf) + 1:
            return w[: -len(suf)]
    return w


def normalize_recall_query(query: str) -> str:
    """Strip recall boilerplate so search targets the subject (e.g. a nickname)."""
    q = query.strip()
    # Length-preserving fold (İ→i, I→ı then lower, NO NFKC): indexes into `low`
    # must map 1:1 back onto `q`. Plain q.lower() shifts by one per leading 'İ'
    # ("İ".lower() → 2 chars), slicing q mid-word; fold_text is out (NFKC can
    # change length on compat chars). re.IGNORECASE is also out — it does not
    # Turkish-fold, so an 'İ'-cased prefix would not match.
    low = q.replace("İ", "i").replace("I", "ı").lower()
    for prefix in _RECALL_PREFIXES:
        if prefix in low:
            idx = low.find(prefix)
            q = q[idx + len(prefix) :].strip(" ?.:,—-")
            break
    q = re.sub(r"^(ne|what)\s+", "", q, flags=re.IGNORECASE).strip(" ?.:,")
    m = _SUBJECT_NE_RE.search(query.strip().lower())
    if m:
        stem = turkish_possessive_stem(m.group(1))
        if stem:
            return stem
    if len(q) >= 2:
        stem = turkish_possessive_stem(q)
        return stem if stem else q
    words = [w for w in _WORD_RE.findall(query) if len(w) >= 2 and w.lower() not in _STOP]
    if words:
        last = words[-1]
        return turkish_possessive_stem(last) or last
    return query.strip()


_MAX_TERMS = 12  # each term is a separate LIKE scan — cap the LLM-controlled blowup
_MAX_TERM_LEN = 80


def escape_like(term: str) -> str:
    """Make LLM-controlled text literal inside a ``LIKE`` pattern (%/_ jokers)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fold_text(text: str) -> str:
    """Turkish-aware case fold for matching/dedup keys.

    Python's locale-blind ``lower()`` mangles Turkish: ``"İ".lower()`` yields
    ``i + U+0307`` (combining dot) and ``"I".lower()`` yields ``i`` instead of
    ``ı`` — so ``"İzmir" vs "İZMİR"`` miss dedup and ``"IŞIK" vs "ışık"`` ("light")
    miss conflict grouping. Fold: NFKC → İ→i, I→ı → lower → strip stray combining
    dots. Use this everywhere keys/values are compared, never bare ``lower()``.
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = s.replace("İ", "i").replace("I", "ı")
    s = s.lower()
    return s.replace("i̇", "i")


def recall_search_terms(query: str) -> list[str]:
    """Expand a user question into ordered SQL search terms (keys + values).

    Capped at ``_MAX_TERMS`` terms of ``_MAX_TERM_LEN`` chars: each term costs a
    table scan, and tool queries are model-controlled input.
    """
    raw = (query or "").strip()
    if not raw:
        return []
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        t = term.strip()[:_MAX_TERM_LEN]
        if len(t) < 2:
            return
        low = t.lower()
        if low in seen:
            return
        seen.add(low)
        terms.append(t)

    add(raw)
    normalized = normalize_recall_query(raw)
    add(normalized)
    stem = turkish_possessive_stem(normalized)
    if stem:
        add(stem)
    m = _SUBJECT_NE_RE.search(raw.lower())
    if m:
        subject = m.group(1)
        add(subject)
        sub_stem = turkish_possessive_stem(subject)
        if sub_stem:
            add(sub_stem)
    for word in _WORD_RE.findall(raw):
        if len(word) < 3 or word.lower() in _STOP:
            continue
        add(word)
        wstem = turkish_possessive_stem(word)
        if wstem:
            add(wstem)
    for key in (stem, normalized, turkish_possessive_stem(normalized)):
        if not key:
            continue
        for alias in _SUBJECT_KEY_ALIASES.get(key.lower(), ()):
            add(alias)
    return terms[:_MAX_TERMS]
