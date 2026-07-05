"""Installed capability catalog — compact inventory appended to the system prompt (WI-2).

Problem: when asked "can you do X?", the agent only looks at the connected MCP
tools and never sees the installed skills/packs; for abbreviations where the
trigger does not match exactly ("tg", "telega", etc.) the WI-1 turn injection
also fails to fire, so the capability is missed.

Solution: a **compact** inventory derived from :meth:`SkillRegistry.list` is
appended to every chat turn's system prompt — title + triggers only (NOT the
SKILL.md body; the body arrives via WI-1 once the request matches). This way the
model knows the capability EXISTS and connects "tg" to Telegram by its own
reasoning.

Contract:

* If the registry is EMPTY the catalog returns "" → the system prompt does not
  change byte-for-byte (behavior-neutral: a block is only added when a capability
  is actually installed).
* The total size is bounded (the system prompt is NEVER trimmed for budget) —
  excess entries / long trigger lists are clipped only at a high safety ceiling;
  a typical install (~80 skills) is never summarized with a "(+N more)" note.
* Every failure (registry scan/read) is swallowed → returns "", the turn is not
  broken.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from akana_server.skills.registry import SkillEntry, SkillRegistry, get_registry

log = logging.getLogger(__name__)

__all__ = [
    "build_capability_catalog",
    "catalog_enabled",
    "catalog_include_ids",
    "list_catalog_skills",
    "resolve_catalog",
]

#: Catalog header/directive — tells the model HOW to use the inventory. Bilingual:
#: selected by the ``language`` runtime setting so it matches the active persona.
_HEADER_EN = (
    "[INSTALLED CAPABILITIES]\n"
    "Installed on this machine. For «can you do X?» — check here first; if it matches, "
    "do it. Map abbreviations yourself (e.g. «tg»→Telegram). Full instructions load on "
    "match; here you only see title + triggers. Not in the list → say it isn't installed."
)
_HEADER_TR = (
    "[KURULU YETENEKLER]\n"
    "Bu makinede kurulu. «Yapabilir misin?» → önce buraya bak; eşleşirse yap. "
    "Kısaltmaları kendin çöz (tg→Telegram). Tam yönerge eşleşince yüklenir; burada "
    "yalnız başlık + tetikleyici. Listede yoksa uydurma."
)
_FOOTER_EN = "[/INSTALLED CAPABILITIES]"
_FOOTER_TR = "[/KURULU YETENEKLER]"

#: language → header/footer/labels. Unknown language falls back to English.
_HEADERS = {"en": _HEADER_EN, "tr": _HEADER_TR}
_FOOTERS = {"en": _FOOTER_EN, "tr": _FOOTER_TR}
_TRIGGERS_LABEL = {"en": "triggers", "tr": "tetikleyiciler"}
_MORE_TMPL = {"en": "- (+{n} more capabilities)", "tr": "- (+{n} yetenek daha)"}
#: Pack grouping labels (bilingual). "Pack" is kept verbatim in TR too (the persona
#: and UI already say "pack"); skills with no owning pack fall under Other/Diğer.
_PACK_LABEL = {"en": "Pack", "tr": "Pack"}
_OTHER_LABEL = {"en": "Other", "tr": "Diğer"}

#: Size limits — since the system prompt is never trimmed, the catalog bounds itself.
#: Limits are generous so a typical install (~80 skills) is never summarized away.
_MAX_ENTRIES = 256
_MAX_TRIGGERS = 8
_MAX_CHARS = 20_000


def _entry_line(entry: SkillEntry, language: str = "en") -> str:
    label = (entry.title or entry.id or "").strip() or entry.id
    trigs: list[str] = []
    seen: set[str] = set()
    for t in entry.triggers:
        t = (t or "").strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            trigs.append(t)
        if len(trigs) >= _MAX_TRIGGERS:
            break
    if trigs:
        label_word = _TRIGGERS_LABEL.get(language, _TRIGGERS_LABEL["en"])
        return f"- {label} — {label_word}: {', '.join(trigs)}"
    return f"- {label}"


def list_catalog_skills(registry: SkillRegistry) -> list[dict[str, str]]:
    """Installed skills within the catalog scope (for UI selection): ``[{id, label}]``.

    The SAME scope as ``build_capability_catalog`` (``source="akana"``) → the ids
    checked in the UI pass straight into ``include_ids``.
    """
    out: list[dict[str, str]] = []
    for entry in registry.list(source_filter="akana"):
        label = (entry.title or entry.id or "").strip() or entry.id
        out.append({"id": entry.id, "label": label})
    return out


def _pack_display(pack_id: str) -> str:
    """Human-facing pack name for a sub-header: the part after the last '/' (or the id)."""
    pid = (pack_id or "").strip()
    return pid.rsplit("/", 1)[-1] or pid


def build_capability_catalog(
    registry: SkillRegistry,
    *,
    max_entries: int = _MAX_ENTRIES,
    max_chars: int = _MAX_CHARS,
    include_ids: set[str] | None = None,
    language: str = "en",
    pack_of: dict[str, str] | None = None,
) -> str:
    """Builds compact catalog text from the capabilities installed in the registry.

    Empty registry → "" (when the caller sees this, it adds nothing to the system
    prompt). If ``include_ids`` is given, ONLY those ids are included (user
    selection); None = all. The output is bounded by ``max_entries`` /
    ``max_chars``; overflowing entries are summarized with a "(+N more)" note.
    Title + triggers only — no body.

    ``pack_of`` (``skill_id -> pack_id``) groups the skills under ``Pack: <name>``
    sub-headers so the model never mistakes a skill for a pack; skills with no owning
    pack fall under ``Other``. A ``None``/empty ``pack_of`` → the classic flat list
    (byte-identical to the pre-grouping output — behavior-neutral without packs).

    Scope: only ``source="akana"`` (skills/packs installed by the user). Cursor IDE
    procedures (trigger-less, dev-focused) are intentionally excluded — this both
    keeps the prompt free of noise and leaves the catalog "" on an empty user setup
    (behavior-neutral).
    """
    header = _HEADERS.get(language, _HEADER_EN)
    footer = _FOOTERS.get(language, _FOOTER_EN)

    entries = registry.list(source_filter="akana")
    if include_ids is not None:
        entries = [e for e in entries if e.id in include_ids]
    if not entries:
        return ""
    total = len(entries)  # before the max_entries cap → drives the "(+N more)" note
    entries = entries[:max_entries]

    # Group by owning pack, preserving first-seen order; the ungrouped bucket sorts
    # last so pack sections lead. Empty pack_of → a single None group → flat list.
    pack_of = pack_of or {}
    order: dict[str | None, int] = {}
    groups: dict[str | None, list[SkillEntry]] = {}
    for entry in entries:
        # Provenance is keyed by the skill's DIRECTORY name (adapters.py records
        # ``prov[sid] = pack_id`` from ``contains.skills`` dir names), but a skill's
        # frontmatter/manifest may declare an ``id``/``name`` that differs from its
        # directory — fall back to the directory name (the last path segment) so
        # such skills still land under their owning pack instead of "Other".
        pid = pack_of.get(entry.id)
        if pid is None and entry.path:
            pid = pack_of.get(Path(entry.path).name)
        if pid not in order:
            order[pid] = len(order)
            groups[pid] = []
        groups[pid].append(entry)
    grouped = any(pid is not None for pid in groups)
    section_ids = sorted(groups, key=lambda p: (p is None, order[p]))

    pack_label = _PACK_LABEL.get(language, _PACK_LABEL["en"])
    other_label = _OTHER_LABEL.get(language, _OTHER_LABEL["en"])

    lines: list[str] = []
    shown = 0

    def _fits(extra: str) -> bool:
        # header + footer + existing lines + this line still within the char budget.
        projected = len(header) + len(footer) + sum(len(x) + 1 for x in lines) + len(extra) + 2
        return not (lines and projected > max_chars)

    stop = False
    for pid in section_ids:
        if stop:
            break
        if grouped:
            head = f"{pack_label}: {_pack_display(pid)}" if pid is not None else other_label
            entry_lines = [_entry_line(entry, language) for entry in groups[pid]]
            first_line = entry_lines[0] if entry_lines else None
            # Commit the header only once we know it AND its first entry both fit —
            # otherwise a pack section with zero entries would be emitted (dangling header).
            if first_line is None or not _fits(head):
                stop = True
                break
            lines.append(head)
            if not _fits(first_line):
                lines.pop()
                stop = True
                break
            for line in entry_lines:
                if not _fits(line):
                    stop = True
                    break
                lines.append(line)
                shown += 1
        else:
            for entry in groups[pid]:
                line = _entry_line(entry, language)
                if not _fits(line):
                    stop = True
                    break
                lines.append(line)
                shown += 1

    if shown == 0:
        return ""

    remaining = total - shown
    if remaining > 0:
        lines.append(_MORE_TMPL.get(language, _MORE_TMPL["en"]).format(n=remaining))
    return "\n".join([header, *lines, footer])


def _pack_of(data_dir: Any) -> dict[str, str]:
    """``skill_id -> pack_id`` from the packs provenance file (defensive; {} on any miss).

    Read directly via the adapter's filename constant so building the catalog does not
    construct a SkillsAdapter; any failure quietly returns {} → the catalog stays a flat
    list and the turn is never broken.
    """
    try:
        from akana_server.packs.adapters import SkillsAdapter

        path = Path(data_dir) / SkillsAdapter.PROVENANCE_FILENAME
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def catalog_enabled(settings: Any) -> bool:
    """RuntimeSettings gate (panel > env ``AKANA_SKILL_CATALOG`` > default on).

    On a resolution failure the default is on (the spec default) — the feature is
    useful and behavior-neutral (adds nothing on an empty registry)."""
    try:
        from akana_server.runtime_settings import get_runtime

        return bool(get_runtime("skill_catalog_enabled", settings))
    except Exception:
        log.warning("could not resolve skill_catalog_enabled; assuming catalog on")
        return True


def resolve_catalog(settings: Any) -> str:
    """Gate + registry + catalog text — single entry point. Returns "" on any failure.

    Both the ContextAssembler (web/voice) and the InboundRouter (Telegram)
    call this; it is the single source for the two paths that build the system prompt.
    The header/labels follow the ``language`` setting so they match the active persona."""
    try:
        if not catalog_enabled(settings):
            return ""
        data_dir = getattr(settings, "data_dir", None)
        if data_dir is None:
            return ""
        # User selection (skill ids included in the catalog): None = all (auto),
        # [] = none → "". The checked ids pass into include_ids.
        include = catalog_include_ids(settings)
        from akana_server.runtime_settings import resolve_language

        language = resolve_language(settings)
        return build_capability_catalog(
            get_registry(Path(data_dir)),
            include_ids=include,
            language=language,
            pack_of=_pack_of(data_dir),
        )
    except Exception:
        log.warning("could not resolve capability catalog; not added to system prompt", exc_info=True)
        return ""


def catalog_include_ids(settings: Any) -> set[str] | None:
    """Allowed skill-id set for the catalog scope (None = all/auto → no filter).

    Single source of truth shared by WI-2 (catalog text) and WI-1 (turn
    injection) so both honor the SAME user selection: ``None`` = all, an empty
    set (``[]`` selection) = none, a populated set = only those ids. Any failure
    → None (no filter) — the catalog/injection is an enhancement, never breaks a turn.
    """
    try:
        data_dir = getattr(settings, "data_dir", None)
        if data_dir is None:
            return None
        selection = _catalog_selection(data_dir)
        return set(selection) if selection is not None else None
    except Exception:
        return None


def _catalog_selection(data_dir: Any) -> list[str] | None:
    """Catalog skill selection from the persona store (None = all/auto). Error = None.

    Lazy import: keeps the skills→persona dependency out of module load time; every
    failure quietly returns None (falls back to auto-generation — the catalog is an
    enhancement and cannot break the turn).
    """
    try:
        from akana_server.persona.registry import get_persona_registry

        return get_persona_registry(Path(data_dir)).get_catalog_selection()
    except Exception:
        return None
