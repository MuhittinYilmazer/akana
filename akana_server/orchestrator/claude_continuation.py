"""Autonomous multi-turn continuation for the claude provider.

This module owns the *auto-continue* feature that lets a coding turn keep working
across several claude runs (``--resume`` + a "Continue." nudge) until the task is
genuinely finished — instead of stopping after one headless run. It is a distinct
responsibility from :mod:`.claude_provider`'s single-shot CLI driver: the provider
runs exactly one ``claude`` invocation and translates its NDJSON into wire events,
while this module is the multi-run *policy* layer wrapping that primitive. Keeping
them separate lets the auto-continue loop, its sentinel protocol and its runtime
gates evolve without touching the event-translation core (and keeps both files
comfortably under the architecture god-file ceiling as a side effect).

It holds:
  - the continuation primitives (sentinel token, the system-prompt instruction,
    the resume nudge, the :class:`_SentinelStripper`, and the runtime gates);
  - the :func:`run_with_continuation` wrapper, which orchestrates one-or-many runs
    of the provider's single-shot primitive.

Dependency direction (ARCH-06): this module does NOT import :mod:`.claude_provider`.
The provider's single-shot ``_stream_single_run`` and its ``_resolve_prompt_language``
are INJECTED into :func:`run_with_continuation` as callables at the provider's call
site — so the old ``claude_provider ⇄ claude_continuation`` cycle (previously held open
by an in-function back-import) is gone. ``claude_provider`` owns the public
``stream_user_chat`` name and supplies those callables, which keeps every existing
``claude_provider.stream_user_chat`` caller — and the ``claude_provider._stream_single_run``
test monkeypatch (read at the provider's call site) — working unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from akana_server.config import Settings
from akana_server.orchestrator import base

#: Autonomous-continuation marker. When ``auto_continue`` is on, the wrapper
#: (:func:`stream_user_chat`) appends :data:`_CONTINUE_INSTRUCTION` to the system
#: prompt asking the model to print this exact token ONLY when the whole task is
#: genuinely finished. The wrapper detects it in the run's text to stop the loop
#: and strips it from the user-visible stream (:class:`_SentinelStripper`). A
#: distinctive token → near-zero chance of colliding with real model output.
_CONTINUE_SENTINEL = "[[AKANA_TASK_COMPLETE]]"

#: Appended to the system prompt ONLY in auto-continue mode (bilingual, follows
#: the active ``language``). Tells the model to keep working across turns and to
#: emit :data:`_CONTINUE_SENTINEL` exactly when — and only when — done.
_CONTINUE_INSTRUCTION = {
    "en": (
        "[Autonomous continuation mode]\n"
        "You run across multiple turns until the task is genuinely finished. Do NOT "
        "stop after a single step, and do NOT pause to ask whether to keep going — "
        "just continue working: implement, then VERIFY (run the tests/build, exercise "
        "the feature).\n"
        "When — and ONLY when — the ENTIRE task is complete and verified, end your "
        "final message with this exact token on its own line:\n"
        f"{_CONTINUE_SENTINEL}\n"
        "While any work remains, do NOT write that token.\n"
        "REACHING THE USER IN THIS MODE — a plain-prose question does NOT pause the turn. "
        "If you end a step with a question written as ordinary text, you will NOT get an "
        "answer: the turn auto-continues and you end up answering yourself. So — if you "
        "can proceed with a reasonable assumption, do so and STATE the assumption. If you "
        "genuinely cannot proceed without the user, ask ONE multiple-choice question via "
        "the [[AKANA_ASK]]…[[/AKANA_ASK]] block (see the system prompt) — that block is the "
        "ONLY thing that pauses the turn and shows the user a prompt. NEVER promise to wait "
        "for the user in prose ('just tell me and I'll…'); either emit the block or keep working."
    ),
    "tr": (
        "[Otonom sürdürme modu]\n"
        "Görev gerçekten bitene kadar birden çok turda çalışırsın. Tek bir adımdan "
        "sonra DURMA ve devam edip etmeyeceğini sormak için BEKLEME — sadece çalışmaya "
        "devam et: uygula, sonra DOĞRULA (testleri/derlemeyi çalıştır, özelliği dene).\n"
        "Görevin TAMAMI bitip doğrulandığında — SADECE o zaman — son mesajını tam olarak "
        "şu jetonla, kendi satırında bitir:\n"
        f"{_CONTINUE_SENTINEL}\n"
        "İş bittiğine emin olmadıkça bu jetonu YAZMA.\n"
        "BU MODDA KULLANICIYA ULAŞMAK — düz yazıyla sorulan bir soru turu DURDURMAZ. Bir "
        "adımı sıradan metin olarak yazılmış bir soruyla bitirirsen yanıt ALAMAZSIN: tur "
        "kendiliğinden devam eder ve kendi soruna kendin cevap verirsin. Yani — makul bir "
        "varsayımla ilerleyebiliyorsan ilerle ve varsayımını BELİRT. Kullanıcı olmadan "
        "gerçekten ilerleyemiyorsan, TEK bir çoktan seçmeli soruyu [[AKANA_ASK]]…"
        "[[/AKANA_ASK]] bloğuyla (sistem promptuna bak) sor — turu durduran ve kullanıcıya "
        "soru gösteren TEK şey o bloktur. Kullanıcıyı bekleyeceğini ('sen söyle, ben "
        "yaparım') ASLA düz yazıyla vaat etme; ya bloğu yaz ya da çalışmaya devam et."
    ),
}

#: The nudge sent on each internal resume (``--resume`` + this text). Language-matched
#: (picked by the resolved ``language``) so it does not bias the reply language. The
#: session already holds full context, so this is short — but it is a GUARDED nudge, not
#: a blind "keep going": it reminds the model that if it actually needs the user's input
#: it must ask via the [[AKANA_ASK]] block (the only thing that pauses the turn) rather
#: than guess. Without this, a model that ended the prior run with a dangling PROSE
#: question gets silently resumed and answers its own question (the failure this guards).
_CONTINUE_PROMPT = {
    "en": (
        "Continue. If you cannot proceed correctly without input from the user, ask it now "
        "via the [[AKANA_ASK]]…[[/AKANA_ASK]] block instead of guessing; otherwise keep "
        "working until the task is fully done."
    ),
    "tr": (
        "Devam et. Kullanıcıdan girdi olmadan doğru şekilde ilerleyemiyorsan, tahmin etmek "
        "yerine şimdi [[AKANA_ASK]]…[[/AKANA_ASK]] bloğuyla sor; aksi halde görev tamamen "
        "bitene kadar çalışmaya devam et."
    ),
}


class _SentinelStripper:
    """Remove :data:`_CONTINUE_SENTINEL` from a streamed delta sequence.

    ``feed`` returns the slice safe to emit now: any complete sentinel is dropped
    and a trailing *partial* sentinel prefix is held back, so a token split across
    delta chunks never leaks to the UI / TTS / persisted answer. ``flush`` returns
    the held-back remainder at end of a run — by then the full token (if present)
    has already been removed, so what flush returns is always real text.
    """

    def __init__(self, sentinel: str) -> None:
        self._s = sentinel
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        if self._s in self._buf:
            self._buf = self._buf.replace(self._s, "")
        hold = self._overlap(self._buf, self._s)
        if hold:
            out, self._buf = self._buf[:-hold], self._buf[-hold:]
        else:
            out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return out.replace(self._s, "")

    @staticmethod
    def _overlap(buf: str, sentinel: str) -> int:
        """Longest k (< len(sentinel)) such that ``buf`` ends with ``sentinel[:k]``."""
        for k in range(min(len(buf), len(sentinel) - 1), 0, -1):
            if buf.endswith(sentinel[:k]):
                return k
        return 0


def _autocontinue_enabled(settings: Settings) -> bool:
    """Master switch for the auto-continue loop (runtime ``agent_autocontinue``).

    OFF by default (owner decision): a turn is a single run, so a question that ends a
    turn genuinely waits for the user instead of auto-resuming and answering itself. The
    owner can opt back into the multi-run loop at runtime by turning ``agent_autocontinue``
    ON. Any resolution failure (test double / unregistered key) → OFF, the safe default.
    """
    try:
        from akana_server.runtime_settings import get_runtime

        return bool(get_runtime("agent_autocontinue", settings))
    except Exception:
        return False


def _continue_limits(settings: Settings) -> tuple[int, float]:
    """Ceilings for the auto-continue loop: ``(max_iters, deadline_seconds)``.

    ``agent_max_continue_iters`` caps the number of claude runs (default 25);
    ``agent_continue_deadline`` is a wall-clock cap across all runs (default 0 =
    off). Both degrade to their literal default if runtime resolution fails.
    """
    from akana_server.runtime_settings import get_runtime

    try:
        iters = int(get_runtime("agent_max_continue_iters", settings))
    except Exception:
        iters = 25
    try:
        deadline = float(get_runtime("agent_continue_deadline", settings))
    except Exception:
        deadline = 0.0
    return max(1, iters), max(0.0, deadline)


def _join_segments(parts: list[str]) -> str:
    """Concatenate per-run answer texts, welding a paragraph break at any seam that
    would otherwise collide.

    Mirrors the live cross-run delta gap (see :func:`base.segment_gap`) so the
    fallback ``done.text`` — used only when the producer received no deltas — reads
    the same as the streamed answer instead of gluing one run's last sentence onto
    the next run's first ("…running it.Node 18…")."""
    out = ""
    for part in parts:
        if not part:
            continue
        if out:
            out += base.segment_gap(out[-1], part)
        out += part
    return out


async def run_with_continuation(
    settings: Settings,
    user_text: str,
    *,
    stream_single_run: Callable[..., AsyncIterator[dict[str, Any]]],
    resolve_language: Callable[[Settings], str],
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    chat_mode: bool = True,
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    plan_mode: bool = False,
    auto_continue: bool = False,
    file_ids: list[str] | None = None,  # claude: accepted-and-ignored (CLI has no native file_ids vision input)
) -> AsyncIterator[dict[str, Any]]:
    """Stream a chat turn — with autonomous multi-turn continuation.

    This wraps the provider's single-shot primitive, which is INJECTED as
    ``stream_single_run`` (with ``resolve_language`` for the resume-nudge language) — see
    the module docstring (ARCH-06: no back-import into ``claude_provider``). The provider
    passes ``claude_provider._stream_single_run`` here, resolved at ITS call site so a test
    monkeypatch of that attribute still takes effect. With ``auto_continue`` off (the
    default) it is a transparent passthrough: exactly one claude run, identical event
    sequence as before. With ``auto_continue`` on it keeps the model working across several
    runs (``--resume`` + "Continue.") until the task is genuinely finished, so a deep
    engineering request does NOT stop after the first step.

    Stop conditions (whichever comes first):
      - the run prints :data:`_CONTINUE_SENTINEL` (model says "done") — primary;
      - a run makes NO tool calls (a conversational reply / nothing left to do) —
        so simple chat finishes in ONE run and never loops;
      - the agent asks the user / presents a plan (``awaiting_user``) — must wait;
      - the iteration cap or wall-clock deadline (:func:`_continue_limits`) hits.

    Only ONE terminal ``{"done": True}`` is emitted (the last); intermediate run
    boundaries are swallowed so the producer sees one continuous turn. The
    sentinel is stripped from every delta and from the final text. STOP works as
    before: cancelling this generator cancels the inner run (kills its process).
    """
    _stream_single_run = stream_single_run
    _resolve_prompt_language = resolve_language

    # Fast path — single run, sentinel OFF: auto-continue not requested, plan mode
    # (which presents a plan and waits, must never loop), or the runtime master
    # switch is off. Pure passthrough → existing behaviour / tests unchanged.
    if not auto_continue or plan_mode or not _autocontinue_enabled(settings):
        async for ev in _stream_single_run(
            settings,
            user_text,
            history=history,
            model=model,
            conversation_id=conversation_id,
            agent_id=agent_id,
            reuse_agent=reuse_agent,
            mcp_servers=mcp_servers,
            chat_mode=chat_mode,
            system_prompt=system_prompt,
            thinking_mode=thinking_mode,
            plan_mode=plan_mode,
            continue_sentinel=False,
        ):
            yield ev
        return

    import time as _time

    max_iters, deadline = _continue_limits(settings)
    language = _resolve_prompt_language(settings)
    started = _time.monotonic()

    cur_text = user_text
    cur_history = history
    cur_agent = agent_id
    cur_reuse = reuse_agent
    emitted_agent: str | None = None
    iters = 0
    # Accumulated across runs → folded into the single terminal done event.
    acc = {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0}
    acc_cost = 0.0
    clean_text_parts: list[str] = []
    # Cross-run segment gap (base.segment_gap): a resumed run continues the SAME
    # answer, but its first text delta has no whitespace at the seam with the prior
    # run's last sentence. Track the last emitted answer char across runs and whether a
    # new run has begun since the last text; the next delta then gets a paragraph break.
    _turn_last_char = ""
    _run_gap_pending = False

    while True:
        iters += 1
        # A resumed run (iters >= 2) continues the prior run's answer → its first text
        # must not glue onto the prior run's last sentence.
        if iters > 1:
            _run_gap_pending = True
        stripper = _SentinelStripper(_CONTINUE_SENTINEL)
        run_done: dict[str, Any] | None = None
        run_bootstrap = False
        run_had_tools = False

        async for ev in _stream_single_run(
            settings,
            cur_text,
            history=cur_history,
            model=model,
            conversation_id=conversation_id,
            agent_id=cur_agent,
            reuse_agent=cur_reuse,
            mcp_servers=mcp_servers,
            chat_mode=chat_mode,
            system_prompt=system_prompt,
            thinking_mode=thinking_mode,
            plan_mode=False,
            continue_sentinel=True,
        ):
            # agent_id: capture the latest session id for the next --resume; emit it
            # to the caller only the first time (the producer persists it once).
            if "agent_id" in ev:
                cur_agent = str(ev["agent_id"])
                if emitted_agent != cur_agent:
                    emitted_agent = cur_agent
                    yield ev
                continue
            # Stale --resume: handled after the loop closes the inner generator.
            if ev.get("need_history_bootstrap"):
                run_bootstrap = True
                break
            # Swallow the per-run terminal done — the wrapper emits its own.
            if ev.get("done"):
                run_done = ev
                continue
            # Filter the sentinel out of the live delta stream (UI / TTS / persisted
            # text all read these deltas).
            d = ev.get("delta")
            if d and not ev.get("done"):
                safe = stripper.feed(d)
                if safe:
                    # Weld a paragraph break at the seam between this resumed run and
                    # the previous one (consumed once, on the run's first real text).
                    if _run_gap_pending:
                        safe = base.segment_gap(_turn_last_char, safe) + safe
                        _run_gap_pending = False
                    _turn_last_char = safe[-1]
                    yield {"delta": safe, "done": False}
                continue
            if isinstance(ev.get("tool_call"), dict):
                run_had_tools = True
            # tool_call / thinking / usage_live / tool_call_delta / live ask_user /
            # live plan → pass straight through.
            yield ev

        # Emit any held-back tail (real text held while it looked like a sentinel prefix).
        tail = stripper.flush()
        if tail:
            if _run_gap_pending:
                tail = base.segment_gap(_turn_last_char, tail) + tail
                _run_gap_pending = False
            _turn_last_char = tail[-1]
            yield {"delta": tail, "done": False}

        # Stale resume. On the first run, preserve the existing producer-driven
        # bootstrap contract (it restarts the turn with a history bootstrap). On a
        # later (internal) resume this is very rare → just finalize what we have.
        if run_bootstrap:
            if iters == 1:
                yield {"need_history_bootstrap": True}
                return
            break

        if run_done is None:
            # Stream ended without a terminal done (e.g. cancelled mid-run) → stop.
            break

        # Accumulate usage across runs.
        u = run_done.get("usage")
        if isinstance(u, dict):
            acc["prompt"] += int(u.get("prompt_tokens") or 0)
            acc["completion"] += int(u.get("completion_tokens") or 0)
            acc["cache_read"] += int(u.get("cache_read_tokens") or 0)
            acc["cache_write"] += int(u.get("cache_write_tokens") or 0)
            try:
                acc_cost += float(u.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass

        raw_text = str(run_done.get("text") or "")
        saw_sentinel = _CONTINUE_SENTINEL in raw_text
        clean_text_parts.append(raw_text.replace(_CONTINUE_SENTINEL, ""))

        status = str(run_done.get("status") or "finished")
        awaiting = (
            status == "awaiting_user"
            or isinstance(run_done.get("ask_user"), dict)
            or isinstance(run_done.get("plan"), dict)
        )
        deadline_hit = deadline > 0 and (_time.monotonic() - started) >= deadline
        iters_hit = iters >= max_iters

        # Finish when: the agent needs the user, it signalled completion, it did no
        # tool work this run (conversational / nothing left), or a ceiling is hit.
        if awaiting or saw_sentinel or not run_had_tools or iters_hit or deadline_hit:
            done_event = dict(run_done)
            done_event["text"] = _join_segments(clean_text_parts)
            done_event["status"] = status if awaiting else "finished"
            tokens = {
                "prompt_tokens": acc["prompt"],
                "completion_tokens": acc["completion"],
                "tool_calls": [],
                "cache_read_tokens": acc["cache_read"],
                "cache_write_tokens": acc["cache_write"],
            }
            if acc_cost > 0:
                tokens["cost_usd"] = acc_cost
            done_event["usage"] = tokens
            yield done_event
            return

        # Otherwise resume the same session and keep working.
        cur_text = _CONTINUE_PROMPT.get(language, _CONTINUE_PROMPT["en"])
        cur_history = None
        cur_reuse = True

    # Reached only on an abnormal break (stale mid-loop resume / no done / cancelled
    # without a terminal done). Emit a best-effort terminal done so the producer can
    # finalize the turn with whatever text/usage accumulated.
    tokens = {
        "prompt_tokens": acc["prompt"],
        "completion_tokens": acc["completion"],
        "tool_calls": [],
        "cache_read_tokens": acc["cache_read"],
        "cache_write_tokens": acc["cache_write"],
    }
    if acc_cost > 0:
        tokens["cost_usd"] = acc_cost
    yield base.stream_done_event(
        usage=tokens,
        text=_join_segments(clean_text_parts),
        status="finished",
        tool_calls=[],
    )
