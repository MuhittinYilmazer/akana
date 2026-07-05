/**
 * Safe Markdown for assistant chat bubbles (no raw HTML pass-through).
 * Preprocesses model output, renders during SSE stream, escapes all text.
 */
(() => {
  const escapeHtml = (s) => window.AkanaCore.escapeHtml(s);

  const BULLET_RE = /^(\s*)(?:[-*+•–—]|\u2022)\s+(.+)$/;
  const ORDERED_RE = /^(\s*)\d+[.)]\s+(.+)$/;
  const TASK_RE = /^(\s*)[-*+]\s+\[([ xX])\]\s+(.+)$/;

  function normalizeMarkdownSrc(src) {
    if (!src) return "";
    let s = String(src).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    s = s.replace(/([^\n])\s+(#{1,6}\s+\S)/g, "$1\n\n$2");
    return s;
  }

  function preprocessMarkdown(src) {
    let s = normalizeMarkdownSrc(src);
    s = s.replace(/\t/g, "  ");
    s = s.replace(/^[ \t]+(#{1,6}\s)/gm, "$1");
    s = s.replace(/^(\d+)\)\s+/gm, "$1. ");
    s = s.replace(/([^\n])\n([-*+•]\s)/g, "$1\n\n$2");
    s = s.replace(/([^\n])\n(\d+\.\s)/g, "$1\n\n$2");
    s = s.replace(/([:.!?])\n(?!\n)(?=[-*+•]\s)/g, "$1\n\n");
    s = s.replace(/([:.!?])\n(?!\n)(?=\d+\.\s)/g, "$1\n\n");
    const lines = s.split("\n");
    promoteImplicitListItems(lines);
    return lines.join("\n");
  }

  function promoteImplicitListItems(lines) {
    let inFence = false;
    for (let i = 0; i < lines.length; i += 1) {
      if (/^\s*```/.test(lines[i])) {
        inFence = !inFence;
        continue;
      }
      if (inFence) continue;
      const trimmed = lines[i].trim();
      if (!/[:：]\s*$/.test(trimmed)) continue;
      const start = i + 1;
      let j = start;
      const indices = [];
      while (j < lines.length) {
        const row = lines[j];
        const t = row.trim();
        if (!t) break;
        if (/^(#{1,6}\s|[-*+•]|\d+\.|```|>\s|\|)/.test(t)) break;
        if (t.length > 140) break;
        indices.push(j);
        j += 1;
      }
      if (indices.length < 2) continue;
      for (const idx of indices) {
        if (!BULLET_RE.test(lines[idx]) && !ORDERED_RE.test(lines[idx])) {
          lines[idx] = `- ${lines[idx].trim()}`;
        }
      }
      i = j - 1;
    }
  }

  function parsePipeTableCells(line) {
    const trimmed = line.trim();
    if (!trimmed.includes("|")) return null;
    let inner = trimmed;
    if (inner.startsWith("|")) inner = inner.slice(1);
    if (inner.endsWith("|")) inner = inner.slice(0, -1);
    return inner.split("|").map((c) => c.trim());
  }

  function isPipeTableSeparator(line) {
    const cells = parsePipeTableCells(line);
    if (!cells || !cells.length) return false;
    return cells.every((c) => /^:?-{2,}:?$/.test(c));
  }

  function inlineFormat(raw) {
    let t = escapeHtml(raw);
    // Mask inline code spans to placeholders BEFORE the emphasis/link passes, so their
    // contents are never reinterpreted as markdown — e.g. `a*b*c` must render the literal
    // text, not `a<em>b</em>c`, and a URL inside backticks must not be auto-linked. The
    // placeholders use NUL sentinels (never present in escaped text) and are restored last.
    const codeSpans = [];
    t = t.replace(/`([^`\n]+)`/g, (_m, code) => {
      codeSpans.push(`<code class="md-inline-code">${code}</code>`);
      return `\u0000C${codeSpans.length - 1}\u0000`;
    });
    t = t.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/gi, (_m, label, url) => {
      return `<a class="md-link" href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    t = t.replace(
      /(?<![\w/"'=])(https?:\/\/[^\s<]+[^\s<.,;:!?)\]}])/gi,
      (url) =>
        `<a class="md-link" href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`,
    );
    t = t.replace(/\u0000C(\d+)\u0000/g, (m, i) => codeSpans[Number(i)] ?? m);
    return t;
  }

  // Full markdown parsing + DOM construction for messages above this threshold
  // can freeze the main thread during history/boot render (live incident: loading
  // workflow test dev output locked the UI at startup). Above the threshold,
  // the fast path is used: escaped plain text + truncation note.
  const MD_RENDER_MAX = 50000;

  const _t = (k, p) => window.AkanaI18n?.t(k, p) ?? k;

  function renderMarkdown(src, opts = {}) {
    if (!src) return "";
    const _raw = typeof src === "string" ? src : String(src);
    if (_raw.length > MD_RENDER_MAX) {
      const head = escapeHtml(_raw.slice(0, MD_RENDER_MAX));
      const total = _raw.length.toLocaleString();
      const shown = MD_RENDER_MAX.toLocaleString();
      return (
        `<pre class="md-code md-too-large">${head}\n\n` +
        escapeHtml(_t("ui.md_truncated", { total, shown })) +
        `</pre>`
      );
    }
    let text = preprocessMarkdown(src);
    const lines = text.split("\n");

    const out = [];
    let i = 0;
    let inUl = false;
    let inOl = false;

    const closeLists = () => {
      if (inUl) {
        out.push("</ul>");
        inUl = false;
      }
      if (inOl) {
        out.push("</ol>");
        inOl = false;
      }
    };

    const renderTable = (start) => {
      const headerCells = parsePipeTableCells(lines[start]);
      if (!headerCells) return start;
      let j = start + 1;
      if (j < lines.length && isPipeTableSeparator(lines[j])) j += 1;
      const bodyRows = [];
      while (j < lines.length) {
        const cells = parsePipeTableCells(lines[j]);
        if (!cells || isPipeTableSeparator(lines[j])) break;
        bodyRows.push(cells);
        j += 1;
      }
      if (!bodyRows.length && j === start + 1) return start;
      const thead = `<thead><tr>${headerCells.map((c) => `<th>${inlineFormat(c)}</th>`).join("")}</tr></thead>`;
      const tbody = bodyRows.length
        ? `<tbody>${bodyRows
            .map(
              (row) =>
                `<tr>${row.map((c) => `<td>${inlineFormat(c)}</td>`).join("")}</tr>`,
            )
            .join("")}</tbody>`
        : "";
      out.push(`<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`);
      return j;
    };

    // INFINITE-LOOP GUARD (defense-in-depth): if any iteration returns here
    // without advancing `i` (via continue) — including any future branch
    // regression — force the line into a plain paragraph and advance. Markdown
    // rendering is tied to user input and must NEVER freeze the page (live freeze
    // incident: a trailing `|` line caused renderTable to return without advancing,
    // creating an infinite loop).
    let _prevLoopI = -1;
    while (i < lines.length) {
      if (i === _prevLoopI) {
        closeLists();
        out.push(`<p>${inlineFormat(lines[i])}</p>`);
        i += 1;
        continue;
      }
      _prevLoopI = i;
      const line = lines[i];

      if (/^\s*```/.test(line)) {
        closeLists();
        const lang = line.trim().slice(3).trim();
        const buf = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) {
          buf.push(lines[i]);
          i += 1;
        }
        const partial = opts.streaming && i >= lines.length;
        if (!partial) i += 1;
        const langAttr = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
        const partialClass = partial ? " md-code--partial" : "";
        out.push(
          `<pre class="md-code${partialClass}"${langAttr}><code>${escapeHtml(buf.join("\n"))}</code></pre>`,
        );
        if (partial) break;
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        closeLists();
        const level = Math.min(6, heading[1].length);
        out.push(`<h${level} class="md-h md-h${level}">${inlineFormat(heading[2])}</h${level}>`);
        i += 1;
        continue;
      }

      const leadBold = line.match(/^\*\*([^*\n]+)\*\*\s*$/);
      if (leadBold) {
        closeLists();
        out.push(`<p class="md-lead"><strong>${inlineFormat(leadBold[1])}</strong></p>`);
        i += 1;
        continue;
      }

      const bq = line.match(/^>\s?(.*)$/);
      if (bq) {
        closeLists();
        const parts = [];
        while (i < lines.length) {
          const m = lines[i].match(/^>\s?(.*)$/);
          if (!m) break;
          parts.push(m[1]);
          i += 1;
        }
        out.push(
          `<blockquote class="md-quote">${parts.map((p) => `<p>${inlineFormat(p)}</p>`).join("")}</blockquote>`,
        );
        continue;
      }

      if (/^[-*_]{3,}\s*$/.test(line.trim())) {
        closeLists();
        out.push('<hr class="md-hr" />');
        i += 1;
        continue;
      }

      const tableCells = parsePipeTableCells(line);
      if (
        tableCells &&
        (i + 1 >= lines.length ||
          isPipeTableSeparator(lines[i + 1]) ||
          parsePipeTableCells(lines[i + 1]))
      ) {
        const tableEnd = renderTable(i);
        // If renderTable did NOT advance (a single-line `|` text is not a real
        // table — e.g. the last line of a message) do NOT `continue`: i would
        // stay fixed and create an infinite loop. Fall through to normal paragraph
        // handling (i advances below).
        if (tableEnd > i) {
          closeLists();
          i = tableEnd;
          continue;
        }
      }

      const task = line.match(TASK_RE);
      if (task && !line.trimStart().startsWith("|")) {
        if (!inUl) {
          closeLists();
          out.push('<ul class="md-task-list">');
          inUl = true;
        }
        const checked = task[2].toLowerCase() === "x";
        out.push(
          `<li class="md-task${checked ? " md-task--done" : ""}"><span class="md-task-box" aria-hidden="true">${checked ? "✓" : ""}</span>${inlineFormat(task[3])}</li>`,
        );
        i += 1;
        continue;
      }

      const ulm = line.match(BULLET_RE);
      if (ulm && !line.trimStart().startsWith("|")) {
        if (!inUl) {
          closeLists();
          out.push("<ul>");
          inUl = true;
        }
        out.push(`<li>${inlineFormat(ulm[2])}</li>`);
        i += 1;
        continue;
      }

      const olm = line.match(ORDERED_RE);
      if (olm) {
        if (!inOl) {
          closeLists();
          out.push("<ol>");
          inOl = true;
        }
        out.push(`<li>${inlineFormat(olm[2])}</li>`);
        i += 1;
        continue;
      }

      if (!line.trim()) {
        closeLists();
        i += 1;
        continue;
      }

      closeLists();
      out.push(`<p>${inlineFormat(line)}</p>`);
      i += 1;
    }

    closeLists();
    return out.join("");
  }

  function setBubbleMarkdown(bubble, text, opts = {}) {
    if (!bubble) return;
    try {
      const streamClass = opts.streaming ? " md-content--stream" : "";
      bubble.innerHTML = `<div class="md-content${streamClass}">${renderMarkdown(text, opts)}</div>`;
    } catch (e) {
      console.warn("markdown render failed", e);
      bubble.textContent = text || "";
    }
  }

  function ensureStreamPlainEl(bubble) {
    let el = bubble.querySelector(".md-stream-plain");
    if (!el) {
      bubble.replaceChildren();
      el = document.createElement("div");
      el.className = "md-content md-content--stream md-stream-plain";
      bubble.appendChild(el);
    }
    return el;
  }

  /** Incremental SSE delta — append-only, avoids rewriting the full string each frame. */
  function appendBubbleStreamText(bubble, piece) {
    if (!bubble || !piece) return;
    ensureStreamPlainEl(bubble).append(document.createTextNode(piece));
  }

  function applyMarkdownToRow(row, selector, text) {
    if (!row) return;
    const bubble = row.querySelector(selector);
    if (bubble) setBubbleMarkdown(bubble, text || "");
  }

  window.AkanaMarkdown = {
    preprocess: preprocessMarkdown,
    render: renderMarkdown,
    setBubbleMarkdown,
    appendBubbleStreamText,
    applyMarkdownToRow,
  };
})();
