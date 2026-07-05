"""Standalone pack-validation CLI — validate any pack without a running server,
print the canonical JSON Schema, or show a pack summary.

Run:  python -m packs.contract.cli <command> [arg]
"""

from __future__ import annotations

import argparse
import json
import sys

from packs.contract.manifest import (
    PackManifest,
    load_manifest,
    validate_pack_dir,
)


def _schema_json() -> str:
    return json.dumps(
        PackManifest.model_json_schema(), indent=2, ensure_ascii=False
    )


def _cmd_validate(pack_dir: str) -> int:
    result = validate_pack_dir(pack_dir)
    status = "ok" if result.ok else "FAIL"
    print(f"[{status}] {pack_dir}")
    for e in result.errors:
        print(f"  error: {e}")
    for w in result.warnings:
        print(f"  warning: {w}")
    return 0 if result.ok else 1


def _cmd_schema() -> int:
    print(_schema_json())
    return 0


def _cmd_info(pack_dir: str) -> int:
    from pathlib import Path

    manifest_path = Path(pack_dir) / "pack.yaml"
    try:
        m = load_manifest(manifest_path)
    except Exception as e:  # noqa: BLE001 — clear error for the user
        print(f"could not read manifest: {e}", file=sys.stderr)
        return 1

    perms = m.permissions
    network = ", ".join(perms.network) if perms.network else "(offline)"
    print(f"id:           {m.id}")
    print(f"version:      {m.version}")
    print(f"title:        {m.title or '-'}")
    print(f"skills:       {len(m.contains.skills)}")
    print("permissions:")
    print(f"  sandbox:    {perms.sandbox}")
    print(f"  network:    {network}")
    print(f"  vault_read: {', '.join(perms.secure_vault_read) or '-'}")
    print(f"  file_system: {', '.join(perms.file_system) or '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="packs.contract.cli",
        description="Standalone pack validator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate a pack directory")
    p_validate.add_argument("pack_dir", help="path to the pack directory")

    sub.add_parser("schema", help="print the canonical JSON Schema")

    p_info = sub.add_parser("info", help="print a pack summary")
    p_info.add_argument("pack_dir", help="path to the pack directory")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _cmd_validate(args.pack_dir)
    if args.command == "schema":
        return _cmd_schema()
    if args.command == "info":
        return _cmd_info(args.pack_dir)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
