"""``AkanaPackHost`` — discovers packs, registers their content, and manages a
minimal enable/disable lifecycle (PACK_INTERFACE.md §4 — the enforced subset).

``register_all`` registers the content of every *enabled* pack (skills/personas,
plus tool declarations + probes) through the ContentAdapters: no mandatory-tool
gate, no automatic MCP mount. Called once during app lifespan.

Lifecycle (the enforced subset of §4 — permissions stay advisory):
``enable``/``disable`` hot-reload a pack's content at runtime. ``disable`` only
withdraws the *derived* registrations (the copies under ``data_dir/skills``,
the in-memory personas, and any pack-managed MCP entries) — the pack's SOURCE
directory under ``packs/`` is never touched, so disabling is fully reversible.
The disabled set is persisted to ``data_dir/packs_state.json``. Mounting a pack's
MCP server is a separate, consent-gated step (``grant_consent`` / the
``POST /packs/consent`` route) — never a side effect of enable.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from akana_server.packs.adapters import (
    PersonasAdapter,
    SkillsAdapter,
    ToolProbeResult,
    ToolsAdapter,
    autodiscover_contents,
)
from packs.contract.host import (
    LoadedPack,
    PackRef,
    PackState,
)
from packs.contract.manifest import (
    ValidationResult,
    load_manifest,
    validate_pack_dir,
)

log = logging.getLogger(__name__)

# Directory holding the contract itself — never a loadable pack.
_CONTRACT_DIRNAME = "contract"
#: Persisted lifecycle state (only the disabled set; everything else is derived).
_STATE_FILENAME = "packs_state.json"


class PackError(Exception):
    """Base error for pack-host operations."""


class UnknownPackError(PackError):
    """Referenced pack id is not loaded/known."""


def pack_discovery_roots() -> list[Path]:
    """Pack roots (user > repo), the canonical ordering used across the skills/packs layer."""
    repo_packs = Path(__file__).resolve().parents[2] / "packs"
    user_packs = Path.home() / ".akana" / "packs"
    return [user_packs, repo_packs]


class AkanaPackHost:
    """In-memory pack manager wiring packs into akana engines via adapters.

    State is tracked per pack in memory; the disabled set is persisted as JSON
    under ``data_dir/packs_state.json`` (best effort; never required for
    correctness — a missing/corrupt file is treated as "nothing disabled").
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        discovery_roots: list[Path] | None = None,
        persist_state: bool = True,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._roots = discovery_roots or pack_discovery_roots()
        self._persist = persist_state
        self._loaded: dict[str, LoadedPack] = {}
        # Guards enable/disable/rescan — they mutate shared adapter state + files
        # and may run from FastAPI's threadpool concurrently.
        self._lock = threading.Lock()
        self._disabled: set[str] = self._load_disabled()

        # The pack standard's three core capabilities (each one observable):
        #   skills   — what the pack can DO          (copied + scanned + injected)
        #   tools    — external programs it NEEDS    (MCP/pip; probe + consent mount)
        #   persona  — how it TALKS                  (system-prompt injection)
        # ``memory_schema_extensions`` / ``plugins`` / ``workflows`` / ``ui_cards``
        # are NO LONGER consumed: a legacy pack may still declare them (they parse),
        # but the host ignores them — nothing is registered for them.
        self._skills = SkillsAdapter(self._data_dir)
        self._tools = ToolsAdapter(self._data_dir)
        self._personas = PersonasAdapter()
        # Order matters for register; reverse for unregister.
        self._adapters = [
            self._skills,
            self._tools,
            self._personas,
        ]

    # -- adapter accessors (exposed for callers / API) --------------------- #

    @property
    def skills_adapter(self) -> SkillsAdapter:
        return self._skills

    @property
    def tools_adapter(self) -> ToolsAdapter:
        return self._tools

    @property
    def personas_adapter(self) -> PersonasAdapter:
        return self._personas

    # -- discovery / validation ------------------------------------------- #

    def discover(self) -> list[PackRef]:
        refs: list[PackRef] = []
        seen: set[str] = set()
        for root in self._roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                if child.name == _CONTRACT_DIRNAME:
                    continue
                manifest_path = child / "pack.yaml"
                if not manifest_path.is_file():
                    continue
                try:
                    pack_id = load_manifest(manifest_path).id
                except Exception as e:  # malformed manifest — skip, don't crash discovery
                    log.warning("discover: skipping %s (bad manifest): %s", child, e)
                    continue
                if pack_id in seen:
                    continue  # first root wins (user overrides repo)
                seen.add(pack_id)
                refs.append(PackRef(pack_id=pack_id, root=child.resolve()))
        return refs

    def validate(self, ref: PackRef) -> ValidationResult:
        return validate_pack_dir(ref.root)

    def load(self, ref: PackRef) -> LoadedPack:
        """Load the manifest + auto-discover skills/personas (content registered by register_all)."""
        manifest = load_manifest(ref.root / "pack.yaml")
        autodiscover_contents(manifest, ref.root)
        pack = LoadedPack(
            manifest=manifest,
            root=ref.root,
            state=PackState.ENABLED,
            registered={},
        )
        self._loaded[manifest.id] = pack
        return pack

    # -- content registration --------------------------------------------- #

    def register_all(self) -> list[str]:
        """Register the content of all *enabled* discovered packs (no gate, no mount).

        Disabled packs (per ``packs_state.json``) are loaded into memory so they
        appear in listings, but their content is NOT registered. The
        ContentAdapters (skills / tools-declare / personas) run for
        each enabled pack; a single adapter failure is logged and skipped without
        affecting other packs. ``ToolsAdapter`` only declares + probes; MCP
        servers are NEVER auto-mounted. Called once during app lifespan.
        """
        activated: list[str] = []
        for ref in self.discover():
            try:
                pack = self.get(ref.pack_id) or self.load(ref)
                if ref.pack_id in self._disabled:
                    pack.registered = {}
                    pack.state = PackState.DISABLED
                    continue
                self._register_pack(pack)
                pack.state = PackState.ENABLED
                activated.append(ref.pack_id)
            except Exception:  # a broken pack must not abort the whole registration
                log.warning(
                    "register_all: pack %s could not be activated", ref.pack_id, exc_info=True
                )
        # Auto-prune skill copies whose owning pack was deleted from packs/ since
        # last run (orphans). User-authored skills (no provenance) are untouched.
        try:
            pruned = self._skills.reconcile(set(self._loaded))
            if pruned:
                log.info(
                    "register_all: %d orphan skill(s) pruned: %s",
                    len(pruned),
                    ", ".join(pruned),
                )
        except Exception:
            log.warning("register_all: orphan skill reconcile failed", exc_info=True)
        return activated

    def _register_pack(self, pack: LoadedPack) -> None:
        """Run every adapter's ``register`` for a pack (forward order)."""
        pack.registered = {}
        for adapter in self._adapters:
            try:
                adapter.register(pack)
            except Exception:  # a single adapter failure must not take down the pack
                log.warning(
                    "pack %s: %s register failed",
                    pack.manifest.id,
                    type(adapter).__name__,
                    exc_info=True,
                )

    def _unregister_pack(self, pack_id: str, *, preserve_tools_mount: bool = False) -> None:
        """Withdraw every adapter's registration for a pack (reverse order).

        ``preserve_tools_mount`` is forwarded to the ToolsAdapter so a reversible
        *disable* keeps the pack's consented MCP entries (flipped ``enabled: false``)
        instead of unmounting them; a *delete/uninstall* leaves it False so the
        entries are truly withdrawn. Skills/personas are always torn down.
        """
        for adapter in reversed(self._adapters):
            try:
                if adapter is self._tools:
                    self._tools.unregister(pack_id, preserve_mount=preserve_tools_mount)
                else:
                    adapter.unregister(pack_id)
            except Exception:  # withdrawal is best-effort; never block disable
                log.warning(
                    "pack %s: %s unregister failed",
                    pack_id,
                    type(adapter).__name__,
                    exc_info=True,
                )

    # -- enable / disable lifecycle (hot-reload) -------------------------- #

    def enable(self, pack_id: str) -> LoadedPack:
        """Re-register a disabled pack's content (idempotent). Hot — no restart."""
        with self._lock:
            pack = self._require(pack_id)
            if pack.state != PackState.DISABLED:
                pack.state = PackState.ENABLED  # normalize
                self._disabled.discard(pack_id)
                self._save_state()
                return pack
            # Re-parse the manifest from disk so skills/personas added to the pack
            # WHILE it was disabled are picked up on enable (the in-memory
            # ``contains`` snapshot froze at load time; a stale re-autodiscover is a
            # no-op). A disabled pack has nothing copied yet, so register alone
            # (no prune) reconciles it. Best effort — a bad manifest keeps the old.
            try:
                manifest = load_manifest(pack.root / "pack.yaml")
                autodiscover_contents(manifest, pack.root)
                pack = LoadedPack(
                    manifest=manifest,
                    root=pack.root,
                    state=PackState.DISABLED,
                    registered={},
                )
                self._loaded[pack_id] = pack
            except Exception:
                log.warning("pack %s: manifest refresh on enable failed", pack_id, exc_info=True)
            self._register_pack(pack)
            # Restore a previously-consented MCP mount that ``disable`` parked as
            # ``enabled: false`` — a reversible toggle must not require a fresh
            # consent to get the pack's tools back. Newly-declared servers that
            # never had consent stay pending (no entry to flip). Best effort.
            try:
                restored = self._tools.set_enabled(pack_id, True)
                if restored:
                    log.info("pack %s: MCP entries re-enabled: %s", pack_id, ", ".join(restored))
            except Exception:
                log.warning("pack %s: MCP re-enable failed", pack_id, exc_info=True)
            pack.state = PackState.ENABLED
            self._disabled.discard(pack_id)
            self._save_state()
            log.info("pack %s enabled", pack_id)
            return pack

    def disable(self, pack_id: str) -> LoadedPack:
        """Withdraw a pack's derived registrations (idempotent). Source untouched.

        MCP entries the owner consented to are kept but parked ``enabled: false``
        (skipped at runtime, restored on the next ``enable``) rather than unmounted,
        so a plain off/on toggle is lossless. A true unmount only happens on
        uninstall / hot-delete (``rescan``) or an explicit ``revoke_consent``.
        """
        with self._lock:
            pack = self._require(pack_id)
            if pack.state == PackState.DISABLED:
                self._disabled.add(pack_id)
                self._save_state()
                return pack
            self._unregister_pack(pack_id, preserve_tools_mount=True)
            pack.registered = {}
            pack.state = PackState.DISABLED
            self._disabled.add(pack_id)
            self._save_state()
            log.info("pack %s disabled", pack_id)
            return pack

    # -- consent-gated MCP mount (product surface for routes/packs.py) ------ #

    def consent_view(self, pack_id: str | None = None) -> list[dict[str, Any]]:
        """Per-pack MCP consent state for the API/UI.

        For every loaded pack that declares ``mcp_server`` tools, report which
        server names are already mounted (approved) and which are still
        ``pending`` (declared but never consented to). ``pack_id`` narrows the
        result to a single pack. Packs with no MCP servers are omitted.
        """
        ids = [pack_id] if pack_id is not None else list(self._loaded)
        out: list[dict[str, Any]] = []
        for pid in sorted(i for i in ids if i in self._loaded):
            declared = [str(t.get("name")) for t in self._tools.mcp_server_tools(pid)]
            if not declared:
                continue
            out.append(
                {
                    "pack_id": pid,
                    "pending": self._tools.pending_consent(pid),
                    "mounted": self._tools.mounted_server_names(pid),
                }
            )
        return out

    def grant_consent(
        self,
        pack_id: str,
        server_configs: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, list[str]]:
        """Human-approved MCP mount (the ONLY intended write path — API/UI driven).

        Wraps ``ToolsAdapter.consent`` under the host lock. Raises
        ``UnknownPackError`` if the pack is not loaded so the route can 404.
        """
        with self._lock:
            self._require(pack_id)
            return self._tools.consent(
                pack_id, server_configs=server_configs, approved=True
            )

    def revoke_consent(self, pack_id: str) -> list[str]:
        """Withdraw the MCP entries mounted on behalf of a pack (idempotent)."""
        with self._lock:
            self._require(pack_id)
            return self._tools.unmount(pack_id)

    def rescan(self) -> dict[str, list[str]]:
        """Reconcile the loaded set with what's on disk — hot, no restart.

        Three directions, atomic under the lock:

        - **added**: packs that appeared under ``packs/`` since the last scan are
          loaded and (unless disabled) registered.
        - **removed**: packs whose source directory has vanished have their derived
          registrations withdrawn immediately (skill copies, persona, MCP entries)
          and are dropped from ``_loaded`` — so a deleted pack's persona/skills
          disappear from the UI/registries without a restart. The pack is also
          discarded from the persisted disabled set (it no longer exists).
        - **updated**: packs still present whose SKILL/persona contents changed on
          disk (a skill added to or removed from an already-mounted pack) are
          re-parsed and re-registered so the new skill is copied + scanned (and a
          removed one pruned) without a full restart.

        Other already-loaded packs keep their state untouched. Returns
        ``{"added": [...], "removed": [...], "updated": [...]}``.
        """
        with self._lock:
            present = {ref.pack_id: ref for ref in self.discover()}
            added: list[str] = []
            removed: list[str] = []
            updated: list[str] = []

            # 1) Newly appeared packs → load + register (enabled ones); still-present
            #    packs whose on-disk content changed → refresh (add/remove skills).
            for pack_id, ref in present.items():
                if pack_id in self._loaded:
                    try:
                        if self._refresh_pack_content(pack_id, ref):
                            updated.append(pack_id)
                    except Exception:
                        log.warning(
                            "rescan: pack %s content refresh failed", pack_id, exc_info=True
                        )
                    continue
                try:
                    pack = self.load(ref)
                    if pack_id in self._disabled:
                        pack.registered = {}
                        pack.state = PackState.DISABLED
                    else:
                        self._register_pack(pack)
                        pack.state = PackState.ENABLED
                    added.append(pack_id)
                except Exception:
                    log.warning("rescan: pack %s could not be loaded", pack_id, exc_info=True)

            # 2) Vanished packs → withdraw registrations + forget (hot-delete).
            #    A pack absent from ``present`` because its pack.yaml momentarily
            #    fails to parse (a YAML typo, a temporarily-invalid id/version saved
            #    mid-edit) is NOT a deletion: discover() skips it, but its source
            #    DIRECTORY still exists. Treating that as an uninstall would truly
            #    unmount the consented MCP entry and rmtree the skill dir (destroying
            #    user runtime state like contacts.json). Keep such packs' derived
            #    state intact (last-good LoadedPack) — only fire the hot-delete when
            #    the pack's directory (and its pack.yaml) is actually gone from disk.
            state_dirty = False
            for pack_id in list(self._loaded):
                if pack_id in present:
                    continue
                pack = self._loaded.get(pack_id)
                if pack is not None and (pack.root / "pack.yaml").is_file():
                    log.warning(
                        "rescan: pack %s has an unparseable pack.yaml but its folder "
                        "still exists — keeping derived state (not uninstalling)",
                        pack_id,
                    )
                    continue
                self._unregister_pack(pack_id)  # skills + persona + MCP unmount
                self._loaded.pop(pack_id, None)
                if pack_id in self._disabled:
                    self._disabled.discard(pack_id)
                    state_dirty = True
                removed.append(pack_id)
            if state_dirty:
                self._save_state()

            # 3) Safety net: prune any orphan skill copies (provenance-driven).
            try:
                pruned = self._skills.reconcile(set(self._loaded))
                if pruned:
                    log.info(
                        "rescan: %d orphan skill(s) pruned: %s", len(pruned), ", ".join(pruned)
                    )
            except Exception:
                log.warning("rescan: orphan skill reconcile failed", exc_info=True)

            if added:
                log.info("rescan: %d new pack(s): %s", len(added), ", ".join(added))
            if removed:
                log.info("rescan: %d removed pack(s): %s", len(removed), ", ".join(removed))
            if updated:
                log.info("rescan: %d updated pack(s): %s", len(updated), ", ".join(updated))
            return {"added": added, "removed": removed, "updated": updated}

    def _refresh_pack_content(self, pack_id: str, ref: PackRef) -> bool:
        """Re-parse a still-present pack and re-register it if its content changed.

        Detects skills/personas added to or removed from a pack that is already
        loaded (the frozen ``contains`` snapshot taken at first ``load`` never
        updates by itself). A fresh ``load_manifest`` + ``autodiscover_contents``
        is required — re-running discovery on the stale in-memory manifest is a
        no-op (``autodiscover_contents`` only fills an EMPTY ``contains``).

        Only skills/personas are reconciled; the pack's consented MCP mount is
        left completely untouched (no ToolsAdapter unregister), so a content
        refresh never parks/flips a mount. Returns True iff something changed.
        """
        current = self._loaded.get(pack_id)
        if current is None:
            return False
        manifest = load_manifest(ref.root / "pack.yaml")
        autodiscover_contents(manifest, ref.root)

        old_skills = set(current.manifest.contains.skills)
        new_skills = set(manifest.contains.skills)
        old_personas = set(current.manifest.contains.personas)
        new_personas = set(manifest.contains.personas)
        # A moved root (folder rename with the same pack id, or removal of a
        # ~/.akana override that shadowed a repo pack) is ALSO a content change:
        # the id sets can be identical while ref.root now points somewhere new.
        # If we returned False here the LoadedPack would keep a root that no longer
        # exists, and a later enable() would find no source dirs and silently strip
        # every skill/persona. Compare resolved roots so a rename is caught.
        root_moved = ref.root.resolve() != current.root.resolve()
        if not root_moved and old_skills == new_skills and old_personas == new_personas:
            return False

        enabled = current.state == PackState.ENABLED
        pack = LoadedPack(
            manifest=manifest,
            root=ref.root,
            state=current.state,
            registered={},
        )
        self._loaded[pack_id] = pack
        if not enabled:
            # A disabled pack registers nothing — just surface the fresh contents
            # in listings. Nothing to copy/scan.
            return True

        # Prune ONLY the skills the pack dropped (targeted): the skills it still
        # ships are re-copied by SkillsAdapter.register, which preserves runtime
        # state (contacts.json) — an unregister-first would rmtree that state.
        dropped = sorted(old_skills - new_skills)
        if dropped:
            self._skills.drop_skills(pack_id, dropped)
            # drop_skills defers the registry reload to the caller (so a batched
            # drop+register does ONE reload). But SkillsAdapter.register early-
            # returns when the pack now ships NO skills, skipping its _refresh — so
            # when a pack drops its LAST skill the disk copy is pruned yet the
            # cached registry (feeding the catalog/turn-injection) still lists it.
            # Force the reload here whenever register won't run one.
            if not new_skills:
                self._skills.refresh()
        # Personas carry no runtime state → a clean unregister+register is safe and
        # removes any persona the pack dropped.
        try:
            self._personas.unregister(pack_id)
        except Exception:
            log.warning("pack %s: persona unregister (refresh) failed", pack_id, exc_info=True)
        self._register_pack(pack)
        return True

    # -- introspection ----------------------------------------------------- #

    def state(self, pack_id: str) -> PackState | None:
        pack = self._loaded.get(pack_id)
        return pack.state if pack else None

    def get(self, pack_id: str) -> LoadedPack | None:
        return self._loaded.get(pack_id)

    def pack_view(self, pack_id: str) -> dict[str, Any] | None:
        """JSON-able summary of one pack for the API/UI (None if unknown)."""
        pack = self._loaded.get(pack_id)
        if pack is None:
            return None
        return self._build_view(pack)

    def list_views(self) -> list[dict[str, Any]]:
        """JSON-able summaries of all loaded packs, sorted by id."""
        return [self._build_view(p) for _, p in sorted(self._loaded.items())]

    def _build_view(self, pack: LoadedPack) -> dict[str, Any]:
        m = pack.manifest
        c = m.contains
        enabled = pack.state == PackState.ENABLED
        # Missing required external tools — only meaningful once probed (enabled).
        missing: list[dict[str, Any]] = []
        if enabled:
            for r in self._tools.missing_required(pack.manifest.id):
                missing.append(
                    {
                        "name": r.name,
                        "required": r.required,
                        "install_hint": r.install_hint,
                        "setup_skill": r.setup_skill,
                    }
                )
        # MCP consent state — lets the UI distinguish "enabled with tools live" from
        # "enabled but its MCP server still needs the owner's approval" (the toggle
        # alone never mounts). Only meaningful once declared (enabled).
        pending: list[str] = self._tools.pending_consent(m.id) if enabled else []
        return {
            "id": m.id,
            "title": m.title or m.id,
            "version": m.version,
            "description": (m.description or "").strip(),
            "state": pack.state.value,
            "enabled": enabled,
            "root": str(pack.root),
            "contains": {
                "skills": list(c.skills),
                "personas": list(c.personas),
                "tools": [t.name for t in m.dependencies.external_tools],
            },
            "counts": {
                "skills": len(c.skills),
                "personas": len(c.personas),
                "tools": len(m.dependencies.external_tools),
            },
            "missing_tools": missing,
            "mcp_pending": pending,
        }

    # -- internals --------------------------------------------------------- #

    def _require(self, pack_id: str) -> LoadedPack:
        pack = self._loaded.get(pack_id)
        if pack is None:
            raise UnknownPackError(f"pack not loaded/unknown: {pack_id}")
        return pack

    def _state_path(self) -> Path:
        return self._data_dir / _STATE_FILENAME

    def _load_disabled(self) -> set[str]:
        if not self._persist:
            return set()
        path = self._state_path()
        if not path.is_file():
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("%s unreadable/corrupt — treated as empty", path, exc_info=True)
            return set()
        disabled = raw.get("disabled") if isinstance(raw, dict) else None
        if not isinstance(disabled, list):
            return set()
        return {str(x) for x in disabled}

    def _save_state(self) -> None:
        if not self._persist:
            return
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"disabled": sorted(self._disabled)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            log.warning("%s could not be written — disabled set not persisted", path, exc_info=True)


__all__ = [
    "AkanaPackHost",
    "PackError",
    "ToolProbeResult",
    "UnknownPackError",
]
