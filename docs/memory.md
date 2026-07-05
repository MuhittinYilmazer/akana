# Memory

Akana's memory subsystem is **review-gated**: nothing becomes durable memory until you approve it in the Memory Studio. Staged items are still visible to recall; they come back flagged as awaiting approval so the assistant can mention them instead of claiming ignorance, but they are not durable and can be rejected. This page covers the storage model, the capture pipeline, vector recall and the event ledger. For a short overview, see the [Memory section in the README](../README.md#memory).

> The memory subsystem is under active development and has not been benchmarked at scale.

## Storage model

Memory lives in one SQLite file at `<data_dir>/db/memory.db` (default `~/.akana/db/memory.db`). Two layers share it:

- **Episodic** — the raw turns of each conversation.
- **Semantic** — durable key/value facts, each tagged with a trust level (`user_statement`, `inferred`, `tool_output`, `synthesis`), a validity window (`valid_from`, `invalidated_at`) and provenance.

## The staging inbox

Facts get proposed two ways: an LLM-driven auto-capture pass that runs after each turn, or the model calling the remember tool. **Both routes land in a staging inbox by default.** Nothing becomes durable until you approve it in the Memory Studio (`web_ui/memory.html`), which has tabs for an overview (including the unified timeline: new fact, invalidated, turn, reset, remembered, forgotten, usage), the inbox, the fact list, a recall search and memory settings.

Auto-capture is one path: an LLM capture call after each turn that proposes candidates into the staging inbox (`akana_server/memory_capture.py` → `persist.py` `_stage_candidates`).

With the default settings (allow-direct off), auto-capture only writes inbox candidates and the remember tool's direct/supersede requests degrade to staging. Enabling "remember without approval" (allow-direct) promotes captures and remembers straight to durable facts, bypassing the inbox.

## Tools exposed to the model

The three memory operations exposed to the LLM are **search**, **remember** and **forget**. Claude and Cursor reach them through a built-in stdio MCP server (`akana_memory`) mounted into their CLI/SDK. Gemini, OpenAI and Ollama do not mount that MCP server; they receive the same three operations re-declared as native function-calling tools.

## Recall

Recall is **keyword-based out of the box**. Semantic (vector) recall is opt-in:

```sh
python akana.py add embeddings
```

That installs `fastembed` and, on first use, downloads `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (~220 MB, ONNX, CPU-only — no torch, no GPU required). If the embedder is missing or a call fails, recall falls back to keyword search rather than raising. A shared circuit breaker (`VectorHealth`) pauses embed calls for 120 seconds on a transient failure and disables the vector layer for the process if the model is missing.

Alternatively, the Ollama embedding backend (`bge-m3` by default) is available if you already run Ollama.

The recall pipeline itself is a **fused semantic + episodic search** with a token budget and a per-call trace (query terms, candidate counts, dropped-for-budget). The trace is returned alongside recall results so the caller can see why any particular fact did or did not make it into the answer. Recall results also include a `pending` field: inbox items matching the query, flagged as awaiting approval, so the assistant can surface them rather than answer "I don't know" about something already staged.

## Event ledger and knowledge graph

Every fact write, invalidation and turn goes through a subscriber seam onto a durable **event ledger** and a small SQLite **knowledge-graph projection**. The graph is internal and has no UI yet. The ledger drives the timeline endpoint (`/api/v1/memory/timeline`) that the Memory Studio renders bilingually.

## Background jobs

Two background jobs run alongside the stores when `session_summary` is enabled (default on):

- a **session-summary cron** that summarises idle or long conversations.
- a **summary-consolidation cron** that merges overlapping session summaries.

Both stage their output into the same inbox, so with the default settings consolidated summaries wait for review like everything else. The allow-direct toggle bypasses the inbox for these paths too.

## Settings

Memory settings (allow-direct, auto-capture, session-summary, vector mode, embed backend, embed model, Ollama URL) persist to `<data_dir>/memory_settings.yaml` and can be overridden by `AKANA_MEMORY_*` env vars.

| Variable | What it does |
| --- | --- |
| `AKANA_MEMORY_TOOLS` | Enable/disable the built-in `akana_memory` MCP server. Default `1`. |
| `AKANA_MEMORY_ALLOW_DIRECT` | Allow direct writes bypassing the inbox. Default off. |
| `AKANA_MEMORY_LLM_CAPTURE` | Turn LLM-driven auto-capture on/off. |
| `AKANA_MEMORY_VECTOR` | Enable vector recall. |
| `AKANA_MEMORY_EMBED_BACKEND` | `local` (fastembed) or `ollama`. |

> **Timezone note:** memory date boundaries ("today", "yesterday") are computed in Turkey local time (fixed `+03:00`, no DST). This is a known limitation for non-Turkey users — see [Known limitations](../README.md#known-limitations).
