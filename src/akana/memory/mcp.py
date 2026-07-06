"""Akana memory MCP server — the ``memory.*`` tools over stdio (§8 delivery).

The Cursor SDK registers MCP servers (``mcpServers`` in ``AgentOptions``), so
this module is the thinnest possible JSON-RPC 2.0 wrapper around
:class:`~akana.memory.orchestrator.MemoryOrchestrator`: the chat agent sees
``memory_search`` / ``memory_remember`` / ``memory_forget``
as native tools and every call lands in ``handle_tool_call``. Tool names use
underscores (MCP name charset); they map 1:1 onto the dotted vision names.

Protocol: newline-delimited JSON-RPC over stdin/stdout. Stdout is protocol-only
— all logging goes to stderr. Lifetime is owned by the agent runtime: serve
until EOF.

Run::

    AKANA_DATA_DIR=~/.akana python -m akana.memory.mcp

Requires the ``akana`` package importable (the server glue passes
``PYTHONPATH=<repo>/src``).
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from akana.memory.embed import (
    Embedder,
    LocalEmbedder,
    OllamaEmbedder,
    has_model,
    is_available,
)
from akana.memory.orchestrator import MemoryOrchestrator, OrchestratorSettings
from akana.memory.settings import MemorySettings, load_memory_settings
from akana.memory.tools import tool_schemas
from akana.memory.vector import VectorStore
from akana.memory.vector_recall import VectorIndexer, enable_vector_recall

if TYPE_CHECKING:
    from akana.memory import Memory

__all__ = [
    "McpServer",
    "mcp_tool_list",
    "serve",
    "main",
    "build_orchestrator",
    "MCP_TO_TOOL",
    "MAX_LINE_CHARS",
]

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "akana-memory", "version": "0.1.0"}

#: Reject (with -32600) any stdin line longer than this instead of feeding it
#: to the JSON parser — a runaway client must not eat the server's memory.
MAX_LINE_CHARS = 4 * 1024 * 1024

# A strict-decoding stdin whose stream ends in a truncated multibyte sequence
# raises UnicodeDecodeError on *every* readline() without consuming anything
# (verified on CPython 3.11) — cap consecutive failures so serve() can't spin.
_MAX_CONSECUTIVE_DECODE_FAILURES = 100

# JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


def _mcp_name(dotted: str) -> str:
    """``memory.search`` → ``memory_search`` (MCP tool-name charset)."""
    return dotted.replace(".", "_")


MCP_TO_TOOL: dict[str, str] = {_mcp_name(s["name"]): s["name"] for s in tool_schemas()}


def mcp_tool_list() -> list[dict[str, Any]]:
    """The §8 schemas in MCP shape (``inputSchema``, underscore names)."""
    out: list[dict[str, Any]] = []
    for schema in tool_schemas():
        out.append(
            {
                "name": _mcp_name(schema["name"]),
                "description": schema.get("description", ""),
                "inputSchema": schema["input_schema"],
            }
        )
    return out


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class McpServer:
    """Dispatch one parsed JSON-RPC message; pure logic, no I/O (testable)."""

    def __init__(
        self,
        orchestrator: MemoryOrchestrator | Callable[[], MemoryOrchestrator],
    ) -> None:
        # Accept a BUILT orchestrator (tests / direct use) OR a zero-arg factory that
        # builds one LAZILY on the first ``tools/call``. Lazy construction keeps the
        # MCP handshake (``initialize`` / ``tools/list`` / ``ping``) free of any
        # ``memory.db`` work: the FTS/vector setup in ``build_orchestrator`` acquires a
        # SQLite writer lock, so doing it before serving could block the handshake
        # until another process (the backend's in-process Memory) released the lock —
        # the client then times out and the server is stuck "connecting". Vault has no
        # DB, which is exactly why it always connected while memory did not.
        if isinstance(orchestrator, MemoryOrchestrator):
            self._orch: MemoryOrchestrator | None = orchestrator
            self._factory: Callable[[], MemoryOrchestrator] | None = None
        else:
            self._orch = None
            self._factory = orchestrator

    def _orchestrator(self) -> MemoryOrchestrator:
        """Resolve the orchestrator, building it once on first use (lazy factory)."""
        if self._orch is None:
            assert self._factory is not None  # one of orch/factory is always set
            self._orch = self._factory()
        return self._orch

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """Return a response dict, or ``None`` when no reply is due."""
        if isinstance(msg, list):
            # JSON-RPC batch. We don't support it (MCP stdio framing is one
            # message per line) — but we must *say* so: swallowing the array
            # leaves the client waiting forever.
            return _error(None, _INVALID_REQUEST, "batch requests are not supported")
        if not isinstance(msg, dict):
            return _error(None, _INVALID_REQUEST, "request must be a JSON object")
        method = msg.get("method")
        msg_id = msg.get("id")
        if not isinstance(method, str):
            return None  # a response or garbage — nothing to do

        if method == "initialize":
            params = msg.get("params") or {}
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if isinstance(requested, str) and requested else PROTOCOL_VERSION
            return _result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": dict(SERVER_INFO),
                },
            )
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": mcp_tool_list()})
        if method == "tools/call":
            return self._tools_call(msg_id, msg.get("params"))
        if "id" not in msg:
            return None  # unknown notification — ignore per JSON-RPC
        # "id" present (even an explicit null) makes it a request: reply,
        # echoing the id exactly as sent (null/float/string included).
        return _error(msg_id, _METHOD_NOT_FOUND, f"method not found: {method}")

    def _tools_call(self, msg_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(msg_id, _INVALID_PARAMS, "tools/call requires params.name")
        name = params["name"]
        arguments = params.get("arguments")
        args = arguments if isinstance(arguments, dict) else {}
        dotted = MCP_TO_TOOL.get(name, name)  # unknown → orchestrator envelopes it
        out = self._orchestrator().handle_tool_call(dotted, args)
        return _result(
            msg_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(out, ensure_ascii=False)}
                ],
                "isError": "error" in out,
            },
        )


def serve(
    orchestrator: MemoryOrchestrator | Callable[[], MemoryOrchestrator],
    stdin: TextIO,
    stdout: TextIO,
) -> None:
    """Newline-delimited JSON-RPC loop; runs until stdin EOF.

    Hostile input never kills the loop: undecodable bytes → -32700, oversized
    lines and batch arrays → -32600, handler crashes → -32603 — and in every
    case we keep reading. EOF is the only normal exit. ``main()`` wraps stdin
    with ``errors="replace"`` so decoding can't even raise; the except branch
    below is a second line of defence for strict text streams.
    """
    server = McpServer(orchestrator)

    def _write(obj: dict[str, Any]) -> None:
        # Defense-in-depth for the protocol write — a single failed write must NEVER
        # silently kill the server mid-handshake (that was the cp1252 bug: a Turkish
        # char in tools/list raised UnicodeEncodeError and the child died → client
        # stuck "connecting"). main() reconfigures stdout to UTF-8, but if that ever
        # fails to take, ensure_ascii=True escapes non-ASCII to \uXXXX (still valid
        # JSON-RPC the client decodes) so the message still goes out. A broken pipe
        # (client gone) is logged, not raised — the read loop then hits EOF cleanly.
        try:
            stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            stdout.flush()
        except UnicodeEncodeError:
            stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
            stdout.flush()
        except (BrokenPipeError, OSError) as exc:  # pragma: no cover - client closed pipe
            log.warning("mcp stdout write failed (client gone?): %s", exc)

    decode_failures = 0  # consecutive — see _MAX_CONSECUTIVE_DECODE_FAILURES
    while True:
        try:
            line = stdin.readline()
        except UnicodeDecodeError:
            log.warning("dropping undecodable stdin data")
            _write(_error(None, _PARSE_ERROR, "parse error: stdin is not valid UTF-8"))
            decode_failures += 1
            if decode_failures >= _MAX_CONSECUTIVE_DECODE_FAILURES:
                log.error("stdin no longer decodable; shutting down")
                return
            continue
        decode_failures = 0
        if not line:
            return  # EOF
        if len(line) > MAX_LINE_CHARS:
            # The line is already in memory (readline), but don't hand it to
            # the JSON parser — answer and move on to the next line.
            _write(_error(None, _INVALID_REQUEST, f"line too large (limit {MAX_LINE_CHARS} chars)"))
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write(_error(None, _PARSE_ERROR, "parse error"))
            continue
        try:
            resp = server.handle(msg)
        except Exception as e:  # the protocol loop must outlive any bad call
            log.exception("mcp handler failed")
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            resp = _error(msg_id, -32603, f"internal error: {type(e).__name__}")
        if resp is not None:
            _write(resp)


def _fastembed_available() -> bool:
    """Is fastembed installed (without importing it, cheap)."""
    import importlib.util

    return importlib.util.find_spec("fastembed") is not None


def _resolve_embedder(settings: MemorySettings) -> Embedder | None:
    """Pick an embedder per the owner's ``embed_backend`` preference; ``None`` = keyword-only.

    ``vector="off"`` never reaches here. The owner does not want Ollama → the default
    is ``"local"`` (fastembed/ONNX, in-process; the model is downloaded on the first
    embed). ``"auto"`` degrades silently, ``"on"`` degrades loudly; in either case the
    server runs with keyword recall (a vector upgrade, not a dependency). If ``"ollama"``
    is chosen explicitly, the old local-daemon path is used (when the model is missing,
    it degrades with a clear line against the "probe green but every embed 404s" trap).
    """
    backend = (settings.embed_backend or "local").strip().lower()
    if backend == "off":
        return None
    if backend == "local":
        if _fastembed_available():
            model = (settings.local_embed_model or "").strip()
            return LocalEmbedder(model=model) if model else LocalEmbedder()
        level = log.error if settings.vector == "on" else log.info
        # Name the interpreter: if a foreign (fastembed-less) install is running
        # against this data dir, sys.executable outs it as the odd one out.
        level(
            "local embedding (fastembed) not installed in %s; run: python akana.py add "
            "embeddings — falling back to keyword recall for now (vector disabled)",
            sys.executable,
        )
        return None
    # backend == "ollama": the owner explicitly chose the local Ollama daemon
    if is_available(settings.ollama_url):
        if has_model(settings.ollama_url, settings.embed_model):
            return OllamaEmbedder(
                model=settings.embed_model,
                url=settings.ollama_url.rstrip("/") + "/api/embed",
            )
        level = log.error if settings.vector == "on" else log.warning
        level(
            "Ollama is reachable at %s but embed model %r is not installed; "
            "run: ollama pull %s — continuing with keyword recall only",
            settings.ollama_url,
            settings.embed_model,
            settings.embed_model,
        )
        return None
    if settings.vector == "on":
        log.error(
            "vector=on but Ollama is unreachable at %s; continuing with keyword recall only",
            settings.ollama_url,
        )
    else:
        log.info(
            "Ollama unreachable at %s; vector recall stays off (vector=auto)",
            settings.ollama_url,
        )
    return None


def _wire_vector(
    memory: Memory,
    orchestrator: MemoryOrchestrator,
    embedder: Embedder | None,
    data_dir: Path,
) -> VectorIndexer | None:
    """Register vector strategies + live indexer; backfill empty / reindex on
    embedder model change."""
    if embedder is None:
        return None
    store = VectorStore.for_data_dir(data_dir)  # same <data_dir>/db/memory.db (K11)
    indexer = enable_vector_recall(memory, orchestrator, embedder, store=store)
    if indexer is None:
        return None
    # Backfill if empty; clear + reindex if the embedder MODEL changed. Otherwise, on
    # a backend change (e.g. ollama:bge-m3 → fastembed) the old 1024d vectors would
    # remain and, not matching the new 384d query, semantic recall died SILENTLY
    # (count>0 → the old backfill was SKIPPED). If there is a stale model, rebuild fully.
    stale = [m for m in store.distinct_models() if m and m != embedder.name]
    # U6: prune embeddings whose fact is gone/invalidated BEFORE counting. Orphans leaked
    # historically (deletes with no subscriber wired) and also inflate store.count() so the
    # `indexed >= expected` check below would wrongly conclude the index is complete —
    # keeping the orphans forever AND masking the resume-backfill of genuinely missing rows.
    try:
        pruned = store.prune_orphans()
        if pruned:
            log.info("pruned %d orphan embedding(s) for deleted/invalidated facts", pruned)
    except Exception:  # never block boot on cleanup
        log.debug("orphan embedding prune failed; continuing", exc_info=True)
    # count() > 0 with the same model does NOT prove the index is complete: reindex
    # commits per batch and stops on the first embed failure / process exit, leaving a
    # PARTIAL index under the current model. Compare against the valid-fact count so an
    # interrupted backfill resumes on the next boot (reindex uses INSERT OR REPLACE, so
    # re-embedding the already-indexed rows is harmless).
    indexed = store.count()
    expected = memory.semantic.count_facts()
    if indexed == 0:
        log.info("vector index empty; backfilling all facts (may take a while on large databases)")
    elif stale:
        log.info(
            "embedder model changed (%s → %s); clearing and rebuilding vector index",
            ", ".join(stale),
            embedder.name,
        )
        store.clear()
    elif indexed < expected:
        log.info(
            "vector index incomplete (%d/%d facts embedded); resuming backfill",
            indexed,
            expected,
        )
    else:
        # count-complete under the same model does NOT prove the TEXT is current:
        # a correct_fact/supersede during a no-indexer window rewrites a fact's
        # value under the same id, leaving a stale-text vector that still counts
        # as indexed (indexed>=expected). Repair only the drifted rows —
        # detected via the embedded-text-hash sidecar — so semantic recall stops
        # matching the retired value and starts matching the corrected one.
        try:
            repaired = indexer.reindex_stale(memory)
            if repaired:
                log.info("vector stale-text repair: re-embedded %d fact(s)", repaired)
        except Exception:  # not fatal: live indexing + keyword recall continue
            log.exception("vector stale-text repair failed; live indexing continues")
        return indexer  # the index is full with the same model → no full reindex needed
    try:
        n = indexer.reindex(memory)
        log.info("vector (re)index complete: %d facts", n)
    except Exception:  # not fatal: live indexing + keyword recall continue
        log.exception("vector backfill failed; live indexing continues")
    return indexer


def build_orchestrator(
    data_dir: Path,
    *,
    embedder: Embedder | None = None,
    memory: Memory | None = None,
) -> tuple[Memory, MemoryOrchestrator, VectorIndexer | None]:
    """The construction seam ``main()`` runs and tests drive directly.

    Reads ``memory_settings.yaml`` (+ env overrides) so the owner's choices
    actually govern the server: K30 ``allow_direct`` lands in
    :class:`OrchestratorSettings`, and the ``vector`` mode decides whether the
    Ollama probe runs. ``embedder`` injects a backend without touching
    settings (tests pass a ``HashingEmbedder``); ``vector="off"`` still wins
    over an injected embedder. Vector setup failures never raise — the worst
    outcome is keyword-only recall.

    ``memory`` injects an EXISTING ``Memory`` instead of building a fresh one —
    the server passes its process-wide singleton (``memory_core.get_memory_core``)
    so route + turn_writer share ONE Memory on ``memory.db`` (else: two objects,
    separate ledger/indexer/cache). ``None`` ⇒ build fresh (MCP child / tests).
    """
    settings = load_memory_settings(data_dir)
    if memory is None:
        from akana.memory import Memory  # late import: keep module import light for tests

        memory = Memory.for_data_dir(data_dir)
    orchestrator = memory.make_orchestrator(
        settings=OrchestratorSettings(allow_direct=settings.allow_direct)
    )
    indexer: VectorIndexer | None = None
    if settings.vector != "off":
        try:
            chosen = embedder if embedder is not None else _resolve_embedder(settings)
            indexer = _wire_vector(memory, orchestrator, chosen, data_dir)
        except Exception:  # the MCP server must come up no matter what
            log.exception("vector recall setup failed; continuing with keyword recall only")
            indexer = None
    return memory, orchestrator, indexer


def main() -> int:
    # WINDOWS CRITICAL: the Windows console/pipe defaults to the locale code page
    # (cp1252/cp1254), NOT UTF-8. The protocol writes JSON with ensure_ascii=False and
    # the tool descriptions contain Turkish characters (ı/ş/ğ…), so stdout.write raised
    # UnicodeEncodeError on the FIRST tools/list — killing the server mid-handshake.
    # ``initialize`` is ASCII so it succeeded, then tools/list crashed → the MCP client
    # saw the child die → akana_memory stuck "connecting" forever (while ASCII-only
    # vault connected). MCP stdio is UTF-8 by spec, so force it on both streams.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - reconfigure missing/locked → best effort
            pass
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    data_dir = Path(os.environ.get("AKANA_DATA_DIR") or Path.home() / ".akana").expanduser()
    # Build the orchestrator LAZILY (on the first tool call), NOT before serving.
    # build_orchestrator opens memory.db and (re)builds the FTS/vector index — work
    # that takes a SQLite writer lock. Done before the protocol loop, it could block
    # the MCP handshake until the lock cleared (the backend writes to the SAME
    # memory.db), so the client timed out and akana_memory was stuck "connecting"
    # while the DB-less vault connected instantly. Deferring it keeps initialize /
    # tools/list free of DB work; only the first real tool call pays the build cost.
    def _factory() -> MemoryOrchestrator:
        return build_orchestrator(data_dir)[1]

    # Wrap the *binary* stdin ourselves: sys.stdin decodes strictly, so one
    # stray non-UTF-8 byte would raise mid-readline and kill the process.
    # With errors="replace" bad bytes become U+FFFD, JSON parsing fails, and
    # the client gets a -32700 reply instead of a dead server.
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    log.info(
        "akana-memory MCP serving on stdio (data_dir=%s; orchestrator builds on first tool call)",
        data_dir,
    )
    serve(_factory, stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
