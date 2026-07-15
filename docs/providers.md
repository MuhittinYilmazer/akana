# Providers

Akana registers six chat providers under a shared dispatch layer (`akana_server/orchestrator/llm_dispatch.py`). There is **no default provider**: with nothing configured, chat calls fail with a `No LLM provider configured` error (HTTP 503) rather than picking one for you.

For a quick comparison, see the [Providers table in the README](../README.md#providers). This page covers per-provider install details, credentials, tool loop, thinking mode, multimodal input and agent reuse.

> **Experimental:** Gemini, OpenAI, Ollama and Codex are wired through the same dispatch hub as Cursor and Claude. Gemini/OpenAI/Ollama use a different tool-delivery mechanism (native function-calling schemas rather than a mounted MCP server); Codex is a CLI-bridge provider like Claude (MCP-native via the `codex exec` CLI). They expose the same memory and vault tool set (see "How dispatch works" for the mechanism). Their paths are covered by unit tests but see less end-to-end use than Cursor and Claude. Treat them as experimental. Codex in particular is validated hermetically (faked subprocess + argv contract) but its live path — including MCP-over-`-c` wiring — needs a real `codex login` to exercise fully.

## How dispatch works

A single dispatch hub, `llm_dispatch.py`, resolves the configured provider name to a `ChatProvider` implementation and forwards each turn. The five module providers (Claude, Gemini, OpenAI, Ollama, Codex) satisfy the same base interface (`akana_server/orchestrator/base.py`) and dispatch through a registry, so adding one of these is one registry entry plus a module implementing the interface. Cursor is the built-in dispatch tail with its own daemon-vs-direct routing; it is excluded from the registry and handled explicitly.

The orchestrator owns the tool surface:

- For MCP-speaking providers (**Cursor**, **Claude**, **Codex**) it composes an `mcp_servers` payload delivered through the CLI/SDK. They see the three built-in MCP servers — `akana_memory`, `akana_vault` and `akana_schedule` (reminders / recurring prompts) — plus anything declared in `mcp_servers.yaml`. Cursor/Claude deliver it as a JSON config file; Codex maps each server onto repeatable `-c mcp_servers.<name>.<key>=<value>` config overrides on the `codex exec` command line.
- For the function-calling providers (**Gemini**, **OpenAI**, **Ollama**) it re-declares the same memory (`memory_search` / `save_memory` / `memory_forget`), vault (all seven), and schedule (`schedule_create` / `list` / `cancel` / `update`) tools as native schemas. The declarations are generated from the same schemas as the MCP tools, so the two surfaces stay identical. Each built-in tool group can be turned off in settings (`*_tools_enabled`), which gates both the MCP-spawn path and the native dispatch. External MCP servers reach these providers through an in-process bridge (`mcp_bridge.py`) that surfaces them as `mcp__<server>__<tool>` functions. The tool loop caps at **five rounds per turn**.

Every provider that Akana configures uses **your own** API key or session, not Cursor's. Gemini keys go straight to Google; OpenAI keys go straight to OpenAI. Foreign-provider keys are stripped from the environment before the Cursor and Claude bridges spawn their child processes.

Parameters not supported by a given provider are accepted for API symmetry and ignored.

## Cursor

- **Install:** `python akana.py add cursor` builds the Node bridge under `cursor_bridge/` and runs `npm install` there. Requires Node 18+ (the bridge polyfills `Symbol.dispose` so it works down to 18.16).
- **Credentials:** `cursor_api_key` in the vault. The bridge spawns as a child process; the environment it receives strips `AKANA_TOKEN`, `ANTHROPIC_*`, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `GOOGLE_API_KEY` so foreign keys cannot reach the Cursor SDK.
- **Tool loop:** MCP-native. The `akana_memory` and `akana_vault` MCP servers are wired through the bridge's `mcp_servers` payload, along with anything declared in `mcp_servers.yaml`.
- **Thinking mode:** not honored. The Cursor SDK exposes no thinking or effort input.
- **Multimodal input:** supported by path reference. Uploaded files are written to disk and the Cursor SDK's built-in file-reading tool opens them itself. Images, PDFs, Word (`.docx`), Excel (`.xlsx`) and plain-text/code files are all readable this way.
- **Agent reuse:** supported. The bridge emits `agent_id` in its NDJSON meta events; setting `reuse_agent=true` resumes an existing agent session.

## Claude

- **Install:** `python akana.py add claude` installs the global `@anthropic-ai/claude-code` CLI (`npm install -g @anthropic-ai/claude-code`).
- **Credentials:** `claude_oauth_token` in the vault. Generate a token with `claude setup-token` and paste it in Settings → Credentials, or log in once with the Claude CLI (`claude`); Akana then falls back to the CLI's OAuth session token from `credentials.json`.
- **Tool loop:** MCP-native. Akana writes an `--mcp-config` file at spawn time and passes the memory and vault servers plus external `mcp_servers.yaml` entries, restricted via `--allowedTools mcp__<name>` flags. This is the only provider that supports **auto-continue** (multi-run autonomous continuation gated by a sentinel token), which is off by default. A plan-mode path (`ExitPlanMode`) exists in the backend API but currently has no UI entry point.
- **Thinking mode:** six user-facing tiers (**Fast, Normal, Deep, Intense, Max, Ultra**) chosen per turn from the composer. The first five map onto the Claude CLI's native `--effort` scale (`low`, `medium`, `high`, `xhigh`, `max`). **Ultra** is Akana-specific: it still selects `--effort max` and additionally appends the `ultracode` keyword to the prompt on fable-persona Claude models, engaging Claude Code's multi-agent orchestration mode. On non-fable Claude models the Ultra tier still uses `--effort max` but drops the keyword; on non-Claude providers the Ultra option is hidden in the composer. Thinking mode is per-turn, not persisted.
- **Multimodal input:** supported by path reference. Akana writes the uploaded file to disk and passes its absolute path to the model; the Claude Code CLI's built-in Read tool (in the always-allowed read-only trio) opens the file directly. Images go through Claude's native vision and PDFs through its native PDF understanding. Word (`.docx`), Excel (`.xlsx`) and plain-text/code files are also readable this way.
- **Agent reuse:** supported via `--resume`.
- **Known limitation:** the Claude CLI runs as a child process with `bypassPermissions` (default `claude_full_tools=true`), so any tool the model chooses to invoke (including shell) runs with the account's own privileges.

## Codex _(experimental)_

Codex bridges the **OpenAI Codex CLI** (`codex exec`) the same way the Claude provider bridges the `claude` CLI. It is **subscription-billed through your ChatGPT sign-in**, not API-key billed — this is the key difference from the OpenAI provider above, which uses `OPENAI_API_KEY` and OpenAI's platform API. The two are independent and can be configured side by side.

- **Install:** `python akana.py add codex` installs the global `@openai/codex` CLI (`npm install -g @openai/codex`), then prompts you to run `codex login` once.
- **Credentials:** none stored by Akana. Auth is the ChatGPT OAuth session that `codex login` writes to `~/.codex/auth.json`. Akana strips `CODEX_API_KEY` / `OPENAI_API_KEY` (and other foreign keys) from the CLI's environment so it always uses the subscription session, never an API key. If the CLI is missing or not logged in, chat fails with a clear HTTP 503 pointing you at `npm install -g @openai/codex` / `codex login`. The Settings model list surfaces the same signal via `codex login status`.
- **Tool loop:** MCP-native, over the CLI. Akana spawns `codex exec --json` and maps the `akana_memory` / `akana_vault` servers plus external `mcp_servers.yaml` entries onto `-c mcp_servers.<name>.command|args|env.<KEY>=<value>` config overrides. Secret-bearing env values (for example the vault master key) are kept **off** the command line — they would be world-readable via `ps`/`tasklist` — and forwarded through the CLI's inherited process environment instead, on which the Codex-spawned MCP child relies. Memory tools (non-secret env) work unconditionally; the vault path additionally requires that Codex propagate its process environment to stdio MCP children (the common MCP convention). `AKANA_VAULT_TOOLS=0` disables the vault path entirely.
- **Sandbox / approvals:** governed by the shared `claude_full_tools` setting (default on). On → `--dangerously-bypass-approvals-and-sandbox` (the `bypassPermissions` analogue: the model runs shell/edits unsupervised with your own privileges). Off → `--sandbox read-only` (reads only, no writes/side effects). `codex exec` is non-interactive so it never prompts for an approval. Akana also passes `--skip-git-repo-check` (the workspace need not be a git repo) and `-C <workspace>`.
- **Thinking mode:** the per-turn tiers map onto Codex's `model_reasoning_effort` config (`minimal`/`low`/`medium`/`high`) via `-c model_reasoning_effort=<level>`. The Ultra tier maps to high; the `ultracode` keyword is Claude-only.
- **Prompt delivery:** the prompt (system persona + framed history + the turn) is written to the CLI's stdin via the `-` positional, never on argv — so user text never rides the command line, which also neutralises the Windows `cmd /c` reparse hazard for a `codex.cmd` shim.
- **Multimodal input:** not wired at the Akana layer; `file_ids` are accepted-and-ignored.
- **Agent reuse:** supported. The first turn's `thread.started` event carries a `thread_id`; Akana persists it (provider-scoped) and resumes with `codex exec resume <thread_id>` so the model keeps the full conversation, exactly like Claude's `--resume`.

## Gemini _(experimental)_

- **Install:** `python akana.py add gemini` runs `pip install -r requirements-gemini.txt` (which pulls `google-genai>=1.0`). If the SDK is missing, the provider reports itself as unavailable and the rest of the app boots cleanly.
- **Credentials:** `gemini_api_key` in the vault. Requests go directly from your machine to Google's API; Akana does not proxy them through Cursor.
- **Tool loop:** native function calling. Memory (`memory_search` / `save_memory` / `memory_forget`) and all seven vault tools are declared as `GEMINI_TOOL_DECLS`, then merged with any bridged external MCP tools. The loop caps at five rounds per turn.
- **Thinking mode:** supported on Gemini 3+ series via `thinking_level`; Gemini 2.5 and older ignore the setting. The Ultra tier maps to high; the `ultracode` keyword is Claude-only.
- **Multimodal input:** images and PDFs only, embedded inline in the request as `inline_data` parts. Word, Excel and text files are unsupported on Gemini. Uploading one attaches a note to the prompt suggesting a switch to the Claude provider. If none of the uploaded files are readable, the turn is rejected with an explanatory message instead of reaching the model.
- **Agent reuse:** not supported.

## OpenAI _(experimental)_

- **Install:** none. The provider uses `httpx` from core requirements.
- **Credentials:** `openai_api_key` in the vault. Requests go directly to OpenAI's API.
- **Tool loop:** native function calling, capped at five rounds per turn. Same memory + vault tool set as Gemini (`OPENAI_TOOL_DECLS`, derived single-source from `GEMINI_TOOL_DECLS`), plus any bridged MCP tools.
- **Thinking mode:** supported on o-series (`o1`, `o3`, etc.) and GPT-5+ via `reasoning_effort`. Other models ignore it. The Ultra tier maps to high; the `ultracode` keyword is Claude-only.
- **Multimodal input:** images and PDFs only, sent as `image_url` parts and base64 `file_data` data URIs. Word, Excel and text files are unsupported. The prompt gets a note suggesting a switch to the Claude provider; if no uploaded file is readable, the turn is rejected with an explanatory message instead of reaching the model.
- **Agent reuse:** not supported.

## Ollama _(experimental)_

- **Install:** none in-repo. The user installs the Ollama app separately from [ollama.com](https://ollama.com). Akana connects to `http://localhost:11434` by default (override with `AKANA_OLLAMA_URL`).
- **Credentials:** none required.
- **Tool loop:** native function calling against `/api/chat`, capped at five rounds per turn. The declared tool set is the same memory + vault set as Gemini and OpenAI. Because Ollama runs local models with varied capability, the provider sends the tool declarations by default; if Ollama rejects them with a "does not support tools" error, it retries the same round without them. On that turn the memory, vault and bridged MCP tools are unavailable. This is logged but not surfaced to the user. The tools are declared, but small local models call them unreliably. This path has not been verified end-to-end on real hardware.
- **Thinking mode:** boolean `think` flag. Whether it actually influences generation depends on the local model.
- **Multimodal input:** not wired at the Akana layer; `file_ids` are accepted-and-ignored. Read-timeout is disabled by default (`AKANA_OLLAMA_TIMEOUT=0`) because cold/slow models can take minutes on the first prompt.
- **Agent reuse:** not supported.

## Multimodal input (images, PDFs and documents)

Akana's web UI accepts uploads of images, PDFs, Word documents (`.docx`), Excel spreadsheets (`.xlsx`) and plain-text or code files. What actually happens with the upload depends on the active provider; there are two different mechanisms.

- **Path-native providers (Cursor and Claude).** The server writes the file to disk under the uploads directory, then appends a reference line to the prompt: `[Görsel: <absolute path>]` for images or `[Dosya: <absolute path>]` for anything else. The provider's own file-reading tool opens the path: Claude Code's built-in Read tool (in Akana's read-only allow-list, so it runs on every turn without a permission prompt), or the Cursor SDK's file-reading tool. No content is embedded in the prompt, only the path. This covers **images, PDFs, `.docx`, `.xlsx` and text/code files** for both providers. The path-reference labels are hardcoded in Turkish for historical reasons; they are read correctly by both models regardless of the UI language.
- **Inline-native providers (Gemini and OpenAI).** File bytes are base64-embedded directly into the request: Gemini receives an `inline_data` part, OpenAI an `image_url` or `file_data` data URI. Only **images and PDFs** are supported. If you upload a `.docx`/`.xlsx`/text file with Gemini or OpenAI selected, Akana attaches a note to the prompt explaining that the current provider cannot read that file kind and suggests switching to the Claude provider instead of silently dropping the file. If none of the uploaded files are readable, the turn is rejected with an explanatory message before it reaches the model.
- **Ollama.** File input is not wired.

The set of provider-native paths lives in `akana_server/multimodal/provider.py`; the gate that assembles the reference lines is `akana_server/api/routes/chat/gates.py`.
