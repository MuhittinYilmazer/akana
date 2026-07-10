/**
 * Akana chat render — DOM building for message bubbles, tool cards, memory cards.
 *
 * Pure-ish helpers (formatJsonPretty, renderActionCard, renderToolCall,
 * renderMemoryUse, upsertToolCallCard) depend only on global modules
 * (AkanaCore, AkanaMarkdown). Functions that need DOM hooks (the chat log,
 * appendUserMessage, etc.) live behind `createRenderer(hooks)`.
 *
 * Contract: window.AkanaChatRender public signatures and the critical
 * class/id selectors (.tool-call, .memory-use, [data-tool-call-id], .action-card*)
 * are IMMUTABLE — transport/threads rely on them. New visual layers
 * (status dot, i18n labels, code-copy, timestamp, TTS wave) arrive only
 * as ADDITIONAL nodes/attributes; styles live in akana-chat.css.
 */
(() => {
  const escapeHtml = (s) => window.AkanaCore.escapeHtml(s);
  const setBubbleMarkdown = (b, t) => window.AkanaMarkdown.setBubbleMarkdown(b, t);

  function formatJsonPretty(value) {
    if (value == null || value === "") return "";
    if (typeof value === "object") {
      try {
        return JSON.stringify(value, null, 2);
      } catch {
        return String(value);
      }
    }
    const s = String(value).trim();
    if (!s || s === "{" || s === "}") return "";
    try {
      return JSON.stringify(JSON.parse(s), null, 2);
    } catch {
      return s.length > 400 ? `${s.slice(0, 400)}…` : s;
    }
  }

  const ARG_KEY_TR = {
    path: window.AkanaI18n.t("msg.arg_file"),
    file_path: window.AkanaI18n.t("msg.arg_file"),
    filePath: window.AkanaI18n.t("msg.arg_file"),
    target_file: window.AkanaI18n.t("msg.arg_file"),
    target_notebook: window.AkanaI18n.t("msg.arg_notebook"),
    command: window.AkanaI18n.t("msg.arg_command"),
    cmd: window.AkanaI18n.t("msg.arg_command"),
    query: window.AkanaI18n.t("msg.arg_query"),
    q: window.AkanaI18n.t("msg.arg_query"),
    search_term: window.AkanaI18n.t("msg.arg_search"),
    pattern: window.AkanaI18n.t("msg.arg_pattern"),
    glob_pattern: window.AkanaI18n.t("msg.arg_glob"),
    url: window.AkanaI18n.t("msg.arg_url"),
    description: window.AkanaI18n.t("msg.arg_description"),
    explanation: window.AkanaI18n.t("msg.arg_description"),
    old_string: window.AkanaI18n.t("msg.arg_old_text"),
    new_string: window.AkanaI18n.t("msg.arg_new_text"),
    input: window.AkanaI18n.t("msg.arg_input"),
    server: window.AkanaI18n.t("msg.arg_server"),
    toolName: window.AkanaI18n.t("msg.arg_tool"),
    key: window.AkanaI18n.t("msg.arg_key"),
    text: window.AkanaI18n.t("msg.arg_text"),
    contents: window.AkanaI18n.t("msg.arg_content"),
    content: window.AkanaI18n.t("msg.arg_content"),
    target_directory: window.AkanaI18n.t("msg.arg_directory"),
    target_id: window.AkanaI18n.t("msg.arg_target"),
    recursive: window.AkanaI18n.t("msg.arg_recursive"),
    is_background: window.AkanaI18n.t("msg.arg_background"),
  };

  const INTERNAL_ARG_KEYS = new Set([
    "provideridentifier",
    "provider_identifier",
    "toolname",
    "tool_name",
    "araç",
    "arac",
    "name",
    "callid",
    "call_id",
    "type",
    "args",
    "input",
    "timeout",
    "headlimit",
    "head_limit",
    "offset",
    "outputmode",
    "output_mode",
    "workspaceresults",
    "workspace_results",
    "ok",
    "status",
  ]);

  const CURATED_ARG_TOOLS = new Set([
    "memory_search",
    "memory_remember",
    "memory_forget",
    "memory_explain",
    "__shell__",
    "read_file",
    "glob_file_search",
    "grep",
    "run_terminal_cmd",
    "list_dir",
    "ls",
    "todo_write",
    "web_fetch",
    "fetch",
  ]);
  const MAX_RESULT_TEXT = 12000;
  const MAX_ARG_VAL = 4800;
  const MAX_LIST_ITEMS = 24;

  function tryParseJsonValue(value) {
    if (value == null) return null;
    if (typeof value === "object") return value;
    const s = String(value).trim();
    if (!s) return "";
    try {
      return JSON.parse(s);
    } catch {
      return s;
    }
  }

  function formatArgValue(val, max = 480) {
    if (val == null) return "";
    if (typeof val === "boolean") return val ? window.AkanaI18n.t("msg.bool_yes") : window.AkanaI18n.t("msg.bool_no");
    if (typeof val === "number") return String(val);
    if (typeof val === "string") return truncateLine(val.replace(/\r\n/g, "\n"), max);
    if (Array.isArray(val)) {
      if (!val.length) return window.AkanaI18n.t("msg.empty_list");
      const preview = val.slice(0, 6).map((v) => formatArgValue(v, 80)).join("; ");
      return val.length > 6 ? `${preview} … (+${val.length - 6})` : preview;
    }
    if (typeof val === "object") {
      const keys = Object.keys(val);
      if (!keys.length) return window.AkanaI18n.t("msg.empty_obj");
      return truncateLine(formatJsonPretty(val), max);
    }
    return truncateLine(String(val), max);
  }

  function pickArg(args, keys) {
    for (const k of keys) {
      const v = args[k];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return "";
  }

  function normalizedToolName(raw) {
    let s = String(raw || "").trim();
    if (!s) return "";
    const mcp = s.match(/^mcp__.+?__(.+)$/i);
    if (mcp) s = mcp[1];
    if (s.includes("/")) s = s.split("/").pop() || s;
    const colon = s.match(/^[^:]+:(.+)$/);
    if (colon && colon[1].trim()) s = colon[1].trim();
    return s.replace(/\./g, "_");
  }

  /* Task-list tool family (Claude `TodoWrite` → norm "todowrite",
     Cursor `todo_write`, etc.). SINGLE source: label/family/action/argBlocksForTool
     and live checklist card routing all feed from here. */
  const TODO_TOOL_RE = /^(todo_write|todowrite|todo|todos|update_todos)$/;

  function toolRawName(call) {
    const fn = (call && call.function) || {};
    return String(
      (call && call.name) || fn.name || (call && call.tool) || (call && call.toolName) || "",
    ).trim();
  }

  function toolNameNorm(call) {
    const raw = inferEffectiveToolName(call);
    if (isShellAsToolName(raw)) return "__shell__";
    if (isFilePathAsToolName(raw)) return "read_file";
    if (looksLikeGlobPattern(raw)) return "glob_file_search";
    if (looksLikeGrepPattern(raw)) return "grep";
    return normalizedToolName(raw).toLowerCase();
  }

  function isShellAsToolName(raw) {
    const s = String(raw || "").trim();
    if (!s) return false;
    if (/\&\&|\||\;/.test(s)) return true;
    if (/\s/.test(s) && /['"`$]/.test(s)) return true;
    return /^(find|which|whereis|xdg-open|open|curl|wget|grep|git|npm|python|node|bash|sh|cd|ls|cat|echo|date|uname|mkdir|rm|mv|cp|chmod|sudo|docker|ffmpeg|mpv|head|tail|wc|sort|pwd|env|test|printf)\b/i.test(
      s,
    );
  }

  function isFilePathAsToolName(raw) {
    const s = String(raw || "").trim();
    return /^(\/|~|\.\.?\/)/.test(s) && /\/[^/]+\.[a-z0-9]{1,12}$/i.test(s);
  }

  function looksLikeGlobPattern(raw) {
    const s = String(raw || "").trim();
    return /\*\*|\/\*\*\/|\*\.[a-z0-9]+/i.test(s);
  }

  function looksLikeGrepPattern(raw) {
    const s = String(raw || "").trim();
    // A bare '.' is NOT a grep signal: it is a common separator in canonical tool
    // names ("memory.search", MCP dotted leaves) — matching it here hijacked those
    // to the grep family before dot-folding (normalizedToolName) could run. Require
    // genuinely regex-y metacharacters instead.
    return /\||ToolCall|tool_call|[+*?^$[\]()]/.test(s) && s.length < 120 && !isFilePathAsToolName(s);
  }

  function sanitizeToolArgsObject(args) {
    if (!args || typeof args !== "object" || Array.isArray(args) || args._raw) return args;
    if (args.args && typeof args.args === "object" && !Array.isArray(args.args)) {
      return { ...args.args };
    }
    return args;
  }

  function parseToolArgsRaw(call) {
    const fn = (call && call.function) || {};
    let rawArgs = call && "args" in call ? call.args : fn.arguments;
    if (typeof rawArgs === "string") {
      try {
        rawArgs = JSON.parse(rawArgs);
      } catch {
        return rawArgs.trim() ? { _raw: rawArgs.trim() } : null;
      }
    }
    if (rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs)) return rawArgs;
    return null;
  }

  function inferEffectiveToolName(call) {
    let raw = toolRawName(call);
    const rawArgs = parseToolArgsRaw(call);
    if (!raw && rawArgs) {
      // No producer in the codebase emits "Araç"/"araç" as a rawArgs KEY (the
      // Turkish word only appears as a translated VALUE in the i18n tables);
      // the localized-key fallback that used to live here was dead code.
      raw = String(
        rawArgs.toolName || rawArgs.tool_name || rawArgs.name || "",
      ).trim();
    }
    if (!raw && rawArgs?.providerIdentifier && rawArgs?.toolName) {
      raw = String(rawArgs.toolName).trim() || `${rawArgs.providerIdentifier}/${rawArgs.toolName}`;
    }
    if (!raw) raw = toolRawName(call);
    return raw;
  }

  function displayCall(call) {
    const name = inferEffectiveToolName(call);
    if (!name || name === toolRawName(call)) return call;
    return { ...call, name };
  }

  function cleanMemoryText(text) {
    return String(text || "")
      .replace(/^[a-z0-9_]+:[a-z0-9_]+:\s*/i, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function shellActionFromCommand(cmd) {
    const c = String(cmd || "").trim();
    if (!c) return { icon: "💻", text: window.AkanaI18n.t("msg.shell_terminal"), sub: "" };

    // Specific cases first (more concrete than verb-table matches)
    const yt = c.match(/https?:\/\/[^\s"']*youtube[^\s"']*/i);
    if (/xdg-open|^\s*open\s/i.test(c) && yt) {
      return { icon: "▶️", text: window.AkanaI18n.t("msg.shell_youtube"), sub: truncateLine(yt[0], 72) };
    }
    if (/xdg-open|^\s*open\s/i.test(c)) {
      const url = c.match(/https?:\/\/[^\s"']+/i);
      return { icon: "🌐", text: window.AkanaI18n.t("msg.shell_open_url"), sub: url ? truncateLine(url[0], 72) : truncateLine(c, 72) };
    }
    if (/^find\b/i.test(c)) return { icon: "🔎", text: window.AkanaI18n.t("msg.shell_find"), sub: truncateLine(c, 80) };
    if (/^which\b|^whereis\b/i.test(c)) return { icon: "🔎", text: window.AkanaI18n.t("msg.shell_which"), sub: truncateLine(c, 64) };
    if (/^date\b/i.test(c) || /^uname\b/i.test(c)) {
      return { icon: "🕐", text: window.AkanaI18n.t("msg.shell_sysinfo"), sub: truncateLine(c, 72) };
    }

    // Split on compound operators, skip noise segments, match first actionable verb
    const NOISE_WORDS = new Set(["cd", "sleep", "echo", "export", "source", ":", "true", "then", "fi", "do", "done", "set"]);
    const segments = c.split(/\s*(?:;|&&|\|\||\|)\s*/);
    for (const seg of segments) {
      const trimmed = seg.trim();
      if (!trimmed) continue;
      const firstWord = trimmed.split(/\s+/)[0].toLowerCase();
      if (NOISE_WORDS.has(firstWord)) continue;

      // Special case: python/python3 -m pytest
      if (/^python3?\s+-m\s+pytest\b/i.test(trimmed)) {
        return { icon: "🧪", text: window.AkanaI18n.t("msg.shell_test"), sub: truncateLine(c, 80) };
      }

      if (firstWord === "git") return { icon: "🔧", text: window.AkanaI18n.t("msg.shell_git"), sub: truncateLine(c, 80) };
      if (["kill", "pkill", "killall"].includes(firstWord)) return { icon: "⏹️", text: window.AkanaI18n.t("msg.shell_kill"), sub: truncateLine(c, 80) };
      if (firstWord === "rm") return { icon: "🗑️", text: window.AkanaI18n.t("msg.shell_remove"), sub: truncateLine(c, 80) };
      if (firstWord === "mkdir") return { icon: "📁", text: window.AkanaI18n.t("msg.shell_mkdir"), sub: truncateLine(c, 80) };
      if (firstWord === "cp") return { icon: "📑", text: window.AkanaI18n.t("msg.shell_copy"), sub: truncateLine(c, 80) };
      if (firstWord === "mv") return { icon: "📦", text: window.AkanaI18n.t("msg.shell_move"), sub: truncateLine(c, 80) };
      if (firstWord === "ls") return { icon: "📁", text: window.AkanaI18n.t("msg.shell_list"), sub: truncateLine(c, 80) };
      if (["cat", "head", "tail", "less"].includes(firstWord)) return { icon: "📄", text: window.AkanaI18n.t("msg.shell_read"), sub: truncateLine(c, 80) };
      if (["curl", "wget"].includes(firstWord)) return { icon: "🌐", text: window.AkanaI18n.t("msg.shell_download"), sub: truncateLine(c, 80) };
      if (["chmod", "chown"].includes(firstWord)) return { icon: "🔐", text: window.AkanaI18n.t("msg.shell_chmod"), sub: truncateLine(c, 80) };
      if (["ps", "top", "htop"].includes(firstWord)) return { icon: "📊", text: window.AkanaI18n.t("msg.shell_ps"), sub: truncateLine(c, 80) };
      if (["grep", "rg", "ag"].includes(firstWord)) return { icon: "🔎", text: window.AkanaI18n.t("msg.shell_grep"), sub: truncateLine(c, 80) };
      if (["npm", "pnpm", "yarn", "npx"].includes(firstWord)) return { icon: "📦", text: window.AkanaI18n.t("msg.shell_pkg"), sub: truncateLine(c, 80) };
      if (["pip", "pip3"].includes(firstWord)) return { icon: "🐍", text: window.AkanaI18n.t("msg.shell_pip"), sub: truncateLine(c, 80) };
      if (firstWord === "pytest") return { icon: "🧪", text: window.AkanaI18n.t("msg.shell_test"), sub: truncateLine(c, 80) };
      if (["python", "python3", "node", "bash", "sh"].includes(firstWord)) return { icon: "▶️", text: window.AkanaI18n.t("msg.shell_script"), sub: truncateLine(c, 80) };
      // First actionable segment matched nothing — stop searching
      break;
    }

    return { icon: "💻", text: window.AkanaI18n.t("msg.shell_run_cmd"), sub: truncateLine(c, 80) };
  }

  function mergeToolCallForDisplay(call, node) {
    const merged = displayCall({ ...(call || {}) });
    const cached = node && node.dataset && node.dataset.toolArgsCache;
    if (cached) {
      try {
        const parsed = JSON.parse(cached);
        if (parsed && typeof parsed === "object" && merged.args == null && !merged.function?.arguments) {
          merged.args = parsed;
        }
      } catch {
        /* ignore */
      }
    }
    const args = parseToolArgs(merged);
    if (args && node && node.dataset) {
      try {
        node.dataset.toolArgsCache = JSON.stringify(args);
      } catch {
        /* ignore */
      }
    }
    return merged;
  }

  function deepUnwrapPayload(raw, depth = 0) {
    if (depth > 8 || raw == null || raw === "") return raw;
    let v = tryParseJsonValue(raw);

    if (typeof v === "string") {
      const inner = tryParseJsonValue(v);
      if (inner !== v) return deepUnwrapPayload(inner, depth + 1);
      return v;
    }

    if (!v || typeof v !== "object") return v;

    if (v._error) return v;

    const st = String(v.status || "").toLowerCase();
    if (st === "error" || st === "failed") {
      return { _error: v.error || v.message || v.detail || window.AkanaI18n.t("msg.err_tool_error") };
    }
    if ("value" in v && (st === "success" || st === "ok" || st === "finished" || v.value != null)) {
      return deepUnwrapPayload(v.value, depth + 1);
    }

    if (Array.isArray(v.content)) {
      const chunks = v.content
        .map((b) => {
          if (!b || typeof b !== "object") return "";
          if (typeof b.text === "string") return b.text;
          if (b.text && typeof b.text === "object" && typeof b.text.text === "string") return b.text.text;
          return "";
        })
        .filter(Boolean);
      if (chunks.length) return deepUnwrapPayload(chunks.join("\n"), depth + 1);
    }

    return v;
  }

  function formatMemoryHitBlocks(data) {
    const items = Array.isArray(data?.items) ? data.items : [];
    const blocks = [];
    const q =
      data?.request?.query ||
      data?.trace?.request?.query ||
      (typeof data?.query === "string" ? data.query : "");
    if (q) blocks.push({ type: "pill", label: window.AkanaI18n.t("msg.mem_search_label"), text: q });
    if (!items.length) {
      blocks.push({ type: "note", text: window.AkanaI18n.t("msg.mem_no_hits") });
      return blocks;
    }
    blocks.push({
      type: "hits",
      items: items.slice(0, MAX_LIST_ITEMS).map((it) => ({
        title: it.type || it.key || window.AkanaI18n.t("msg.mem_hit_record"),
        body: cleanMemoryText(it.summary || it.text || it.preview || it.value || ""),
        badge:
          typeof it.score === "number" && Number.isFinite(it.score)
            ? `%${Math.round(Math.max(0, Math.min(1, it.score)) * 100)}`
            : "",
        meta: it.trust ? String(it.trust) : "",
        id: it.id ? String(it.id).slice(0, 10) : "",
      })),
      total: items.length,
    });
    if (data.explain_id) {
      blocks.push({ type: "note", text: window.AkanaI18n.t("msg.mem_trace", { n: items.length, id: String(data.explain_id).slice(0, 14) }) });
    }
    return blocks;
  }

  function formatWorkspaceResultsBlocks(obj) {
    const ws = obj.workspaceResults || obj.workspace_results;
    if (!ws || typeof ws !== "object") return null;
    const files = [];
    for (const data of Object.values(ws)) {
      if (!data || typeof data !== "object") continue;
      const out = data.output || data;
      const list = out?.files || out?.matches || out?.paths || [];
      if (Array.isArray(list)) {
        for (const f of list) {
          if (typeof f === "string") files.push(f);
          else if (f && typeof f === "object") files.push(f.path || f.file || f.relativePath || String(f));
        }
      }
    }
    if (files.length) return formatSearchResultList(files);
    return [{ type: "note", text: window.AkanaI18n.t("msg.no_files_found") }];
  }

  function isInternalArgKey(key) {
    return INTERNAL_ARG_KEYS.has(String(key || "").toLowerCase().replace(/-/g, "_"));
  }

  function formatToolArgsBlocks(call) {
    const dc = displayCall(call);
    const args = parseToolArgs(dc);
    const rawName = inferEffectiveToolName(dc);
    const norm = toolNameNorm(dc);
    const blocks = [];
    const isCurated = CURATED_ARG_TOOLS.has(norm);

    if (isShellAsToolName(rawName) || norm === "__shell__") {
      const cmd = pickArg(args || {}, ["command", "cmd"]) || rawName;
      blocks.push({ type: "code", lang: "bash", text: cmd, label: window.AkanaI18n.t("msg.cmd_label") });
      return blocks;
    }

    if (!args) {
      blocks.push({
        type: "note",
        text: call && call.phase === "start" ? window.AkanaI18n.t("msg.running_ellipsis") : window.AkanaI18n.t("msg.no_params"),
      });
      return blocks;
    }
    if (typeof args._raw === "string") {
      blocks.push({ type: "code", lang: "text", text: truncateBlock(args._raw, MAX_ARG_VAL), label: window.AkanaI18n.t("msg.input_label") });
      return blocks;
    }

    if (Object.keys(args).length === 1 && typeof args.input === "string" && args.input.trim()) {
      const inner = tryParseJsonValue(args.input.trim());
      if (inner && typeof inner === "object" && !Array.isArray(inner)) {
        return formatToolArgsBlocks({ ...call, args: inner });
      }
      blocks.push({ type: "text", text: truncateLine(args.input.trim(), MAX_ARG_VAL) });
      return blocks;
    }

    const shown = new Set();
    function mark(...keys) {
      keys.forEach((k) => shown.add(k));
    }

    if (/^memory_search$/.test(norm)) {
      const q = pickArg(args, ["query", "q", "search_term", "text"]);
      if (q) blocks.push({ type: "pill", label: window.AkanaI18n.t("msg.arg_query"), text: q });
      mark("query", "q", "search_term", "text");
    } else if (/^memory_remember$/.test(norm)) {
      const key = pickArg(args, ["key", "text", "value"]);
      if (key) blocks.push({ type: "pill", label: window.AkanaI18n.t("msg.record_label"), text: key });
      mark("key", "text", "value");
    } else if (/^(read_file|read|cat)$/.test(norm)) {
      const p = pickArg(args, ["file_path", "path", "target_file", "filePath"]);
      if (p) {
        // pickArg only returns strings — extract numeric line numbers separately.
        const pickNum = (ks) => {
          for (const k of ks) {
            const v = args[k];
            if (typeof v === "number" && Number.isFinite(v)) return v;
            if (typeof v === "string" && v.trim() && !Number.isNaN(Number(v))) {
              return Number(v.trim());
            }
          }
          return null;
        };
        const start = pickNum(["start_line", "start_line_one_indexed", "offset", "start"]);
        const end = pickNum([
          "end_line",
          "end_line_one_indexed_inclusive",
          "end_line_one_indexed",
          "limit",
          "end",
        ]);
        let meta = "";
        if (start != null && end != null) meta = window.AkanaI18n.t("msg.line_range", { s: start, e: end });
        else if (start != null) meta = window.AkanaI18n.t("msg.line_from", { s: start });
        blocks.push({ type: "file", path: String(p), meta });
        mark(
          "file_path", "path", "target_file", "filePath",
          "start_line", "end_line", "offset", "limit",
          "start_line_one_indexed", "end_line_one_indexed",
          "end_line_one_indexed_inclusive", "start", "end",
        );
      }
    } else if (/^(write|write_file|create_file)$/.test(norm)) {
      const p = pickArg(args, ["file_path", "path", "target_file"]);
      const content = pickArg(args, ["contents", "content", "text", "body"]);
      if (p) {
        blocks.push({ type: "kv", items: [{ k: window.AkanaI18n.t("msg.arg_file"), v: p }] });
        mark("file_path", "path", "target_file");
      }
      if (content) {
        blocks.push({ type: "code", lang: "text", text: truncateBlock(content, MAX_ARG_VAL), label: window.AkanaI18n.t("msg.write_content_label") });
        mark("contents", "content", "text", "body");
      }
    } else if (/^(edit_file|str_replace|apply_patch|edit|search_replace)$/.test(norm)) {
      const p = pickArg(args, ["file_path", "path", "target_file"]);
      if (p) {
        blocks.push({ type: "kv", items: [{ k: window.AkanaI18n.t("msg.arg_file"), v: p }] });
        mark("file_path", "path", "target_file");
      }
      const oldS = pickArg(args, ["old_string", "old_str"]);
      const newS = pickArg(args, ["new_string", "new_str"]);
      if (oldS != null || newS != null) {
        // Single unified line-by-line diff (−red / +green) — instead of two separate
        // blocks. LCS is computed at render time.
        blocks.push({ type: "diff", old: String(oldS || ""), new: String(newS || "") });
        mark("old_string", "old_str", "new_string", "new_str");
      }
    } else if (/^(run_terminal_cmd|terminal|shell|bash|exec)$/.test(norm)) {
      const cmd = pickArg(args, ["command", "cmd"]);
      if (cmd) {
        blocks.push({ type: "code", lang: "bash", text: cmd });
        mark("command", "cmd");
      }
      const desc = pickArg(args, ["description", "explanation"]);
      if (desc) {
        blocks.push({ type: "note", text: desc });
        mark("description", "explanation");
      }
    } else if (/^(web_search)$/.test(norm)) {
      const q = pickArg(args, ["query", "q", "search_term"]);
      if (q) blocks.push({ type: "kv", items: [{ k: window.AkanaI18n.t("msg.search_label"), v: q }] });
      mark("query", "q", "search_term");
    } else if (/^grep$/.test(norm)) {
      const q = pickArg(args, ["pattern", "query", "q", "search_term"]);
      const path = pickArg(args, ["path", "target_directory", "glob_pattern"]);
      if (q) blocks.push({ type: "pill", label: window.AkanaI18n.t("msg.arg_pattern"), text: q });
      if (path) blocks.push({ type: "pill", label: window.AkanaI18n.t("msg.scope_label"), text: path });
      mark("pattern", "query", "q", "search_term", "path", "target_directory", "glob_pattern");
    } else if (/^(glob_file_search|glob)$/.test(norm)) {
      const p = pickArg(args, ["glob_pattern", "pattern", "target_directory", "path"]);
      if (p) blocks.push({ type: "pill", label: window.AkanaI18n.t("msg.arg_glob"), text: p });
      mark("glob_pattern", "pattern", "target_directory", "path");
    } else if (/^(codebase_search|search|rg)$/.test(norm)) {
      const q = pickArg(args, ["query", "pattern", "q", "search_term"]);
      const path = pickArg(args, ["path", "target_directory", "glob_pattern"]);
      const items = [];
      if (q) items.push({ k: window.AkanaI18n.t("msg.search_label"), v: q });
      if (path) items.push({ k: window.AkanaI18n.t("msg.scope_label"), v: path });
      if (items.length) blocks.push({ type: "kv", items });
      mark("query", "pattern", "q", "search_term", "path", "target_directory", "glob_pattern");
    } else if (/^(list_dir|ls)$/.test(norm)) {
      const p = pickArg(args, ["path", "target_directory"]);
      if (p) blocks.push({ type: "file", path: String(p), meta: window.AkanaI18n.t("msg.dir_meta") });
      mark("path", "target_directory");
    } else if (/^(fetch|web_fetch|read_url|browse|open_url|read_website)$/.test(norm)) {
      const url = pickArg(args, ["url", "uri", "link", "href"]);
      if (url) {
        blocks.push({ type: "link", url: String(url) });
        mark("url", "uri", "link", "href");
      }
    } else if (TODO_TOOL_RE.test(norm)) {
      const raw = Array.isArray(args.todos)
        ? args.todos
        : Array.isArray(args.items)
          ? args.items
          : null;
      if (raw && raw.length) {
        const items = raw
          .slice(0, 50)
          .map((t) => ({
            text:
              (t && (t.content || t.text || t.title || t.activeForm || t.task)) ||
              (typeof t === "string" ? t : ""),
            status: (t && t.status) || "pending",
          }))
          .filter((x) => x.text);
        if (items.length) {
          blocks.push({ type: "todos", items });
          mark("todos", "items", "merge");
        }
      }
    } else if (/^memory_/.test(norm)) {
      const q = pickArg(args, ["query", "q", "key", "text", "target_id", "explain_id"]);
      if (q) blocks.push({ type: "pill", label: window.AkanaI18n.t("msg.memory_label"), text: q });
      mark("query", "q", "key", "text", "target_id", "explain_id", "used_ids");
    }

    if (!isCurated) {
      const rest = [];
      for (const [key, val] of Object.entries(args)) {
        if (shown.has(key) || isInternalArgKey(key)) continue;
        if (val == null || val === "") continue;
        if (key === "used_ids" && Array.isArray(val)) {
          rest.push({ k: window.AkanaI18n.t("msg.record_ids_label"), v: window.AkanaI18n.t("msg.record_ids_n", { n: val.length }) });
          continue;
        }
        const label = ARG_KEY_TR[key] || key.replace(/_/g, " ");
        rest.push({ k: label, v: formatArgValue(val) });
      }
      if (rest.length) blocks.push({ type: "kv", items: rest });
    }
    if (!blocks.length) blocks.push({ type: "note", text: window.AkanaI18n.t("msg.no_detail") });
    return blocks;
  }

  /** list_dir result → file/folder chips (tolerates string/array/object shapes). */
  function formatDirEntriesBlocks(unwrapped) {
    let entries = null;
    if (Array.isArray(unwrapped)) entries = unwrapped;
    else if (unwrapped && typeof unwrapped === "object") {
      entries =
        unwrapped.entries || unwrapped.files || unwrapped.items || unwrapped.children || null;
    } else if (typeof unwrapped === "string") {
      entries = unwrapped
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
    }
    if (!Array.isArray(entries) || !entries.length) return null;
    const items = entries
      .slice(0, 80)
      .map((e) => {
        if (typeof e === "string") {
          const isDir = /\/$/.test(e);
          const name = e.replace(/\/+$/, "");
          return { name: name.split("/").pop() || name, kind: isDir ? "dir" : "file" };
        }
        if (e && typeof e === "object") {
          const rawName = String(e.name || e.path || e.file || e.filename || "");
          const isDir =
            e.type === "dir" ||
            e.type === "directory" ||
            e.is_dir ||
            e.isDirectory ||
            e.directory ||
            /\/$/.test(rawName);
          const name = rawName.replace(/\/+$/, "");
          return { name: name.split("/").pop() || name, kind: isDir ? "dir" : "file" };
        }
        return { name: String(e), kind: "file" };
      })
      .filter((x) => x.name);
    if (!items.length) return null;
    items.sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "dir" ? -1 : 1));
    return [{ type: "files", items, total: entries.length }];
  }

  function formatSearchResultList(arr) {
    const items = arr.slice(0, MAX_LIST_ITEMS).map((it) => {
      if (typeof it === "string") return truncateLine(it, 160);
      if (it && typeof it === "object") {
        const title = it.title || it.name || it.path || it.file || it.key || it.id;
        const snippet = it.snippet || it.preview || it.text || it.content;
        if (title && snippet) return `${title}: ${truncateLine(String(snippet), 100)}`;
        if (title) return String(title);
        return truncateLine(formatJsonPretty(it).replace(/\n/g, " "), 160);
      }
      return truncateLine(String(it), 160);
    });
    if (arr.length > MAX_LIST_ITEMS) items.push(window.AkanaI18n.t("msg.more_records", { n: arr.length - MAX_LIST_ITEMS }));
    return [{ type: "list", items }];
  }

  function formatObjectAsKv(obj, maxKeys = 16) {
    const items = [];
    for (const [k, v] of Object.entries(obj || {})) {
      if (items.length >= maxKeys) break;
      items.push({ k: ARG_KEY_TR[k] || k.replace(/_/g, " "), v: formatArgValue(v, 320) });
    }
    if (Object.keys(obj || {}).length > maxKeys) {
      items.push({ k: "…", v: window.AkanaI18n.t("msg.more_fields", { n: Object.keys(obj).length - maxKeys }) });
    }
    return items.length ? [{ type: "kv", items }] : [];
  }

  function formatShellResult(obj) {
    const blocks = [];
    const code = obj.exit_code ?? obj.exitCode;
    if (code != null) blocks.push({ type: "kv", items: [{ k: window.AkanaI18n.t("msg.exit_code"), v: String(code) }] });
    const out = obj.stdout ?? obj.output ?? "";
    const err = obj.stderr ?? obj.error ?? "";
    if (out) {
      blocks.push({
        type: "code",
        lang: "text",
        text: truncateBlock(String(out), MAX_RESULT_TEXT),
        label: window.AkanaI18n.t("msg.output_label"),
      });
    }
    if (err) {
      blocks.push({
        type: "code",
        lang: "text",
        text: truncateBlock(String(err), MAX_RESULT_TEXT),
        label: window.AkanaI18n.t("msg.err_output_label"),
      });
    }
    return blocks.length ? blocks : null;
  }

  /* ── Terminal card (Feature 2): shell/Bash tool calls get a dedicated `.term-card`
     instead of the generic Input/Output panel — a real terminal look: "$ command"
     header, dark mono <pre> body, ANSI SGR color/bold spans, a live elapsed-time
     counter while running, and an exit-status chip. ── */

  /** Tiny ANSI SGR (`\x1b[...m`) → HTML span converter. Supports 30-37/90-97
   *  foreground, 1 (bold), 0/39 (reset). Any OTHER escape sequence (cursor moves,
   *  clear-line, background colors, etc.) is stripped, never left as raw bytes.
   *  Output is escaped (textContent-safe) before spans are inserted — no XSS via
   *  tool stdout. */
  const ANSI_FG_16 = {
    30: "#4b5563", 31: "#f87171", 32: "#4ade80", 33: "#facc15",
    34: "#60a5fa", 35: "#c084fc", 36: "#22d3ee", 37: "#e5e7eb",
    90: "#6b7280", 91: "#fca5a5", 92: "#86efac", 93: "#fde047",
    94: "#93c5fd", 95: "#e9d5ff", 96: "#67e8f9", 97: "#f9fafb",
  };
  function ansiToHtml(text) {
    const s = String(text || "");
    let out = "";
    let openSpans = 0;
    let fg = null;
    let bold = false;
    // eslint-disable-next-line no-control-regex
    const re = /\x1b\[([0-9;]*)m|\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|$)/g;
    let last = 0;
    let m;
    const flushSpanState = () => {
      if (openSpans) {
        out += "</span>".repeat(openSpans);
        openSpans = 0;
      }
      if (fg || bold) {
        const style = [fg ? `color:${fg}` : "", bold ? "font-weight:700" : ""]
          .filter(Boolean)
          .join(";");
        out += `<span style="${style}">`;
        openSpans = 1;
      }
    };
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) out += escapeHtml(s.slice(last, m.index));
      last = re.lastIndex;
      const codesRaw = m[1];
      if (codesRaw === undefined) continue; // non-SGR escape (cursor/clear/OSC) → stripped
      const codes = codesRaw === "" ? [0] : codesRaw.split(";").map((n) => parseInt(n, 10));
      for (const code of codes) {
        if (code === 0 || Number.isNaN(code)) {
          fg = null;
          bold = false;
        } else if (code === 1) {
          bold = true;
        } else if (code === 22) {
          bold = false;
        } else if (code === 39) {
          fg = null;
        } else if (ANSI_FG_16[code]) {
          fg = ANSI_FG_16[code];
        }
        // Other SGR codes (background, underline, etc.) intentionally ignored —
        // out-of-scope per spec (fg 30-37/90-97 + bold + reset only).
      }
      flushSpanState();
    }
    if (last < s.length) out += escapeHtml(s.slice(last));
    if (openSpans) out += "</span>".repeat(openSpans);
    return out;
  }

  /** Live elapsed-time label while a shell command is running ("1.2s", ticking). */
  function termElapsedLabel(startedAtMs) {
    if (!startedAtMs) return "";
    const ms = Math.max(0, Date.now() - startedAtMs);
    return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  /** Exit-status chip from a shell result object/number ({exit_code}|{exitCode}). */
  function termExitChip(call) {
    const dc = displayCall(call);
    const raw = dc && (dc.result ?? dc.output);
    if (raw == null || raw === "") return null;
    const u = deepUnwrapPayload(raw);
    const code = u && typeof u === "object" ? u.exit_code ?? u.exitCode : null;
    if (typeof code !== "number") return null;
    return { code, ok: code === 0 };
  }

  /** Best-effort shell output text (stdout+stderr) from any result shape — string,
   *  {stdout,stderr}, or wrapped tool_result content blocks (deepUnwrapPayload). */
  function termOutputText(call) {
    const dc = displayCall(call);
    const raw = dc && (dc.result ?? dc.output);
    if (raw == null || raw === "") return "";
    const u = deepUnwrapPayload(raw);
    if (typeof u === "string") return u;
    if (u && typeof u === "object") {
      const out = u.stdout ?? u.output ?? "";
      const err = u.stderr ?? u.error ?? "";
      return [out, err].filter(Boolean).join(out && err ? "\n" : "");
    }
    return "";
  }

  /** Build (or refresh) the `.term-card` panel for a shell tool call. `existing`
   *  (if provided) is patched in place — output growing on live delta re-renders,
   *  elapsed/exit chip refreshed — instead of rebuilding the DOM each time. */
  function renderTermCard(call, existing) {
    const dc = displayCall(call);
    const args = parseToolArgs(dc) || {};
    const rawName = inferEffectiveToolName(dc);
    // For the name-is-the-command shape (Cursor/MCP shell-as-toolname) there are no
    // args and no arg-highlight, so fall back to the raw name — the command itself —
    // exactly as the sibling paths (formatToolArgsBlocks, toolCallActionSentence) do.
    const cmd =
      pickArg(args, ["command", "cmd"]) ||
      toolCallArgHighlight(dc) ||
      (isShellAsToolName(rawName) ? rawName : "");
    const running = toolCallStatus(dc) === "running";
    // A card BORN already-done with no persisted start (history/F5 restore) has no
    // real runtime; a render-time startedAt would fabricate "0ms" (see the elapsed
    // guard below). Live/running cards and patched existing cards keep a real start.
    const persistedStart = existing && Number(existing.dataset.startedAt);
    const startedAt = persistedStart || Date.now();

    const card = existing || document.createElement("div");
    if (!existing) {
      card.className = "term-card";
      const head = document.createElement("div");
      head.className = "term-card-head";
      const prompt = document.createElement("span");
      prompt.className = "term-card-prompt";
      prompt.textContent = "$";
      const cmdEl = document.createElement("span");
      cmdEl.className = "term-card-cmd";
      const elapsedEl = document.createElement("span");
      elapsedEl.className = "term-card-elapsed";
      const exitEl = document.createElement("span");
      exitEl.className = "term-card-exit";
      head.append(prompt, cmdEl, elapsedEl, exitEl);
      const pre = document.createElement("pre");
      pre.className = "term-card-body";
      card.append(head, pre);
      // Live copy button: reads the CURRENT body text at click time (not the text
      // at creation time) — output keeps growing while the command is running.
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "action-card-copy term-card-copy";
      copyBtn.textContent = window.AkanaI18n.t("msg.panel_copy_btn");
      copyBtn.title = window.AkanaI18n.t("msg.panel_copy_title");
      copyBtn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(copyBtn.dataset.copyText || "");
          copyBtn.textContent = "✓";
          window.setTimeout(() => {
            copyBtn.textContent = window.AkanaI18n.t("msg.panel_copy_btn");
          }, 1400);
        } catch {
          copyBtn.textContent = "×";
        }
      });
      card.appendChild(copyBtn);
      card.dataset.startedAt = String(startedAt);
    }
    card.dataset.status = running ? "running" : "done";

    const cmdEl = card.querySelector(".term-card-cmd");
    if (cmdEl) {
      const full = String(cmd || "").trim();
      const short = truncateLine(full, 140);
      cmdEl.textContent = short;
      if (short !== full) {
        cmdEl.title = full;
        cmdEl.classList.add("is-truncated");
      }
    }

    const exitEl = card.querySelector(".term-card-exit");
    if (exitEl) {
      const chip = termExitChip(dc);
      if (chip) {
        exitEl.hidden = false;
        exitEl.dataset.ok = chip.ok ? "1" : "0";
        exitEl.textContent = window.AkanaI18n.t("msg.chip_exit", { code: chip.code });
      } else {
        exitEl.hidden = true;
        exitEl.textContent = "";
      }
    }

    const elapsedEl = card.querySelector(".term-card-elapsed");
    // Show elapsed only with a genuine start: a running card, or an existing card
    // with a persisted start. A fresh, already-done card (history/F5 restore) has no
    // real start → suppress the fabricated "0ms".
    if (elapsedEl) {
      elapsedEl.textContent = running || persistedStart ? termElapsedLabel(startedAt) : "";
    }

    const pre = card.querySelector(".term-card-body");
    if (pre) {
      const text = termOutputText(dc);
      pre.innerHTML = text ? ansiToHtml(truncateBlock(text, MAX_RESULT_TEXT)) : "";
      const copyBtn = card.querySelector(".action-card-copy");
      if (copyBtn) copyBtn.dataset.copyText = text || "";
    }

    // Live ticking counter (~250ms) while running; stopped on end/removal.
    if (running) {
      if (!card._termTicker) {
        card._termTicker = window.setInterval(() => {
          if (!card.isConnected || card.dataset.status !== "running") {
            window.clearInterval(card._termTicker);
            card._termTicker = null;
            return;
          }
          const el = card.querySelector(".term-card-elapsed");
          if (el) el.textContent = termElapsedLabel(Number(card.dataset.startedAt) || Date.now());
        }, 250);
      }
    } else if (card._termTicker) {
      window.clearInterval(card._termTicker);
      card._termTicker = null;
    }
    return card;
  }

  function formatToolResultBlocks(call) {
    const dc = displayCall(call);
    if (dc && dc.error) {
      return [{ type: "error", text: truncateLine(String(dc.error), MAX_RESULT_TEXT) }];
    }
    const status = String((dc && dc.status) || "").toLowerCase();
    if (/error|fail|denied|abort|timeout|reject/.test(status)) {
      const msg = dc?.result ?? dc?.output ?? dc?.status;
      if (msg) return [{ type: "error", text: truncateLine(String(msg), MAX_RESULT_TEXT) }];
    }

    const raw = dc && (dc.result ?? dc.output);
    if (raw == null || raw === "") {
      if (toolCallStatus(dc) === "running") return [{ type: "note", text: window.AkanaI18n.t("msg.result_pending") }];
      return [];
    }

    const norm = toolNameNorm(dc);
    const unwrapped = deepUnwrapPayload(raw);

    if (unwrapped && typeof unwrapped === "object" && unwrapped._error) {
      return [{ type: "error", text: String(unwrapped._error) }];
    }

    if (/^memory_search$/.test(norm) && unwrapped && typeof unwrapped === "object") {
      return formatMemoryHitBlocks(unwrapped);
    }
    if (/^(list_dir|ls)$/.test(norm)) {
      const dirBlocks = formatDirEntriesBlocks(unwrapped);
      if (dirBlocks) return dirBlocks;
    }

    if (typeof unwrapped === "string") {
      const text = unwrapped.replace(/\r\n/g, "\n");
      if (text.includes("\n") || text.length > 120) {
        return [{ type: "code", lang: "text", text: truncateBlock(text, MAX_RESULT_TEXT), label: window.AkanaI18n.t("msg.output_label") }];
      }
      return [{ type: "text", text }];
    }

    if (Array.isArray(unwrapped)) return formatSearchResultList(unwrapped);

    if (unwrapped && typeof unwrapped === "object") {
      const wsBlocks = formatWorkspaceResultsBlocks(unwrapped);
      if (wsBlocks) return wsBlocks;

      const shell = formatShellResult(unwrapped);
      if (shell) return shell;

      const nested =
        unwrapped.hits || unwrapped.results || unwrapped.items || unwrapped.matches || unwrapped.files;
      if (Array.isArray(nested) && nested.length) {
        if (nested[0] && (nested[0].summary || nested[0].type)) return formatMemoryHitBlocks({ items: nested });
        return formatSearchResultList(nested);
      }

      if (unwrapped.message || unwrapped.detail) {
        return [{ type: "text", text: String(unwrapped.message || unwrapped.detail) }];
      }

      const rawPretty = formatJsonPretty(unwrapped);
      if (rawPretty.length > 120) {
        return [{ type: "raw", text: rawPretty }];
      }
      if (CURATED_ARG_TOOLS.has(norm)) {
        return rawPretty ? [{ type: "text", text: rawPretty }] : [];
      }
      return formatObjectAsKv(unwrapped);
    }

    return [{ type: "text", text: truncateLine(String(raw), MAX_RESULT_TEXT) }];
  }

  /* ── Tool label + icon map ────────────────────────────────────────────────
     Names are normalized: "mcp__akana_memory__memory_search",
     "memory.search", "memory_search" all fall under the same rule. */
  const TOOL_LABEL_RULES = [
    { re: /^memory[._]?search$/, label: () => window.AkanaI18n.t("msg.tool_mem_search"), icon: "🧠" },
    { re: /^memory[._]?remember$/, label: () => window.AkanaI18n.t("msg.tool_mem_remember"), icon: "🧠" },
    { re: /^memory[._]?forget$/, label: () => window.AkanaI18n.t("msg.tool_mem_forget"), icon: "🧠" },
    { re: /^memory[._]?explain$/, label: () => window.AkanaI18n.t("msg.tool_mem_explain"), icon: "🧠" },
    { re: /^memory[._]?mark[._]?used$/, label: () => window.AkanaI18n.t("msg.tool_mem_mark"), icon: "🧠" },
    { re: /^(codebase_search|search|rg)$/, label: () => window.AkanaI18n.t("msg.tool_code_search"), icon: "🔎" },
    { re: /^grep$/, label: () => window.AkanaI18n.t("msg.tool_text_search"), icon: "🔎" },
    { re: /^(file_search|glob|glob_file_search|find)$/, label: () => window.AkanaI18n.t("msg.tool_file_search"), icon: "🔎" },
    { re: /^(read_file|read|cat)$/, label: () => window.AkanaI18n.t("msg.tool_file_read"), icon: "📄" },
    { re: /^(write|write_file|create_file)$/, label: () => window.AkanaI18n.t("msg.tool_file_write"), icon: "✏️" },
    { re: /^(edit_file|str_replace|apply_patch|edit|search_replace)$/, label: () => window.AkanaI18n.t("msg.tool_file_edit"), icon: "✏️" },
    { re: /^(delete_file|rm|delete)$/, label: () => window.AkanaI18n.t("msg.tool_file_delete"), icon: "🗑️" },
    { re: /^(list_dir|ls)$/, label: () => window.AkanaI18n.t("msg.tool_dir_list"), icon: "📁" },
    { re: /^(run_terminal_cmd|terminal|shell|bash|exec)$/, label: () => window.AkanaI18n.t("msg.tool_terminal"), icon: "💻" },
    { re: /^(web_search)$/, label: () => window.AkanaI18n.t("msg.tool_web_search"), icon: "🌐" },
    { re: /^(fetch|web_fetch|read_url|browse)$/, label: () => window.AkanaI18n.t("msg.tool_web_read"), icon: "🌐" },
    { re: TODO_TOOL_RE, label: () => window.AkanaI18n.t("msg.tool_todo"), icon: "📋" },
    { re: /^(task|mcp_call_tool|mcp_get_tools)$/, label: () => window.AkanaI18n.t("msg.tool_call"), icon: "⚙️" },
    { re: /^(switchmode|switch_mode)$/, label: () => window.AkanaI18n.t("msg.tool_mode_switch"), icon: "🔀" },
    { re: /^(generateimage|generate_image)$/, label: () => window.AkanaI18n.t("msg.tool_image_gen"), icon: "🖼️" },
    { re: /^(await)$/, label: () => window.AkanaI18n.t("msg.tool_await"), icon: "⏳" },
  ];

  /** Known-tool label + icon; falls back to raw name + 🔧 for unknown tools. */
  function toolCallLabelTr(call) {
    const fn = (call && call.function) || {};
    const raw =
      (call && call.name) || fn.name || (call && call.tool) || (call && call.toolName) || "";
    const norm = normalizedToolName(raw).toLowerCase();
    for (const rule of TOOL_LABEL_RULES) {
      if (rule.re.test(norm)) return { label: rule.label(), icon: rule.icon, raw };
    }
    if (norm.startsWith("memory")) return { label: window.AkanaI18n.t("msg.tool_mem_generic"), icon: "🧠", raw };
    return { label: raw, icon: "🔧", raw };
  }

  /** Short, human-readable base from a path/command (filename / first word). */
  function _basename(p) {
    const s = String(p || "").trim().replace(/[\\/]+$/, "");
    if (!s) return "";
    const parts = s.split(/[\\/]/);
    return parts[parts.length - 1] || s;
  }

  /* Human-readable ACTION sentence map — derived from tool name + arg.
     Card title is no longer a static label like "File read"; it's a single
     line describing WHAT WAS DONE like "📄 read hosts". Falls back to generic
     verb ("ran command") when no arg is available. Unknown tool → raw name. */
  const TOOL_ACTION_RULES = [
    { re: /^(read_file|read|cat)$/, icon: "📄",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_file_read", { a: _basename(a) }) : window.AkanaI18n.t("msg.action_file_read_gen")) },
    { re: /^(write|write_file|create_file)$/, icon: "✏️",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_file_write", { a: _basename(a) }) : window.AkanaI18n.t("msg.action_file_write_gen")) },
    { re: /^(edit_file|str_replace|apply_patch|edit|search_replace)$/, icon: "✏️",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_file_edit", { a: _basename(a) }) : window.AkanaI18n.t("msg.action_file_edit_gen")) },
    { re: /^(delete_file|rm|delete)$/, icon: "🗑️",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_file_delete", { a: _basename(a) }) : window.AkanaI18n.t("msg.action_file_delete_gen")) },
    { re: /^(run_terminal_cmd|terminal|shell|bash|exec)$/, icon: "⚙️",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_run_cmd", { a }) : window.AkanaI18n.t("msg.action_run_cmd_gen")) },
    { re: /^(web_search)$/, icon: "🔍",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_web_search", { a }) : window.AkanaI18n.t("msg.action_web_search_gen")) },
    { re: /^(fetch|web_fetch|read_url|browse)$/, icon: "🌐",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_web_read", { a }) : window.AkanaI18n.t("msg.action_web_read_gen")) },
    { re: /^(codebase_search|search|rg)$/, icon: "🔎",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_code_search", { a }) : window.AkanaI18n.t("msg.action_code_search_gen")) },
    { re: /^grep$/, icon: "🔎",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_text_search", { a }) : window.AkanaI18n.t("msg.action_text_search_gen")) },
    { re: /^(glob_file_search|glob)$/, icon: "🔎",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_file_search", { a }) : window.AkanaI18n.t("msg.action_file_search_gen")) },
    { re: /^(file_search|find)$/, icon: "🔎",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_file_search", { a }) : window.AkanaI18n.t("msg.action_file_search_gen")) },
    { re: /^(list_dir|ls)$/, icon: "📁",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_dir_list", { a: _basename(a) }) : window.AkanaI18n.t("msg.action_dir_list_gen")) },
    { re: /^memory[._]?search$/, icon: "🧠",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_mem_search", { a }) : window.AkanaI18n.t("msg.action_mem_search_gen")) },
    { re: /^memory[._]?remember$/, icon: "🧠", fn: () => window.AkanaI18n.t("msg.action_mem_remember") },
    { re: /^memory[._]?forget$/, icon: "🧠", fn: () => window.AkanaI18n.t("msg.action_mem_forget") },
    { re: /^memory_explain$/, icon: "🧠", fn: () => window.AkanaI18n.t("msg.action_mem_explain") },
    { re: TODO_TOOL_RE, icon: "📋", fn: () => window.AkanaI18n.t("msg.action_todo") },
    { re: /^(switchmode|switch_mode)$/, icon: "🔀", fn: () => window.AkanaI18n.t("msg.action_mode_switch") },
    { re: /^(generateimage|generate_image)$/, icon: "🖼️", fn: () => window.AkanaI18n.t("msg.action_image_gen") },
    // Akana native MCP capability tools (capabilities/*; mcp__…__ prefix is
    // stripped by normalizedToolName). Human-readable verbs — chip shortens them.
    { re: /^hatirlatma_kur$/, icon: "⏰",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_reminder_set", { a }) : window.AkanaI18n.t("msg.action_reminder_gen")) },
    { re: /^persona_degistir$/, icon: "🎭",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_persona_switch", { a }) : window.AkanaI18n.t("msg.action_persona_gen")) },
    { re: /^akis_calistir$/, icon: "⚡",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_flow_run", { a }) : window.AkanaI18n.t("msg.action_flow_gen")) },
    { re: /^profil_goster$/, icon: "👤", fn: () => window.AkanaI18n.t("msg.action_profile_show") },
    { re: /^guven_goster$/, icon: "🔐", fn: () => window.AkanaI18n.t("msg.action_trust_show") },
    { re: /^bilgi_ogret$/, icon: "🧠",
      fn: (a) => (a ? window.AkanaI18n.t("msg.action_knowledge_teach", { a }) : window.AkanaI18n.t("msg.action_knowledge_gen")) },
    { re: /^gecmis_goster$/, icon: "🕘", fn: () => window.AkanaI18n.t("msg.action_history_show") },
    { re: /^(task|mcp_call_tool|mcp_get_tools)$/, icon: "⚙️", fn: () => window.AkanaI18n.t("msg.action_tool_call_gen") },
    { re: /^(await)$/, icon: "⏳", fn: () => window.AkanaI18n.t("msg.action_awaited") },
  ];

  /** Subtitle is shown only when it differs meaningfully from the title. */
  function toolCallCollapsedSubtitle(action) {
    const sub = String((action && action.sub) || "").trim();
    if (!sub) return "";
    const text = String((action && action.text) || "").trim();
    if (sub === text) return "";
    if (text.includes(sub)) return "";
    return sub;
  }

  /** {icon, text} — human-readable action sentence shown in the card title.
   *  arg highlight (path/query/command) is embedded in the sentence if present. */
  function toolCallActionSentence(call) {
    const dc = displayCall(call);
    const rawName = inferEffectiveToolName(dc);
    if (isShellAsToolName(rawName) || toolNameNorm(dc) === "__shell__") {
      const cmd = toolCallArgHighlight(dc) || rawName;
      const sh = shellActionFromCommand(cmd);
      return { icon: sh.icon, text: sh.text, sub: sh.sub, raw: rawName };
    }
    const norm = toolNameNorm(dc);
    const arg = truncateLine(toolCallArgHighlight(dc), 64);
    for (const rule of TOOL_ACTION_RULES) {
      if (rule.re.test(norm)) {
        return { icon: rule.icon, text: rule.fn(arg), sub: toolCallCollapsedSubtitle({ text: rule.fn(arg), sub: arg }), raw: rawName };
      }
    }
    if (norm.startsWith("memory_")) {
      const text = arg ? window.AkanaI18n.t("msg.action_mem_used", { a: arg }) : window.AkanaI18n.t("msg.action_mem_used_gen");
      return { icon: "🧠", text, sub: toolCallCollapsedSubtitle({ text, sub: arg }), raw: rawName };
    }
    // Unknown tool — Cursor often leaves the name empty; command/path/query come from args.
    // Derive WHAT WAS DONE from args → readable sentence instead of raw "tool: …".
    const gArgs = parseToolArgs(dc) || {};
    const gCmd = pickArg(gArgs, ["command", "cmd"]);
    if (gCmd || isShellAsToolName(arg)) {
      const sh = shellActionFromCommand(gCmd || arg);
      return { icon: sh.icon, text: sh.text, sub: sh.sub, raw: rawName };
    }
    const gPath = pickArg(gArgs, ["file_path", "path", "target_file", "filePath", "target_notebook"]);
    if (gPath || isFilePathAsToolName(arg)) {
      const p = gPath || arg;
      const text = window.AkanaI18n.t("msg.action_file_read", { a: _basename(p) });
      return { icon: "📄", text, sub: toolCallCollapsedSubtitle({ text, sub: p }), raw: rawName };
    }
    const gUrl = pickArg(gArgs, ["url"]);
    if (gUrl) {
      const text = window.AkanaI18n.t("msg.action_read_url", { a: truncateLine(gUrl, 56) });
      return { icon: "🌐", text, sub: "", raw: rawName };
    }
    const gQuery = pickArg(gArgs, ["query", "q", "search_term", "pattern"]);
    if (gQuery) {
      const text = window.AkanaI18n.t("msg.action_searched", { a: gQuery });
      return { icon: "🔎", text, sub: toolCallCollapsedSubtitle({ text, sub: gQuery }), raw: rawName };
    }
    const name = normalizedToolName(rawName) || window.AkanaI18n.t("msg.action_unknown_tool");
    const text = arg ? `${name}: ${arg}` : name === window.AkanaI18n.t("msg.action_unknown_tool") ? window.AkanaI18n.t("msg.action_tool_call_gen") : name;
    return { icon: "🔧", text, sub: toolCallCollapsedSubtitle({ text, sub: arg }), raw: rawName };
  }

  /** Tool family → sets data-tool-family on the card; CSS colors the icon chip
   *  and left rail by type (aurora-chat.css). Unknown tool returns "" (generic accent). */
  function toolCallFamily(call) {
    const dc = displayCall(call);
    const rawName = inferEffectiveToolName(dc);
    const norm = toolNameNorm(dc);
    if (isShellAsToolName(rawName) || norm === "__shell__" ||
        /^(run_terminal_cmd|terminal|shell|bash|exec)$/.test(norm)) return "shell";
    if (/^(read_file|read|cat|write|write_file|create_file|list_dir|ls)$/.test(norm)) return "file";
    if (/^(edit_file|str_replace|apply_patch|edit|search_replace|delete_file|rm|delete)$/.test(norm)) return "edit";
    if (/^(grep|codebase_search|search|rg|glob_file_search|glob|file_search|find|web_search)$/.test(norm)) return "search";
    if (/^memory_/.test(norm) || norm.startsWith("memory")) return "mem";
    if (/^(fetch|web_fetch|read_url|browse)$/.test(norm)) return "web";
    if (TODO_TOOL_RE.test(norm)) return "todo";
    // Name not recognised (Cursor tools): derive family from args / arg-highlight.
    const a = parseToolArgs(dc) || {};
    if (pickArg(a, ["command", "cmd"])) return "shell";
    if (pickArg(a, ["url"])) return "web";
    if (pickArg(a, ["file_path", "path", "target_file", "filePath", "target_notebook"])) return "file";
    if (pickArg(a, ["query", "q", "search_term", "pattern"])) return "search";
    const hl = toolCallArgHighlight(dc);
    if (isShellAsToolName(hl)) return "shell";
    if (isFilePathAsToolName(hl)) return "file";
    return "";
  }

  function toolCallSubtitle(call) {
    const action = toolCallActionSentence(call);
    const collapsed = toolCallCollapsedSubtitle(action);
    if (collapsed) return collapsed;
    const raw = call && (call.result ?? call.output);
    if (raw == null || raw === "") return "";
    const unwrapped = deepUnwrapPayload(raw);
    if (unwrapped && typeof unwrapped === "object" && Array.isArray(unwrapped.items)) {
      return window.AkanaI18n.t("msg.records_found", { n: unwrapped.items.length });
    }
    if (typeof unwrapped === "string") return truncateLine(unwrapped.replace(/\s+/g, " "), 72);
    return "";
  }

  /** Collapses tool status to three values: running | done | error.
   *  CRITICAL: if a result (result/output) or end marker (phase==="end",
   *  status==="ok"/"done"/"finished") has arrived, the card must NEVER stay
   *  "running" — even if phase is still "start" (event ordering / race). */
  function toolCallStatus(call) {
    const status = String((call && call.status) || "").toLowerCase();
    const hasError =
      /error|fail|denied|abort|cancel|timeout|reject/.test(status) ||
      Boolean(call && call.error);
    if (hasError) return "error";
    const hasResult = call && call.result != null && call.result !== "";
    const hasOutput = call && call.output != null && call.output !== "";
    const ended =
      (call && call.phase === "end") ||
      /^(ok|done|finished|success|completed?)$/.test(status) ||
      hasResult ||
      hasOutput;
    if (ended) return "done";
    if (call && call.phase === "start") return "running";
    return "done";
  }

  /** Tool duration (ms) → compact label ("· 120 ms" / "· 1.4 s"); empty string if absent. */
  function toolCallDurationLabel(call) {
    const ms =
      (call && typeof call.duration_ms === "number" && call.duration_ms) ||
      (call && typeof call.latency_ms === "number" && call.latency_ms) ||
      (call && typeof call.elapsed_ms === "number" && call.elapsed_ms) ||
      null;
    if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
    if (ms < 1000) return window.AkanaI18n.t("msg.duration_ms", { n: Math.round(ms) });
    return window.AkanaI18n.t("msg.duration_s", { n: (ms / 1000).toFixed(1) });
  }

  /** Line-based LCS diff → {lines:[{type:'ctx'|'del'|'add',text}], added, removed}.
   *  Returns null for very large inputs (caller falls back to two separate blocks). */
  function computeLineDiff(oldText, newText) {
    const a = String(oldText == null ? "" : oldText).split("\n");
    const b = String(newText == null ? "" : newText).split("\n");
    const n = a.length;
    const m = b.length;
    if (n > 600 || m > 600 || n * m > 200000) return null; // safety ceiling
    const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const lines = [];
    let added = 0;
    let removed = 0;
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        lines.push({ type: "ctx", text: a[i] });
        i += 1;
        j += 1;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        lines.push({ type: "del", text: a[i] });
        i += 1;
        removed += 1;
      } else {
        lines.push({ type: "add", text: b[j] });
        j += 1;
        added += 1;
      }
    }
    while (i < n) {
      lines.push({ type: "del", text: a[i] });
      i += 1;
      removed += 1;
    }
    while (j < m) {
      lines.push({ type: "add", text: b[j] });
      j += 1;
      added += 1;
    }
    return { lines, added, removed };
  }

  const DIFF_MAX_LINES = 220;

  /* Tool-specific visual icons (checklist / folder / mini file). */
  const TODO_STATUS_ICON = {
    completed:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8 12.4l2.6 2.6L16 9"/></svg>',
    in_progress:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v4.7l3 1.8"/></svg>',
    pending:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke-dasharray="2.5 3"/></svg>',
  };
  const FOLDER_ICON_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7.5a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  const FILE_MINI_ICON_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 3.5v4.5a1 1 0 0 0 1 1h4.5"/><path d="M7.5 3.5h6L19 9v10.5a1 1 0 0 1-1 1H7.5a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z"/></svg>';

  function normalizeTodoStatus(s) {
    const v = String(s || "").toLowerCase();
    if (/^(completed|done|finished|complete)$/.test(v)) return "completed";
    if (/^(in_progress|in-progress|running|active|doing|started)$/.test(v)) return "in_progress";
    return "pending";
  }

  /* ── Lightweight syntax highlighting (bash / json / js) ───────────────────
     SECURITY: all token text is HTML-escaped first; only our own fixed-class
     <span> elements are added → tool output (observed content) is printed safely.
     Unknown language → null (plain text). Very large text → null (perf). */
  function escHtmlMin(s) {
    return String(s).replace(/[&<>]/g, (c) =>
      c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;",
    );
  }

  const HL_BASH_RE =
    /(#[^\n]*)|("(?:[^"\\]|\\.)*"|'[^']*')|(\$\{?[A-Za-z_]\w*\}?)|(?<=^)([A-Za-z][\w./-]*)|(\||&&|\|\||>>|>|<|;)/gm;
  const HL_BASH_CLS = { 1: "tok-com", 2: "tok-str", 3: "tok-var", 4: "tok-cmd", 5: "tok-op" };
  const HL_JSON_RE =
    /("(?:[^"\\]|\\.)*")(?=\s*:)|("(?:[^"\\]|\\.)*")|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  const HL_JSON_CLS = { 1: "tok-prop", 2: "tok-str", 3: "tok-kw", 4: "tok-num" };
  const HL_JS_RE =
    /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|\b(const|let|var|function|return|if|else|for|while|await|async|new|class|import|from|export|try|catch|throw|typeof|of|in|do|switch|case|break|continue|default|extends|super|this|yield|void|delete|instanceof)\b|\b(true|false|null|undefined|NaN)\b|\b(\d+(?:\.\d+)?)\b/g;
  const HL_JS_CLS = { 1: "tok-com", 2: "tok-str", 3: "tok-kw", 4: "tok-lit", 5: "tok-num" };

  function highlightWith(text, re, classes) {
    let out = "";
    let last = 0;
    let m;
    re.lastIndex = 0;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) out += escHtmlMin(text.slice(last, m.index));
      let cls = null;
      for (let g = 1; g < m.length; g += 1) {
        if (m[g] != null) {
          cls = classes[g];
          break;
        }
      }
      const tok = m[0];
      out += cls ? `<span class="${cls}">${escHtmlMin(tok)}</span>` : escHtmlMin(tok);
      last = m.index + tok.length;
      if (tok.length === 0) re.lastIndex += 1; // infinite-loop guard
    }
    if (last < text.length) out += escHtmlMin(text.slice(last));
    return out;
  }

  /** Returns highlighted (escaped) HTML, or null if the language is unsupported. */
  function highlightCode(text, lang) {
    const t = String(text == null ? "" : text);
    if (!t || t.length > 20000) return null;
    const l = String(lang || "").toLowerCase();
    if (/^(bash|sh|shell|terminal|zsh)$/.test(l)) return highlightWith(t, HL_BASH_RE, HL_BASH_CLS);
    if (/^json$/.test(l)) return highlightWith(t, HL_JSON_RE, HL_JSON_CLS);
    if (/^(js|javascript|ts|typescript|jsx|tsx|mjs|cjs)$/.test(l))
      return highlightWith(t, HL_JS_RE, HL_JS_CLS);
    return null;
  }

  /** Unified colored diff shell (−red / +green + "+N −M" stat). */
  function renderDiffBlock(wrap, block) {
    const diff = computeLineDiff(block.old, block.new);
    if (!diff) {
      // too large → fall back to the old two-block path (shell code blocks)
      if (block.old)
        appendActionCardBlock(wrap, {
          type: "code",
          lang: "diff",
          text: truncateLine(block.old, MAX_ARG_VAL),
          label: window.AkanaI18n.t("msg.diff_removed_label"),
        });
      if (block.new)
        appendActionCardBlock(wrap, {
          type: "code",
          lang: "diff",
          text: truncateLine(block.new, MAX_ARG_VAL),
          label: window.AkanaI18n.t("msg.diff_added_label"),
        });
      return;
    }
    const shell = document.createElement("div");
    shell.className = "ac-code-shell ac-diff-shell";
    shell.dataset.lang = "diff";
    const head = document.createElement("div");
    head.className = "ac-code-head";
    const dot = document.createElement("span");
    dot.className = "ac-code-dot";
    dot.setAttribute("aria-hidden", "true");
    const lang = document.createElement("span");
    lang.className = "ac-code-lang";
    lang.textContent = "diff";
    const stat = document.createElement("span");
    stat.className = "ac-diff-stat";
    stat.innerHTML =
      `<span class="ac-diff-stat-add">+${diff.added}</span>` +
      `<span class="ac-diff-stat-del">−${diff.removed}</span>`;
    head.append(dot, lang, stat);
    head.appendChild(makeCopyIconButton(block.new || ""));
    const body = document.createElement("div");
    body.className = "ac-diff-body";
    const shown = diff.lines.slice(0, DIFF_MAX_LINES);
    for (const ln of shown) {
      const row = document.createElement("div");
      row.className = `ac-diff-line ac-diff-line--${ln.type}`;
      const g = document.createElement("span");
      g.className = "ac-diff-gutter";
      g.setAttribute("aria-hidden", "true");
      g.textContent = ln.type === "add" ? "+" : ln.type === "del" ? "−" : " ";
      const t = document.createElement("span");
      t.className = "ac-diff-text";
      t.textContent = ln.text === "" ? " " : ln.text;
      row.append(g, t);
      body.appendChild(row);
    }
    if (diff.lines.length > DIFF_MAX_LINES) {
      const more = document.createElement("div");
      more.className = "ac-diff-more";
      more.textContent = window.AkanaI18n.t("msg.diff_more_lines", { n: diff.lines.length - DIFF_MAX_LINES });
      body.appendChild(more);
    }
    shell.append(head, body);
    wrap.appendChild(shell);
  }

  function appendActionCardBlock(panel, block) {
    if (!block || !block.type) return null;
    const wrap = document.createElement("div");
    wrap.className = `action-card-block action-card-block--${block.type}`;

    // code block label is shown in the shell header → avoid repeating it above.
    if (block.label && block.type !== "code") {
      const lbl = document.createElement("div");
      lbl.className = "action-card-block-label";
      lbl.textContent = block.label;
      wrap.appendChild(lbl);
    }

    switch (block.type) {
      case "summary":
      case "note":
      case "text":
        wrap.textContent = block.text || "";
        break;
      case "error":
        wrap.classList.add("is-error");
        wrap.textContent = block.text || "";
        break;
      case "kv": {
        const dl = document.createElement("dl");
        dl.className = "action-card-kv";
        for (const it of block.items || []) {
          const dt = document.createElement("dt");
          dt.textContent = it.k || "";
          const dd = document.createElement("dd");
          dd.textContent = it.v || "";
          dl.append(dt, dd);
        }
        wrap.appendChild(dl);
        break;
      }
      case "code": {
        // Premium code shell: header strip (lang/label + copy icon) + body.
        // A real code/terminal card instead of a plain log feel.
        const shell = document.createElement("div");
        shell.className = "ac-code-shell";
        if (block.lang) shell.dataset.lang = block.lang;
        const head = document.createElement("div");
        head.className = "ac-code-head";
        const dot = document.createElement("span");
        dot.className = "ac-code-dot";
        dot.setAttribute("aria-hidden", "true");
        const lang = document.createElement("span");
        lang.className = "ac-code-lang";
        lang.textContent =
          block.label ||
          (block.lang === "bash"
            ? "terminal"
            : block.lang === "diff"
              ? "diff"
              : block.lang || window.AkanaI18n.t("msg.code_lang_text"));
        head.append(dot, lang);
        if (block.text) head.appendChild(makeCopyIconButton(block.text));
        const pre = document.createElement("pre");
        pre.className = "action-card-code";
        if (block.lang) pre.dataset.lang = block.lang;
        const hl = highlightCode(block.text, block.lang);
        if (hl != null) pre.innerHTML = hl;
        else pre.textContent = block.text || "";
        shell.append(head, pre);
        wrap.appendChild(shell);
        break;
      }
      case "diff": {
        renderDiffBlock(wrap, block);
        break;
      }
      case "file": {
        const row = document.createElement("div");
        row.className = "ac-file";
        const ic = document.createElement("span");
        ic.className = "ac-file-ic";
        ic.innerHTML = toolIconSvg("file");
        const path = document.createElement("span");
        path.className = "ac-file-path";
        path.textContent = block.path || "";
        row.append(ic, path);
        if (block.meta) {
          const meta = document.createElement("span");
          meta.className = "ac-file-meta";
          meta.textContent = block.meta;
          row.appendChild(meta);
        }
        wrap.appendChild(row);
        break;
      }
      case "link": {
        const row = document.createElement("div");
        row.className = "ac-link";
        const ic = document.createElement("span");
        ic.className = "ac-link-ic";
        ic.innerHTML = toolIconSvg("web");
        const main = document.createElement("span");
        main.className = "ac-link-main";
        let domain = "";
        try {
          domain = new URL(block.url).hostname.replace(/^www\./, "");
        } catch {
          domain = "";
        }
        if (domain) {
          const d = document.createElement("span");
          d.className = "ac-link-domain";
          d.textContent = domain;
          main.appendChild(d);
        }
        const u = document.createElement("span");
        u.className = "ac-link-url";
        u.textContent = block.url || "";
        main.appendChild(u);
        row.append(ic, main);
        wrap.appendChild(row);
        break;
      }
      case "todos": {
        const ul = document.createElement("ul");
        ul.className = "ac-todos";
        let doneN = 0;
        for (const it of block.items || []) {
          const st = normalizeTodoStatus(it.status);
          if (st === "completed") doneN += 1;
          const li = document.createElement("li");
          li.className = `ac-todo ac-todo--${st}`;
          const box = document.createElement("span");
          box.className = "ac-todo-box";
          box.innerHTML = TODO_STATUS_ICON[st] || TODO_STATUS_ICON.pending;
          const txt = document.createElement("span");
          txt.className = "ac-todo-text";
          txt.textContent = it.text || "";
          li.append(box, txt);
          ul.appendChild(li);
        }
        wrap.appendChild(ul);
        break;
      }
      case "files": {
        const box = document.createElement("div");
        box.className = "ac-files";
        for (const it of block.items || []) {
          const chip = document.createElement("span");
          chip.className = `ac-file-chip${it.kind === "dir" ? " ac-file-chip--dir" : ""}`;
          const cic = document.createElement("span");
          cic.className = "ac-file-chip-ic";
          cic.innerHTML = it.kind === "dir" ? FOLDER_ICON_SVG : FILE_MINI_ICON_SVG;
          const nm = document.createElement("span");
          nm.className = "ac-file-chip-name";
          nm.textContent = it.name || "";
          chip.append(cic, nm);
          box.appendChild(chip);
        }
        const extra = (block.total || 0) - (block.items || []).length;
        if (extra > 0) {
          const more = document.createElement("span");
          more.className = "ac-file-chip ac-file-chip--more";
          more.textContent = window.AkanaI18n.t("msg.files_more", { n: extra });
          box.appendChild(more);
        }
        wrap.appendChild(box);
        break;
      }
      case "list": {
        const ul = document.createElement("ul");
        ul.className = "action-card-list";
        for (const item of block.items || []) {
          const li = document.createElement("li");
          li.textContent = item;
          ul.appendChild(li);
        }
        wrap.appendChild(ul);
        break;
      }
      case "pill": {
        const row = document.createElement("div");
        row.className = "action-card-pill-row";
        if (block.label) {
          const lab = document.createElement("span");
          lab.className = "action-card-pill-label";
          lab.textContent = block.label;
          row.appendChild(lab);
        }
        const pill = document.createElement("span");
        pill.className = "action-card-pill";
        pill.textContent = block.text || "";
        row.appendChild(pill);
        wrap.appendChild(row);
        break;
      }
      case "stat": {
        const dl = document.createElement("dl");
        dl.className = "action-card-stat";
        for (const it of block.items || []) {
          const dt = document.createElement("dt");
          dt.textContent = it.k || "";
          const dd = document.createElement("dd");
          dd.textContent = it.v || "";
          dl.append(dt, dd);
        }
        wrap.appendChild(dl);
        break;
      }
      case "hits": {
        const list = document.createElement("div");
        list.className = "action-card-hits";
        for (const hit of block.items || []) {
          const card = document.createElement("article");
          card.className = "action-card-hit";
          const head = document.createElement("div");
          head.className = "action-card-hit-head";
          const title = document.createElement("span");
          title.className = "action-card-hit-title";
          title.textContent = hit.title || window.AkanaI18n.t("msg.hit_record_default");
          head.appendChild(title);
          if (hit.badge) {
            const badge = document.createElement("span");
            badge.className = "action-card-hit-badge";
            badge.textContent = hit.badge;
            head.appendChild(badge);
          }
          card.appendChild(head);
          if (hit.body) {
            const body = document.createElement("p");
            body.className = "action-card-hit-body";
            body.textContent = hit.body;
            card.appendChild(body);
          }
          if (hit.meta || hit.id) {
            const foot = document.createElement("div");
            foot.className = "action-card-hit-foot";
            foot.textContent = [hit.meta, hit.id].filter(Boolean).join(" · ");
            card.appendChild(foot);
          }
          list.appendChild(card);
        }
        if (block.total > (block.items || []).length) {
          const more = document.createElement("div");
          more.className = "action-card-hit-more";
          more.textContent = window.AkanaI18n.t("msg.hits_more", { n: block.total - block.items.length });
          list.appendChild(more);
        }
        wrap.appendChild(list);
        break;
      }
      case "raw": {
        const det = document.createElement("details");
        det.className = "action-card-raw";
        const sum = document.createElement("summary");
        sum.textContent = window.AkanaI18n.t("msg.raw_dev_data");
        const pre = document.createElement("pre");
        pre.className = "action-card-pre";
        const rawHl = highlightCode(block.text, "json");
        if (rawHl != null) pre.innerHTML = rawHl;
        else pre.textContent = block.text || "";
        det.append(sum, pre);
        wrap.appendChild(det);
        break;
      }
      default:
        wrap.textContent = block.text || "";
    }
    panel.appendChild(wrap);
    return wrap;
  }

  function appendActionCardPanel(det, sec) {
    if (!sec) return null;
    const hasBlocks = Array.isArray(sec.blocks) && sec.blocks.length > 0;
    const hasBody = sec.body != null && sec.body !== "";
    if (!hasBlocks && !hasBody) return null;

    const panel = document.createElement("div");
    panel.className = "action-card-panel";
    if (sec.label) panel.dataset.panelLabel = String(sec.label);
    if (sec.kind) panel.dataset.panelKind = String(sec.kind);
    if (sec.label || sec.badge) {
      const lbl = document.createElement("div");
      lbl.className = "action-card-panel-label";
      // Section icon (Input → arrow-in, Output → arrow-out) — SVG.
      if (sec.kind === "input" || sec.kind === "output") {
        const ic = document.createElement("span");
        ic.className = "action-card-panel-ic";
        ic.setAttribute("aria-hidden", "true");
        ic.innerHTML =
          sec.kind === "input"
            ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v9"/><path d="M8 9.5l4 4 4-4"/><path d="M5 19h14"/></svg>'
            : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20v-9"/><path d="M8 14.5l4-4 4 4"/><path d="M5 5h14"/></svg>';
        lbl.appendChild(ic);
      }
      const txt = document.createElement("span");
      txt.className = "action-card-panel-name";
      txt.textContent = sec.label || "";
      lbl.appendChild(txt);
      if (sec.badge) {
        const badge = document.createElement("span");
        badge.className = "action-card-panel-badge";
        if (sec.badgeKind) badge.dataset.kind = String(sec.badgeKind);
        badge.textContent = sec.badge;
        lbl.appendChild(badge);
      }
      panel.appendChild(lbl);
    }

    if (hasBlocks) {
      for (const block of sec.blocks) appendActionCardBlock(panel, block);
    } else {
      const pre = document.createElement("pre");
      pre.className = "action-card-pre";
      pre.textContent = sec.body;
      panel.appendChild(pre);
      attachCopyButton(panel, sec.body);
    }
    det.appendChild(panel);
    return panel;
  }

  function materializeToolCallDetail(node, call) {
    const merged = call ? mergeToolCallForDisplay(call, node) : call;
    if (merged && node && node.dataset.lazyPanels === "1") {
      try {
        node.dataset.lazySections = JSON.stringify(toolCallSections(merged));
      } catch {
        /* ignore */
      }
    }
    materializeLazyActionCardPanels(node);
  }

  function materializeLazyActionCardPanels(det) {
    if (!det || det.dataset.lazyPanels !== "1") return;
    let raw = det.dataset.lazySections;
    if (!raw) return;
    delete det.dataset.lazyPanels;
    delete det.dataset.lazySections;
    try {
      const sections = JSON.parse(raw);
      if (!Array.isArray(sections)) return;
      for (const sec of sections) appendActionCardPanel(det, sec);
    } catch {
      /* ignore corrupt cache */
    }
  }

  /* ── SVG icon system ──────────────────────────────────────────────────────
     Emoji icons (🔧🧠⚡🔎📄) looked amateur → line SVG icon per tool FAMILY.
     All use a 24×24 viewBox with stroke=currentColor (color comes from CSS →
     family color). Unknown tool → generic "tool" icon. */
  const TOOL_ICON_PATHS = {
    shell:
      '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 9.5l3 2.5-3 2.5"/><line x1="12.5" y1="15" x2="16.5" y2="15"/>',
    file:
      '<path d="M13 3.5v4.5a1 1 0 0 0 1 1h4.5"/><path d="M7.5 3.5h6L19 9v10.5a1 1 0 0 1-1 1H7.5a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z"/>',
    edit:
      '<path d="M4 20h4.5L19 9.5 14.5 5 4 15.5z"/><line x1="13" y1="6.5" x2="17.5" y2="11"/>',
    search:
      '<circle cx="10.5" cy="10.5" r="6"/><line x1="20" y1="20" x2="15" y2="15"/>',
    mem:
      '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.66 3.13 3 7 3s7-1.34 7-3V6"/><path d="M5 12v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6"/>',
    web:
      '<circle cx="12" cy="12" r="8.5"/><line x1="3.5" y1="12" x2="20.5" y2="12"/><path d="M12 3.5a13 13 0 0 1 0 17M12 3.5a13 13 0 0 0 0 17"/>',
    todo:
      '<rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="M8 12l2.8 2.8L16.5 9"/>',
    web2:
      '<circle cx="12" cy="12" r="8.5"/><line x1="3.5" y1="12" x2="20.5" y2="12"/>',
    memory:
      '<path d="M12 3a3.5 3.5 0 0 0-3.45 2.9A3 3 0 0 0 6.5 11a3 3 0 0 0 1.2 5.6A3 3 0 0 0 12 19.5a3 3 0 0 0 4.3-2.9A3 3 0 0 0 17.5 11a3 3 0 0 0-2.05-5.1A3.5 3.5 0 0 0 12 3z"/><path d="M12 7v9"/>',
    skill:
      '<path d="M13 2.5L5.5 13H10l-1 8.5L18.5 11H13z"/>',
    agent:
      '<rect x="5" y="8" width="14" height="11" rx="2.5"/><path d="M12 8V4.5M9.5 4.5h5"/><circle cx="9.5" cy="13" r="1"/><circle cx="14.5" cy="13" r="1"/>',
    tool:
      '<path d="M15.8 7.4a3.6 3.6 0 0 1-4.6 4.6l-5 5a2 2 0 0 1-2.9-2.9l5-5a3.6 3.6 0 0 1 4.6-4.6l-2.4 2.4 1.9 1.9z"/>',
  };

  function toolIconSvg(name) {
    const paths = TOOL_ICON_PATHS[name] || TOOL_ICON_PATHS.tool;
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      paths +
      "</svg>"
    );
  }

  /** Status icon (SVG): running→spinning ring, done→checkmark, error→cross. */
  const STATUS_ICON_SVG = {
    running:
      '<svg class="acs-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M12 3a9 9 0 1 0 9 9" opacity="0.95"/></svg>',
    done:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.2 4.2L19 7.5"/></svg>',
    error:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><line x1="6.5" y1="6.5" x2="17.5" y2="17.5"/><line x1="17.5" y1="6.5" x2="6.5" y2="17.5"/></svg>',
  };

  function setStatusIcon(el, status) {
    if (!el) return;
    el.innerHTML = STATUS_ICON_SVG[status] || STATUS_ICON_SVG.done;
  }

  function renderActionCard(kind, title, subtitle, sections, opts = {}) {
    const lazyPanels = Boolean(opts.lazyPanels);
    const det = document.createElement("details");
    det.className = `action-card action-card--${kind}`;
    det.open = false;
    const sum = document.createElement("summary");
    sum.className = "action-card-summary";
    const icon = document.createElement("span");
    icon.className = "action-card-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = toolIconSvg(
      kind === "memory" ? "memory" : kind === "skill" ? "skill" : "tool",
    );
    const textWrap = document.createElement("span");
    textWrap.className = "action-card-text";
    const titleEl = document.createElement("span");
    titleEl.className = "action-card-title";
    titleEl.textContent = title;
    textWrap.appendChild(titleEl);
    if (subtitle) {
      const sub = document.createElement("span");
      sub.className = "action-card-subtitle";
      sub.textContent = subtitle;
      textWrap.appendChild(sub);
    }
    const chev = document.createElement("span");
    chev.className = "action-card-chevron";
    chev.setAttribute("aria-hidden", "true");
    sum.append(icon, textWrap, chev);
    det.appendChild(sum);
    if (lazyPanels) {
      det.dataset.lazyPanels = "1";
      try {
        det.dataset.lazySections = JSON.stringify(sections || []);
      } catch {
        det.dataset.lazySections = "[]";
      }
      det.addEventListener("toggle", () => {
        if (det.open) materializeLazyActionCardPanels(det);
      });
    } else {
      for (const sec of sections) appendActionCardPanel(det, sec);
    }
    return det;
  }

  function parseToolArgs(call) {
    return sanitizeToolArgsObject(parseToolArgsRaw(displayCall(call)));
  }

  /** Single-line meaningful summary from arguments (query/path/command priority). */
  function toolCallArgHighlight(call) {
    const rawArgs = parseToolArgs(displayCall(call));
    if (!rawArgs) return "";
    if (typeof rawArgs._raw === "string") return rawArgs._raw;
    const keys = [
      "query",
      "command",
      "description",
      "path",
      "file_path",
      "filePath",
      "target_file",
      "target_notebook",
      "url",
      "pattern",
      "glob_pattern",
      "q",
      "key",
      "text",
      "search_term",
      "server",
    ];
    for (const k of keys) {
      if (isInternalArgKey(k)) continue;
      const v = rawArgs[k];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return "";
  }

  function truncateLine(text, max = 96) {
    const s = String(text || "").replace(/\s+/g, " ").trim();
    if (!s) return "";
    return s.length > max ? `${s.slice(0, max - 1)}…` : s;
  }

  // Preserves newlines — for multi-line code blocks. Does NOT collapse whitespace.
  function truncateBlock(text, maxChars = MAX_RESULT_TEXT, maxLines = 200) {
    let s = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    // Strip trailing spaces/tabs per line, keep the \n
    s = s.split("\n").map((l) => l.replace(/[ \t]+$/, "")).join("\n");
    // Strip leading/trailing blank lines
    s = s.replace(/^\n+/, "").replace(/\n+$/, "");
    if (!s) return "";
    let truncated = false;
    const lines = s.split("\n");
    if (lines.length > maxLines) {
      s = lines.slice(0, maxLines).join("\n");
      truncated = true;
    }
    if (s.length > maxChars) {
      s = s.slice(0, maxChars);
      truncated = true;
    }
    if (truncated) s += "\n…";
    return s;
  }

  function attachCopyButton(panel, text) {
    if (!text || !panel) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "action-card-copy";
    btn.textContent = window.AkanaI18n.t("msg.panel_copy_btn");
    btn.title = window.AkanaI18n.t("msg.panel_copy_title");
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "✓";
        window.setTimeout(() => {
          btn.textContent = window.AkanaI18n.t("msg.panel_copy_btn");
        }, 1400);
      } catch {
        btn.textContent = "×";
      }
    });
    panel.appendChild(btn);
  }

  const COPY_ICON_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>';
  const COPY_OK_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.2 4.2L19 7.5"/></svg>';

  /** Icon copy button (for the code shell header). Copies text to the clipboard. */
  function makeCopyIconButton(text) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ac-code-copy";
    btn.title = window.AkanaI18n.t("msg.code_icon_copy_title");
    btn.setAttribute("aria-label", window.AkanaI18n.t("msg.code_icon_copy_aria"));
    btn.innerHTML = COPY_ICON_SVG;
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(text || "");
        btn.innerHTML = COPY_OK_SVG;
        btn.classList.add("is-ok");
        window.setTimeout(() => {
          btn.innerHTML = COPY_ICON_SVG;
          btn.classList.remove("is-ok");
        }, 1300);
      } catch {
        /* silent */
      }
    });
    return btn;
  }

  function toolCallSections(call) {
    const dc = displayCall(call);
    const inputBlocks = formatToolArgsBlocks(dc);
    const resultBlocks = formatToolResultBlocks(dc);
    // Input and Output are SEPARATE, LABELED sections → structured detail
    // instead of a plain log feel. Output header gets an at-a-glance result badge.
    const sections = [];
    if (inputBlocks.length) {
      sections.push({ label: window.AkanaI18n.t("msg.section_input"), kind: "input", blocks: inputBlocks });
    }
    if (resultBlocks.length) {
      const chip = toolCallResultChip(dc);
      sections.push({
        label: window.AkanaI18n.t("msg.section_output"),
        kind: "output",
        badge: chip ? chip.text : null,
        badgeKind: chip ? chip.kind || "" : null,
        blocks: resultBlocks,
      });
    }
    return sections;
  }

  // RADICALLY COMPACT DESIGN: single line — icon + HUMAN-READABLE ACTION sentence +
  // status + duration. Raw command/JSON HIDDEN BY DEFAULT (<details>); click the row →
  // Args/Result panels expand. The old static "File read" label +
  // inline arg chip + result preview line REMOVED — height ~24px.
  /** At-a-glance result chip (mockup .tool-result): {text, kind: ok|err|""}.
   *  done+result → "✓ ok/N results" (green), error → "✕ error", neutral → "→ text". */
  function toolCallResultChip(call) {
    const dc = displayCall(call);
    const status = toolCallStatus(dc);
    if (status === "running") return null;
    if (status === "error") return { text: window.AkanaI18n.t("msg.chip_error"), kind: "err" };
    const raw = dc && (dc.result ?? dc.output);
    if (raw == null || raw === "") return null;
    const u = deepUnwrapPayload(raw);
    if (Array.isArray(u)) return { text: window.AkanaI18n.t("msg.chip_n_results", { n: u.length }), kind: "ok" };
    if (u && typeof u === "object") {
      if (u._error) return { text: window.AkanaI18n.t("msg.chip_error"), kind: "err" };
      const list = u.items || u.hits || u.results || u.matches || u.files;
      if (Array.isArray(list)) return { text: window.AkanaI18n.t("msg.chip_n_results", { n: list.length }), kind: "ok" };
      if (typeof u.hits === "number") return { text: window.AkanaI18n.t("msg.chip_n_records", { n: u.hits }), kind: "ok" };
      const code = u.exit_code ?? u.exitCode;
      if (typeof code === "number") {
        return code === 0 ? { text: window.AkanaI18n.t("msg.chip_ok"), kind: "ok" } : { text: window.AkanaI18n.t("msg.chip_exit", { code }), kind: "err" };
      }
      if (/^(recorded|ok|success|done|finished|completed?)$/i.test(String(u.status || ""))) {
        return { text: window.AkanaI18n.t("msg.chip_ok"), kind: "ok" };
      }
      if (u.message || u.detail) return { text: truncateLine(String(u.message || u.detail), 22), kind: "" };
      return { text: window.AkanaI18n.t("msg.chip_ok"), kind: "ok" };
    }
    if (typeof u === "string") {
      const nonEmptyLines = u.split("\n").filter((l) => l.trim()).length;
      if (nonEmptyLines > 1) return { text: window.AkanaI18n.t("msg.chip_n_lines", { n: nonEmptyLines }), kind: "ok" };
      const s = u.replace(/\s+/g, " ").trim();
      return s ? { text: truncateLine(s, 28), kind: "" } : { text: window.AkanaI18n.t("msg.chip_ok"), kind: "ok" };
    }
    return { text: window.AkanaI18n.t("msg.chip_ok"), kind: "ok" };
  }

  /** Add/update the result chip in the summary (to the left of the status indicator). */
  function applyToolResultChip(sum, call) {
    if (!sum) return;
    const chip = toolCallResultChip(call);
    let el = sum.querySelector(".aur-tool-result");
    if (!chip) {
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement("span");
      el.className = "aur-tool-result";
      const st = sum.querySelector(".action-card-status");
      if (st) sum.insertBefore(el, st);
      else sum.appendChild(el);
    }
    el.dataset.kind = chip.kind || "neutral";
    el.textContent = chip.text;
  }

  /* ── Task list (TodoWrite) LIVE checklist card ──────────────────────────────
     Always renders as a clearly visible, always-open checklist rather than a
     raw JSON tool card. Consecutive TodoWrite calls update the SINGLE card
     in-place via family-dedup (data-todo-card="1") — they do not stack.
     The card carries the `.tool-call` class → counted in the process counter,
     but with a flat checklist body rather than a collapsible action-card. */
  function extractTodoItems(call) {
    const dc = displayCall(call);
    const args = parseToolArgs(dc) || {};
    const raw = Array.isArray(args.todos)
      ? args.todos
      : Array.isArray(args.items)
        ? args.items
        : null;
    if (!raw) return [];
    return raw
      .slice(0, 50)
      .map((t) => ({
        text:
          (t && (t.content || t.text || t.title || t.activeForm || t.task)) ||
          (typeof t === "string" ? t : ""),
        status: normalizeTodoStatus(t && t.status),
      }))
      .filter((x) => x.text);
  }

  /** Build the checklist body (ul.ac-todos) from items and refresh the counter text. */
  function fillTodoChecklist(card, items) {
    const ul = card.querySelector(".ac-todos");
    if (!ul) return;
    ul.textContent = "";
    let done = 0;
    for (const it of items) {
      const st = normalizeTodoStatus(it.status);
      if (st === "completed") done += 1;
      const li = document.createElement("li");
      li.className = `ac-todo ac-todo--${st}`;
      const box = document.createElement("span");
      box.className = "ac-todo-box";
      box.innerHTML = TODO_STATUS_ICON[st] || TODO_STATUS_ICON.pending;
      const txt = document.createElement("span");
      txt.className = "ac-todo-text";
      txt.textContent = it.text || "";
      li.append(box, txt);
      ul.appendChild(li);
    }
    const countEl = card.querySelector(".aur-todo-count");
    if (countEl) countEl.textContent = items.length ? `${done}/${items.length}` : "";
    card.dataset.todoEmpty = items.length ? "0" : "1";
  }

  function renderTodoCard(call) {
    const merged = mergeToolCallForDisplay(call, null);
    const card = document.createElement("div");
    card.className = "tool-call aur-todo-card";
    card.dataset.todoCard = "1";
    card.dataset.toolFamily = "todo";
    card.dataset.status = toolCallStatus(merged);
    const tid = merged && (merged.id || merged.call_id);
    if (tid) card.dataset.toolCallId = String(tid);
    mergeToolCallForDisplay(merged, card); // cache args on the card (for end phase)

    const head = document.createElement("div");
    head.className = "aur-todo-head";
    const ic = document.createElement("span");
    ic.className = "aur-todo-ic";
    ic.innerHTML = toolIconSvg("todo");
    const title = document.createElement("span");
    title.className = "aur-todo-title";
    title.textContent = window.AkanaI18n.t("msg.todo_card_title");
    const count = document.createElement("span");
    count.className = "aur-todo-count";
    head.append(ic, title, count);

    const ul = document.createElement("ul");
    ul.className = "ac-todos";

    card.append(head, ul);
    fillTodoChecklist(card, extractTodoItems(merged));
    return card;
  }

  function patchTodoCard(node, call) {
    const merged = mergeToolCallForDisplay(call, node); // args come back from cache
    node.dataset.status = toolCallStatus(merged);
    const tid = merged && (merged.id || merged.call_id);
    if (tid) node.dataset.toolCallId = String(tid); // adopt the latest id
    const items = extractTodoItems(merged);
    // Empty update (result/end phase only, no args) must not wipe the existing list.
    if (items.length || !node.querySelector(".ac-todo")) fillTodoChecklist(node, items);
    return node;
  }

  /** Does call belong to the task-list family? (live/replay dedup routing). */
  function isTodoCall(call) {
    return toolCallFamily(call) === "todo";
  }

  /** Add/update the task card in body using family-dedup: attaches to the SINGLE
   *  `[data-todo-card]` card at the top of body — not by ID → consecutive
   *  TodoWrite calls update the one live checklist in place. */
  function upsertTodoCard(bodyEl, call) {
    if (!bodyEl) return null;
    // Prefer a DIRECT-child card; the descendant fallback must SKIP cards nested in a
    // subagent group's body — otherwise a top-level TodoWrite (targeting the timeline
    // body) would hijack a subagent's own nested checklist, overwriting it inside the
    // auto-collapsed group and never rendering a top-level card.
    const existing =
      bodyEl.querySelector(':scope > [data-todo-card="1"]') ||
      Array.from(bodyEl.querySelectorAll('[data-todo-card="1"]')).find(
        (el) => !(el.closest && el.closest(".aur-subagent-body")),
      );
    if (existing) return patchTodoCard(existing, call || {});
    const fresh = renderTodoCard(call || {});
    bodyEl.appendChild(fresh);
    return fresh;
  }

  function renderToolCall(call) {
    const merged = mergeToolCallForDisplay(call, null);
    const status = toolCallStatus(merged);
    const action = toolCallActionSentence(merged);
    const subtitle = toolCallCollapsedSubtitle(action);
    const family = toolCallFamily(merged);
    const isShell = family === "shell";
    // Terminal cards (Feature 2) render their OWN live body (elapsed ticker, ANSI
    // output) instead of the generic lazy Input/Output panels — pass no sections
    // to renderActionCard, then append the term card eagerly below.
    const sections = isShell ? [] : toolCallSections(merged);
    const card = renderActionCard("tool", action.text, subtitle, sections, { lazyPanels: true });
    card.classList.add("tool-call", "tool-call--compact");
    card.dataset.status = status;
    if (family) card.dataset.toolFamily = family;
    if (merged && merged.phase) card.dataset.phase = String(merged.phase);
    const tid = merged && (merged.id || merged.call_id);
    if (tid) card.dataset.toolCallId = String(tid);
    // Capture creation time so a later generic→shell family flip (a Cursor/MCP tool
    // whose command only lands at `end`) can hand the mounted terminal body a real
    // start time for its elapsed label instead of ~0. Unused on cards that never flip.
    card.dataset.startedAt = String(Date.now());
    mergeToolCallForDisplay(merged, card);
    if (isShell) {
      delete card.dataset.lazyPanels;
      delete card.dataset.lazySections;
      card.appendChild(renderTermCard(merged, null));
    }

    const sum = card.querySelector(".action-card-summary");
    const iconEl = card.querySelector(".action-card-icon");
    // Family-colored SVG icon instead of emoji (falls back to generic "tool" icon).
    if (iconEl) iconEl.innerHTML = toolIconSvg(family || "tool");
    if (sum) {
      const textWrap = sum.querySelector(".action-card-text");

      // Duration badge (if present) — compact, to the left of the status indicator.
      const durLabel = toolCallDurationLabel(merged);
      if (durLabel && textWrap) {
        const dur = document.createElement("span");
        dur.className = "action-card-duration";
        dur.textContent = durLabel;
        textWrap.appendChild(dur);
      }

      const st = document.createElement("span");
      st.className = "action-card-status";
      st.dataset.status = status;
      st.setAttribute("role", "img");
      st.setAttribute(
        "aria-label",
        status === "running" ? window.AkanaI18n.t("msg.status_running") : status === "error" ? window.AkanaI18n.t("msg.status_error") : window.AkanaI18n.t("msg.status_done"),
      );
      st.title = status === "running" ? window.AkanaI18n.t("msg.status_running_t") : status === "error" ? window.AkanaI18n.t("msg.status_error_t") : window.AkanaI18n.t("msg.status_done_t");
      setStatusIcon(st, status);
      const chev = sum.querySelector(".action-card-chevron");
      if (chev) sum.insertBefore(st, chev);
      else sum.appendChild(st);
      applyToolResultChip(sum, merged);
    }

    return card;
  }

  function syncToolCallLazySections(node, call) {
    const merged = mergeToolCallForDisplay(call, node);
    const sections = toolCallSections(merged);
    if (node.dataset.lazyPanels === "1") {
      try {
        node.dataset.lazySections = JSON.stringify(sections);
      } catch {
        /* ignore */
      }
      return;
    }
    node.querySelectorAll(".action-card-panel").forEach((p) => p.remove());
    for (const sec of sections) appendActionCardPanel(node, sec);
  }

  function patchToolCallCard(node, call) {
    const merged = mergeToolCallForDisplay(call, node);
    const status = toolCallStatus(merged);
    node.dataset.status = status;
    if (merged && merged.phase) node.dataset.phase = String(merged.phase);
    const family = toolCallFamily(merged);
    if (family) node.dataset.toolFamily = family;
    const st = node.querySelector(".action-card-status");
    if (st) {
      const prev = st.dataset.status;
      st.dataset.status = status;
      st.title = status === "running" ? window.AkanaI18n.t("msg.status_running_t") : status === "error" ? window.AkanaI18n.t("msg.status_error_t") : window.AkanaI18n.t("msg.status_done_t");
      st.setAttribute(
        "aria-label",
        status === "running" ? window.AkanaI18n.t("msg.status_running") : status === "error" ? window.AkanaI18n.t("msg.status_error") : window.AkanaI18n.t("msg.status_done"),
      );
      if (prev !== status) setStatusIcon(st, status); // refresh icon only when status changes
    }
    const action = toolCallActionSentence(merged);
    const iconEl = node.querySelector(".action-card-icon");
    // Keep family SVG icon (don't fall back to emoji); family may have changed → update.
    if (iconEl) iconEl.innerHTML = toolIconSvg(family || "tool");
    const titleEl = node.querySelector(".action-card-title");
    if (titleEl && action.text) titleEl.textContent = action.text;
    const sub = toolCallCollapsedSubtitle(action);
    let subEl = node.querySelector(".action-card-subtitle");
    if (sub) {
      if (!subEl) {
        subEl = document.createElement("span");
        subEl.className = "action-card-subtitle";
        titleEl?.parentNode?.insertBefore(subEl, titleEl?.nextSibling || null);
      }
      subEl.textContent = sub;
    } else if (subEl) subEl.remove();
    const durLabel = toolCallDurationLabel(merged);
    if (durLabel) {
      const textWrap = node.querySelector(".action-card-text");
      if (textWrap) {
        let dur = textWrap.querySelector(".action-card-duration");
        if (!dur) {
          dur = document.createElement("span");
          dur.className = "action-card-duration";
          textWrap.appendChild(dur);
        }
        dur.textContent = durLabel;
      }
    }
    applyToolResultChip(node.querySelector(".action-card-summary"), merged);
    if (family === "shell") {
      // Terminal card owns its own body — refresh it in place (growing output,
      // elapsed/exit chip) instead of the generic Input/Output panel sync.
      let term = node.querySelector(".term-card");
      if (!term) {
        // The card was first rendered GENERIC — a Cursor/MCP tool whose `start`
        // event carried no recognizable name and no command arg, so its family was
        // unknown and it got lazy Input/Output panels. The `end` event now reveals a
        // shell command (args.command present → family "shell"). Upgrade the body in
        // place, mirroring renderToolCall's isShell branch: drop the stale Input/Output
        // panels (frozen at "Running…/Waiting for result…") and mount the terminal
        // body. Without this the panels never refresh — the card stays stuck on the
        // running placeholders even though the header already flipped to done.
        node.querySelectorAll(".action-card-panel").forEach((p) => p.remove());
        delete node.dataset.lazyPanels;
        delete node.dataset.lazySections;
        term = renderTermCard(merged, null);
        if (node.dataset.startedAt) term.dataset.startedAt = node.dataset.startedAt;
        node.appendChild(term);
      } else {
        renderTermCard(merged, term);
      }
    } else {
      syncToolCallLazySections(node, merged);
    }
    // B2 streaming tool input: once the REAL args (or an end result) land, the live
    // input preview is done — drop the streaming flag so the blink stops and the
    // subtitle just recomputed above stands as the final one.
    if (node.dataset.streaming && (merged.args != null || merged.phase === "end")) {
      delete node.dataset.streaming;
    }
    return node;
  }

  /* ── Tool-calls group: instead of N separate large cards, a single
     collapsible dropdown BELOW the message ("🔧 N tool calls ⌄"). Closed by default;
     click expands all cards. During streaming the title live-counts (running…/error).
     Card lifecycle (dedup/patch) is unchanged — only the position: group body. */
  function ensureToolGroupBody(container, turnId) {
    let group = container.querySelector(":scope > .tool-calls-group");
    if (!group) {
      group = document.createElement("div");
      group.className = "tool-calls-group";
      // Saved open/close preference (keyed by turn_id) → applied on F5 restore.
      // No record → default CLOSED.
      const pref = turnId ? getPanelCollapsed(turnId) : null;
      const startOpen = pref === false; // open only if an "open" record exists
      if (turnId) group.dataset.turnId = String(turnId);
      group.dataset.open = startOpen ? "1" : "0";
      const head = document.createElement("button");
      head.type = "button";
      head.className = "tool-calls-toggle";
      head.setAttribute("aria-expanded", startOpen ? "true" : "false");
      const ic = document.createElement("span");
      ic.className = "tcg-icon";
      ic.setAttribute("aria-hidden", "true");
      ic.textContent = "🔧";
      const lbl = document.createElement("span");
      lbl.className = "tcg-label";
      const chev = document.createElement("span");
      chev.className = "tcg-chevron";
      chev.setAttribute("aria-hidden", "true");
      head.append(ic, lbl, chev);
      const body = document.createElement("div");
      body.className = "tool-calls-body";
      body.hidden = !startOpen;
      head.addEventListener("click", () => {
        const open = head.getAttribute("aria-expanded") === "true";
        head.setAttribute("aria-expanded", open ? "false" : "true");
        body.hidden = open;
        group.dataset.open = open ? "0" : "1";
        // Save open/close preference by turn_id (persist across F5).
        const tid = group.dataset.turnId || container.closest?.(".row")?.dataset?.turnId;
        if (tid) setPanelCollapsed(tid, open); // open(old state)=true → now closed
      });
      group.append(head, body);
      container.appendChild(group); // AFTER the bubble → below the message
    }
    return group.querySelector(".tool-calls-body");
  }

  function refreshToolGroup(container) {
    const group = container.querySelector(":scope > .tool-calls-group");
    if (!group) return;
    const cards = group.querySelectorAll(".tool-calls-body > .tool-call");
    const n = cards.length;
    let running = 0;
    let errored = 0;
    cards.forEach((c) => {
      if (c.dataset.status === "running") running += 1;
      else if (c.dataset.status === "error") errored += 1;
    });
    const lbl = group.querySelector(".tcg-label");
    if (lbl) {
      lbl.textContent = errored ? window.AkanaI18n.t("msg.n_tools_errors", { n, e: errored }) : window.AkanaI18n.t("msg.n_tools", { n });
    }
    group.dataset.running = running ? "1" : "0";
    group.dataset.hasError = errored ? "1" : "0";
    group.hidden = n === 0;
  }

  function upsertToolCallCard(msgBody, call, _insertBeforeBubble) {
    if (isTodoCall(call)) {
      const node = upsertTodoCard(ensureToolGroupBody(msgBody), call);
      refreshToolGroup(msgBody);
      return node;
    }
    const id = call && (call.id || call.call_id);
    const key = id ? String(id) : null;
    let node = key ? msgBody.querySelector(`[data-tool-call-id="${CSS.escape(key)}"]`) : null;
    if (node) {
      patchToolCallCard(node, call || {});
      refreshToolGroup(msgBody);
      return node;
    }
    const fresh = renderToolCall(call || {});
    ensureToolGroupBody(msgBody).appendChild(fresh);
    refreshToolGroup(msgBody);
    return fresh;
  }

  /** Claude's `Task` tool call IS the subagent boundary itself (see claude_provider.py
   *  _TASK_TOOL). Matched on the raw tool name — same source of truth the backend uses
   *  to emit the `subagent` SSE event. */
  const TASK_TOOL_RE = /^task$/i;
  function isTaskCall(call) {
    return TASK_TOOL_RE.test(String(toolRawName(call) || "").trim());
  }

  /** Derive a `sub` payload (id/name/description/phase/status) from a `Task` tool
   *  call's own start/end fields — the SAME shape the live `subagent` SSE event
   *  carries. Used to synthesize/refresh the subagent group directly from the stored
   *  `tool_calls` list (history reload / finalized re-render have no `subagent`
   *  events, only the flat call list with `parent_id`). */
  function subagentInfoFromTaskCall(call) {
    const id = call && (call.id || call.call_id);
    if (!id) return null;
    const args = parseToolArgsRaw(call) || {};
    const name = String(args.subagent_type || args.description || "Task").slice(0, 80);
    const description = String(args.description || "").slice(0, 200);
    const phase = call && call.phase === "end" ? "end" : "start";
    const status = call && call.status === "error" ? "error" : call && call.status;
    return { id: String(id), name, description, phase, status };
  }

  /** Live elapsed-time label for a running subagent group ("· 1.4 s", ticking).
   *  Mirrors toolCallDurationLabel's thresholds/format for visual consistency. */
  function subagentElapsedLabel(startedAtMs) {
    if (!startedAtMs) return "";
    const ms = Math.max(0, Date.now() - startedAtMs);
    return ms < 1000
      ? window.AkanaI18n.t("msg.duration_ms", { n: Math.round(ms) })
      : window.AkanaI18n.t("msg.duration_s", { n: (ms / 1000).toFixed(1) });
  }

  /** Start (idempotent) the ~1s ticking interval that keeps a RUNNING group's
   *  elapsed-time chip live. Cleared automatically once the group leaves
   *  data-status="running" (see refreshSubagentSummary). */
  function ensureSubagentTicker(group) {
    if (!group || group._aurTicker) return;
    group._aurTicker = window.setInterval(() => {
      if (!group.isConnected || group.dataset.status !== "running") {
        window.clearInterval(group._aurTicker);
        group._aurTicker = null;
        return;
      }
      refreshSubagentSummary(group);
    }, 1000);
  }

  /** Refresh the group's elapsed-time chip + collapsed one-line summary
   *  ("name + N tools + duration"). Called on every status change AND by the
   *  ticker while running. */
  function refreshSubagentSummary(group) {
    if (!group) return;
    const startedAt = Number(group.dataset.startedAt) || 0;
    const elapsed = subagentElapsedLabel(startedAt);
    const elEl = group.querySelector(":scope > .aur-subagent-head > .aur-subagent-elapsed");
    if (elEl) elEl.textContent = elapsed;
    const n = group.querySelectorAll(":scope > .aur-subagent-body .tool-call").length;
    const summaryEl = group.querySelector(":scope > .aur-subagent-head > .aur-subagent-summary");
    if (summaryEl) {
      const nameEl = group.querySelector(":scope > .aur-subagent-head > .aur-subagent-title");
      const parts = [nameEl ? nameEl.textContent : "", window.AkanaI18n.t("msg.n_tools", { n })];
      if (elapsed) parts.push(elapsed);
      summaryEl.textContent = parts.filter(Boolean).join(" · ");
    }
  }

  /** Remove the "working…" placeholder from a subagent body once real content (a
   *  nested tool card) lands or the subagent ends. Idempotent. */
  function clearSubagentWorking(body) {
    if (!body) return;
    const w = body.querySelector(":scope > .aur-subagent-working");
    if (w) w.remove();
  }

  /** AGENT ACTIVITY (Batch 1): render a Claude subagent (Task) boundary as a labelled
   *  group in the timeline. The subagent's own nested tool steps carry
   *  `parent_id` = this Task id → they are placed INSIDE `.aur-subagent-body` by
   *  `subagentBodyFor`. Header: [agent-icon] [Subagent · name] [status] [elapsed].
   *  Collapses to a one-line summary when done (click to expand/collapse anytime). */
  function renderSubagentGroup(sub) {
    const group = document.createElement("div");
    group.className = "aur-subagent-group aur-timeline-tool";
    group.dataset.subagentId = String((sub && sub.id) || "");
    group.dataset.status = "running";
    group.dataset.startedAt = String(Date.now());
    group.dataset.open = "1";
    const head = document.createElement("div");
    head.className = "aur-subagent-head";
    head.setAttribute("role", "button");
    head.setAttribute("tabindex", "0");
    head.setAttribute("aria-expanded", "true");
    const ic = document.createElement("span");
    ic.className = "aur-subagent-ic";
    ic.setAttribute("aria-hidden", "true");
    ic.innerHTML = toolIconSvg("agent");
    const title = document.createElement("span");
    title.className = "aur-subagent-title";
    const name = (sub && sub.name && String(sub.name).trim()) || "";
    title.textContent = name
      ? window.AkanaI18n.t("msg.subagent_title", { name })
      : window.AkanaI18n.t("msg.subagent_fallback");
    const summary = document.createElement("span");
    summary.className = "aur-subagent-summary";
    const elapsed = document.createElement("span");
    elapsed.className = "aur-subagent-elapsed";
    const st = document.createElement("span");
    st.className = "aur-subagent-status";
    st.setAttribute("role", "img");
    setStatusIcon(st, "running");
    const chev = document.createElement("span");
    chev.className = "aur-subagent-chev";
    chev.setAttribute("aria-hidden", "true");
    head.append(ic, title, summary, elapsed, st, chev);
    const body = document.createElement("div");
    body.className = "aur-subagent-body";
    // Optional one-line description (skip when it merely repeats the name).
    const desc = sub && sub.description ? String(sub.description).trim() : "";
    if (desc && desc !== name) {
      const d = document.createElement("div");
      d.className = "aur-subagent-desc";
      d.textContent = truncateLine(desc, 140);
      body.appendChild(d);
    }
    // Placeholder while running so the body is not empty before nested cards land
    // (Cursor delivers a subagent's inner steps in a burst on completion, not live);
    // removed by clearSubagentWorking on the first child or on end.
    const working = document.createElement("div");
    working.className = "aur-subagent-working";
    working.setAttribute("aria-hidden", "true");
    working.textContent = window.AkanaI18n.t("msg.subagent_working");
    body.appendChild(working);
    group.append(head, body);
    const toggle = () => {
      const open = group.dataset.open !== "0";
      group.dataset.open = open ? "0" : "1";
      head.setAttribute("aria-expanded", open ? "false" : "true");
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
    ensureSubagentTicker(group);
    return group;
  }

  /** Create-or-update the subagent group for `sub` (matched by data-subagent-id).
   *  `phase:"start"` creates it (running); `phase:"end"` flips it to done/error.
   *  Also MERGES a placeholder group (created early by `subagentBodyFor` when a
   *  child card arrived before this group's own start event) — same DOM node, its
   *  name/description text is filled in here and the placeholder flag cleared, so
   *  children already nested inside are never re-parented or lost. */
  function upsertSubagentGroup(timelineBody, sub) {
    if (!timelineBody || !sub || !sub.id) return null;
    const key = String(sub.id);
    let group;
    try {
      group = timelineBody.querySelector(
        `.aur-subagent-group[data-subagent-id="${CSS.escape(key)}"]`,
      );
    } catch {
      return null;
    }
    const created = !group;
    if (!group) {
      group = renderSubagentGroup(sub);
      timelineBody.appendChild(group);
    } else if (group.dataset.placeholder === "1") {
      // Placeholder merge: fill in the real title/description now that the
      // start/Task-call data has landed; keep the existing body (nested children).
      delete group.dataset.placeholder;
      const name = (sub.name && String(sub.name).trim()) || "";
      const title = group.querySelector(":scope > .aur-subagent-head > .aur-subagent-title");
      if (title) {
        title.textContent = name
          ? window.AkanaI18n.t("msg.subagent_title", { name })
          : window.AkanaI18n.t("msg.subagent_fallback");
      }
      const desc = sub.description ? String(sub.description).trim() : "";
      const body = group.querySelector(":scope > .aur-subagent-body");
      if (desc && desc !== name && body && !body.querySelector(":scope > .aur-subagent-desc")) {
        const d = document.createElement("div");
        d.className = "aur-subagent-desc";
        d.textContent = truncateLine(desc, 140);
        body.insertBefore(d, body.firstChild);
      }
    }
    const phase = sub.phase || "start";
    // A group synthesized already-ended (history/F5 reload builds it from a persisted
    // Task call whose phase is "end") never ran live, so its render-time startedAt
    // would fabricate a "· 0 ms" in the summary. Drop it → no elapsed segment.
    if (created && phase === "end") group.dataset.startedAt = "0";
    const wasRunning = group.dataset.status === "running";
    const status =
      phase === "end" ? (sub.status === "error" ? "error" : "done") : "running";
    group.dataset.status = status;
    group.dataset.phase = phase;
    // Two-hop (direct child) lookup → nesting-safe (a subagent-in-subagent won't
    // let the outer group grab the inner group's status icon).
    const head = group.querySelector(":scope > .aur-subagent-head");
    const st = head ? head.querySelector(":scope > .aur-subagent-status") : null;
    if (st) setStatusIcon(st, status);
    // Ended: freeze the elapsed time and auto-collapse to the one-line summary
    // (still expandable on click — see renderSubagentGroup's toggle).
    if (status !== "running" && wasRunning) {
      group.dataset.open = "0";
      if (head) head.setAttribute("aria-expanded", "false");
      clearSubagentWorking(group.querySelector(":scope > .aur-subagent-body"));
      if (group._aurTicker) {
        window.clearInterval(group._aurTicker);
        group._aurTicker = null;
      }
    }
    refreshSubagentSummary(group);
    return group;
  }

  /** If `call` was produced by a subagent (`parent_id`), return its
   *  `.aur-subagent-body` so the card nests inside — creating a PLACEHOLDER group
   *  first if the `subagent`/`Task` start event hasn't landed yet (live streaming
   *  can deliver a child's `tool_call` before the parent Task's own start event is
   *  flushed, since tool_call is queued/batched per-rAF while `subagent` applies
   *  immediately — but the reverse race is also possible under reordering). The
   *  placeholder is later merged in place by `upsertSubagentGroup`/
   *  `subagentInfoFromTaskCall` (same data-subagent-id → same DOM node, only its
   *  header text/status gets filled in — no new node, no lost children). Returns
   *  null only if `call` has no parent_id (top-level card). */
  function subagentBodyFor(timelineBody, call) {
    const pid = call && call.parent_id;
    if (!pid || !timelineBody) return null;
    let group;
    try {
      group = timelineBody.querySelector(
        `.aur-subagent-group[data-subagent-id="${CSS.escape(String(pid))}"]`,
      );
    } catch {
      return null;
    }
    if (!group) {
      group = renderSubagentGroup({ id: pid });
      group.dataset.placeholder = "1";
      timelineBody.appendChild(group);
    }
    return group.querySelector(":scope > .aur-subagent-body");
  }

  /** AURORA unified timeline: add/update a tool card CHRONOLOGICALLY into the
   *  thought-feed body (timeline) — instead of a separate `.tool-calls-group`.
   *  Dedup/patch logic is the SAME as upsertToolCallCard (find by id →
   *  patchToolCallCard, else append renderToolCall). Card contract
   *  (.tool-call / .action-card / data-tool-call-id / aur-tool-result)
   *  is preserved; only POSITION changes. Live turns use this via transport.js;
   *  history/replay still uses the group path. */
  function upsertToolCardIntoTimeline(timelineBody, call) {
    if (!timelineBody) return null;
    // The `Task` tool call IS the subagent boundary — it must not ALSO render as a
    // separate flat `.tool-call` card (redundant with its own group header). This
    // is also what makes history reload/finalized re-render group correctly: those
    // paths only have the flat `tool_calls` list (no live `subagent` SSE events), so
    // the group has to be synthesized here from the Task call's own start/end data.
    if (isTaskCall(call)) {
      return upsertSubagentGroup(timelineBody, subagentInfoFromTaskCall(call));
    }
    // Subagent nesting: a card with parent_id joins its group's body when present.
    const target = subagentBodyFor(timelineBody, call) || timelineBody;
    if (target !== timelineBody && target.classList && target.classList.contains("aur-subagent-body")) {
      clearSubagentWorking(target);
    }
    if (isTodoCall(call)) {
      const node = upsertTodoCard(target, call);
      if (node) node.classList.add("aur-timeline-tool");
      return node;
    }
    const id = call && (call.id || call.call_id);
    const key = id ? String(id) : null;
    // Patch wherever the card already lives (nested or top-level) — search the whole
    // timeline so an `end` event updates the same node the `start` created.
    const node = key
      ? timelineBody.querySelector(`[data-tool-call-id="${CSS.escape(key)}"]`)
      : null;
    if (node) {
      patchToolCallCard(node, call || {});
      return node;
    }
    const fresh = renderToolCall(call || {});
    fresh.classList.add("aur-timeline-tool");
    target.appendChild(fresh);
    return fresh;
  }

  /** B2 streaming tool input: a readable one-line tail of the partial input JSON
   *  (whitespace collapsed, tail kept so the freshest keystrokes show). */
  function streamInputPreview(text, max = 88) {
    let s = String(text || "").replace(/\s+/g, " ").trim();
    if (s.length > max) s = "…" + s.slice(-(max - 1));
    return s;
  }

  /** B2 streaming tool input: create-or-update the tool card for a `tool_call_delta`
   *  (keyed by tool id) and stream the partial input into its subtitle so the input
   *  is visible BUILDING while the card is still collapsed. The subsequent real
   *  `tool_call` (start) patches the SAME card (by id) via `patchToolCallCard`,
   *  which recomputes the subtitle and clears the streaming flag — a clean
   *  live-preview → final-args transition, no duplicate card. */
  function upsertToolInputStream(timelineBody, d) {
    if (!timelineBody || !d || !d.id) return null;
    const key = String(d.id);
    let node;
    try {
      node = timelineBody.querySelector(`[data-tool-call-id="${CSS.escape(key)}"]`);
    } catch {
      return null;
    }
    if (!node) {
      node = renderToolCall({ id: d.id, name: d.name, phase: "start" });
      node.classList.add("aur-timeline-tool");
      (subagentBodyFor(timelineBody, d) || timelineBody).appendChild(node);
    }
    node.dataset.streaming = "1";
    const preview = streamInputPreview(d.text);
    if (!preview) return node;
    let sub = node.querySelector(".action-card-subtitle");
    if (!sub) {
      const titleEl = node.querySelector(".action-card-title");
      if (titleEl) {
        sub = document.createElement("span");
        sub.className = "action-card-subtitle";
        titleEl.parentNode?.insertBefore(sub, titleEl.nextSibling || null);
      }
    }
    if (sub) sub.textContent = preview;
    return node;
  }

  /** History/blocking: group an array of tool calls (call after the bubble).
   *  If turnId is provided, the panel open/close preference is persisted by turn_id.
   *  NOTE: renderToolProcessCard is now the main path; this function remains only
   *  as a legacy fallback (upsertToolCallCard). */
  function appendToolCallsGrouped(container, calls, turnId) {
    if (!Array.isArray(calls) || !calls.length) return;
    const bodyEl = ensureToolGroupBody(container, turnId);
    for (const c of calls) {
      if (isTodoCall(c)) upsertTodoCard(bodyEl, c);
      else bodyEl.appendChild(renderToolCall(c || {}));
    }
    refreshToolGroup(container);
  }

  /** History/blocking tool block — IDENTICAL to the LIVE finalized process card:
   *  aurora "process card" (spark + "N tools" header + chevron) with timeline cards
   *  below. This keeps the post-F5 appearance CONSISTENT with the live turn
   *  (same header + same position). Caller inserts this ABOVE the bubble.
   *  Default: COLLAPSED; turnId makes the open/close preference persistent
   *  (same key as the live feed). */
  function renderToolProcessCard(calls, turnId) {
    if (!Array.isArray(calls) || !calls.length) return null;
    const feed = document.createElement("div");
    feed.className = "akana-thought-feed aur-process is-done";
    feed.dataset.finalized = "1";
    const head = document.createElement("div");
    head.className = "akana-thought-feed-head aur-process-head";
    head.dataset.toggleWired = "1";
    head.setAttribute("role", "button");
    head.setAttribute("tabindex", "0");
    head.style.cursor = "pointer";
    const spark = document.createElement("span");
    spark.className = "aur-process-spark";
    spark.setAttribute("aria-hidden", "true");
    const lbl = document.createElement("span");
    lbl.className = "aur-process-label";
    const sub = document.createElement("span");
    sub.className = "aur-process-sub"; // empty — pushes chevron to the right (same layout as live)
    const chev = document.createElement("span");
    chev.className = "aur-process-chev";
    chev.setAttribute("aria-hidden", "true");
    head.append(spark, lbl, sub, chev);
    const body = document.createElement("div");
    body.className = "akana-thought-feed-body aur-timeline";
    for (const c of calls) upsertToolCardIntoTimeline(body, c || {});
    // The "N tools" header comes from the UNIQUE card count that entered the
    // timeline — NOT the raw array length: upsertToolCardIntoTimeline deduplicates
    // by id, but calls can carry start+end events per tool → raw length is 2×
    // inflated ("4 tools" but only 2 cards). Same count as live finalize (DOM cards).
    const n = body.querySelectorAll(".tool-call").length;
    const errored = body.querySelectorAll('.tool-call[data-status="error"]').length;
    lbl.textContent = errored ? window.AkanaI18n.t("msg.n_tools_errors", { n, e: errored }) : window.AkanaI18n.t("msg.n_tools", { n });
    feed.append(head, body);
    // Default: COLLAPSED; if an "open" record exists, open it (persistent preference by turn_id).
    const pref = turnId ? getPanelCollapsed(turnId) : null;
    if (pref === false) feed.classList.remove("is-collapsed");
    else feed.classList.add("is-collapsed");
    const toggle = () => {
      feed.classList.toggle("is-collapsed");
      if (turnId) setPanelCollapsed(turnId, feed.classList.contains("is-collapsed"));
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
    return feed;
  }

  const MEMORY_KIND_TR = {
    recall: () => window.AkanaI18n.t("msg.mem_kind_recall"),
    context: () => window.AkanaI18n.t("msg.mem_kind_context"),
    staging: () => window.AkanaI18n.t("msg.mem_kind_staging"),
  };

  function renderMemoryUse(payload) {
    const items = (payload && payload.items) || [];
    if (!items.length) return null;
    const recall = items.some((it) => it && it.kind === "recall");
    const title =
      items.length === 1 && recall
        ? window.AkanaI18n.t("msg.mem_from_one")
        : window.AkanaI18n.t("msg.mem_from_n", { n: items.length });
    const firstShown =
      items.length === 1 ? items[0] : items.find((it) => it && it.kind !== "recall") || items[0];
    const subtitleText = String((firstShown && (firstShown.preview || firstShown.text)) || "")
      .slice(0, 88);
    const sections = items.map((it, idx) => {
      const label =
        it.label ||
        (it.kind === "recall" ? window.AkanaI18n.t("msg.mem_section_recall") : it.kind === "context" ? window.AkanaI18n.t("msg.mem_section_context", { n: idx + 1 }) : window.AkanaI18n.t("msg.mem_section_record"));
      const score =
        typeof it.score === "number" && Number.isFinite(it.score)
          ? ` · %${Math.round(Math.max(0, Math.min(1, it.score)) * 100)}`
          : "";
      return {
        label,
        body: it.text || it.preview || "",
        badge: `${(MEMORY_KIND_TR[it.kind] && MEMORY_KIND_TR[it.kind]()) || it.kind || window.AkanaI18n.t("msg.mem_badge_record")}${score}`,
        badgeKind: it.kind || "kayit",
      };
    });
    const card = renderActionCard("memory", title, subtitleText, sections);
    card.classList.add("memory-use");
    card.dataset.count = String(items.length);
    return card;
  }

  /** WI-2 skill injection — makes the `skill_used` list in the done payload
   *  visible via the existing action-card pattern (was silently swallowed before).
   *  entries: [{id, title, score, match_reason, status, ...}] */
  const SKILL_STATUS_TR = {
    injected: () => window.AkanaI18n.t("msg.skill_injected"),
    used: () => window.AkanaI18n.t("msg.skill_injected"),
  };

  function renderSkillUse(entries) {
    const items = Array.isArray(entries) ? entries.filter(Boolean) : [];
    if (!items.length) return null;
    const title =
      items.length === 1
        ? window.AkanaI18n.t("msg.skill_one", { title: items[0].title || items[0].id || "?" })
        : window.AkanaI18n.t("msg.skill_n", { n: items.length });
    const subtitle =
      items.length === 1
        ? (SKILL_STATUS_TR[String(items[0].status || "")] && SKILL_STATUS_TR[String(items[0].status || "")]()) || String(items[0].status || "")
        : items.map((s) => s.title || s.id || "?").join(", ").slice(0, 88);
    const sections = items.map((s) => {
      const score =
        typeof s.score === "number" && Number.isFinite(s.score)
          ? ` · %${Math.round(Math.max(0, Math.min(1, s.score)) * 100)}`
          : "";
      return {
        label: s.title || s.id || "skill",
        body: s.match_reason || s.description || s.id || "",
        badge: `${(SKILL_STATUS_TR[String(s.status || "")] && SKILL_STATUS_TR[String(s.status || "")]()) || s.status || "skill"}${score}`,
        badgeKind: "skill",
      };
    });
    const card = renderActionCard("skill", title, subtitle, sections);
    card.classList.add("skill-use");
    card.dataset.count = String(items.length);
    return card;
  }

  /* ═══════════════════════════════════════════════════════════════════════
     AURORA below-response surfaces (mockup .cites / .approval / .err-card).
     All use `aur-` prefixed new classes; does not touch existing card contracts.
     Data is derived ONLY from what the backend sends (memory_use + done
     memory_writes) — nothing is fabricated; returns null when no data
     (row stays hidden).
     ═══════════════════════════════════════════════════════════════════════ */

  /** Trust colour for a source chip → CSS data-trust (tokens.css --j-trust-*). */
  function _sourceTrustKind(kind) {
    const k = String(kind || "").toLowerCase();
    if (k === "recall" || k === "user" || k === "user_statement") return "user";
    if (k === "context" || k === "inferred" || k === "hybrid_recall") return "inferred";
    if (k === "tool" || k === "staging" || k === "episodic") return "tool";
    return "synth";
  }

  function _sourceChipLabel(item) {
    if (!item || typeof item !== "object") return "";
    const kind = String(item.kind || "").toLowerCase();
    if (kind === "recall") {
      const q = String(item.text || item.preview || item.label || "").trim();
      return q ? window.AkanaI18n.t("msg.mem_recall_chip", { q: truncateLine(q, 28) }) : window.AkanaI18n.t("msg.mem_recall_chip_gen");
    }
    if (kind === "staging") {
      // Keyless staging fallback follows the UI language (same key transport's
      // memory toast uses) — a bare Turkish "bilgi" leaked into English chips.
      const fallback = window.AkanaI18n.t("transport.toast.memory_key_fallback");
      const key = String(item.key || item.label || fallback).trim();
      return truncateLine(key, 30);
    }
    const txt = String(item.preview || item.text || item.label || "").trim();
    return txt ? truncateLine(txt, 30) : "";
  }

  /** SOURCES row (mockup .cites): colored source chips. items =
   *  memory_use entries and/or staging memory_writes. Null if empty. */
  function renderSourcesRow(items) {
    const list = (Array.isArray(items) ? items : []).filter(
      (it) => it && typeof it === "object",
    );
    if (!list.length) return null;
    const seen = new Set();
    const chips = [];
    for (const it of list) {
      const label = _sourceChipLabel(it);
      if (!label) continue;
      const dedup = `${it.kind || ""}::${label}`;
      if (seen.has(dedup)) continue;
      seen.add(dedup);
      chips.push({ label, trust: _sourceTrustKind(it.kind) });
      if (chips.length >= 6) break;
    }
    if (!chips.length) return null;
    const row = document.createElement("div");
    row.className = "aur-sources";
    const lbl = document.createElement("span");
    lbl.className = "aur-sources-label";
    lbl.textContent = window.AkanaI18n.t("msg.sources_label");
    row.appendChild(lbl);
    for (const c of chips) {
      const chip = document.createElement("span");
      chip.className = "aur-source-chip";
      chip.dataset.trust = c.trust;
      const dot = document.createElement("span");
      dot.className = "aur-source-dot";
      dot.setAttribute("aria-hidden", "true");
      const t = document.createElement("span");
      t.className = "aur-source-text";
      t.textContent = c.label;
      chip.append(dot, t);
      row.appendChild(chip);
    }
    return row;
  }

  /** ADD source chips to a turn's ".msg-body" (create-or-expand).
   *  For staging sources arriving after the turn: a ``.aur-sources`` row from
   *  recalls may already exist at ``done`` time → appends to THAT row instead
   *  of creating a new one (dedup by label + 6-chip cap).
   *  Does not create a row for empty/label-less input. */
  function appendMemorySources(msgBody, items) {
    if (!msgBody) return null;
    const list = (Array.isArray(items) ? items : []).filter(
      (it) => it && typeof it === "object",
    );
    if (!list.length) return null;
    let row = msgBody.querySelector(".aur-sources");
    const created = !row;
    if (!row) {
      row = document.createElement("div");
      row.className = "aur-sources";
      const lbl = document.createElement("span");
      lbl.className = "aur-sources-label";
      lbl.textContent = window.AkanaI18n.t("msg.sources_label");
      row.appendChild(lbl);
    }
    const existing = new Set(
      Array.from(row.querySelectorAll(".aur-source-text")).map((n) => n.textContent || ""),
    );
    let count = row.querySelectorAll(".aur-source-chip").length;
    for (const it of list) {
      if (count >= 6) break;
      const label = _sourceChipLabel(it);
      if (!label || existing.has(label)) continue;
      existing.add(label);
      const chip = document.createElement("span");
      chip.className = "aur-source-chip";
      chip.dataset.trust = _sourceTrustKind(it.kind);
      const dot = document.createElement("span");
      dot.className = "aur-source-dot";
      dot.setAttribute("aria-hidden", "true");
      const t = document.createElement("span");
      t.className = "aur-source-text";
      t.textContent = label;
      chip.append(dot, t);
      row.appendChild(chip);
      count += 1;
    }
    if (created) {
      if (row.querySelector(".aur-source-chip")) msgBody.appendChild(row);
      else return null; // no chips could be added — don't leave an empty row
    }
    return row;
  }

  /** Approval card (mockup .approval): inline permission prompt for risky/write ops.
   *  Backend sends it with `meta.approval_required=true` (no structured command —
   *  Plan→Approve→Execute text reflection). Card appearance follows the mockup;
   *  "Allow"/"Deny" are wired to callbacks (transport composer writes "approve"/"deny").
   *  Shows command in a code box if provided; otherwise shows intent description. */
  function renderApprovalCard(opts = {}) {
    const card = document.createElement("div");
    card.className = "aur-approval";
    card.dataset.state = "pending";

    const head = document.createElement("div");
    head.className = "aur-approval-head";
    const ic = document.createElement("span");
    ic.className = "aur-approval-ic";
    ic.setAttribute("aria-hidden", "true");
    ic.textContent = opts.icon || "⚡";
    const t = document.createElement("span");
    t.className = "aur-approval-t";
    t.textContent = opts.title || window.AkanaI18n.t("msg.approval_title");
    head.append(ic, t);
    const badgeText = opts.badge || window.AkanaI18n.t("msg.approval_badge");
    if (badgeText) {
      const badge = document.createElement("span");
      badge.className = "aur-approval-badge";
      badge.textContent = badgeText;
      head.appendChild(badge);
    }
    card.appendChild(head);

    const detail = String(opts.command || opts.detail || "").trim();
    if (detail) {
      const body = document.createElement("div");
      body.className = "aur-approval-body";
      if (opts.command) {
        const pre = document.createElement("pre");
        pre.className = "aur-code";
        pre.textContent = String(opts.command);
        body.appendChild(pre);
      } else {
        const p = document.createElement("p");
        p.className = "aur-approval-detail";
        p.textContent = detail;
        body.appendChild(p);
      }
      card.appendChild(body);
    }

    const foot = document.createElement("div");
    foot.className = "aur-approval-foot";
    const once = document.createElement("label");
    once.className = "aur-approval-once";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    const onceText = document.createElement("span");
    onceText.textContent = window.AkanaI18n.t("msg.approval_once");
    once.append(cb, onceText);
    const grow = document.createElement("span");
    grow.className = "aur-approval-grow";
    const deny = document.createElement("button");
    deny.type = "button";
    deny.className = "aur-btn-deny";
    deny.textContent = window.AkanaI18n.t("msg.approval_deny");
    const allow = document.createElement("button");
    allow.type = "button";
    allow.className = "aur-btn-allow";
    allow.innerHTML =
      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" d="M20 6L9 17l-5-5"/></svg>';
    const allowLabel = document.createElement("span");
    allowLabel.textContent = window.AkanaI18n.t("msg.approval_allow");
    allow.appendChild(allowLabel);
    foot.append(once, grow, deny, allow);
    card.appendChild(foot);

    const result = document.createElement("div");
    result.className = "aur-approval-result";
    const resultText = document.createElement("span");
    resultText.className = "aur-approval-result-text";
    result.appendChild(resultText);
    card.appendChild(result);

    function resolve(ok) {
      if (card.dataset.state !== "pending") return;
      card.dataset.state = ok ? "allowed" : "denied";
      resultText.textContent = ok
        ? window.AkanaI18n.t("msg.approval_allowed")
        : window.AkanaI18n.t("msg.approval_denied");
    }
    allow.addEventListener("click", () => {
      resolve(true);
      try {
        opts.onAllow?.({ once: cb.checked });
      } catch (e) {
        console.warn("approval onAllow", e);
      }
    });
    deny.addEventListener("click", () => {
      resolve(false);
      try {
        opts.onDeny?.({ once: cb.checked });
      } catch (e) {
        console.warn("approval onDeny", e);
      }
    });
    return card;
  }

  /** AskUserQuestion card — shown when Claude (headless) asks a structured question
   *  (single/multi-select/free text). Intercepts the backend tool_use, suppresses
   *  the CLI auto-deny; this card shows options and sends the answer as the NEXT
   *  user message (`--resume` lets Claude read the response).
   *  opts: { question: {id, questions:[{question, header, multiSelect, options:[{label, description}]}]},
   *          onSubmit: (answerText) => void }
   *  The returned card carries `data-ask-id` → prevents double-rendering the same
   *  question (live `ask_user` + `done.ask_user` double-emit guard). */
  function renderAskUserCard(opts = {}) {
    const payload = (opts && opts.question) || {};
    const questions = Array.isArray(payload.questions)
      ? payload.questions.filter((q) => q && Array.isArray(q.options))
      : [];
    if (!questions.length) return null;

    const card = document.createElement("div");
    card.className = "aur-ask";
    card.dataset.state = "pending";
    if (payload.id) card.dataset.askId = String(payload.id);

    const head = document.createElement("div");
    head.className = "aur-ask-head";
    const ic = document.createElement("span");
    ic.className = "aur-ask-ic";
    ic.setAttribute("aria-hidden", "true");
    ic.textContent = "❓";
    const t = document.createElement("span");
    t.className = "aur-ask-t";
    t.textContent = questions.length > 1 ? window.AkanaI18n.t("msg.ask_title_n", { n: questions.length }) : window.AkanaI18n.t("msg.ask_title_one");
    head.append(ic, t);
    const badge = document.createElement("span");
    badge.className = "aur-ask-badge";
    badge.textContent = window.AkanaI18n.t("msg.ask_badge");
    head.appendChild(badge);
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "aur-ask-body";

    // For each question: text + option buttons (+ free text). Selection
    // state is kept in the DOM (.is-on) and collected at submit time.
    const blocks = questions.map((q) => {
      const multi = q.multiSelect === true;
      const block = document.createElement("div");
      block.className = "aur-ask-q";
      block.dataset.multi = multi ? "1" : "0";

      const qtext = document.createElement("div");
      qtext.className = "aur-ask-q-text";
      qtext.textContent = String(q.question || q.header || "").trim();
      block.appendChild(qtext);

      const opts_ = document.createElement("div");
      opts_.className = "aur-ask-opts";
      const optBtns = (q.options || []).map((o) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "aur-ask-opt";
        btn.dataset.label = String((o && o.label) || "").trim();
        const lab = document.createElement("span");
        lab.className = "aur-ask-opt-l";
        lab.textContent = btn.dataset.label || window.AkanaI18n.t("msg.ask_opt_empty");
        btn.appendChild(lab);
        const desc = String((o && o.description) || "").trim();
        if (desc) {
          const d = document.createElement("small");
          d.className = "aur-ask-opt-d";
          d.textContent = desc;
          btn.appendChild(d);
        }
        btn.addEventListener("click", () => {
          if (card.dataset.state !== "pending") return;
          if (multi) {
            btn.classList.toggle("is-on");
          } else {
            optBtns.forEach((b) => b.classList.remove("is-on"));
            btn.classList.add("is-on");
          }
          refreshSubmit();
        });
        opts_.appendChild(btn);
        return btn;
      });
      block.appendChild(opts_);

      // Free text: Claude's implicit "write your own answer" behaviour. When filled,
      // it is appended to selected labels (multi-select) or becomes the sole answer (single).
      const free = document.createElement("input");
      free.type = "text";
      free.className = "aur-ask-free";
      free.placeholder = multi ? window.AkanaI18n.t("msg.ask_free_multi") : window.AkanaI18n.t("msg.ask_free_single");
      free.addEventListener("input", refreshSubmit);
      free.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          if (!submit.disabled) submit.click();
        }
      });
      block.appendChild(free);

      body.appendChild(block);
      return { q, multi, optBtns, free };
    });
    card.appendChild(body);

    const foot = document.createElement("div");
    foot.className = "aur-ask-foot";
    const hint = document.createElement("span");
    hint.className = "aur-ask-hint";
    hint.textContent = blocks.some((b) => b.multi)
      ? window.AkanaI18n.t("msg.ask_hint_multi")
      : window.AkanaI18n.t("msg.ask_hint_one");
    const grow = document.createElement("span");
    grow.className = "aur-ask-grow";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "aur-ask-submit";
    submit.textContent = window.AkanaI18n.t("msg.ask_submit");
    submit.disabled = true;
    foot.append(hint, grow, submit);
    card.appendChild(foot);

    const result = document.createElement("div");
    result.className = "aur-ask-result";
    const resultText = document.createElement("span");
    resultText.className = "aur-ask-result-text";
    result.appendChild(resultText);
    card.appendChild(result);

    function valuesFor(b) {
      const vals = b.optBtns.filter((x) => x.classList.contains("is-on")).map((x) => x.dataset.label);
      const extra = String(b.free.value || "").trim();
      if (extra) {
        // Multi: split by comma; single: single free answer.
        if (b.multi) extra.split(",").map((s) => s.trim()).filter(Boolean).forEach((s) => vals.push(s));
        else vals.push(extra);
      }
      // In single-select, free text takes priority if present (conflict: selected button + text).
      if (!b.multi && extra) return [extra];
      return vals;
    }

    function hasAnyAnswer() {
      return blocks.some((b) => valuesFor(b).length > 0);
    }

    function refreshSubmit() {
      submit.disabled = card.dataset.state !== "pending" || !hasAnyAnswer();
    }

    function buildAnswer() {
      const lines = [];
      blocks.forEach((b) => {
        const vals = valuesFor(b);
        if (!vals.length) return;
        const headTxt = String(b.q.header || b.q.question || "").trim();
        const joined = vals.join(", ");
        lines.push(headTxt && blocks.length > 1 ? `${headTxt}: ${joined}` : joined);
      });
      return lines.join("\n");
    }

    submit.addEventListener("click", () => {
      if (card.dataset.state !== "pending") return;
      const answer = buildAnswer();
      if (!answer.trim()) return;
      card.dataset.state = "answered";
      submit.disabled = true;
      // Lock interaction (no resubmit / change).
      blocks.forEach((b) => {
        b.optBtns.forEach((x) => (x.disabled = true));
        b.free.disabled = true;
      });
      resultText.textContent = window.AkanaI18n.t("msg.ask_answered", { answer: answer.replace(/\n/g, " · ") });
      try {
        opts.onSubmit?.(answer);
      } catch (e) {
        console.warn("askUser onSubmit", e);
      }
    });

    return card;
  }

  /** Plan card (claude plan-mode / ExitPlanMode): model presents a plan before writing.
   *  User clicks "Apply" (resume plan-mode OFF → executes the plan) or
   *  "Revise" (free text + resume plan-mode ON → replans).
   *  Same lifecycle as renderAskUserCard: pending → applied/revised, then locked.
   *  opts: { plan:{id, plan, plan_file}, onApprove:()=>void,
   *  onRevise:(text)=>void }. Returns null if plan.plan (markdown body) is absent. */
  function renderPlanCard(opts = {}) {
    const payload = (opts && opts.plan) || {};
    const planMd = String(payload.plan || "").trim();
    if (!planMd) return null;

    const card = document.createElement("div");
    card.className = "aur-plan";
    card.dataset.state = "pending";
    if (payload.id) card.dataset.planId = String(payload.id);

    const head = document.createElement("div");
    head.className = "aur-plan-head";
    const ic = document.createElement("span");
    ic.className = "aur-plan-ic";
    ic.setAttribute("aria-hidden", "true");
    ic.textContent = "📋";
    const t = document.createElement("span");
    t.className = "aur-plan-t";
    t.textContent = window.AkanaI18n.t("msg.plan_title");
    head.append(ic, t);
    const badge = document.createElement("span");
    badge.className = "aur-plan-badge";
    badge.textContent = window.AkanaI18n.t("msg.plan_badge");
    head.appendChild(badge);
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "aur-plan-body";
    // Plan body is markdown — render rich with AkanaMarkdown if available, else plain text.
    if (window.AkanaMarkdown && typeof window.AkanaMarkdown.setBubbleMarkdown === "function") {
      try {
        window.AkanaMarkdown.setBubbleMarkdown(body, planMd);
      } catch (e) {
        body.textContent = planMd;
      }
    } else {
      body.textContent = planMd;
    }
    card.appendChild(body);

    // Revise free-text input (hidden initially; shown when "Revise" is clicked).
    const reviseWrap = document.createElement("div");
    reviseWrap.className = "aur-plan-revise";
    reviseWrap.hidden = true;
    const reviseInput = document.createElement("input");
    reviseInput.type = "text";
    reviseInput.className = "aur-plan-revise-in";
    reviseInput.placeholder = window.AkanaI18n.t("msg.plan_revise_ph");
    reviseWrap.appendChild(reviseInput);

    const foot = document.createElement("div");
    foot.className = "aur-plan-foot";
    const hint = document.createElement("span");
    hint.className = "aur-plan-hint";
    hint.textContent = window.AkanaI18n.t("msg.plan_hint");
    const grow = document.createElement("span");
    grow.className = "aur-plan-grow";
    const reviseBtn = document.createElement("button");
    reviseBtn.type = "button";
    reviseBtn.className = "aur-plan-revise-btn";
    reviseBtn.textContent = window.AkanaI18n.t("msg.plan_revise_btn");
    const applyBtn = document.createElement("button");
    applyBtn.type = "button";
    applyBtn.className = "aur-plan-apply";
    applyBtn.textContent = window.AkanaI18n.t("msg.plan_apply_btn");
    foot.append(hint, grow, reviseBtn, applyBtn);

    card.appendChild(reviseWrap);
    card.appendChild(foot);

    const result = document.createElement("div");
    result.className = "aur-plan-result";
    const resultText = document.createElement("span");
    resultText.className = "aur-plan-result-text";
    result.appendChild(resultText);
    card.appendChild(result);

    function lock(stateName, msg) {
      card.dataset.state = stateName;
      applyBtn.disabled = true;
      reviseBtn.disabled = true;
      reviseInput.disabled = true;
      resultText.textContent = msg;
    }

    applyBtn.addEventListener("click", () => {
      if (card.dataset.state !== "pending") return;
      lock("applied", window.AkanaI18n.t("msg.plan_applying"));
      try {
        opts.onApprove?.();
      } catch (e) {
        console.warn("plan onApprove", e);
      }
    });

    // First "Revise" click opens/focuses the input; second click (when text is filled)
    // submits the revision. Enter also submits.
    function submitRevise() {
      if (card.dataset.state !== "pending") return;
      const txt = String(reviseInput.value || "").trim();
      if (!txt) {
        reviseInput.focus();
        return;
      }
      lock("revised", window.AkanaI18n.t("msg.plan_revised", { txt }));
      try {
        opts.onRevise?.(txt);
      } catch (e) {
        console.warn("plan onRevise", e);
      }
    }

    reviseBtn.addEventListener("click", () => {
      if (card.dataset.state !== "pending") return;
      if (reviseWrap.hidden) {
        reviseWrap.hidden = false;
        reviseInput.focus();
        return;
      }
      submitRevise();
    });
    reviseInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitRevise();
      }
    });

    return card;
  }

  /** Error card (mockup .err-card): red-toned card + action buttons for pack/permission errors.
   *  opts: {title, detail|html, retryLabel, onRetry, secondaryLabel, onSecondary}. */
  function renderErrorCard(opts = {}) {
    const card = document.createElement("div");
    card.className = "aur-err-card";
    const ic = document.createElement("span");
    ic.className = "aur-err-ico";
    ic.setAttribute("aria-hidden", "true");
    ic.textContent = "!";
    const main = document.createElement("div");
    main.className = "aur-err-main";
    const t = document.createElement("div");
    t.className = "aur-err-t";
    t.textContent = opts.title || window.AkanaI18n.t("msg.err_default_title");
    main.appendChild(t);
    const d = document.createElement("div");
    d.className = "aur-err-d";
    // Error text may come from server/exception → always use textContent
    // (innerHTML XSS sink removed; no caller ever passed html anyway).
    d.textContent = opts.detail || "";
    main.appendChild(d);
    const actions = document.createElement("div");
    actions.className = "aur-err-actions";
    let hasAction = false;
    if (opts.onRetry) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "aur-btn-retry";
      retry.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path stroke="currentColor" stroke-width="2.4" stroke-linecap="round" d="M21 12a9 9 0 11-3-6.7M21 4v4h-4"/></svg>';
      const retryLabel = document.createElement("span");
      retryLabel.textContent = opts.retryLabel || window.AkanaI18n.t("msg.err_retry");
      retry.appendChild(retryLabel);
      retry.addEventListener("click", () => {
        try {
          opts.onRetry();
        } catch (e) {
          console.warn("err retry", e);
        }
      });
      actions.appendChild(retry);
      hasAction = true;
    }
    if (opts.secondaryLabel && opts.onSecondary) {
      const sec = document.createElement("button");
      sec.type = "button";
      sec.className = "aur-btn-mini";
      sec.textContent = opts.secondaryLabel;
      sec.addEventListener("click", () => {
        try {
          opts.onSecondary();
        } catch (e) {
          console.warn("err secondary", e);
        }
      });
      actions.appendChild(sec);
      hasAction = true;
    }
    if (hasAction) main.appendChild(actions);
    card.append(ic, main);
    return card;
  }

  function messageTextFromServer(m) {
    if (!m || typeof m !== "object") return "";
    const raw = m.content ?? m.text ?? "";
    return typeof raw === "string" ? raw : String(raw);
  }

  /* ── Tool-call persistence (keep cards after F5) ───────────────────────────
     The server's /messages endpoint does NOT return tool calls (no tool_calls
     column in the turns table; records live in the audit ledger keyed by turn_id).
     However the live `done` event carries both the turn_id and the FULL tool_calls
     list, and the assistant turn is stored with `assistant_turn_id = turn_id` →
     at done we write the turn_id→calls mapping to localStorage and re-attach it by
     message id in mapServerMessagesToThread on F5 restore. This way the tools called
     by past turns are always inspectable. Limitation: not portable across devices and
     dropped when localStorage is cleared (accepted — complaint was F5-specific). */
  const TOOLCALLS_LS = "akana.toolCalls.v1";
  const TOOLCALLS_MAX = 500; // cap on turn count — prevents unbounded growth
  let _toolCallStore = null;

  function toolCallStore() {
    if (_toolCallStore) return _toolCallStore;
    try {
      const raw = localStorage.getItem(TOOLCALLS_LS);
      const o = raw ? JSON.parse(raw) : null;
      _toolCallStore = o && typeof o === "object" ? o : {};
    } catch {
      _toolCallStore = {};
    }
    return _toolCallStore;
  }

  function persistToolCallStore() {
    const store = toolCallStore();
    const keys = Object.keys(store);
    if (keys.length > TOOLCALLS_MAX) {
      // ULID keys preserve insertion order → evict the oldest turns (FIFO).
      for (const k of keys.slice(0, keys.length - TOOLCALLS_MAX)) delete store[k];
    }
    // If the quota is full, evict oldest turns one by one and retry. Otherwise a
    // single very large tool result can consume the entire cache, setItem silently
    // throws, and NOTHING is written (cards disappear after F5). Evict→retry ensures
    // at least the most recent turns are persisted.
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try {
        localStorage.setItem(TOOLCALLS_LS, JSON.stringify(store));
        return;
      } catch {
        const oldest = Object.keys(store)[0];
        if (!oldest) return; // evicted everything but still doesn't fit — give up
        delete store[oldest];
      }
    }
  }

  /** Called at the `done` event: persist the full tool_calls list for this turn. */
  function putToolCallsForTurn(turnId, calls) {
    if (!turnId || !Array.isArray(calls) || !calls.length) return;
    const store = toolCallStore();
    store[String(turnId)] = calls;
    persistToolCallStore();
  }

  function getToolCallsForTurn(turnId) {
    if (!turnId) return null;
    const calls = toolCallStore()[String(turnId)];
    return Array.isArray(calls) && calls.length ? calls : null;
  }

  /* ── Process/tool panel open/close state (persisted per turn_id) ───────────
     When the user collapses a turn's tool panel we store the state keyed by
     turn_id → after F5, the same turn (restore group OR resume timeline) is
     rendered with that state. null = no record (default behaviour applies). */
  const PANEL_LS = "akana.panelCollapsed.v1";
  const PANEL_MAX = 600;
  let _panelStore = null;

  function panelStore() {
    if (_panelStore) return _panelStore;
    try {
      const raw = localStorage.getItem(PANEL_LS);
      const o = raw ? JSON.parse(raw) : null;
      _panelStore = o && typeof o === "object" ? o : {};
    } catch {
      _panelStore = {};
    }
    return _panelStore;
  }

  function persistPanelStore() {
    const store = panelStore();
    const keys = Object.keys(store);
    if (keys.length > PANEL_MAX) {
      for (const k of keys.slice(0, keys.length - PANEL_MAX)) delete store[k];
    }
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try {
        localStorage.setItem(PANEL_LS, JSON.stringify(store));
        return;
      } catch {
        const oldest = Object.keys(store)[0];
        if (!oldest) return;
        delete store[oldest];
      }
    }
  }

  function setPanelCollapsed(turnId, collapsed) {
    if (!turnId) return;
    panelStore()[String(turnId)] = collapsed ? 1 : 0;
    persistPanelStore();
  }

  /** true=collapsed, false=open, null=no record (leave to default). */
  function getPanelCollapsed(turnId) {
    if (!turnId) return null;
    const v = panelStore()[String(turnId)];
    return v === undefined ? null : Boolean(v);
  }

  function mapServerMessagesToThread(messages) {
    const out = [];
    for (const m of messages || []) {
      const text = messageTextFromServer(m);
      const ts = typeof m.created_at === "string" ? m.created_at : "";
      if (m.role === "user")
        out.push({
          kind: "user",
          text,
          ts,
          fileIds: Array.isArray(m.file_ids) ? m.file_ids : [],
        });
      else if (m.role === "assistant") {
        const am = {
          kind: "assistant",
          text,
          turnId: m.id || "",
          latencyMs: null,
          ts,
        };
        // Tool cards: SERVER is now the source of truth — /messages returns the
        // full tool_calls list for each turn. This closes the bug where cards
        // disappeared when a concurrent second chat finished in the background
        // (client never received that turn's `done` event) or on F5 / device
        // change. If the server returns empty/missing, fall back to the old
        // localStorage path (for turns predating the tool_calls column).
        const serverTools =
          Array.isArray(m.tool_calls) && m.tool_calls.length ? m.tool_calls : null;
        const tools = serverTools || getToolCallsForTurn(m.id);
        if (tools) am.toolCalls = tools;
        // SSE contract 4: /messages appends an optional `usage`
        // {prompt, completion, cost_usd?} to each assistant turn → token/cost visible after F5.
        if (m.usage && typeof m.usage === "object") {
          am.usage = {
            prompt: Number(m.usage.prompt) || 0,
            completion: Number(m.usage.completion) || 0,
            cost_usd: typeof m.usage.cost_usd === "number" ? m.usage.cost_usd : undefined,
          };
        }
        // A QUESTION turn persisted server-side carries the structured AskUser
        // payload → re-render the interactive card (not just the summary text) after
        // a chat switch / reload. Pending (still awaiting an answer) is decided below
        // once the whole thread is mapped: only the LAST message being this ask turn
        // means it's unanswered (a following user message = it was answered).
        if (m.ask_user && typeof m.ask_user === "object" && Array.isArray(m.ask_user.questions)) {
          am.askUser = m.ask_user;
        }
        out.push(am);
      } else if (m.role === "error") {
        // A FAILED turn persisted server-side (LLM unavailable / empty response).
        // Render it as an error card with Retry, exactly like the live `error` SSE
        // frame → the card survives a page reload (F5). `userText` (the question to
        // retry) is the nearest preceding user message in this thread.
        let userText = "";
        for (let i = out.length - 1; i >= 0; i--) {
          if (out[i].kind === "user") {
            userText = out[i].text || "";
            break;
          }
          if (out[i].kind === "error") break;
        }
        out.push({ kind: "error", text, userText, ts });
      }
    }
    // Pending-ask detection: the interactive card is shown ONLY when the ask turn is
    // the final entry — any following entry (a user answer, a later assistant turn)
    // means the question was already answered, so it renders as summary text only.
    const last = out.length ? out[out.length - 1] : null;
    if (last && last.kind === "assistant" && last.askUser) last.askUserPending = true;
    return out;
  }

  /* ── Clock formatting (timestamp badges) ──────────────────────────────── */
  function formatClock(value) {
    let d = null;
    if (value instanceof Date) d = value;
    else if (typeof value === "string" && value) {
      const parsed = new Date(value);
      if (!Number.isNaN(parsed.getTime())) d = parsed;
    }
    if (!d) d = new Date();
    try {
      return d.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
    } catch {
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      return `${hh}:${mm}`;
    }
  }

  /* ── Log enhancer — render side-layer ─────────────────────────────────────
     Single MutationObserver: (1) stamps data-time on new .row elements (shown
     on hover via CSS attr()), (2) adds a header strip (language label + COPY)
     to completed code blocks. During streaming, bubble.innerHTML is rebuilt
     every frame; partial blocks (md-code--partial) are skipped, and the
     observer runs in a microtask before paint so there is no flicker. */

  function decorateCodeBlock(pre) {
    if (!pre || pre.closest(".md-code-shell")) return;
    const shell = document.createElement("div");
    shell.className = "md-code-shell";
    const head = document.createElement("div");
    head.className = "md-code-head";
    const lang = document.createElement("span");
    lang.className = "md-code-lang";
    lang.textContent = pre.dataset.lang || window.AkanaI18n.t("msg.code_lang_fallback");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "md-code-copy";
    btn.textContent = window.AkanaI18n.t("msg.code_copy_btn");
    btn.setAttribute("aria-label", window.AkanaI18n.t("msg.code_copy_aria"));
    const actions = document.createElement("div");
    actions.className = "md-code-actions";
    // If the block is previewable (html/svg), add a live-preview trigger button.
    const codeText = (pre.querySelector("code") || pre).textContent || "";
    if (window.AkanaArtifacts?.isPreviewable(pre.dataset.lang || "", codeText)) {
      const pv = document.createElement("button");
      pv.type = "button";
      pv.className = "md-code-preview";
      pv.title = window.AkanaI18n.t("msg.code_preview_title");
      pv.setAttribute("aria-label", window.AkanaI18n.t("msg.code_preview_aria"));
      pv.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
        '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" stroke="currentColor" stroke-width="2"/>' +
        '<circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2"/></svg><span>' +
        window.AkanaI18n.t("msg.code_preview_span") + '</span>';
      actions.append(pv);
    }
    actions.append(btn);
    head.append(lang, actions);
    pre.replaceWith(shell);
    shell.append(head, pre);
  }

  function decorateCodeBlocksIn(root) {
    if (!root || typeof root.querySelectorAll !== "function") return;
    if (root.matches?.("pre.md-code:not(.md-code--partial)")) decorateCodeBlock(root);
    for (const pre of root.querySelectorAll("pre.md-code:not(.md-code--partial)")) {
      decorateCodeBlock(pre);
    }
  }

  function fallbackCopy(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }

  function flashCopyFeedback(btn, ok) {
    if (!btn) return;
    if (btn._jcrTimer) window.clearTimeout(btn._jcrTimer);
    btn.classList.toggle("is-copied", ok);
    btn.classList.toggle("is-failed", !ok);
    btn.textContent = ok ? window.AkanaI18n.t("msg.copy_ok") : window.AkanaI18n.t("msg.copy_fail");
    btn._jcrTimer = window.setTimeout(() => {
      btn.classList.remove("is-copied", "is-failed");
      btn.textContent = window.AkanaI18n.t("msg.code_copy_btn");
      btn._jcrTimer = null;
    }, 1600);
  }

  async function copyCodeFromButton(btn) {
    const shell = btn.closest(".md-code-shell");
    const code = shell?.querySelector("pre.md-code code") || shell?.querySelector("pre.md-code");
    const text = code ? code.textContent || "" : "";
    if (!text) {
      flashCopyFeedback(btn, false);
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      flashCopyFeedback(btn, true);
    } catch {
      flashCopyFeedback(btn, fallbackCopy(text));
    }
  }

  function stampRowTime(row, iso) {
    if (!row || row.dataset.time) return;
    row.dataset.time = formatClock(iso);
  }

  /** Attaches a wave chip to the latest assistant bubble while TTS is playing
      (polls the ttsPlayer.playing field from the hooks contract — does not touch voice.js). */
  function startTtsWaveWatcher(hooks) {
    if (!hooks.log || !hooks.ttsPlayer) return;
    let wasPlaying = false;
    let chip = null;
    let bubble = null;
    const clear = () => {
      if (chip) chip.remove();
      if (bubble) bubble.classList.remove("is-speaking");
      chip = null;
      bubble = null;
    };
    window.setInterval(() => {
      const playing = Boolean(hooks.ttsPlayer && hooks.ttsPlayer.playing);
      if (playing === wasPlaying) return;
      wasPlaying = playing;
      if (!playing) {
        clear();
        return;
      }
      const rows = hooks.log.querySelectorAll(".row-assistant");
      const row = rows[rows.length - 1];
      const body = row?.querySelector(".msg-body");
      if (!body) return;
      clear();
      bubble = body.querySelector(".bubble-assistant, .bubble-bot");
      if (bubble) bubble.classList.add("is-speaking");
      chip = document.createElement("div");
      chip.className = "tts-wave";
      chip.setAttribute("role", "status");
      chip.innerHTML =
        '<span class="tts-wave-bars" aria-hidden="true"><i></i><i></i><i></i></span>' +
        '<span class="tts-wave-label">' + window.AkanaI18n.t("msg.tts_playing") + '</span>';
      body.appendChild(chip);
    }, 350);
  }

  function enhanceChatLog(hooks) {
    // audit B7: bind to the CONTAINER (#log), NOT hooks.log (= active-pane getter).
    // Old code bound the observer + click-delegate to the SINGLE pane at init time
    // → chat panes created later never received observer/click events → code-copy
    // and block decoration broke in the 2nd+ conversation. Container with subtree:true
    // sees all child panes; click-delegation also works (panes are descendants).
    const log = (typeof document !== "undefined" && document.getElementById("log")) || hooks.log;
    if (!log || log._jcrEnhanced) return;
    log._jcrEnhanced = true;

    decorateCodeBlocksIn(log);
    for (const row of log.querySelectorAll(".row")) stampRowTime(row);

    let decorateRaf = null;
    const pendingDecorate = new Set();
    function scheduleDecorate(node) {
      if (!node) return;
      pendingDecorate.add(node);
      if (decorateRaf != null) return;
      decorateRaf = requestAnimationFrame(() => {
        decorateRaf = null;
        for (const root of pendingDecorate) {
          // Skip decoration while the PANE holding this node is still streaming —
          // setBubbleMarkdown rebuilds that pane's innerHTML each frame, so a header
          // strip added now is destroyed and re-added → flicker. The chat-streaming
          // flag lives on the per-conversation pane (a child of #log), not on the
          // #log container; walk from the mutated node to find it.
          const streamingHost =
            (root.closest && root.closest('[data-chat-streaming="1"]')) ||
            (log.dataset.chatStreaming === "1" ? log : null);
          if (streamingHost) continue;
          decorateCodeBlocksIn(root);
        }
        pendingDecorate.clear();
      });
    }

    const obs = new MutationObserver((muts) => {
      for (const mut of muts) {
        for (const node of mut.addedNodes) {
          if (!(node instanceof Element)) continue;
          if (node.classList.contains("row")) stampRowTime(node);
          scheduleDecorate(node);
        }
      }
    });
    obs.observe(log, { childList: true, subtree: true });

    log.addEventListener("click", (e) => {
      const pv = e.target?.closest?.(".md-code-preview");
      if (pv && log.contains(pv)) {
        e.preventDefault();
        window.AkanaArtifacts?.openFromButton(pv);
        return;
      }
      const btn = e.target?.closest?.(".md-code-copy");
      if (!btn || !log.contains(btn)) return;
      e.preventDefault();
      void copyCodeFromButton(btn);
    });

    startTtsWaveWatcher(hooks);
  }

  /** Premium meta line duration segment: "N ms" below 1s, "X.Xs" at/above 1s.
   *  SYMMETRIC with the duration formatting in formatAssistantStreamMeta (transport.js). */
  function formatMetaDuration(ms) {
    if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
    return `${ms} ms`;
  }

  /** "provider/model" segment read from the top-bar pill (no model/provider field
   *  travels in the persisted turn or the SSE done payload — see formatAssistantStreamMeta
   *  for the same fallback). Pill text is "Claude · opus" → normalized "claude/opus".
   *  Returns "" when the pill is absent/empty (segment omitted). */
  function formatMetaModelSeg() {
    try {
      const pill = document.getElementById("model-pill");
      if (!pill) return "";
      // data-state warn/bad = unresolved/errored pill text ("select model",
      // "no connection") — skip the model segment; since the real label has the
      // form "Provider · tag", '·' is also required (review finding).
      // SYMMETRIC with formatStreamModelSeg in akana-chat-transport.js.
      if (pill.dataset.state) return "";
      const raw = (pill.querySelector(".status-text") || pill).textContent || "";
      const txt = raw.trim();
      if (!txt || txt === "…" || txt.indexOf("·") === -1) return "";
      const parts = txt.split("·").map((p) => p.trim()).filter(Boolean);
      if (parts.length < 2) return "";
      // Claude & Cursor providers: omit the provider/model segment entirely (user preference).
      const prov = parts[0].toLowerCase();
      if (prov === "claude" || prov === "cursor") return "";
      return parts.join("/").toLowerCase();
    } catch {
      return "";
    }
  }

  /** Safe i18n access — in contexts where the i18n module isn't loaded (node-vm
   *  contract harnesses) a bare window.AkanaI18n.t call throws; falls back to
   *  the fallback text. SYMMETRIC with metaT in akana-chat-transport.js. */
  function metaT(key, fallback, params) {
    const i18n = window.AkanaI18n;
    if (i18n && typeof i18n.t === "function") return i18n.t(key, params);
    let s = fallback || key;
    if (params) for (const k in params) s = s.split(`{${k}}`).join(String(params[k]));
    return s;
  }

  /** Build (but do not insert) the "tamam/hata" status chip span for the meta line.
   *  Caller appends it INSIDE .msg-label.meta so CSS can style it. */
  function buildMetaStatusChip(state) {
    const chip = document.createElement("span");
    chip.className = "turn-status-chip";
    chip.dataset.state = state;
    chip.textContent =
      state === "err"
        ? metaT("chat.turn_status_err", "error")
        : metaT("chat.turn_status_done", "done");
    return chip;
  }

  /** History assistant-row meta text: "Akana · {n} araç · {ms} ms · {tokens} tok · ${cost} · provider/model".
   *  SSE contract 4: read from the `usage` field provided by mapServerMessagesToThread.
   *  SYMMETRIC with formatAssistantStreamMeta in transport.js (not a copy —
   *  independent module, same format). `toolCount` is the accurate/deduplicated count
   *  (caller passes the rendered process card's unique `.tool-call` DOM count — see
   *  renderAssistantFromPersist — not the raw m.toolCalls.length, which can be 2×
   *  inflated by start+end events per tool); omitted when 0/falsy. */
  function formatHistoryMeta(latencyMs, usage, toolCount) {
    const segs = ["Akana"];
    // Tool count — same key/pattern as the process-card header ("N araç" / "N tools").
    if (typeof toolCount === "number" && toolCount > 0) {
      segs.push(metaT("msg.n_tools", "{n} tools", { n: toolCount }));
    }
    if (typeof latencyMs === "number") segs.push(formatMetaDuration(latencyMs));
    // Token/cost is shown only when the "Show token & cost" setting is on
    // (show-usage body class) — same gate as formatAssistantStreamMeta in transport.js.
    if (usage && typeof usage === "object" && document.body.classList.contains("show-usage")) {
      const total = (Number(usage.prompt) || 0) + (Number(usage.completion) || 0);
      if (total > 0) {
        const v = total;
        const tokStr = v < 1000 ? String(v)
          : v < 10000 ? (v / 1000).toFixed(1).replace(/\.0$/, "") + "k"
          : Math.round(v / 1000) + "k";
        segs.push(window.AkanaI18n.t("msg.history_tokens", { n: tokStr }));
      }
      if (typeof usage.cost_usd === "number" && usage.cost_usd > 0) {
        const c = usage.cost_usd;
        const costStr = c >= 1 ? `$${c.toFixed(2)}`
          : c >= 0.01 ? `$${c.toFixed(3)}`
          : `$${c.toFixed(4)}`;
        segs.push(costStr);
      }
    }
    const modelSeg = formatMetaModelSeg();
    if (modelSeg) segs.push(modelSeg);
    return segs.join(" · ");
  }

  /** Build renderers bound to chat-log DOM hooks. */
  function createRenderer(hooks) {
    enhanceChatLog(hooks);

    function renderAssistantFromPersist(m) {
      const wrap = document.createElement("div");
      wrap.className = "row row-assistant";
      // Tag with turn_id so that, during an F5+resume race, the live row can
      // find and remove this restored copy with the same id (prevents double render).
      if (m.turnId) wrap.dataset.turnId = String(m.turnId);
      if (m.ts) wrap.dataset.time = formatClock(m.ts);
      const avatar = document.createElement("div");
      avatar.className = "msg-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = "A";
      const msgBody = document.createElement("div");
      msgBody.className = "msg-body";
      const meta = document.createElement("div");
      meta.className = "msg-label meta";
      msgBody.appendChild(meta);
      if (m.droppedTurns && m.droppedTurns > 0) {
        const warn = document.createElement("div");
        warn.className = "history-warn";
        warn.textContent = window.AkanaI18n.t("msg.dropped_turns", { n: m.droppedTurns });
        msgBody.appendChild(warn);
      }
      // A PENDING question turn: re-render the interactive AskUser card (options/
      // submit), not just the summary text. The card's answer routes through
      // submitAnswerText → the next user message (--resume), same as the live turn.
      let askCard = null;
      if (m.askUser && m.askUserPending) {
        askCard = renderAskUserCard({
          question: m.askUser,
          onSubmit: (answer) => {
            try {
              window.AkanaChat?.submitAnswerText?.(answer);
            } catch (e) {
              console.warn("askUser onSubmit", e);
            }
          },
        });
      }
      const bubble = document.createElement("div");
      bubble.className = "bubble-assistant bubble-bot";
      // When the card is shown AND the turn body is exactly the question summary
      // (joined question lines — the live turn leaves the bubble empty and draws the
      // card), suppress the bubble text so the question is not printed twice. A
      // preamble/tool-only body that differs from the summary is preserved.
      const askSummary =
        m.askUser && Array.isArray(m.askUser.questions)
          ? m.askUser.questions
              .map((q) => String((q && q.question) || "").trim())
              .filter(Boolean)
              .join("\n")
          : "";
      const suppressBubble = !!askCard && String(m.text || "").trim() === askSummary && askSummary !== "";
      setBubbleMarkdown(bubble, suppressBubble ? "" : (m.text || window.AkanaI18n.t("msg.empty_bubble")));
      if (!suppressBubble) msgBody.appendChild(bubble);
      if (askCard) msgBody.appendChild(askCard);
      // Tool calls go ABOVE the bubble — consistent position + appearance with
      // the live turn (aurora process card). Cards must not slip below the message after F5.
      let toolCount = 0;
      if (Array.isArray(m.toolCalls) && m.toolCalls.length) {
        const card = renderToolProcessCard(m.toolCalls, m.turnId);
        if (card) {
          // Bubble may be suppressed on a pending-ask turn (not in msgBody) → append.
          if (bubble.parentNode === msgBody) msgBody.insertBefore(card, bubble);
          else msgBody.appendChild(card);
          // Unique rendered card count (NOT raw array length — start+end events per
          // tool can inflate it 2×; same reasoning as renderToolProcessCard's own header).
          toolCount = card.querySelectorAll(".tool-call").length;
        }
      }
      // SSE contract 4: if usage is present, show tokens + cost in the meta row
      // (should survive F5). Built AFTER the process card so the tool count reflects
      // the actual rendered cards.
      meta.textContent = formatHistoryMeta(
        typeof m.latencyMs === "number" ? m.latencyMs : null,
        m.usage || null,
        toolCount,
      );
      // Premium status chip: a persisted/history assistant row always represents a
      // COMPLETED turn (failed turns persist as kind:"error" and take a separate
      // render path — see chatRenderMessage below) → always "ok" here.
      meta.appendChild(buildMetaStatusChip("ok"));
      wrap.appendChild(avatar);
      wrap.appendChild(msgBody);
      hooks.log.appendChild(wrap);
    }

    function chatRenderMessage(m) {
      if (m.kind === "user") {
        const row = hooks.appendUserMessage(m.text || "", m.fileIds || []);
        if (row && m.ts) row.dataset.time = formatClock(m.ts);
      } else if (m.kind === "assistant") renderAssistantFromPersist(m);
      else if (m.kind === "system") hooks.appendSystemNotice(m.text || "");
      else if (m.kind === "error") {
        // Render persisted errors as the SAME card shown live (with Retry) — a log redraw
        // (WS turn_completed / F5) must not downgrade the live error card to a plain
        // "Error" row. Retry re-sends the turn's user text (matches the live card path).
        const card = renderErrorCard({
          title: window.AkanaI18n.t("transport.err_card.generic_title"),
          detail: m.text || "",
          onRetry: m.userText
            ? () => {
                try {
                  window.AkanaChat?.submitAnswerText?.(m.userText);
                } catch (e) {
                  console.warn("err retry", e);
                }
              }
            : undefined,
        });
        const wrap = document.createElement("div");
        wrap.className = "row row-assistant";
        const avatar = document.createElement("div");
        avatar.className = "msg-avatar";
        avatar.setAttribute("aria-hidden", "true");
        avatar.textContent = "A";
        const msgBody = document.createElement("div");
        msgBody.className = "msg-body";
        msgBody.appendChild(card);
        wrap.appendChild(avatar);
        wrap.appendChild(msgBody);
        hooks.log.appendChild(wrap);
      }
    }

    return { renderAssistantFromPersist, chatRenderMessage };
  }

  window.AkanaChatRender = {
    toolCallLabelTr,
    toolCallActionSentence,
    toolCallStatus,
    renderToolCall,
    renderTodoCard,
    patchTodoCard,
    upsertTodoCard,
    isTodoCall,
    extractTodoItems,
    patchToolCallCard,
    materializeToolCallDetail,
    upsertToolCallCard,
    upsertToolCardIntoTimeline,
    upsertToolInputStream,
    renderSubagentGroup,
    upsertSubagentGroup,
    setStatusIcon,
    appendToolCallsGrouped,
    renderToolProcessCard,
    renderMemoryUse,
    renderSkillUse,
    renderSourcesRow,
    appendMemorySources,
    renderApprovalCard,
    renderAskUserCard,
    renderPlanCard,
    renderErrorCard,
    mapServerMessagesToThread,
    putToolCallsForTurn,
    setPanelCollapsed,
    getPanelCollapsed,
    createRenderer,
  };
})();
