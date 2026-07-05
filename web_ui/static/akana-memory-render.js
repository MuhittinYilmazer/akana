/**
 * Akana Memory Studio render helpers — pure DOM producers.
 * No HTTP calls, no element-id queries; studio passes data and callbacks.
 * Empty-state icon/action is read from the list element's own data-* attributes
 * (data-empty-icon, data-empty-action-href, data-empty-action-label).
 */
(() => {
  const t = (key, vars) => window.AkanaI18n ? window.AkanaI18n.t(key, vars) : key;

  function formatTs(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return String(iso);
      return d.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    } catch {
      return String(iso);
    }
  }

  // ─── Empty / loading / error states ─────────────────────────────────────────
  const ICON_PATHS = {
    inbox:
      "M5.2 5.5h13.6l1.7 8v4.4a1.6 1.6 0 0 1-1.6 1.6H5.1a1.6 1.6 0 0 1-1.6-1.6v-4.4l1.7-8Z" +
      "M3.5 13.5h4.6l1.5 2.5h4.8l1.5-2.5h4.6",
    facts:
      "M12 3.2 3.6 7.7 12 12.2l8.4-4.5L12 3.2Z" +
      "M3.6 12.2 12 16.7l8.4-4.5" +
      "M3.6 16.6 12 21.1l8.4-4.5",
    recall:
      "M10.8 4.4a6.2 6.2 0 1 0 0 12.4 6.2 6.2 0 0 0 0-12.4Z" +
      "M15.4 15.4 20 20",
    alert: "M12 4.2 2.8 19.6h18.4L12 4.2ZM12 10.2v4.2M12 16.8v.5",
  };

  function stateIcon(name) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "22");
    svg.setAttribute("height", "22");
    svg.setAttribute("fill", "none");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", ICON_PATHS[name] || ICON_PATHS.facts);
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.7");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }

  /** Splits "Title. Rest…" / "Title — rest" into title + hint.
   *  Empty-state messages interpolate the raw user query inside «guillemets»
   *  (e.g. «why not? maybe»), and that query can contain .!? — which would make
   *  the sentence split fire INSIDE the query and truncate the title mid-word
   *  (audit C35). Mask the «…» span(s) before splitting, then restore, so the
   *  split only ever anchors to the template's own punctuation. */
  function splitStateText(text) {
    const s = String(text || "").trim();
    const masks = [];
    const masked = s.replace(/«[^»]*»/g, (m) => {
      masks.push(m);
      return "QQMASKQQ" + (masks.length - 1) + "QQ";
    });
    const restore = (str) =>
      str.replace(/QQMASKQQ(\d+)QQ/g, (_, i) => masks[Number(i)] ?? "");
    const sentence = masked.match(/^(.+?[.!?])\s+(\S[\s\S]*)$/);
    if (sentence) {
      return { title: restore(sentence[1]).replace(/\.$/, ""), hint: restore(sentence[2]) };
    }
    const dash = masked.split(" — ");
    if (dash.length > 1) {
      return { title: restore(dash[0]), hint: restore(dash.slice(1).join(" — ")) };
    }
    return { title: restore(masked).replace(/\.$/, ""), hint: "" };
  }

  function setListState(el, kind, text) {
    if (!el) return;
    // Don't touch cards mid-exit animation; remove the rest (no patch).
    for (const child of Array.from(el.children)) {
      if (!(child.classList && child.classList.contains("is-leaving"))) el.removeChild(child);
    }
    const li = document.createElement("li");
    li.className = `memory-state memory-state-${kind}`;
    const msg = String(text || "");
    const ds = el.dataset || {};

    if (kind === "loading") {
      const spin = document.createElement("span");
      spin.className = "memory-spinner";
      spin.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.textContent = msg;
      li.append(spin, label);
    } else if (kind === "empty") {
      li.classList.add("memory-state-rich");
      const iconWrap = document.createElement("span");
      iconWrap.className = "memory-state-icon";
      iconWrap.appendChild(stateIcon(ds.emptyIcon || "facts"));
      const { title, hint } = splitStateText(msg);
      const titleEl = document.createElement("p");
      titleEl.className = "memory-empty-title";
      titleEl.textContent = title;
      li.append(iconWrap, titleEl);
      if (hint) {
        const hintEl = document.createElement("p");
        hintEl.className = "memory-empty-hint";
        hintEl.textContent = hint;
        li.appendChild(hintEl);
      }
      if (ds.emptyActionHref && ds.emptyActionLabel) {
        const a = document.createElement("a");
        a.className = "btn-ghost btn-sm memory-empty-action";
        a.href = ds.emptyActionHref;
        a.textContent = ds.emptyActionLabel;
        li.appendChild(a);
      }
    } else if (kind === "error") {
      li.classList.add("memory-state-rich");
      const iconWrap = document.createElement("span");
      iconWrap.className = "memory-state-icon memory-state-icon-error";
      iconWrap.appendChild(stateIcon("alert"));
      const textEl = document.createElement("p");
      textEl.className = "memory-error-text";
      textEl.textContent = msg;
      li.append(iconWrap, textEl);
    } else {
      li.textContent = msg;
    }
    el.appendChild(li);
  }

  // ─── Badge (chip) producers ──────────────────────────────────────────────────
  function badge(text, cls, title) {
    const b = document.createElement("span");
    b.className = `memory-chip ${cls || ""}`.trim();
    b.textContent = text;
    if (title) b.title = title;
    return b;
  }

  /** P6 trust ladder — source type → token colour (--j-trust-*). */
  const TRUST_SOURCES = {
    user_statement: { labelKey: "memory.trust_user_label",    cls: "memory-chip-trust-user",     titleKey: "memory.trust_user_title" },
    inferred:       { labelKey: "memory.trust_inferred_label", cls: "memory-chip-trust-inferred", titleKey: "memory.trust_inferred_title" },
    tool_output:    { labelKey: "memory.trust_tool_label",     cls: "memory-chip-trust-tool",     titleKey: "memory.trust_tool_title" },
    synthesis:      { labelKey: "memory.trust_synth_label",    cls: "memory-chip-trust-synth",    titleKey: "memory.trust_synth_title" },
  };

  function trustBadge(trust) {
    if (trust === null || trust === undefined || trust === "") return null;
    const source = TRUST_SOURCES[String(trust)];
    if (source) return badge(t(source.labelKey), `memory-chip-trust ${source.cls}`, t(source.titleKey));
    const n = Number(trust);
    if (Number.isNaN(n)) return badge(String(trust), "memory-chip-trust");
    const pct = n <= 1 ? Math.round(n * 100) : Math.round(n);
    const level = pct >= 80 ? "high" : pct >= 50 ? "mid" : "low";
    return badge(t("memory.trust_pct_label", { n: pct }), `memory-chip-trust memory-chip-trust-${level}`, t("memory.trust_pct_title"));
  }

  // ─── Provenance (citation-native): source badge + "Where did this come from?" ─
  /** Origin → label/title; colours in css via [data-origin=…] → --j-trust-* token. */
  const SOURCE_ORIGIN_META = {
    user_statement: { labelKey: "memory.origin_user_label",    titleKey: "memory.origin_user_title" },
    inferred:       { labelKey: "memory.origin_inferred_label", titleKey: "memory.origin_inferred_title" },
    tool_output:    { labelKey: "memory.origin_tool_label",     titleKey: "memory.origin_tool_title" },
    synthesis:      { labelKey: "memory.origin_synth_label",    titleKey: "memory.origin_synth_title" },
    legacy:         { labelKey: "memory.origin_legacy_label",   titleKey: "memory.origin_legacy_title" },
  };

  /** Popover body: origin/detail/observation-time rows + close button. */
  function buildProvenancePopover(source, meta, onClose) {
    const pop = document.createElement("div");
    pop.className = "memory-provenance-popover";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", t("memory.provenance_aria"));

    const head = document.createElement("div");
    head.className = "memory-provenance-head";
    const title = document.createElement("strong");
    title.textContent = t("memory.provenance_heading");
    const close = document.createElement("button");
    close.type = "button";
    close.className = "memory-provenance-close";
    close.setAttribute("aria-label", t("memory.provenance_close_aria"));
    close.textContent = "×";
    close.addEventListener("click", onClose);
    head.append(title, close);
    pop.appendChild(head);

    const rows = [
      [t("memory.provenance_row_origin"),   t(meta.labelKey)],
      [t("memory.provenance_row_detail"),   source.detail || "—"],
      [t("memory.provenance_row_observed"), source.observed_at ? formatTs(source.observed_at) : "—"],
    ];
    for (const [k, v] of rows) {
      const row = document.createElement("div");
      row.className = "memory-provenance-row";
      const label = document.createElement("span");
      label.className = "memory-provenance-label";
      label.textContent = k;
      const value = document.createElement("span");
      value.className = "memory-provenance-value";
      value.textContent = v;
      row.append(label, value);
      pop.appendChild(row);
    }
    return pop;
  }

  /**
   * Source badge: coloured mini-chip by origin; clicking opens a provenance
   * popover (click-to-verify). source: {origin, detail, observed_at}.
   */
  // Only ONE origin-popover open at a time. When the list is re-rendered
  // (search/filter/refresh) the open popover's card detaches from the DOM but
  // closePop is never called, leaving document listeners (onDocClick/onDocKey)
  // behind. We close the previous popover on open to prevent listener leaks.
  let activeSourcePopClose = null;
  function sourceBadge(source) {
    if (!source || !source.origin) return null;
    const origin = String(source.origin);
    const meta = SOURCE_ORIGIN_META[origin] || { labelKey: null, titleKey: "memory.origin_generic_title" };
    const wrap = document.createElement("span");
    wrap.className = "memory-source-wrap";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "memory-chip memory-chip-source";
    btn.dataset.origin = origin;
    btn.title = `${t(meta.titleKey)}${t("memory.origin_source_btn_suffix")}`;
    btn.setAttribute("aria-haspopup", "dialog");
    btn.setAttribute("aria-expanded", "false");
    btn.textContent = meta.labelKey ? t(meta.labelKey) : origin;

    let pop = null;
    const onDocClick = (e) => {
      if (!wrap.contains(e.target)) closePop();
    };
    const onDocKey = (e) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation(); // Esc must not trigger other layers (e.g. voice cancel)
      closePop();
      try {
        btn.focus();
      } catch {
        /* focus not available — fine */
      }
    };
    function closePop() {
      if (!pop) return;
      pop.remove();
      pop = null;
      btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onDocKey, true);
      if (activeSourcePopClose === closePop) activeSourcePopClose = null;
    }
    btn.addEventListener("click", () => {
      if (pop) {
        closePop();
        return;
      }
      // Close another card's detached/open popover → prevent listener accumulation.
      if (activeSourcePopClose) activeSourcePopClose();
      pop = buildProvenancePopover(source, meta, closePop);
      wrap.appendChild(pop);
      btn.setAttribute("aria-expanded", "true");
      document.addEventListener("click", onDocClick, true);
      document.addEventListener("keydown", onDocKey, true);
      activeSourcePopClose = closePop;
    });

    wrap.appendChild(btn);
    return wrap;
  }

  /** Kind chip: type-based muted colour via css [data-kind=…]. */
  function kindBadge(kind, title) {
    const b = badge(String(kind), "memory-chip-kind", title);
    b.dataset.kind = String(kind).toLowerCase();
    return b;
  }

  function metaBadges(item) {
    const wrap = document.createElement("div");
    wrap.className = "memory-chip-row";
    if (item.kind) wrap.appendChild(kindBadge(item.kind));
    const tr = trustBadge(item.trust);
    if (tr) wrap.appendChild(tr);
    const src = sourceBadge(item.source);
    if (src) wrap.appendChild(src);
    if (item.invalidated_at) {
      wrap.appendChild(badge(
        t("memory.badge_invalid"),
        "memory-chip-invalid",
        t("memory.badge_invalid_title", { ts: formatTs(item.invalidated_at) }),
      ));
    }
    return wrap;
  }

  /** Card heading: key (mono, dominant) + time/source meta on the right. */
  function cardHead(keyText, metaText) {
    const head = document.createElement("div");
    head.className = "memory-card-head";
    const key = document.createElement("span");
    key.className = "memory-card-key";
    key.textContent = keyText;
    head.appendChild(key);
    if (metaText) {
      const meta = document.createElement("span");
      meta.className = "memory-hit-meta";
      meta.textContent = metaText;
      head.appendChild(meta);
    }
    return head;
  }

  function actionButton(label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  /**
   * Optimistic removal hook: when studio calls li.remove() the card slides out.
   * Counter selectors (querySelectorAll) stay correct synchronously because
   * `countedClass` is dropped IMMEDIATELY (the visual continuity is provided
   * by the `-leaving` class); the DOM node detaches once the transition ends.
   */
  function attachLeaveAnimation(li, countedClass) {
    const nativeRemove = li.remove.bind(li);
    li.remove = () => {
      if (li.dataset.memoryLeaving === "1") return;
      li.dataset.memoryLeaving = "1";
      if (countedClass) {
        li.classList.remove(countedClass);
        li.classList.add(`${countedClass}-leaving`);
      }
      li.setAttribute("aria-hidden", "true");
      li.style.maxHeight = `${li.scrollHeight}px`;
      void li.offsetHeight; // reflow → max-height transition can fire
      li.classList.add("is-leaving");
      window.setTimeout(nativeRemove, 480);
    };
  }

  /** Inbox (staging) card: key/value/reason/trust/source + Approve/Reject. */
  function renderInboxItem(item, { shortId, onApprove, onReject }) {
    const li = document.createElement("li");
    li.className = "memory-fact-card memory-inbox-item";
    li.dataset.stagingId = item.id;
    li.appendChild(metaBadges(item));

    const metaText = [formatTs(item.ts), item.conversation_id ? t("memory.inbox_source_label", { id: shortId(item.conversation_id) }) : ""]
      .filter(Boolean)
      .join(" · ");
    li.appendChild(cardHead(item.key || t("memory.inbox_key_default"), metaText));

    const body = document.createElement("span");
    body.className = "memory-hit-text";
    body.textContent = item.value || "";
    li.appendChild(body);

    if (item.reason) {
      const reason = document.createElement("p");
      reason.className = "memory-inbox-reason";
      reason.textContent = t("memory.inbox_reason_prefix", { reason: item.reason });
      li.appendChild(reason);
    }

    const actions = document.createElement("div");
    actions.className = "memory-fact-actions";
    actions.append(
      actionButton(t("memory.inbox_approve_btn"), "btn-primary btn-sm memory-approve-btn", () => onApprove(item, li)),
      actionButton(t("memory.inbox_reject_btn"),  "btn-ghost btn-sm memory-fact-delete",   () => onReject(item, li)),
    );
    li.appendChild(actions);
    attachLeaveAnimation(li, "memory-inbox-item");
    return li;
  }

  /** "↑N uses" salience badge — added to fact cards when hit_count is present. */
  function salienceBadge(f) {
    const n = Number(f && f.hit_count);
    if (!Number.isFinite(n) || n <= 0) return null;
    const title = f.last_hit_at
      ? t("memory.badge_salience_last", { ts: formatTs(f.last_hit_at) })
      : t("memory.badge_salience_title");
    return badge(t("memory.badge_salience_label", { n }), "memory-chip-salience", title);
  }

  /**
   * Permanent fact card: badges + salience + value + Edit/Delete.
   * Invalidated records are read-only (no action buttons).
   */
  function renderFactCard(f, { onEdit, onDelete }) {
    const li = document.createElement("li");
    li.className = "memory-fact-card";
    if (f.invalidated_at) li.classList.add("is-invalidated");
    li.dataset.factId = f.id;
    const badges = metaBadges(f);
    const sal = salienceBadge(f);
    if (sal) badges.appendChild(sal);
    li.appendChild(badges);
    li.appendChild(cardHead(f.key || t("memory.fact_key_default"), formatTs(f.ts_last)));

    const body = document.createElement("span");
    body.className = "memory-hit-text";
    body.textContent = f.value || "";
    li.appendChild(body);

    const actions = document.createElement("div");
    actions.className = "memory-fact-actions";
    if (!f.invalidated_at) {
      actions.append(
        actionButton(t("memory.fact_edit_btn"),   "btn-ghost btn-sm",                   () => onEdit(f, li)),
        actionButton(t("memory.fact_delete_btn"), "btn-ghost btn-sm memory-fact-delete", () => onDelete(f)),
      );
    }
    if (actions.children.length) li.appendChild(actions);
    return li;
  }

  /** Inline editor: new value + mode (replace / correct wording). onSave(newValue, mode, doneCb). */
  function buildFactEditor(f, { onSave }) {
    const editor = document.createElement("div");
    editor.className = "memory-fact-editor";

    const ta = document.createElement("textarea");
    ta.rows = 3;
    ta.value = f.value || "";
    ta.setAttribute("aria-label", t("memory.editor_value_aria"));
    editor.appendChild(ta);

    const modeRow = document.createElement("div");
    modeRow.className = "memory-fact-editor-modes";
    const modes = [
      { value: "supersede", labelKey: "memory.editor_supersede_label", hintKey: "memory.editor_supersede_hint" },
      { value: "correct",   labelKey: "memory.editor_correct_label",   hintKey: "memory.editor_correct_hint" },
    ];
    for (const m of modes) {
      const lab = document.createElement("label");
      lab.className = "memory-fact-editor-mode";
      lab.title = t(m.hintKey);
      const input = document.createElement("input");
      input.type = "radio";
      input.name = `memory-edit-mode-${f.id}`;
      input.value = m.value;
      input.checked = m.value === "supersede";
      lab.append(input, document.createTextNode(` ${t(m.labelKey)}`));
      modeRow.appendChild(lab);
    }
    editor.appendChild(modeRow);

    const actions = document.createElement("div");
    actions.className = "memory-fact-actions";
    const save = actionButton(t("memory.editor_save_btn"), "btn-primary btn-sm", () => {
      const mode = editor.querySelector(`input[name="memory-edit-mode-${f.id}"]:checked`)?.value || "supersede";
      onSave(ta.value.trim(), mode, save);
    });
    actions.append(save, actionButton(t("memory.editor_cancel_btn"), "btn-ghost btn-sm", () => editor.remove()));
    editor.appendChild(actions);
    return { editor, focus: () => ta.focus() };
  }

  /** Recall result card: type/score/trust badges + score mini-bar + summary. */
  function renderRecallItem(it) {
    const li = document.createElement("li");
    li.className = "memory-fact-card";
    const chips = document.createElement("div");
    chips.className = "memory-chip-row";
    if (it.type) chips.appendChild(kindBadge(it.type));
    if (typeof it.score === "number") chips.appendChild(badge(t("memory.badge_score_label", { n: it.score.toFixed(2) }), "memory-chip-score"));
    const tr = trustBadge(it.trust);
    if (tr) chips.appendChild(tr);
    const src = sourceBadge(it.source);
    if (src) chips.appendChild(src);
    li.appendChild(chips);
    const body = document.createElement("span");
    body.className = "memory-hit-text";
    body.textContent = it.summary || "";
    li.appendChild(body);
    if (typeof it.score === "number") {
      const clamped = Math.max(0, Math.min(1, it.score));
      const bar = document.createElement("span");
      bar.className = "memory-score-bar";
      bar.title = t("memory.badge_score_bar_title", { n: it.score.toFixed(2) });
      bar.setAttribute("aria-hidden", "true");
      const fill = document.createElement("span");
      fill.className = "memory-score-fill";
      fill.style.width = "0%";
      bar.appendChild(fill);
      li.appendChild(bar);
      const grow = () => {
        fill.style.width = `${Math.round(clamped * 100)}%`;
      };
      // Expand after first paint (bar "fills in"); rAF may never fire in a hidden
      // tab — the timer guarantees the final state in all cases.
      if (typeof window.requestAnimationFrame === "function") {
        window.requestAnimationFrame(() => window.requestAnimationFrame(grow));
      }
      window.setTimeout(grow, 300);
    }
    return li;
  }

  /** Recall trace: strategy + vector on/off + warning badges. */
  function renderRecallTrace(traceEl, data) {
    if (!traceEl) return;
    traceEl.innerHTML = "";
    const trace = (data && data.trace) || {};
    if (trace.strategy) {
      const usesVector = /vector|embed|semantic|hybrid/i.test(String(trace.strategy));
      traceEl.appendChild(badge(t("memory.trace_strategy_label", { s: trace.strategy }), "memory-chip-strategy"));
      traceEl.appendChild(
        badge(
          usesVector ? t("memory.trace_vector_on") : t("memory.trace_vector_off"),
          usesVector ? "memory-chip-vector-on" : "memory-chip-vector-off",
          usesVector ? t("memory.trace_vector_on_title") : t("memory.trace_vector_off_title"),
        ),
      );
    }
    const warnings = data && Array.isArray(data.warnings) ? data.warnings : [];
    for (const w of warnings) traceEl.appendChild(badge(String(w), "memory-chip-warn"));
  }

  /** "session:<id>: summary" → {sessionId, text}; no prefix → {sessionId:"", text}. */
  function splitSessionPrefix(detail) {
    const s = String(detail || "");
    const m = s.match(/^\s*(?:session|oturum)\s*:\s*([A-Za-z0-9_-]+)\s*[:\-–—]?\s*([\s\S]*)$/i);
    if (m) return { sessionId: m[1], text: m[2].trim() };
    return { sessionId: "", text: s.trim() };
  }

  /**
   * Overview timeline item: kind dot + title + session chip +
   * time + (2-line clamped) detail. Raw "session:ID:" prefix is extracted from
   * detail and placed in a mono chip — the feed stays scannable.
   * item: {ts, kind, title, detail, ref_id} (newest-first order supplied by studio).
   */
  function renderTimelineItem(it) {
    const li = document.createElement("li");
    li.className = "memory-timeline-item";
    const kind = String(it.kind || "").toLowerCase();
    if (kind) li.dataset.kind = kind;
    if (it.ref_id) li.dataset.refId = it.ref_id;

    const dot = document.createElement("span");
    dot.className = "memory-timeline-dot";
    dot.setAttribute("aria-hidden", "true");
    li.appendChild(dot);

    const main = document.createElement("div");
    main.className = "memory-timeline-main";

    const { sessionId, text: detailText } = splitSessionPrefix(it.detail);

    const head = document.createElement("div");
    head.className = "memory-timeline-head";
    const title = document.createElement("span");
    title.className = "memory-timeline-title";
    title.textContent = it.title || it.kind || t("memory.timeline_title_default");
    head.appendChild(title);
    if (sessionId) {
      const src = document.createElement("span");
      src.className = "memory-timeline-src";
      src.textContent = `#${sessionId.length > 8 ? sessionId.slice(0, 6) : sessionId}`;
      src.title = t("memory.timeline_session_title", { id: sessionId });
      head.appendChild(src);
    }
    if (it.ts) {
      const ts = document.createElement("span");
      ts.className = "memory-timeline-ts";
      ts.textContent = formatTs(it.ts);
      head.appendChild(ts);
    }
    main.appendChild(head);

    if (detailText) {
      const detail = document.createElement("p");
      detail.className = "memory-timeline-detail";
      detail.textContent = detailText;
      detail.title = detailText;
      main.appendChild(detail);
    }
    li.appendChild(main);
    return li;
  }

  window.AkanaMemoryRender = {
    setListState,
    badge,
    sourceBadge,
    renderInboxItem,
    renderFactCard,
    buildFactEditor,
    renderRecallItem,
    renderRecallTrace,
    renderTimelineItem,
  };
})();
