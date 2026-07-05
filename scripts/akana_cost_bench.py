#!/usr/bin/env python3
"""Akana vs bare claude — measures per-turn token/cost overhead.

Runs both arms with the SAME ``claude`` CLI, the SAME message sequence, the SAME
model, and leaves Akana's prompt shape as the only variable:

* **normal** — plain ``claude -p <message> --model M [--resume sid]``. This is
  "using Claude normally"; the baseline (tool schemas + system prompt) is already
  included.
* **akana** — the same call + Akana's real add-ons: ``--append-system-prompt``
  (``CHAT_SYSTEM_PREFIX``), ``--mcp-config`` (real ``mcp_servers_payload``) and
  full-tools flags. Mirror of ``claude_provider._build_args`` / ``_tool_flags``.

Each arm continues ITS OWN session with ``--resume`` (as Akana does) → history is
not resent, so the comparison is fair. The token count (cache-independent) is the
primary metric; since ``total_cost_usd`` is sensitive to cache timing, a
cache-independent "list price" is also reported.

Usage (from the project root)::

    venv/bin/python scripts/akana_cost_bench.py                 # 10 messages, 3 reps
    venv/bin/python scripts/akana_cost_bench.py --reps 5 --model claude-haiku-4-5
    venv/bin/python scripts/akana_cost_bench.py --no-mcp        # isolate the persona
    venv/bin/python scripts/akana_cost_bench.py --messages my_chat.txt --out rapor.json

NOTE: Only measures the claude path (exact usage comes from the claude CLI).
Cursor's usage carries no cost (it is estimated), so it is out of scope. Messages
must be CONVERSATIONAL — requests that trigger file/bash operations make the two
arms incomparable (the akana arm can actually run tools with bypassPermissions).
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

# When run from ``scripts/``, sys.path[0] is the script directory; prepend the
# root so ``akana_server`` (project root) can be imported.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- price table: mirror of base.py:_PRICING ($/MTok: input, output) ----------
_PRICING: list[tuple[str, float, float]] = [
    ("opus", 15.0, 75.0),
    ("haiku", 0.80, 4.0),
    ("sonnet", 3.0, 15.0),
]
_PRICING_DEFAULT = (3.0, 15.0)


def price_for(model: str) -> tuple[float, float]:
    tag = (model or "").lower()
    for kw, inp, outp in _PRICING:
        if kw in tag:
            return inp, outp
    return _PRICING_DEFAULT


# --- import Akana add-ons (degrade gracefully if absent) ----------------------
def load_akana_bits() -> tuple[str | None, dict | None, str]:
    """(CHAT_SYSTEM_PREFIX, mcp_payload, claude_bin) — None's if the import fails."""
    persona: str | None = None
    mcp_payload: dict | None = None
    claude_bin = "claude"
    try:
        from akana_server.persona.builtin import CHAT_SYSTEM_PREFIX

        persona = CHAT_SYSTEM_PREFIX
    except Exception as exc:  # noqa: BLE001
        print(f"[warning] could not import persona ({exc}); akana arm runs without persona", file=sys.stderr)
    try:
        from akana_server.config import load_settings
        from akana_server.orchestrator.memory_tools import mcp_servers_payload

        settings = load_settings()
        claude_bin = settings.claude_bin or "claude"
        mcp_payload = mcp_servers_payload(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"[warning] could not obtain MCP payload ({exc}); akana arm runs without MCP", file=sys.stderr)
    return persona, mcp_payload, claude_bin


DEFAULT_MESSAGES = [
    "Selam, bugün biraz Python'la uğraşacağım. Hazır mısın?",
    "Liste comprehension ile generator expression farkını kısaca anlat.",
    "Generator'ın bellek avantajını küçük bir örnekle göster.",
    "Peki async generator ne zaman işime yarar?",
    "Dekoratör konusunu özetle, çok uzatma.",
    "functools.lru_cache ne işe yarar?",
    "lru_cache'in maxsize parametresini nasıl seçerim?",
    "Threading mi asyncio mu — ne zaman hangisi?",
    "GIL'i tek cümleyle açıkla.",
    "Teşekkürler, kısa bir kapanış notu yaz.",
]


def _coerce_int(v: object) -> int:
    try:
        return max(0, int(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def run_claude(argv: list[str], timeout: float) -> dict:
    """A single ``claude`` call → normalized metric dict. Never raises on error."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"timeout>{timeout:.0f}s"}
    except FileNotFoundError:
        return {"ok": False, "err": f"binary not found: {argv[0]}"}
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "err": f"rc={proc.returncode} {proc.stderr.strip()[:200]}"}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "err": f"json-parse: {proc.stdout.strip()[:200]}"}
    result = data
    if isinstance(data, list):  # some versions return an event array
        result = next(
            (e for e in data if isinstance(e, dict) and e.get("type") == "result"),
            data[-1] if data else {},
        )
    if not isinstance(result, dict):
        return {"ok": False, "err": "unexpected json shape"}
    usage = result.get("usage") or {}
    inp = _coerce_int(usage.get("input_tokens"))
    cr = _coerce_int(usage.get("cache_read_input_tokens"))
    cw = _coerce_int(usage.get("cache_creation_input_tokens"))
    out = _coerce_int(usage.get("output_tokens"))
    cost = result.get("total_cost_usd")
    return {
        "ok": not result.get("is_error", False),
        "err": None if not result.get("is_error", False) else str(result.get("subtype")),
        "input": inp,
        "cache_read": cr,
        "cache_creation": cw,
        "output": out,
        "ctx_in": inp + cr + cw,  # total input into the model (cache included)
        "real_cost": float(cost) if isinstance(cost, (int, float)) else None,
        "session_id": result.get("session_id"),
    }


def build_raw_argv(claude_bin: str, msg: str, model: str, sid: str | None) -> list[str]:
    argv = [claude_bin, "-p", msg, "--output-format", "json", "--model", model]
    if sid:
        argv += ["--resume", sid]
    return argv


def build_akana_argv(
    claude_bin: str,
    msg: str,
    model: str,
    sid: str | None,
    persona: str | None,
    mcp_payload: dict | None,
) -> list[str]:
    """Mirror of ``claude_provider._build_args`` + ``_tool_flags`` (full-tools default)."""
    argv = [claude_bin, "-p", msg, "--output-format", "json", "--model", model]
    if sid:
        argv += ["--resume", sid]
    if persona:
        argv += ["--append-system-prompt", persona]
    if mcp_payload:
        argv += ["--mcp-config", json.dumps({"mcpServers": mcp_payload})]
        allowed = [f"mcp__{n}" for n in mcp_payload] + ["Read", "Grep", "Glob"]
    else:
        allowed = ["Read", "Grep", "Glob"]
    # full-tools default ON → bypassPermissions + allowedTools
    argv += ["--permission-mode", "bypassPermissions", "--allowedTools", ",".join(allowed)]
    return argv


def run_conversation(arm: str, builder, messages: list[str], timeout: float, sleep: float) -> list[dict]:
    """Run one arm end to end; each turn resumes with the previous turn's session_id."""
    sid: str | None = None
    turns: list[dict] = []
    for i, msg in enumerate(messages, 1):
        argv = builder(msg, sid)
        m = run_claude(argv, timeout)
        m["turn"] = i
        turns.append(m)
        if not m["ok"]:
            print(f"    [{arm}] turn {i} ERROR: {m['err']}", file=sys.stderr)
            # keep sid so the resume chain doesn't break; continue
        elif m.get("session_id"):
            sid = m["session_id"]
        if sleep:
            time.sleep(sleep)
    return turns


def conv_totals(turns: list[dict]) -> dict:
    ok = [t for t in turns if t.get("ok")]
    return {
        "ctx_in": sum(t["ctx_in"] for t in ok),
        "output": sum(t["output"] for t in ok),
        "cache_read": sum(t["cache_read"] for t in ok),
        "cache_creation": sum(t["cache_creation"] for t in ok),
        "real_cost": sum((t["real_cost"] or 0.0) for t in ok),
        "real_cost_complete": all(t.get("real_cost") is not None for t in ok),
        "ok_turns": len(ok),
        "total_turns": len(turns),
    }


def list_price(ctx_in: float, output: float, model: str) -> float:
    in_p, out_p = price_for(model)
    return ctx_in * in_p / 1_000_000 + output * out_p / 1_000_000


def mean_std(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    return statistics.fmean(xs), (statistics.pstdev(xs) if len(xs) > 1 else 0.0)


def fmt_pair(mean: float, std: float, intlike: bool = False) -> str:
    if intlike:
        return f"{mean:,.0f} ± {std:,.0f}"
    return f"{mean:,.4f} ± {std:,.4f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Akana vs bare claude token/cost comparison")
    ap.add_argument("--model", default="claude-sonnet-4-6", help="Fixed for BOTH arms (avoids opus/sonnet confound)")
    ap.add_argument("--reps", type=int, default=3, help="How many times to repeat the whole conversation (mean±std)")
    ap.add_argument("--turns", type=int, default=0, help="Limit to the first N messages (0=all)")
    ap.add_argument("--messages", type=Path, help="One-message-per-line file (defaults to the built-in 10)")
    ap.add_argument("--no-mcp", action="store_true", help="Drop MCP tools from the akana arm (isolate the persona)")
    ap.add_argument("--no-persona", action="store_true", help="Drop the persona from the akana arm (isolate MCP)")
    ap.add_argument("--warmup", action="store_true", help="One discarded turn per arm before measuring (warm the cache)")
    ap.add_argument("--timeout", type=float, default=240.0, help="Seconds per turn")
    ap.add_argument("--sleep", type=float, default=0.0, help="Wait between calls (rate-limit)")
    ap.add_argument("--out", type=Path, help="Write the raw results here as JSON")
    args = ap.parse_args()

    messages = (
        [ln.strip() for ln in args.messages.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if args.messages
        else list(DEFAULT_MESSAGES)
    )
    if args.turns > 0:
        messages = messages[: args.turns]
    if not messages:
        print("Message list is empty.", file=sys.stderr)
        return 2

    persona, mcp_payload, claude_bin = load_akana_bits()
    if args.no_persona:
        persona = None
    if args.no_mcp:
        mcp_payload = None

    mcp_names = list(mcp_payload or {})
    print("=" * 72)
    print("Akana cost comparison — claude path")
    print(f"  model       : {args.model}  (fixed for both arms)")
    print(f"  msg/turn    : {len(messages)}")
    print(f"  reps        : {args.reps}{'  (+warmup)' if args.warmup else ''}")
    print(f"  persona     : {'YES (%d chars)' % len(persona) if persona else 'NO'}")
    print(f"  akana MCP   : {mcp_names if mcp_names else 'NO'}")
    print(f"  claude bin  : {claude_bin}")
    print("=" * 72)

    def raw_builder(msg: str, sid: str | None) -> list[str]:
        return build_raw_argv(claude_bin, msg, args.model, sid)

    def akana_builder(msg: str, sid: str | None) -> list[str]:
        return build_akana_argv(claude_bin, msg, args.model, sid, persona, mcp_payload)

    # show the argv once (auditability)
    print("\n[normal argv] " + " ".join(raw_builder(messages[0], None)))
    a0 = akana_builder(messages[0], None)
    print("[akana  argv] " + " ".join(a0[:6]) + (" … +%d flags" % (len(a0) - 6)))

    if args.warmup:
        print("\n[warmup] warming the cache…", file=sys.stderr)
        run_conversation("normal", raw_builder, messages[:1], args.timeout, args.sleep)
        run_conversation("akana", akana_builder, messages[:1], args.timeout, args.sleep)

    raw_reps: list[dict] = []
    akana_reps: list[dict] = []
    rep1_turns: dict[str, list[dict]] = {}

    for r in range(1, args.reps + 1):
        print(f"\n--- rep {r}/{args.reps} ---")
        print("  running normal…")
        raw_turns = run_conversation("normal", raw_builder, messages, args.timeout, args.sleep)
        print("  running akana…")
        akana_turns = run_conversation("akana", akana_builder, messages, args.timeout, args.sleep)
        raw_reps.append(conv_totals(raw_turns))
        akana_reps.append(conv_totals(akana_turns))
        if r == 1:
            rep1_turns = {"normal": raw_turns, "akana": akana_turns}

    # --- aggregation ---------------------------------------------------------
    def agg(reps: list[dict], key: str) -> tuple[float, float]:
        return mean_std([rp[key] for rp in reps])

    raw_ctx = agg(raw_reps, "ctx_in")
    ak_ctx = agg(akana_reps, "ctx_in")
    raw_out = agg(raw_reps, "output")
    ak_out = agg(akana_reps, "output")
    raw_list = mean_std([list_price(rp["ctx_in"], rp["output"], args.model) for rp in raw_reps])
    ak_list = mean_std([list_price(rp["ctx_in"], rp["output"], args.model) for rp in akana_reps])
    raw_real = mean_std([rp["real_cost"] for rp in raw_reps])
    ak_real = mean_std([rp["real_cost"] for rp in akana_reps])

    def delta_pct(a: float, b: float) -> str:
        if b == 0:
            return "—"
        return f"{(a - b) / b * 100:+.1f}%"

    n = len(messages)
    print("\n" + "=" * 72)
    print(f"RESULT — total for a {n}-message conversation (mean ± std, {args.reps} reps)")
    print("=" * 72)
    rows = [
        ("Context input tokens", raw_ctx, ak_ctx, True),
        ("Output tokens", raw_out, ak_out, True),
        ("List price $ (cache-free)", raw_list, ak_list, False),
        ("Real cost $ (with cache)", raw_real, ak_real, False),
    ]
    print(f"{'Metric':<28}{'normal':>20}{'akana':>20}{'Δ%':>8}")
    print("-" * 76)
    for label, rawv, akv, intlike in rows:
        print(
            f"{label:<28}{fmt_pair(*rawv, intlike):>20}{fmt_pair(*akv, intlike):>20}"
            f"{delta_pct(akv[0], rawv[0]):>8}"
        )
    if not (all(rp["real_cost_complete"] for rp in raw_reps + akana_reps)):
        print("  (note: total_cost_usd missing in some turns — real cost is a LOWER bound)")

    # average per-turn overhead
    per_turn_tok = (ak_ctx[0] - raw_ctx[0]) / n if n else 0
    per_turn_cost = (ak_list[0] - raw_list[0]) / n if n else 0
    print("-" * 76)
    print(
        f"AKANA OVERHEAD: ~{per_turn_tok:,.0f} context tokens per turn "
        f"({delta_pct(ak_ctx[0], raw_ctx[0])}), ~${per_turn_cost:.5f} (list price)"
    )

    # --- rep1 per-turn breakdown ---------------------------------------------
    print("\n" + "-" * 76)
    print("Rep 1 — per turn (ctx_in / cache_read / out / $real)")
    print(f"{'trn':>3} | {'normal ctx':>11} {'cr':>7} {'out':>5} {'$':>8} | {'akana ctx':>10} {'cr':>7} {'out':>5} {'$':>8}")
    for i in range(n):
        rt = rep1_turns["normal"][i]
        at = rep1_turns["akana"][i]

        def cell(t: dict) -> tuple[str, str, str, str]:
            if not t.get("ok"):
                return ("ERR", "-", "-", "-")
            c = f"{t['real_cost']:.5f}" if t.get("real_cost") is not None else "-"
            return (f"{t['ctx_in']:,}", f"{t['cache_read']:,}", f"{t['output']:,}", c)

        rc = cell(rt)
        ac = cell(at)
        print(f"{i + 1:>3} | {rc[0]:>11} {rc[1]:>7} {rc[2]:>5} {rc[3]:>8} | {ac[0]:>10} {ac[1]:>7} {ac[2]:>5} {ac[3]:>8}")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "config": {
                        "model": args.model,
                        "reps": args.reps,
                        "messages": messages,
                        "persona_chars": len(persona) if persona else 0,
                        "mcp_servers": mcp_names,
                    },
                    "raw_reps": raw_reps,
                    "akana_reps": akana_reps,
                    "rep1_turns": rep1_turns,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nRaw results written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
