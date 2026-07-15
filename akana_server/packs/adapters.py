"""The three ``ContentAdapter`` implementations — PACK_INTERFACE.md §3.

Each adapter binds one content type from a loaded pack to the relevant akana
engine on ``register`` (enable) and tears it back down on ``unregister``
(disable). These three are the only content types the reference consumer
registers:

  skills   -> SkillsAdapter   (copy into data_dir/skills + reload)             [real]
  tools    -> ToolsAdapter    (probe + consent-gated MCP mount)                [real; mount ONLY via consent()]
  personas -> PersonasAdapter (load personas/*.yaml, prompt-inject exposed)    [real]
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from akana_server.orchestrator.mcp_config import (
    CONFIG_FILENAME,
    RESERVED_SERVER_NAMES,
)
from akana_server.skills.registry import (
    get_registry,
    akana_skills_dir,
    reload_skills,
)
from packs.contract.host import LoadedPack
# Re-exported so the host + skill_resolve keep importing auto-discovery from here.
from packs.contract.manifest import autodiscover_contents  # noqa: F401

log = logging.getLogger(__name__)

_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
#: Pack-supplied skill ids are used as DIRECTORY NAMES (data_dir/skills/<id>).
#: Untrusted pack content is therefore restricted to a plain name — an id
#: containing separators or ".." would cause rmtree/copytree to escape the
#: skills root (path traversal). Consistent with ``_SERVER_NAME_RE`` and the
#: secure_vault namespace validation idiom.
_SKILL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

#: Mount-time marker a pack may put in an MCP ``command``/``args`` entry to point at
#: a launcher FILE under the repo (e.g. ``<AKANA_REPO>/scripts/mcp_computer.py``).
#: The manifest can't know the absolute repo path, so ``ToolsAdapter.consent``
#: rewrites this token to the repo root when it writes the entry to
#: ``mcp_servers.yaml``. A launcher FILE (cwd-immune — it bootstraps sys.path from
#: its own ``__file__``) is required for packs whose child imports ``akana_server``,
#: which is NOT pip-installed and so only resolves via ``-m`` from the repo-root cwd.
_REPO_ROOT_MARKER = "<AKANA_REPO>"
#: Repo root: this file is ``<repo>/akana_server/packs/adapters.py``.
_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# 1. SkillsAdapter — the one that must work end-to-end.                        #
# --------------------------------------------------------------------------- #


class SkillsAdapter:
    """Copy ``contains.skills`` dirs into ``data_dir/skills`` then rescan.

    register: copy ``<pack>/skills/<id>/`` -> ``akana_skills_dir(data_dir)/<id>/``
    and refresh the registry so the skills resolve via ``scan_akana_skills``.
    unregister: remove the copied dirs and refresh again.

    Provenance: each install is recorded in ``data_dir/.pack_skills.json``
    (``skill_id -> owning pack_id``). This persists across restarts so the host
    can later tell a *pack-owned* skill copy apart from a *user-authored* one and
    prune orphans whose pack was deleted (via ``reconcile`` on host rescan).
    Skills NOT in this map (hand-authored via skill_teach) are never auto-removed.
    """

    content_type = "skills"
    #: Persistent ``skill_id -> pack_id`` map (next to packs_state.json, outside skills/).
    PROVENANCE_FILENAME = ".pack_skills.json"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        # pack_id -> [installed skill ids] so disable/uninstall is precise.
        self._installed: dict[str, list[str]] = {}

    def register(self, pack: LoadedPack) -> None:
        skill_ids = list(pack.manifest.contains.skills)
        if not skill_ids:
            return
        dest_root = akana_skills_dir(self._data_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        # Existing provenance: a skill id present here is already pack-owned and
        # may be safely overwritten (re-enable/reinstall). An id absent here whose
        # dir nonetheless exists is user-authored (skill_teach) — never clobber it.
        prov = self.provenance()
        # Bind _installed to the SAME list we append to inside the loop, before the
        # loop runs. A mid-loop copytree/rmtree failure (Windows file lock, disk
        # full) then still leaves _installed reflecting every skill actually copied
        # so far — so a later unregister removes exactly those dirs and
        # _forget_provenance only forgets what was torn down. Assigning _installed
        # once AFTER the loop (the previous behaviour) meant a raised exception
        # escaped register() with NO _installed entry, wedging the already-copied
        # skills: their dirs stayed on disk with provenance but disable removed 0
        # dirs while _forget_provenance stripped their provenance, permanently
        # orphaning them (register's user-authored guard then refuses to reinstall).
        installed: list[str] = []
        self._installed[pack.manifest.id] = installed
        for sid in skill_ids:
            # Path-traversal guard (untrusted pack content): the skill id must be a
            # plain name — an id containing separators or ".." would cause
            # dest=dest_root/sid to escape the root, enabling arbitrary directory
            # deletion/overwriting via rmtree/copytree.
            if not _SKILL_ID_RE.match(str(sid)):
                log.warning("pack %s: skipped invalid skill id: %r", pack.manifest.id, sid)
                continue
            src = pack.root / "skills" / sid
            if not src.is_dir():
                log.warning("pack %s: skill dir missing, skipping: %s", pack.manifest.id, sid)
                continue
            dest = dest_root / sid
            # Data-loss guard: refuse to overwrite a user-authored skill (a dir
            # that exists but has no provenance entry). Skill ids are short common
            # words, so a pack skill colliding with a hand-authored one is
            # plausible; clobbering + recording provenance would destroy the
            # user's skill AND delete it on pack disable/reconcile.
            if dest.exists() and sid not in prov:
                log.warning(
                    "pack %s: skill id %r collides with a user-authored skill — "
                    "keeping the user's skill, not installing the pack copy",
                    pack.manifest.id,
                    sid,
                )
                continue
            # Cross-pack collision guard: the id is already installed AND owned by
            # a DIFFERENT pack. Clobbering it would reassign provenance to us while
            # the other pack's in-memory _installed list still claims the id, so
            # disabling either pack would rmtree a still-enabled pack's live skill.
            # Keep the incumbent pack's copy rather than corrupting both.
            owner = prov.get(sid)
            if dest.exists() and owner is not None and owner != pack.manifest.id:
                log.warning(
                    "pack %s: skill id %r is already provided by pack %s — "
                    "keeping the existing pack's skill, not installing this copy",
                    pack.manifest.id,
                    sid,
                    owner,
                )
                continue
            # Preserve user-populated runtime state (a connector's resolved
            # contacts.json) across the re-copy below: copytree starts from a
            # clean dest, so without this the pack's shipped empty file would
            # clobber contacts the user built up at runtime.
            preserved: dict[str, bytes] = {}
            for rel in ("contacts.json",):
                p = dest / rel
                if p.is_file():
                    preserved[rel] = p.read_bytes()
            if dest.exists():
                shutil.rmtree(dest)
            # Build artefacts (node_modules, etc.) are not copied: they are
            # unnecessary for skill discovery and can be large. Runtime
            # dependencies are provided by the skill's own installer (npm
            # install) rather than by a snapshot copy.
            shutil.copytree(
                src,
                dest,
                ignore=shutil.ignore_patterns(
                    "node_modules", "__pycache__", ".git", "*.pyc", "*.log"
                ),
            )
            for rel, data in preserved.items():
                (dest / rel).write_bytes(data)
            # Record provenance for THIS skill immediately after its dir is in
            # place, not once after the whole loop: a mid-loop copytree failure
            # (Windows file lock, disk full) would otherwise leave already-copied
            # skills on disk with no provenance entry, wedging them permanently
            # as 'user-authored' (the line-108 guard refuses to overwrite them and
            # reconcile never prunes no-provenance dirs).
            self._record_provenance(pack.manifest.id, [sid])
            installed.append(sid)
        # ``installed`` IS self._installed[pack_id] (bound before the loop), so the
        # incremental appends above already committed each successful copy.
        pack.registered.setdefault("skills", []).extend(installed)
        self._refresh()

    def unregister(self, pack_id: str) -> None:
        dest_root = akana_skills_dir(self._data_dir)
        # Only remove a skill dir this pack still OWNS per the persisted
        # provenance: a colliding id may have been reassigned to another,
        # still-enabled pack since we recorded it in _installed, and deleting it
        # from our stale list would destroy that pack's live skill.
        prov = self.provenance()
        for sid in self._installed.pop(pack_id, []):
            if prov.get(sid) not in (None, pack_id):
                continue
            dest = dest_root / sid
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
        self._forget_provenance(pack_id)
        self._refresh()

    def drop_skills(self, pack_id: str, skill_ids: list[str]) -> list[str]:
        """Withdraw ONLY the named skill copies of a pack (targeted, precise).

        Used by a content-change rescan when skills were removed from a
        still-present pack: the skills the pack still ships are re-copied by
        ``register`` (which preserves runtime state like contacts.json), so only
        the ones the pack *dropped* need pruning. Returns the removed ids.
        ``_refresh`` is left to the caller so a batched drop+register triggers a
        single registry reload.

        Unlike ``unregister`` (which iterates ``_installed`` — dirs this pack
        actually copied), this iterates the MANIFEST diff, so the ownership guard
        must be strict: delete ONLY a copy whose provenance names THIS pack. A
        no-provenance dir is user-authored (a pack skill id can collide with a
        skill_teach one; ``register`` refuses to install over it, so it never gets
        provenance) — deleting it here would destroy the user's skill. An id owned
        by another pack must likewise be left alone.
        """
        if not skill_ids:
            return []
        dest_root = akana_skills_dir(self._data_dir)
        prov = self.provenance()
        removed: list[str] = []
        installed = self._installed.get(pack_id, [])
        for sid in skill_ids:
            if prov.get(sid) != pack_id:
                continue  # user-authored (no provenance) or another pack's — never touch it
            if not _SKILL_ID_RE.match(str(sid)):  # path-traversal guard
                continue
            dest = dest_root / sid
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            if sid in installed:
                installed.remove(sid)
            removed.append(sid)
        if pack_id in self._installed:
            self._installed[pack_id] = installed
        if removed:
            self._forget_provenance_ids(removed)
        return removed

    # -- provenance (skill_id -> pack_id), persisted across restarts ---------- #

    def _provenance_path(self) -> Path:
        return self._data_dir / self.PROVENANCE_FILENAME

    def provenance(self) -> dict[str, str]:
        """``skill_id -> owning pack_id`` (persisted; empty/corrupt file → {})."""
        path = self._provenance_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("%s unreadable/corrupt — treating as empty", path, exc_info=True)
            return {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _save_provenance(self, prov: dict[str, str]) -> None:
        path = self._provenance_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(prov, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            log.warning("%s could not be written — skill provenance not persisted", path, exc_info=True)

    def _record_provenance(self, pack_id: str, skill_ids: list[str]) -> None:
        if not skill_ids:
            return
        prov = self.provenance()
        for sid in skill_ids:
            prov[sid] = pack_id
        self._save_provenance(prov)

    def _forget_provenance(self, pack_id: str) -> None:
        prov = self.provenance()
        drop = [sid for sid, pid in prov.items() if pid == pack_id]
        if not drop:
            return
        for sid in drop:
            del prov[sid]
        self._save_provenance(prov)

    def _forget_provenance_ids(self, skill_ids: list[str]) -> None:
        prov = self.provenance()
        drop = [sid for sid in skill_ids if sid in prov]
        if not drop:
            return
        for sid in drop:
            del prov[sid]
        self._save_provenance(prov)

    def reconcile(self, present_pack_ids: set[str]) -> list[str]:
        """Prune skill copies whose owning pack id is not in ``present_pack_ids``.

        Driven by the provenance map: a recorded skill whose pack is gone (folder
        deleted) is removed from ``data_dir/skills`` and dropped from the map.
        Skills with NO provenance entry (user-authored) are never touched. Returns
        the removed skill ids.
        """
        prov = self.provenance()
        if not prov:
            return []
        dest_root = akana_skills_dir(self._data_dir)
        new_prov = dict(prov)
        removed: list[str] = []
        for sid, pack_id in prov.items():
            if pack_id in present_pack_ids:
                continue  # owning pack still present (enabled or disabled)
            if _SKILL_ID_RE.match(str(sid)):  # path-traversal guard
                dest = dest_root / sid
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
            del new_prov[sid]
            removed.append(sid)
        if removed:
            self._save_provenance(new_prov)
            self._refresh()
        return removed

    def refresh(self) -> None:
        """Force a registry reload (public entry point for the host).

        ``drop_skills`` defers the reload to the caller so a batched drop+register
        does ONE reload via ``register``'s own ``_refresh``; when ``register`` will
        NOT run one (the pack now ships no skills), the host calls this directly."""
        self._refresh()

    def _refresh(self) -> None:
        # Drop the module-level data_dir cache, then force a fresh scan for this
        # data_dir so callers (and tests) see the new state immediately.
        reload_skills()
        get_registry(self._data_dir).reload()


# --------------------------------------------------------------------------- #
# 2. ToolsAdapter — declare MCP servers + run preflight probes.               #
# --------------------------------------------------------------------------- #


@dataclass
class ToolProbeResult:
    name: str
    required: bool
    present: bool
    probe: str | None = None
    setup_skill: str | None = None
    install_hint: str | None = None
    detail: str | None = None


class ToolsMountError(Exception):
    """``mcp_servers.yaml`` could not be read or written — prevents overwriting the user file."""


class ToolsAdapter:
    """Record declared MCP servers, run probes, mount ONLY via consent.

    register: read ``dependencies.external_tools`` and run each ``probe``
    (best effort). NO side effects during enable — declared ``mcp_server`` tools
    wait in *pending_consent* state.

    consent: the sole write point. On user approval, adds the pack's MCP servers
    idempotently to akana's external-MCP mechanism (``<data_dir>/mcp_servers.yaml``,
    see ``orchestrator.mcp_config``); entries are marked ``managed_by: pack:<id>``.
    Non-pack (user) entries are NEVER overwritten. ``unregister``
    (disable/uninstall) withdraws the entries the pack mounted.
    """

    content_type = "tools"

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else None
        # pack_id -> declared external tool dicts
        self._declared: dict[str, list[dict[str, Any]]] = {}
        # pack_id -> last probe results
        self._probes: dict[str, list[ToolProbeResult]] = {}

    def register(self, pack: LoadedPack) -> None:
        tools = pack.manifest.dependencies.external_tools
        decls: list[dict[str, Any]] = []
        results: list[ToolProbeResult] = []
        for t in tools:
            decls.append(t.model_dump())
            results.append(self.probe(t.model_dump()))
        self._declared[pack.manifest.id] = decls
        self._probes[pack.manifest.id] = results
        if decls:
            pack.registered.setdefault("tools", []).extend(d["name"] for d in decls)

    def unregister(self, pack_id: str, *, preserve_mount: bool = False) -> None:
        """Drop the pack's in-memory declarations/probes and take down its MCP mount.

        ``preserve_mount`` splits the two teardown reasons: a *disable* (reversible)
        must NOT destroy the owner's prior consent — the yaml entries are flipped to
        ``enabled: false`` (``load_external_mcp_servers`` skips them, so no tools
        reach the LLM) and a later *enable* can restore them without a fresh consent.
        A *delete/uninstall* (``preserve_mount=False``, the default) truly removes the
        entries. Both paths always drop the in-memory declarations/probes.
        """
        self._declared.pop(pack_id, None)
        self._probes.pop(pack_id, None)
        try:
            if preserve_mount:
                disabled = self.set_enabled(pack_id, False)
                if disabled:
                    log.info("pack %s: MCP entries disabled (kept): %s", pack_id, ", ".join(disabled))
                return
            removed = self.unmount(pack_id)
        except ToolsMountError as e:  # withdraw is best-effort; must not block pack disable
            log.warning("pack %s: MCP unmount failed: %s", pack_id, e)
            return
        if removed:
            log.info("pack %s: MCP entries withdrawn: %s", pack_id, ", ".join(removed))

    def probe(self, tool: dict[str, Any]) -> ToolProbeResult:
        """Best-effort missing-tool detection.

        The contract's ``probe`` is a human-readable description, not an
        executable command, so we cannot reliably shell out. We treat the tool
        as *absent* unless an obvious binary on PATH proves otherwise — this is
        deliberately conservative so ``required`` tools surface their
        ``setup_skill`` rather than silently passing.
        """
        name = str(tool.get("name") or "?")
        present = self._binary_present(name)
        return ToolProbeResult(
            name=name,
            required=bool(tool.get("required")),
            present=present,
            probe=tool.get("probe"),
            setup_skill=tool.get("setup_skill"),
            install_hint=tool.get("install_hint"),
            detail=None if present else "probe could not confirm tool presence",
        )

    @staticmethod
    def _binary_present(name: str) -> bool:
        # Map a few known tool names to a candidate binary. Unknown tools are
        # reported absent (conservative).
        candidate = {"ghidra-mcp": "ghidra", "ghidra": "ghidra"}.get(name)
        if not candidate:
            return False
        try:
            return shutil.which(candidate) is not None
        except (OSError, subprocess.SubprocessError):
            return False

    # -- exposure for the host / API ----------------------------------------- #

    def missing_required(self, pack_id: str) -> list[ToolProbeResult]:
        return [r for r in self._probes.get(pack_id, []) if r.required and not r.present]

    # -- consent-gated MCP mount (mcp_servers.yaml) -------------------------- #

    @staticmethod
    def _marker(pack_id: str) -> str:
        return f"pack:{pack_id}"

    @staticmethod
    def _resolve_repo_marker(entry: dict[str, Any]) -> dict[str, Any]:
        """Rewrite ``<AKANA_REPO>`` in an entry's ``command``/``args`` to the repo root.

        Lets a pack ship a launcher-FILE spawn (cwd-immune) without knowing the
        absolute repo path at author time. Only the ``command`` string and ``args``
        list are rewritten (the two places a launcher path can appear); other fields
        are left untouched. Applied at mount time so the resolved absolute path is
        what lands in ``mcp_servers.yaml``.
        """
        repo = str(_REPO_ROOT)
        cmd = entry.get("command")
        if isinstance(cmd, str) and _REPO_ROOT_MARKER in cmd:
            entry["command"] = cmd.replace(_REPO_ROOT_MARKER, repo)
        args = entry.get("args")
        if isinstance(args, list):
            entry["args"] = [
                a.replace(_REPO_ROOT_MARKER, repo) if isinstance(a, str) else a
                for a in args
            ]
        return entry

    def _config_path(self) -> Path:
        if self._data_dir is None:
            raise ToolsMountError("ToolsAdapter was set up without a data_dir — MCP mount disabled")
        return self._data_dir / CONFIG_FILENAME

    def _read_config(self) -> dict[str, Any]:
        """Read the existing yaml; on parse error do NOT write (preserve the user file)."""
        path = self._config_path()
        if not path.is_file():
            return {"servers": {}}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise ToolsMountError(f"{path} unreadable/corrupt — mount cancelled: {e}") from e
        if raw is None:
            return {"servers": {}}
        if not isinstance(raw, dict):
            raise ToolsMountError(f"{path} root is not a mapping — mount cancelled")
        servers = raw.get("servers")
        if servers is None:
            raw["servers"] = {}
        elif not isinstance(servers, dict):
            raise ToolsMountError(f"{path}: 'servers' is not a mapping — mount cancelled")
        return raw

    def _write_config(self, data: dict[str, Any]) -> None:
        path = self._config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8"
            )
        except OSError as e:
            raise ToolsMountError(f"{path} could not be written: {e}") from e

    def mcp_server_tools(self, pack_id: str) -> list[dict[str, Any]]:
        """The ``kind=mcp_server`` tools declared by the pack."""
        return [
            t
            for t in self._declared.get(pack_id, [])
            if str(t.get("kind") or "mcp_server") == "mcp_server"
        ]

    def mounted_server_names(self, pack_id: str) -> list[str]:
        """Entry names added to mcp_servers.yaml on behalf of this pack."""
        if self._data_dir is None:
            return []
        try:
            servers = self._read_config()["servers"]
        except ToolsMountError:
            return []
        marker = self._marker(pack_id)
        return sorted(
            n
            for n, cfg in servers.items()
            if isinstance(cfg, dict) and cfg.get("managed_by") == marker
        )

    def pending_consent(self, pack_id: str) -> list[str]:
        """MCP server names declared but not yet approved and mounted."""
        mounted = set(self.mounted_server_names(pack_id))
        return [
            str(t.get("name"))
            for t in self.mcp_server_tools(pack_id)
            if str(t.get("name")) not in mounted
        ]

    def consent(
        self,
        pack_id: str,
        server_configs: dict[str, dict[str, Any]] | None = None,
        *,
        approved: bool = False,
    ) -> dict[str, list[str]]:
        """The sole ``mcp_servers.yaml`` write point — MUST carry explicit approval.

        ``approved`` is the human-in-the-loop gate. It is **False by default**:
        without an affirmative approval the method NEVER writes yaml — it classifies
        each declared server and returns the mountable names under ``pending``
        instead of ``mounted`` (a preview). Only ``approved=True`` (the
        bearer-protected ``POST /packs/consent`` route, i.e. the authenticated
        owner) performs the idempotent mount. This closes the "agent consents to
        itself" hole: an agent-invoked path that omits ``approved`` cannot grant
        itself an MCP mount.

        Server config priority: caller-supplied ``server_configs[name]`` >
        the tool's ``mcp`` (extra) field in the manifest. Tools with no config
        fall into ``needs_config`` (no fabricated entry is ever written); if a
        name clashes with an existing entry not owned by this pack, it falls into
        ``conflicts``.
        """
        tools = self.mcp_server_tools(pack_id)
        marker = self._marker(pack_id)
        mounted: list[str] = []
        pending: list[str] = []
        needs_config: list[str] = []
        conflicts: list[str] = []
        invalid: list[str] = []
        if not tools:
            return {
                "mounted": [],
                "pending": [],
                "needs_config": [],
                "conflicts": [],
                "invalid": [],
            }

        data = self._read_config()
        servers: dict[str, Any] = data["servers"]
        changed = False
        for t in tools:
            name = str(t.get("name") or "").strip()
            if not _SERVER_NAME_RE.fullmatch(name) or name in RESERVED_SERVER_NAMES:
                invalid.append(name or "?")
                continue
            cfg = (server_configs or {}).get(name) or t.get("mcp")
            if not isinstance(cfg, dict) or not cfg:
                needs_config.append(name)
                continue
            if not (cfg.get("command") or cfg.get("url")):
                invalid.append(name)
                continue
            existing = servers.get(name)
            if existing is not None and not (
                isinstance(existing, dict) and existing.get("managed_by") == marker
            ):
                conflicts.append(name)  # user entry — never overwrite
                continue
            # Consent gate: without explicit approval the entry is only a preview —
            # it is reported as pending and NOTHING is written.
            if not approved:
                pending.append(name)
                continue
            entry = self._resolve_repo_marker(dict(cfg))
            entry["managed_by"] = marker
            if existing != entry:
                servers[name] = entry
                changed = True
            mounted.append(name)
        if changed:
            self._write_config(data)
        return {
            "mounted": mounted,
            "pending": pending,
            "needs_config": needs_config,
            "conflicts": conflicts,
            "invalid": invalid,
        }

    def unmount(self, pack_id: str) -> list[str]:
        """Withdraw all entries this pack mounted (idempotent)."""
        if self._data_dir is None or not self._config_path().is_file():
            return []
        data = self._read_config()
        servers: dict[str, Any] = data["servers"]
        marker = self._marker(pack_id)
        removed = [
            n
            for n, cfg in list(servers.items())
            if isinstance(cfg, dict) and cfg.get("managed_by") == marker
        ]
        for n in removed:
            del servers[n]
        if removed:
            self._write_config(data)
        return sorted(removed)

    def set_enabled(self, pack_id: str, enabled: bool) -> list[str]:
        """Flip the ``enabled`` flag on this pack's mounted entries (idempotent).

        Reversible disable/enable without losing the owner's consent: the yaml entry
        stays in place (``managed_by`` preserved, so ``mounted_server_names`` still
        reports it and a later enable can restore it) but ``enabled: false`` makes
        ``load_external_mcp_servers`` skip it at runtime. Only entries whose flag
        actually changes are rewritten. Returns the affected names (sorted)."""
        if self._data_dir is None or not self._config_path().is_file():
            return []
        data = self._read_config()
        servers: dict[str, Any] = data["servers"]
        marker = self._marker(pack_id)
        changed: list[str] = []
        for name, cfg in servers.items():
            if not (isinstance(cfg, dict) and cfg.get("managed_by") == marker):
                continue
            # Default is enabled=True (mcp_config treats a missing key as enabled),
            # so only rewrite when the effective flag differs from the target.
            if bool(cfg.get("enabled", True)) == enabled:
                continue
            cfg["enabled"] = enabled
            changed.append(name)
        if changed:
            self._write_config(data)
        return sorted(changed)


# --------------------------------------------------------------------------- #
# 3. PersonasAdapter — load personas/*.yaml for prompt injection.            #
# --------------------------------------------------------------------------- #


class PersonasAdapter:
    """Load ``personas/<id>.yaml`` (legacy ``plugins/personas/``) and expose active personas."""

    content_type = "personas"

    def __init__(self) -> None:
        # persona_id -> persona dict (with _pack_id marker)
        self._active: dict[str, dict[str, Any]] = {}
        self._by_pack: dict[str, list[str]] = {}

    def register(self, pack: LoadedPack) -> None:
        # Record ownership incrementally so a later persona raising does not
        # strand the personas already added to _active as unremovable actives:
        # _by_pack must reflect every id in _active for unregister() to withdraw
        # it. _load itself is now defensive (a malformed YAML → None, skipped).
        ids: list[str] = []
        self._by_pack[pack.manifest.id] = ids
        for pid in pack.manifest.contains.personas:
            data = self._load(pack.root, pid)
            if data is None:
                log.warning("pack %s: persona file missing/unreadable: %s", pack.manifest.id, pid)
                continue
            # Cross-pack collision guard (mirrors SkillsAdapter): persona ids are
            # short common words, so two loaded packs shipping the same id is
            # plausible (e.g. a copied pack). Do NOT let a later-registered pack
            # overwrite an ACTIVE persona owned by a different pack — that would
            # silently shadow the incumbent's persona, and a later unregister would
            # then pop it even though the incumbent pack is still enabled. Keep the
            # incumbent; skip this copy.
            existing = self._active.get(pid)
            if existing is not None and existing.get("_pack_id") not in (None, pack.manifest.id):
                log.warning(
                    "pack %s: persona id %r is already provided by pack %s — "
                    "keeping the existing pack's persona, not installing this copy",
                    pack.manifest.id,
                    pid,
                    existing.get("_pack_id"),
                )
                continue
            data["_pack_id"] = pack.manifest.id
            self._active[pid] = data
            ids.append(pid)
        if ids:
            pack.registered.setdefault("personas", []).extend(ids)

    def unregister(self, pack_id: str) -> None:
        for pid in self._by_pack.pop(pack_id, []):
            # Only withdraw a persona this pack still OWNS: a colliding id could
            # belong to another, still-enabled pack (the incumbent that shadowed
            # this pack's copy), and popping it would drop that pack's live persona.
            active = self._active.get(pid)
            if active is not None and active.get("_pack_id") not in (None, pack_id):
                continue
            self._active.pop(pid, None)

    @staticmethod
    def _load(root: Path, persona_id: str) -> dict[str, Any] | None:
        # Path-traversal guard (untrusted pack content): the persona id is joined as a
        # FILE NAME under the pack root, so it must be a plain name — an id containing
        # separators or ".." escapes the pack dir, and an ABSOLUTE id replaces the base
        # entirely (pathlib join semantics), letting a manifest read any .yaml/.yml on
        # disk into the system prompt. Mirrors SkillsAdapter's ``_SKILL_ID_RE`` guard.
        if not _SKILL_ID_RE.match(str(persona_id)):
            log.warning("skipped invalid persona id (path-traversal guard): %r", persona_id)
            return None
        # New standard: ``personas/<id>.yaml``. Legacy: ``plugins/personas/<id>.yaml``.
        for base in (root / "personas", root / "plugins" / "personas"):
            for ext in (".yaml", ".yml"):
                p = base / f"{persona_id}{ext}"
                if p.is_file():
                    # A malformed persona file must not raise out of register()
                    # and strand earlier personas — treat it as missing (None).
                    try:
                        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                    except (OSError, yaml.YAMLError):
                        log.warning("persona file unreadable/corrupt: %s", p, exc_info=True)
                        return None
                    # personas may be stored under a ``persona:`` key.
                    return raw.get("persona", raw) if isinstance(raw, dict) else None
        return None

    def get_active_personas(self) -> list[dict[str, Any]]:
        return list(self._active.values())


__all__ = [
    "PersonasAdapter",
    "SkillsAdapter",
    "ToolProbeResult",
    "ToolsAdapter",
    "ToolsMountError",
    "autodiscover_contents",
]
