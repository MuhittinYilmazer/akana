"""Unified skill index: Akana SKILL.md/manifest.yaml + Cursor SKILL.md.

SkillEngine F0-F2 (SKILL_SPRINT_PLAN §4-6):
- Directory scan + L1 metadata cache (name/description, always in memory)
- L2 body on-demand loading (when a skill is selected), L3 resource files (when called)
- Hybrid search (F2): substring + SQLite FTS5 (with Turkish folding), RRF k=60
  fusion. An FTS error never breaks the search — substring keeps answering
  (``skills/retrieval.py``).
- ``suggest_for_text`` — WI-1's single entry point for "find the right skill at the start of the turn".
- A broken skill does not block the others; errors are reported in the `errors` list.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from akana_server.skills.cursor_paths import cursor_skill_roots
from akana_server.skills.parser import (
    SkillParseError,
    normalize_metadata,
    parse_skill_md,
    validate_required,
)
from akana_server.skills.retrieval import (
    SkillFtsIndex,
    fold_text,
    rrf_fuse,
)

log = logging.getLogger(__name__)

SkillSource = Literal["akana", "cursor"]

_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_RESOURCE_EXCLUDE = {"SKILL.md", "manifest.yaml"}

#: A trigger must be at least this many characters to match as a SUBSTRING.
#: Shorter triggers (1–2 letters, e.g. ``"e"``, ``"as"``) match only by EXACT
#: equality — otherwise a short fragment occurring inside ordinary text produces
#: ``trigger_exact`` (score 1.0) and pins an irrelevant skill to the top (noise/false positive).
_MIN_TRIGGER_SUBSTR_LEN = 3


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """L1 metadata — a lightweight record kept in memory at all times (SP2)."""

    id: str
    source: SkillSource
    title: str
    path: str
    type: str = "skill"
    risk: str = "low"
    trust_tier: str = "user"
    version: int | str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    cursor_skills: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    tools_allowed: tuple[str, ...] = ()
    requires_approval: bool | None = None
    learn_from_success: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("tags", "cursor_skills", "triggers", "tools_allowed"):
            d[key] = list(d[key])
            if not d[key]:
                d.pop(key)
        for key in ("version", "description", "requires_approval", "learn_from_success"):
            if d[key] is None:
                d.pop(key)
        return d


@dataclass(frozen=True, slots=True)
class ScoredSkill:
    """Hybrid search result (F2).

    ``match_reason`` carries the layer info: apart from the ``trigger_exact``
    short-circuit, it is a layer list joined with ``+`` — the substring reason
    (``title``/``tag``/``description``/``trigger_partial``) + ``fts`` +
    ``vector`` (e.g. ``"title+fts"``, ``"fts+vector"``). The schema has the same
    fields as F0/F1 (the cmdk palette consumes it); only the value got richer.
    """

    entry: SkillEntry
    score: float
    match_reason: str

    def to_dict(self) -> dict[str, Any]:
        d = self.entry.to_dict()
        # RRF scores are small (a sum of 1/(60+rank)) — 4 digits preserve the rank difference.
        d["score"] = round(self.score, 4)
        d["match_reason"] = self.match_reason
        return d


def akana_skills_dir(data_dir: Path) -> Path:
    return (data_dir / "skills").resolve()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("skill file unreadable %s: %s", path, e)
        return None


def _title_from_skill_md(text: str, fallback: str) -> str:
    m = _TITLE_RE.search(text)
    if m:
        return m.group(1).strip()
    first = text.strip().splitlines()
    if first:
        line = first[0].lstrip("#").strip()
        if line:
            return line
    return fallback


def _load_manifest(path: Path) -> dict[str, Any] | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        log.warning("invalid manifest %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        log.warning("manifest not a mapping: %s", path)
        return None
    return data


def _coerce_version(raw: Any) -> int | str | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    # ``str.isdigit()`` is True for non-ASCII digits (e.g. '²') that ``int()``
    # rejects, so guard the conversion rather than trust ``isdigit()`` alone.
    try:
        return int(s)
    except ValueError:
        return s


def _entry_from_akana_dir(child: Path, errors: list[dict[str, str]]) -> SkillEntry | None:
    """Parses a single skill directory; on error skips the broken skill and records it in `errors`."""
    skill_md = child / "SKILL.md"
    if not skill_md.is_file():
        return None
    manifest_path = child / "manifest.yaml"
    manifest = _load_manifest(manifest_path) if manifest_path.is_file() else None
    md_text = _read_text(skill_md)
    if md_text is None:
        errors.append({"path": str(skill_md), "error": "SKILL.md could not be read"})
        return None
    try:
        parsed = parse_skill_md(md_text, path=skill_md)
        if not parsed.frontmatter and manifest is None:
            raise SkillParseError(
                "neither YAML frontmatter nor manifest.yaml present — skill cannot be defined",
                path=skill_md,
            )
        # Frontmatter overrides the manifest (the plan-canonical source is the frontmatter).
        meta = normalize_metadata({**(manifest or {}), **parsed.frontmatter}, path=skill_md)
        if manifest is None:
            validate_required(meta, path=skill_md)
    except SkillParseError as e:
        log.warning("skill skipped %s: %s", child, e)
        errors.append({"path": str(skill_md), "error": str(e)})
        return None

    skill_id = str(meta.get("name") or "").strip() or child.name
    title = str(meta.get("title") or "").strip() or _title_from_skill_md(
        parsed.body, skill_id.replace("_", " ").title()
    )
    description = meta.get("description")
    requires_approval = meta.get("requires_approval")
    learn = meta.get("learn_from_success")
    return SkillEntry(
        id=skill_id,
        source="akana",
        title=title,
        path=str(child.resolve()),
        type=str(meta.get("type") or "skill"),
        risk=str(meta.get("risk") or "low").strip().lower() or "low",
        trust_tier=str(meta.get("trust_tier") or "user").strip().lower() or "user",
        version=_coerce_version(meta.get("version")),
        description=str(description).strip() if description else None,
        tags=tuple(meta.get("tags") or ()),
        cursor_skills=tuple(meta.get("cursor_skills") or ()),
        triggers=tuple(meta.get("triggers") or ()),
        tools_allowed=tuple(meta.get("tools_allowed") or ()),
        requires_approval=bool(requires_approval) if requires_approval is not None else None,
        learn_from_success=bool(learn) if learn is not None else None,
    )


def scan_akana_skills(
    root: Path, *, errors: list[dict[str, str]] | None = None
) -> list[SkillEntry]:
    """Scan ``<root>/<id>/`` — SKILL.md (frontmatter) and/or manifest.yaml."""
    if not root.is_dir():
        return []
    collected: list[dict[str, str]] = errors if errors is not None else []
    entries: list[SkillEntry] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            entry = _entry_from_akana_dir(child, collected)
        except Exception as e:  # one broken skill must not abort the whole scan
            log.warning("skill skipped %s: %s", child, e)
            collected.append({"path": str(child / "SKILL.md"), "error": str(e)})
            continue
        if entry is not None:
            entries.append(entry)
    return entries


def scan_cursor_skills(roots: list[Path] | None = None) -> list[SkillEntry]:
    """Scan Cursor skill dirs for ``<id>/SKILL.md`` (first root wins per id)."""
    roots = roots if roots is not None else cursor_skill_roots()
    by_id: dict[str, SkillEntry] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            skill_id = child.name
            if skill_id in by_id:
                continue
            text = _read_text(skill_md) or ""
            # Parse frontmatter like the akana path: a Claude-format skill (YAML
            # frontmatter + ``##`` subheadings, no ``# `` H1) must not fall back to
            # the literal ``---`` delimiter as its title. Use the frontmatter
            # title/name when present, and derive the H1 fallback from the STRIPPED
            # body (never the raw text, whose first line is the ``---`` delimiter).
            fm: dict[str, Any] = {}
            body = text
            try:
                parsed = parse_skill_md(text, path=skill_md)
                fm, body = parsed.frontmatter, parsed.body
            except SkillParseError as e:
                log.warning("cursor SKILL.md parse failed %s: %s", skill_md, e)
            title = (
                str(fm.get("title") or fm.get("name") or "").strip()
                or _title_from_skill_md(body, skill_id.replace("-", " ").title())
            )
            description = fm.get("description")
            by_id[skill_id] = SkillEntry(
                id=skill_id,
                source="cursor",
                title=title,
                path=str(child.resolve()),
                risk="low",
                description=str(description).strip() if description else None,
            )
    return sorted(by_id.values(), key=lambda e: e.id)


def _score_entry(entry: SkillEntry, q: str) -> tuple[float, str]:
    """Substring scoring: trigger > id/title > tag > description.

    ``q`` arrives folded via :func:`fold_text`; the fields go through the same
    folding too, so Turkish pairs like ``İzmir``/``İZMİR`` match.
    """
    best_score, best_reason = 0.0, ""
    for trig in entry.triggers:
        tl = fold_text(trig)
        if tl == q:
            return 1.0, "trigger_exact"
        # Very short triggers (1–2 letters) match only by exact equality: a substring
        # match produces a false positive in ordinary text.
        if len(tl) < _MIN_TRIGGER_SUBSTR_LEN:
            continue
        if q in tl or tl in q:
            if best_score < 0.9:
                best_score, best_reason = 0.9, "trigger_partial"
    if q in fold_text(f"{entry.id} {entry.title}") and best_score < 0.8:
        best_score, best_reason = 0.8, "title"
    if best_score < 0.6 and any(q in fold_text(t) for t in entry.tags):
        best_score, best_reason = 0.6, "tag"
    if best_score < 0.5 and entry.description and q in fold_text(entry.description):
        best_score, best_reason = 0.5, "description"
    return best_score, best_reason


#: Length (in characters) of the L2 body summary that enters the FTS document.
_BODY_SUMMARY_CHARS = 400


def _entry_fts_doc(entry: SkillEntry) -> str:
    """FTS document: metadata + L2 body summary (the first part, without frontmatter).

    The body is read here *while building the index* but is not written to the L2
    cache — the progressive-disclosure (L2 on-demand) contract is not broken. An
    unreadable/broken SKILL.md does not block the rest of the index (the metadata
    still enters).
    """
    parts = [entry.id, entry.title, entry.description or ""]
    parts.extend(entry.triggers)
    parts.extend(entry.tags)
    text = _read_text(Path(entry.path) / "SKILL.md")
    if text is not None:
        try:
            body = parse_skill_md(text).body
        except SkillParseError:
            body = ""
        parts.append(body[:_BODY_SUMMARY_CHARS])
    return " ".join(p for p in parts if p)


class SkillRegistry:
    """L1 metadata cache + L2 on-demand body + L3 resources + hybrid search (F2).

    The FTS5 index in ``<data_dir>/db/skills.db`` is rebuilt on every reload. A
    failure of that layer does not affect the substring search.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        include_cursor: bool = True,
    ) -> None:
        self._data_dir = data_dir
        self._include_cursor = include_cursor
        self._entries: list[SkillEntry] = []
        self._index: dict[str, SkillEntry] = {}
        self._bodies: dict[str, str] = {}  # L2 cache (only the loaded ones)
        self._errors: list[dict[str, str]] = []
        self._loaded = False
        self._fts = SkillFtsIndex(data_dir / "db" / "skills.db")
        # reload() can fire on the event loop (GET /skills?reload=true, a pack
        # enable/disable) WHILE suggest_for_text runs search() in a worker thread
        # (plan_skill_turn's asyncio.to_thread on the SAME cached instance). The
        # lock serialises reloads so two first-readers can't rebuild concurrently
        # and the FTS rebuild isn't racing; readers publish/consume _index and
        # _entries via single atomic rebinds, so they need no lock on the hot path.
        self._reload_lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------

    @property
    def errors(self) -> list[dict[str, str]]:
        """Error records for broken skills skipped during the scan."""
        self._ensure_loaded()
        return list(self._errors)

    def reload(self) -> None:
        """Rescans all directories; the L2 body cache is cleared.

        Build into LOCALS and publish with single atomic rebinds: a concurrent
        search() reading _index/_entries never observes a half-cleared dict (the
        previous ``self._index = {}`` then repopulate window let a worker's
        self._index[eid] hit a transiently-absent id → KeyError). The whole
        rebuild is serialised by _reload_lock so two first-readers can't race.
        """
        with self._reload_lock:
            errors: list[dict[str, str]] = []
            akana = scan_akana_skills(akana_skills_dir(self._data_dir), errors=errors)
            cursor = scan_cursor_skills() if self._include_cursor else []
            new_index: dict[str, SkillEntry] = {}
            for e in [*akana, *cursor]:  # on an id clash, akana takes priority
                new_index.setdefault(e.id, e)
            # _entries MUST agree with _index: keep exactly the deduped winners so
            # list()/suggest_for_text (which iterate _entries) can never surface an
            # id that get()/load_body (via _index) resolves to a DIFFERENT entry,
            # and so a tie on (id, trigger-len) can't fall through to comparing
            # unorderable SkillEntry objects in suggest_for_text's sort.
            new_entries = sorted(new_index.values(), key=lambda e: (e.source, e.id))
            self._rebuild_search_index(new_index)
            # Publish atomically, then flip _loaded LAST so _ensure_loaded's
            # unlocked read either sees the old snapshot or the fully new one.
            self._entries = new_entries
            self._index = new_index
            self._bodies = {}
            self._errors = errors
            self._loaded = True

    def _rebuild_search_index(self, index: dict[str, SkillEntry]) -> None:
        """Rebuilds the FTS index from the given deduplicated L1 index.

        Error tolerance: FTS degrades on its own (``available`` turns off); the
        reload itself never fails because of this.
        """
        entries = list(index.values())  # deduplicated set on an id clash
        self._fts.rebuild([(e.id, _entry_fts_doc(e)) for e in entries])

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        # Re-check under the (reentrant) lock so two concurrent first-readers
        # don't each run a full reload/FTS rebuild. reload() re-acquires it.
        with self._reload_lock:
            if not self._loaded:
                self.reload()

    # -- L1: metadata ---------------------------------------------------------

    def list(
        self, *, type_filter: str | None = None, source_filter: str | None = None
    ) -> list[SkillEntry]:
        self._ensure_loaded()
        out = self._entries
        if type_filter:
            out = [e for e in out if e.type == type_filter]
        if source_filter:
            out = [e for e in out if e.source == source_filter]
        return list(out)

    def get(self, skill_id: str) -> SkillEntry | None:
        self._ensure_loaded()
        return self._index.get(skill_id)

    # -- L2: body (on-demand) ------------------------------------------------

    def body_loaded(self, skill_id: str) -> bool:
        return skill_id in self._bodies

    def load_body(self, skill_id: str) -> str:
        """Loads and caches the SKILL.md body (without frontmatter) (L2)."""
        cached = self._bodies.get(skill_id)
        if cached is not None:
            return cached
        entry = self.get(skill_id)
        if entry is None:
            raise KeyError(skill_id)
        skill_md = Path(entry.path) / "SKILL.md"
        text = _read_text(skill_md)
        if text is None:
            raise SkillParseError("SKILL.md could not be read", path=skill_md)
        body = parse_skill_md(text, path=skill_md).body
        self._bodies[skill_id] = body
        return body

    # -- L3: resource files (scripts/attachments) -----------------------------

    def list_resources(self, skill_id: str) -> list[str]:
        """Relative paths of the extra files in the skill directory (content not loaded)."""
        entry = self.get(skill_id)
        if entry is None:
            raise KeyError(skill_id)
        base = Path(entry.path)
        out: list[str] = []
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(base).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            rel = "/".join(rel_parts)
            if rel in _RESOURCE_EXCLUDE:
                continue
            out.append(rel)
        return out

    # -- search (F2 hybrid: substring + FTS5 + optional vector, RRF fusion) --------

    def search(self, query: str, top_k: int = 5) -> list[ScoredSkill]:
        """Hybrid skill search.

        Two rankings are produced — substring (F0/F1 scoring), FTS5 (bm25) — and
        fused with RRF (k=60). The ``trigger_exact`` short-circuit: if the query
        exactly equals a trigger, that skill is pinned to the top with a 1.0 score
        and the unchanged ``"trigger_exact"`` reason. For the other results,
        ``match_reason`` lists the contributing layers joined with ``+``
        (e.g. ``"title+fts"``).
        """
        q = fold_text(query.strip())
        if not q:
            return []
        self._ensure_loaded()
        # Snapshot the index ONCE into a local and score/look up from it alone: a
        # concurrent reload() rebinds self._index atomically, so scoring self.list()
        # (self._entries) while looking up a different self._index snapshot could let
        # fused carry an id absent from the lookup dict → KeyError on index[eid].
        index = self._index
        sub: dict[str, tuple[float, str]] = {}
        for entry in index.values():
            score, reason = _score_entry(entry, q)
            if score > 0:
                sub[entry.id] = (score, reason)
        sub_ranking = [
            eid for eid, _ in sorted(sub.items(), key=lambda kv: (-kv[1][0], kv[0]))
        ]
        known = set(index)
        fts_ranking = [i for i in self._fts.search(query) if i in known]
        fused = rrf_fuse([sub_ranking, fts_ranking])
        fts_set = set(fts_ranking)
        scored: list[ScoredSkill] = []
        for eid, score in fused:
            entry = index[eid]
            sub_hit = sub.get(eid)
            if sub_hit is not None and sub_hit[1] == "trigger_exact":
                scored.append(ScoredSkill(entry=entry, score=1.0, match_reason="trigger_exact"))
                continue
            parts = [sub_hit[1]] if sub_hit is not None else []
            if eid in fts_set:
                parts.append("fts")
            scored.append(
                ScoredSkill(entry=entry, score=score, match_reason="+".join(parts))
            )
        scored.sort(
            key=lambda s: (
                0 if s.match_reason == "trigger_exact" else 1,
                -s.score,
                s.entry.id,
            )
        )
        return scored[: max(1, top_k)]

    def suggest_for_text(
        self, user_text: str, top_k: int = 3, *, allowed: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """WI-1 contract — the single entry point for "find the right skill at the start of the turn".

        Input:
            user_text: Raw user turn text (Turkish folding is done here, the caller
                does not have to normalize).
            top_k: Maximum number of suggestions (default 3; ``<1`` → 1).
            allowed: Optional catalog selection (WI-2). ``None`` = all skills
                eligible; otherwise only ids in this set are considered, and the
                filter is applied BEFORE the ``top_k`` cap so an excluded skill can
                never fill a slot ahead of a selected one.

        Returns: at most ``top_k`` dicts, ordered. Each dict carries the
        ``SkillEntry.to_dict()`` fields (id, title, source, risk, trust_tier, ...)
        and the following:

        - ``score``: float — ``1.0`` on an exact trigger match, otherwise the RRF
          fusion score (small values; only the ordering is meaningful).
        - ``match_reason``: ``"trigger_exact"`` or a layer list
          (like ``"title+fts"``) — the same vocabulary as :meth:`search`.
        - ``requires_approval``: bool — **always present**; ``False`` if undefined
          in the frontmatter. WI-1 builds the approval gate from this field.

        Behavior contract:

        1. **Exact trigger match short-circuit:** if a skill trigger occurs
           verbatim (folded) inside the user text, that skill is placed at the top
           with ``score=1.0, match_reason="trigger_exact"`` without waiting for
           embed/FTS. On multiple matches the longest trigger comes first (the more
           specific one wins), with ids alphabetical on a tie.
        2. The remaining slots are filled from the hybrid search (:meth:`search`).
        3. **Error guarantee:** an embed/FTS failure never leaks an exception — at
           worst it returns the substring results. Empty/meaningless text →
           empty list.
        """
        folded = fold_text(user_text.strip())
        if not folded:
            return []
        k = max(1, top_k)

        def to_suggestion(entry: SkillEntry, score: float, reason: str) -> dict[str, Any]:
            d = ScoredSkill(entry=entry, score=score, match_reason=reason).to_dict()
            d["requires_approval"] = bool(entry.requires_approval)
            return d

        by_id: dict[str, SkillEntry] = {}
        # Sort key is (-max_trigger_len, id) only — a TOTAL order over hashable
        # values. Never put SkillEntry in the key: it is frozen but not order=True,
        # so a tie on (len, id) would fall through to comparing entries → TypeError.
        exact: list[tuple[int, str]] = []
        for entry in self.list():
            if allowed is not None and entry.id not in allowed:
                continue  # catalog-excluded skills must not consume a suggestion slot
            # A very short trigger (1–2 letters) counts only if it equals the ENTIRE
            # text — a substring match produces a wrong ``trigger_exact`` (score 1.0)
            # in ordinary text and pins an irrelevant skill to the top.
            hits = [
                t
                for t in (fold_text(trig) for trig in entry.triggers)
                if t
                and (t in folded if len(t) >= _MIN_TRIGGER_SUBSTR_LEN else t == folded)
            ]
            if hits:
                by_id.setdefault(entry.id, entry)
                exact.append((-max(len(t) for t in hits), entry.id))
        exact.sort()

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, eid in exact[:k]:
            entry = by_id[eid]
            out.append(to_suggestion(entry, 1.0, "trigger_exact"))
            seen.add(entry.id)
        if len(out) < k:
            # Over-request so that dropping excluded entries below still leaves
            # enough allowed results to fill the remaining slots.
            for sk in self.search(user_text, top_k=k + len(seen) + len(self._entries)):
                if sk.entry.id in seen:
                    continue
                if allowed is not None and sk.entry.id not in allowed:
                    continue  # catalog-excluded → never fills a slot
                out.append(to_suggestion(sk.entry, sk.score, sk.match_reason))
                seen.add(sk.entry.id)
                if len(out) >= k:
                    break
        return out


# -- module-level cache (one registry per data_dir) ---------------------------

_registries: dict[str, SkillRegistry] = {}


def get_registry(data_dir: Path) -> SkillRegistry:
    key = str(data_dir.resolve())
    reg = _registries.get(key)
    if reg is None:
        reg = SkillRegistry(data_dir)
        _registries[key] = reg
    return reg


def reload_skills() -> None:
    """Clear cached registries (next access rescans)."""
    _registries.clear()


