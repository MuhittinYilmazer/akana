# Pack Interface Contract (L2)

> The **authoritative contract** between a pack (producer) and akana (consumer).
> The consumer side is implemented against this document plus the `packs/contract/`
> code. The single source of truth is the code:
> `packs/contract/manifest.py` (schema) + `packs/contract/host.py` (protocols).

> ## ⚠️ Enforcement status (v0.1) — HONEST LABEL
>
> This document is the contract; where the reference consumer (`akana_server`)
> does **less** than the target spec, that gap is stated inline. What v0.1 actually
> enforces:
> - **Enable / disable** (§4): `AkanaPackHost.enable/disable` hot-reloads a pack's
>   content at runtime; the disabled state is persisted to `data_dir/packs_state.json`,
>   and `register_all` reads it and does not register disabled packs. `disable` only
>   revokes **derived** registrations (skill copies, in-memory personas, pack-managed
>   MCP entries); the source `packs/<id>/` folder is left untouched → fully reversible.
> - **Rescan reconciliation** (§4): `rescan()` diffs the loaded set against `packs/`
>   on disk — a newly dropped folder is hot-registered, a vanished folder is
>   hot-deleted (its skills + persona disappear from Settings immediately, no
>   restart).
> - **Consent-gated MCP mount** (§5.1): a pack's MCP server is written into
>   `mcp_servers.yaml` **only** through `ToolsAdapter.consent(..., approved=True)`,
>   exposed as the bearer-protected `POST /packs/consent` route. `consent()` without
>   an affirmative approval writes nothing and returns the servers as `pending`, so a
>   pack cannot self-mount just by being enabled.
>
> **NOT enforced in v0.1** (advisory metadata — present in the schema, not wired to
> behavior):
> - **`requires_approval`**: kept as an inert per-skill flag for downstream consumers
>   that want to build their own gate; the reference consumer never stops on it.
> - **`permissions`** (§5): an **optional** schema block; when present it is
>   declarative only — no PolicyEngine/sandbox enforcement in v0.1.
> - **Skill turn-injection**: skills a pack ships are auto-injected into the turn with
>   full autonomy — there is no per-skill approval gate. The one code-backed gate is
>   the MCP-mount consent above.
>
> So parts of §5 describe "what should be," not "what is." Open-source consumers should
> write their own enforcement layer if they want one; **downloading a pack means
> trusting its content — review it before you drop it into `packs/`.**

## 0. Roles

| Party | Responsibility | Location |
|-------|----------------|----------|
| **Producer** (pack) | `pack.yaml` + content files (skills/plugins/...) | `packs/<name>/` |
| **Contract** (us) | schema, protocol, spec, conformance tests | `packs/contract/`, this file |
| **Consumer** (akana) | `PackHost` + `ContentAdapter` implementation | `akana_server/` |

## 1. Pack directory layout (producer contract)

```
packs/<name>/
  pack.yaml                       # L2 manifest — PackManifest schema (§2)
  skills/<skill_id>/
    manifest.yaml                 # akana SkillEntry schema (registry.py)
    SKILL.md                      # procedure (markdown, starts with "# Title")
  personas/<id>.yaml              # L3 persona (canonical location)
  install.sh                      # OPTIONAL — consent-gated external-tool setup
```

The reference consumer registers exactly three content types: **skills**,
**personas**, and **tools** (external MCP declarations). Skills and personas are
**auto-discovered** by scanning these directories — a canonical pack carries no
`contains` block (it remains an optional legacy hint). Personas live in
`personas/<id>.yaml`; the legacy `plugins/personas/<id>.yaml` path is also scanned for
backward compatibility. A legacy `plugins/` folder (entity types, tool hooks) is
**no longer consumed** — it is ignored.

The two **manifests** never get mixed up:
- `pack.yaml` → **pack** level (PackManifest, this contract).
- `skills/<id>/manifest.yaml` → **skill** level (akana `SkillEntry`, the existing `registry.py`).

## 2. `pack.yaml` schema

Canonical definition: `packs/contract/manifest.py::PackManifest`. JSON Schema:
`PackManifest.model_json_schema()`. It lives under the `pack:` key at the root of
`pack.yaml`.

Required: `id` (`namespace/name`) and `version` (semver). `permissions` is
**optional** (advisory metadata, §5). A canonical pack omits the `contains` block
entirely — content is auto-discovered (§1); when present, `contains` may list
`skills` / `personas` / `tools` as optional **registration references** to the
relevant engine (§3). Any other key (a legacy `isolation` / `learning` block, or
`contains.workflows` / `ui_cards` / `plugins` / `memory_schema_extensions`) is
accepted for backward compatibility but **ignored** — it maps to no behavior.

## 3. Registration boundary — "what akana may use"

Each content type is bound to its engine through a `ContentAdapter`
(`packs/contract/host.py`):

| content type | Producer provides | Consumer adapter → engine | Status today |
|--------------|-------------------|---------------------------|--------------|
| `skills` | `skills/<id>/{manifest.yaml,SKILL.md}` | `SkillsAdapter` → copy into `data_dir/skills` + registry scan + turn injection | ✓ |
| `tools` | `dependencies.external_tools` (MCP decl) | `ToolsAdapter` → probe on enable; **consent-gated MCP mount** (§5.1) | declare + probe ✓ / mount via consent ✓ |
| `personas` | `personas/<id>.yaml` | `PersonasAdapter` → load + expose for system-prompt injection | ✓ |
| `permissions` *(optional)* | `pack.yaml` | PolicyEngine: net/sandbox/vault/fs enforce | ✗ (advisory only) |

`ToolsAdapter` **declares + probes** external tools when a pack is enabled but never
mounts them automatically; the mount is a separate, consent-gated step (§5.1).

## 4. Lifecycle (state machine) — *enabled ⇄ disabled is enforced; full autonomy, no gate*

> **v0.1 reality:** a newly discovered pack comes up directly as `ENABLED` — there is
> **no quarantine and no approval gate**. The `enabled ⇄ disabled` transitions are REAL
> (`AkanaPackHost.enable/disable`, a hot-reload); the disabled state is written to
> `data_dir/packs_state.json` and managed from Settings → Packs. `rescan()` keeps the
> loaded set in sync with `packs/` on disk — drop a folder in to hot-add it, delete a
> folder to hot-remove it (skills + persona vanish from Settings with no restart).

Source: `packs/contract/host.py::PackState` (only `ENABLED` / `DISABLED` exist).

```
discover → enabled ⇄ disabled
              │
              └──→ removed (folder deleted → rescan hot-deletes; there is no
                   persistent "uninstalled" state)
```

| `PackHost` method | Does |
|-------------------|------|
| `discover()` | Scan pack directories → `PackRef[]` |
| `validate(ref)` | Schema + file existence (`validate_pack_dir`) |
| `load(ref)` | Load the manifest into memory (content registered on enable) |
| `enable(id)` | Register content via the `ContentAdapter`s (hot-reload) |
| `disable(id)` | Revoke content; data remains, nothing stays active |
| `rescan()` | Reconcile loaded set with `packs/`: hot-add new, hot-delete vanished |

## 5. Isolation + security enforcement — *optional target spec; declarative in v0.1*

> **v0.1 reality:** `permissions` is an **optional** block, and even when present it
> is NOT wired to any enforcement — it sits in the schema as advisory metadata. There
> is no sandbox and no per-skill approval gate. The one real, code-backed protection is
> the **consent-gated MCP mount** (§5.1). Trust comes from reviewing a pack's content
> before you install it. (The former `isolation` block was removed from the schema.)

These fields describe the *target* enforcement a downstream consumer could build:

- If `permissions.network` is empty, the pack is **offline**; a PolicyEngine would close egress.
- The `permissions.sandbox` tier (`host|container|gvisor|microvm|wasm|vm`) would be enforced
  when running tools.
- If `secure_vault_read` is non-empty, explicit consent would be required.

### 5.1 Dependency preflight + consent-gated MCP mount

Each `dependencies.external_tools[]` entry may carry a `probe` (to detect absence),
an `install_hint`, and a `setup_skill`. When a pack is enabled the consumer runs the
`probe` of the `required` tools (`ToolsAdapter.register`); missing required tools
surface via `missing_required` (shown in the pack view).

**The MCP mount is a distinct, consent-gated step — it never happens on enable.** A
pack's MCP server is written into `data_dir/mcp_servers.yaml` **only** through
`ToolsAdapter.consent(pack_id, approved=True)`, which is the sole write point and
never overwrites an entry the user placed by hand. This is exposed as bearer-protected
HTTP so a human (not the agent) drives it:

| Route | Does |
|-------|------|
| `GET /packs/consents[?pack_id=]` | Per-pack MCP consent state: `pending` vs. `mounted` server names |
| `POST /packs/consent` `{pack_id, server_configs?}` | Approve + idempotently mount the pack's MCP servers |
| `POST /packs/consent/revoke` `{pack_id}` | Withdraw the entries the pack mounted |

`consent()` called **without** `approved=True` writes nothing and reports the
mountable servers as `pending`, so a pack cannot self-mount merely by being enabled.
In browser-pack the browser binary itself is installed by the `browser_setup` skill
(consent-gated); the MCP entry is mounted by `install.sh`, which must route through
`consent(..., approved=True)`.

## 6. Reference packs: `browser-pack`, `pack-author-pack`

These two shipping packs are the **reference implementations** of this contract;
`tests/test_pack_contract.py` validates each against the schema and the akana skill
registry → proof that they are "ready." Each one anchors a different part of the
contract:

- **`pack-author-pack`** — the **canonical reference architecture** every pack should
  follow: pure auto-discovery (no `contains` block), minimal manifests (no
  `permissions`/`isolation`), a `pack_architect` persona, and offline skills that use
  the repo's own `packs.contract.cli` locally. Start here when authoring a new pack.
- **`browser-pack`** — the external-tool path (§5.1): declares a `required` MCP tool
  with a `probe` + a consent-gated `browser_setup` skill, and ships an `install.sh`
  that wraps the consent-gated MCP mount.

When the consumer implements `PackHost` and calls `register_all()`, each pack's
auto-discovered skills + persona are registered automatically.

## 7. Minimal getting-started for the consumer

1. Import `packs/contract` (`PackManifest`, `PackHost`, `ContentAdapter`, `PackState`).
2. Choose the discovery directories (`~/.akana/packs/` + the repo `packs/`).
3. Write the three `ContentAdapter`s (skills/tools/personas) — the §3 mapping.
4. Implement `PackHost` with the lifecycle (§4) — enable/disable + rescan reconciliation,
   plus the consent-gated MCP mount (§5.1).
5. `tests/test_pack_contract.py` must stay green; then call `register_all()` to load the shipping packs.
