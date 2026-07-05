# Pack: validate (schema + files)

TAKES a pack directory and validates its contract compliance. Read-only; changes no files.

## Preconditions

- The pack directory to validate must exist (`packs/<name>/`).
- The repo's `packs.contract.cli` module must be reachable (local, in-repo).

## Steps

1. Run the validator:
   ```
   python -m packs.contract.cli validate packs/<name>
   ```
   (In this repo the interpreter may be `venv/bin/python`.)
2. For a summary: `python -m packs.contract.cli info packs/<name>`.
3. Interpret the output:
   - `[ok]` → schema + file presence passed (skills are **auto-discovered** from
     `skills/`, the persona from `personas/`, and validated; `contains` is not needed).
   - `error:` lines → **blocking** (manifest invalid, or a skill's manifest.yaml/SKILL.md
     missing). Must be fixed one by one.
   - `warning:` lines → good to fix but not blocking (e.g. persona file not found, a
     required external_tool with no purpose).
4. Extra check: do the skill ids resolve in the akana registry — verified in the
   conformance test with `scan_akana_skills`; run that test if needed.
5. List errors clearly to the user; for each, suggest a concrete fix (return to
   `pack_skill_add`/`pack_scaffold` if needed).

## Failure modes

- `python: command not found`: try `venv/bin/python -m packs.contract.cli ...`.
- Wrong directory path: ask/confirm the correct `packs/<name>` path.

## Notes

- Don't consider a pack "ready" before validation passes.
- This skill only produces a report; the fix is applied by the relevant author skill.
