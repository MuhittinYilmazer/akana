/* =================================================================== */
/*  aurora-ui.js — Aurora Wave 4 behavior layer                        */
/*  ---------------------------------------------------------------    */
/*  Loads LAST (after akana-settings.js). It only:                    */
/*    1. Fixes the dead accent swatches + adds 4 more (8 total),       */
/*       persisting via the SAME 'cockpit:accent' key the inline boot  */
/*       script reads on load so the choice survives reload.           */
/*    2. Injects 3 `aur-`-prefixed segmented controls (Atmosphere /    */
/*       Shape / Density) into the Appearance settings pane,           */
/*       wired to documentElement.dataset + localStorage, applied on   */
/*       load. aurora-settings.css defines the presets.                */
/*                                                                     */
/*  CONTRACT: touches NO existing markup names; only reads existing    */
/*  ids/classes/data-attrs and appends NEW `aur-`-prefixed nodes.      */
/*  Defensive: every step no-ops if its target is absent (this file    */
/*  may load on pages without the settings panel).                     */
/* =================================================================== */
(function () {
  'use strict';

  var _t = function (k) { return window.AkanaI18n && window.AkanaI18n.t ? window.AkanaI18n.t(k) : k; };

  var root = document.documentElement;

  // ---- localStorage helpers (mirror the 'cockpit:' namespace) ----
  function readPref(key, fallback) {
    try {
      var v = localStorage.getItem('cockpit:' + key);
      return v === null || v === '' ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }
  function writePref(key, value) {
    try { localStorage.setItem('cockpit:' + key, value); } catch (e) {}
  }

  // ---- accent definitions ------------------------------------------------
  // The 4 originals (azure/violet/teal/amber) live in index.html; we append
  // these 20. Dot colors here are inline fallbacks — aurora-settings.css also
  // styles them (kept in sync) so the swatch reads correctly even before paint.
  var EXTRA_ACCENTS = [
    { choice: 'sky',       label: 'Sky',       dot: '#4fb8ff' },
    { choice: 'cobalt',    label: 'Cobalt',    dot: '#5f7fff' },
    { choice: 'indigo',    label: 'Indigo',    dot: '#7b82f5' },
    { choice: 'lavender',  label: 'Lavender',  dot: '#a78bf0' },
    { choice: 'fuchsia',   label: 'Fuchsia',   dot: '#f25cc8' },
    { choice: 'plum',      label: 'Plum',      dot: '#c06ee0' },
    { choice: 'rose',      label: 'Rose',      dot: '#ff7aa6' },
    { choice: 'ruby',      label: 'Ruby',      dot: '#f0405f' },
    { choice: 'burgundy',  label: 'Burgundy',  dot: '#c23358' },
    { choice: 'coral',     label: 'Coral',     dot: '#ff7a68' },
    { choice: 'peach',     label: 'Peach',     dot: '#ff9e76' },
    { choice: 'sunset',    label: 'Sunset',    dot: '#ff8a5c' },
    { choice: 'copper',    label: 'Copper',    dot: '#d9783f' },
    { choice: 'gold',      label: 'Gold',      dot: '#f0b429' },
    { choice: 'olive',     label: 'Olive',     dot: '#aeb84a' },
    { choice: 'lime',      label: 'Lime',      dot: '#8ed24b' },
    { choice: 'emerald',   label: 'Emerald',   dot: '#3fd99a' },
    { choice: 'mint',      label: 'Mint',      dot: '#2fd0a4' },
    { choice: 'turquoise', label: 'Turquoise', dot: '#2fd6e6' },
    { choice: 'slate',     label: 'Slate',     dot: '#6e8bb0' }
  ];

  // Master left-to-right order for the picker — a smooth colour-wheel sweep
  // (azure first as the default, slate the neutral, then the "Custom"
  // chip last). The originals live in index.html as a no-JS fallback; we
  // reorder every chip to match.
  var ACCENT_ORDER = [
    'azure', 'cobalt', 'sky', 'indigo', 'violet', 'lavender', 'fuchsia',
    'plum', 'rose', 'ruby', 'burgundy', 'coral', 'peach', 'sunset',
    'copper', 'amber', 'gold', 'olive', 'lime', 'emerald', 'mint',
    'turquoise', 'teal', 'slate', 'custom'
  ];

  // Default accent matches the boot default ('cyan' == base 'azure').
  function currentAccent() {
    var a = root.getAttribute('data-accent');
    if (!a) a = readPref('accent', 'cyan');
    // 'cyan' and 'azure' are the same base bucket (no override) — normalize
    // so the base swatch (data-accent-choice="azure") reads as pressed.
    return a === 'cyan' ? 'azure' : a;
  }

  // ---- CUSTOM ACCENT: build a full family from TWO chosen colours --------
  // The user picks a PRIMARY colour and an optional SECONDARY (gradient) colour.
  // Primary → accent/strong/ink; secondary → accent-2 (+ accent-3 as its tint).
  // If no secondary is chosen, it auto-derives from the primary (hue +32°) so
  // single-colour use still works. All tuned per theme (brighter on dark glass,
  // deeper on light). aurora-settings.css' grouped rule then derives
  // soft/grad/title/glow/shadow — so a custom theme recolours the whole UI.
  // Persisted: 'cockpit:accentCustom' (primary), 'cockpit:accentCustom2'
  // (secondary), 'cockpit:accentCustom2set' ('1' once secondary is explicit).
  var CUSTOM_VARS = ['--j-accent', '--j-accent-strong', '--j-accent-2', '--j-accent-3', '--j-accent-ink'];
  var DEFAULT_CUSTOM = '#6c8cff';          // seed when "Custom" is opened with nothing saved
  var chipDot = null;                      // the "Custom" chip's gradient preview dot
  var studioEl = null, studioAA = null;    // studio panel + AA badge
  var studioSwatchP = null, studioHexP = null, studioSwatchS = null, studioHexS = null;
  var studioColorP = null, studioColorS = null; // hidden native pickers

  function clamp(n, lo, hi) { return n < lo ? lo : (n > hi ? hi : n); }

  function hexToRgb(hex) {
    var h = String(hex).replace('#', '').trim();
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    if (h.length !== 6 || /[^0-9a-f]/i.test(h)) return null;
    var n = parseInt(h, 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), h = 0, s = 0, l = (mx + mn) / 2;
    if (mx !== mn) {
      var d = mx - mn;
      s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
      if (mx === r) h = (g - b) / d + (g < b ? 6 : 0);
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h /= 6;
    }
    return { h: h * 360, s: s * 100, l: l * 100 };
  }
  function hslToHex(h, s, l) {
    h = ((h % 360) + 360) % 360; s = clamp(s, 0, 100) / 100; l = clamp(l, 0, 100) / 100;
    var c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2, r, g, b;
    if (h < 60) { r = c; g = x; b = 0; }
    else if (h < 120) { r = x; g = c; b = 0; }
    else if (h < 180) { r = 0; g = c; b = x; }
    else if (h < 240) { r = 0; g = x; b = c; }
    else if (h < 300) { r = x; g = 0; b = c; }
    else { r = c; g = 0; b = x; }
    function to(v) { return ('0' + Math.round((v + m) * 255).toString(16)).slice(-2); }
    return '#' + to(r) + to(g) + to(b);
  }
  function relLum(hex) {
    var c = hexToRgb(hex); if (!c) return 0;
    function f(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function normalizeHex(hex) {
    var c = hexToRgb(hex); if (!c) return null;
    function to(v) { return ('0' + v.toString(16)).slice(-2); }
    return '#' + to(c.r) + to(c.g) + to(c.b);
  }
  // WCAG contrast ratio between two hex colours (1..21).
  function contrastRatio(a, b) {
    var la = relLum(a), lb = relLum(b);
    var hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }
  // Pick the text colour for an accent fill: keep the conventional choice
  // (white on dark accents, near-black on light ones) when it already meets
  // AA, otherwise flip to whichever side reads better. Keeps buttons readable.
  function pickInk(accent, dark) {
    var darkInk = dark ? '#0f130a' : '#161009';
    var conventional = relLum(accent) > 0.5 ? darkInk : '#ffffff';
    var other = conventional === '#ffffff' ? darkInk : '#ffffff';
    if (contrastRatio(accent, conventional) >= 4.5) return conventional;
    return contrastRatio(accent, other) > contrastRatio(accent, conventional) ? other : conventional;
  }

  // a primary colour → a harmonious secondary (hue +32°), used when the user
  // hasn't explicitly chosen the second gradient colour.
  function autoSecondaryBase(primaryHex) {
    var rgb = hexToRgb(primaryHex); if (!rgb) return primaryHex;
    var hsl = rgbToHsl(rgb.r, rgb.g, rgb.b);
    return hslToHex(hsl.h + 32, clamp(hsl.s, 38, 96), hsl.l);
  }

  // primary + secondary base colours + theme → the 5 inline vars the grouped
  // rule consumes. Primary drives accent/strong/ink; secondary drives the
  // gradient end (accent-2) and a lighter tint of it becomes accent-3.
  function deriveCustomFamily(primaryHex, secondaryHex, theme) {
    var pr = hexToRgb(primaryHex), sr = hexToRgb(secondaryHex || primaryHex);
    if (!pr) return null;
    var p = rgbToHsl(pr.r, pr.g, pr.b), s = rgbToHsl(sr.r, sr.g, sr.b);
    var dark = theme === 'dark';
    var pS = clamp(p.s, 42, 96);                          // vivid enough to read as accent
    var pL = dark ? clamp(p.l, 58, 76) : clamp(p.l, 40, 52);
    var accent = hslToHex(p.h, pS, pL);
    var strong = hslToHex(p.h, pS, clamp(pL + (dark ? 9 : -9), 6, 94));
    var sS = clamp(s.s, 38, 96);
    var sL = dark ? clamp(s.l, 56, 74) : clamp(s.l, 42, 54);
    var a2 = hslToHex(s.h, sS, sL);
    var a3 = hslToHex(s.h, clamp(sS - 12, 22, 96), clamp(sL + (dark ? 8 : 6), 14, 92));
    var ink = pickInk(accent, dark);
    return { accent: accent, strong: strong, a2: a2, a3: a3, ink: ink };
  }

  function setCustomVars(fam) {
    root.style.setProperty('--j-accent', fam.accent);
    root.style.setProperty('--j-accent-strong', fam.strong);
    root.style.setProperty('--j-accent-2', fam.a2);
    root.style.setProperty('--j-accent-3', fam.a3);
    root.style.setProperty('--j-accent-ink', fam.ink);
  }
  // inline style overrides stylesheet presets, so clear it when leaving custom.
  function clearCustomVars() {
    CUSTOM_VARS.forEach(function (p) { root.style.removeProperty(p); });
  }
  function currentTheme() {
    return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }
  // Reflect the active custom theme across the studio UI: chip gradient dot,
  // the two swatches + hex fields, and the live AA contrast badge.
  function syncStudioUI(primary, secondary) {
    var fam = deriveCustomFamily(primary, secondary, currentTheme());
    if (chipDot) {
      chipDot.style.background = fam
        ? 'linear-gradient(135deg, ' + fam.accent + ' 0%, ' + fam.a2 + ' 100%)'
        : '';
    }
    if (studioSwatchP) studioSwatchP.style.background = primary;
    if (studioSwatchS) studioSwatchS.style.background = secondary;
    if (studioHexP && document.activeElement !== studioHexP) studioHexP.value = primary;
    if (studioHexS && document.activeElement !== studioHexS) studioHexS.value = secondary;
    // keep the hidden native pickers in step (hex edits / Surprise / boot) so
    // they open at the live colour, not whatever was last picked natively.
    if (studioColorP && document.activeElement !== studioColorP) studioColorP.value = primary;
    if (studioColorS && document.activeElement !== studioColorS) studioColorS.value = secondary;
    if (studioAA && fam) {
      var r = contrastRatio(fam.accent, fam.ink);
      var pass = r >= 4.5;
      studioAA.textContent = (pass ? 'AA ✓ ' : 'AA ✗ ') + r.toFixed(1) + ':1';
      studioAA.classList.toggle('ts-aa-pass', pass);
      studioAA.classList.toggle('ts-aa-fail', !pass);
      studioAA.title = pass
        ? _t('ui.aurora_aa_pass')
        : _t('ui.aurora_aa_fail');
    }
  }

  // ---- custom state helpers (persisted bases) ----
  function readBaseP() { return normalizeHex(readPref('accentCustom', '')); }
  function readBaseS() { return normalizeHex(readPref('accentCustom2', '')); }
  function secExplicit() { return readPref('accentCustom2set', '') === '1'; }

  // compute + apply the inline vars and refresh the studio UI for the live
  // theme. Pure render — does NOT persist the bases (used by boot/theme-flip).
  function renderCustom(primary, secondary) {
    var fam = deriveCustomFamily(primary, secondary, currentTheme());
    if (!fam) return;
    setCustomVars(fam);
    root.setAttribute('data-accent', 'custom');
    persistCustomCss(primary, secondary); // keep the boot-paint cache in sync
    syncStudioUI(primary, secondary);
    syncAccentPressed();
  }

  // Pre-compute the derived family for BOTH themes and store it, so the inline
  // <head> bootstrap can paint the custom accent before first paint (no FOUC) —
  // without duplicating the colour math.
  function persistCustomCss(p, s) {
    try {
      writePref('accentCustomCss', JSON.stringify({
        light: deriveCustomFamily(p, s, 'light'),
        dark: deriveCustomFamily(p, s, 'dark')
      }));
    } catch (e) { /* storage/JSON unavailable — boot just falls back */ }
  }

  // user picked the PRIMARY colour. Secondary follows automatically until the
  // user explicitly sets it (then it stays put).
  function chosePrimary(hex) {
    var p = normalizeHex(hex); if (!p) return;
    var s = secExplicit() ? (readBaseS() || autoSecondaryBase(p)) : autoSecondaryBase(p);
    writePref('accent', 'custom');
    writePref('accentCustom', p);
    writePref('accentCustom2', s);
    renderCustom(p, s);
  }

  // user picked the SECONDARY (gradient) colour — locks it as explicit.
  function choseSecondary(hex) {
    var s = normalizeHex(hex); if (!s) return;
    var p = readBaseP() || DEFAULT_CUSTOM;
    writePref('accent', 'custom');
    writePref('accentCustom', p);
    writePref('accentCustom2', s);
    writePref('accentCustom2set', '1');
    renderCustom(p, s);
  }

  // re-activate custom from persisted bases (chip click / boot). If nothing was
  // ever picked, seed a pleasant default so the studio opens with a live theme.
  function reactivateCustom() {
    var p = readBaseP();
    if (!p) { chosePrimary(DEFAULT_CUSTOM); return; }
    // persist the switch — renderCustom is render-only, so without this a
    // reload would silently fall back to the previously saved preset.
    writePref('accent', 'custom');
    renderCustom(p, readBaseS() || autoSecondaryBase(p));
  }

  // "Surprise": a random but harmonious two-colour palette.
  function surpriseTheme() {
    var h = Math.floor(Math.random() * 360);
    var s = 70 + Math.floor(Math.random() * 18);  // 70–88
    var l = 50 + Math.floor(Math.random() * 10);  // 50–60
    var offsets = [30, 150, 195, 330];
    var off = offsets[Math.floor(Math.random() * offsets.length)];
    chosePrimary(hslToHex(h, s, l));
    choseSecondary(hslToHex(h + off, clamp(s - 6, 40, 96), l));
  }

  // "Reset": drop the custom theme and fall back to the default accent.
  function resetCustom() {
    ['accentCustom', 'accentCustom2', 'accentCustom2set', 'accentCustomCss'].forEach(function (k) {
      try { localStorage.removeItem('cockpit:' + k); } catch (e) { /* ignore */ }
    });
    clearCustomVars();
    writePref('accent', 'cyan');
    root.setAttribute('data-accent', 'cyan');
    if (chipDot) chipDot.style.background = '';
    if (studioSwatchP) studioSwatchP.style.background = '';
    if (studioSwatchS) studioSwatchS.style.background = '';
    syncAccentPressed();
  }

  // ---- 1. ACCENT PICKER --------------------------------------------------
  function injectExtraSwatches(row) {
    EXTRA_ACCENTS.forEach(function (a) {
      if (row.querySelector('.accent-swatch[data-accent-choice="' + a.choice + '"]')) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'accent-swatch';
      btn.setAttribute('data-accent-choice', a.choice);
      btn.setAttribute('aria-label', _t('ui.aurora_accent_label_prefix') + a.label);
      btn.setAttribute('aria-pressed', 'false');
      var dot = document.createElement('span');
      dot.className = 'accent-swatch-dot';
      dot.setAttribute('aria-hidden', 'true');
      dot.style.background = a.dot;
      var label = document.createElement('span');
      label.textContent = a.label;
      btn.appendChild(dot);
      btn.appendChild(label);
      row.appendChild(btn);
    });
  }

  // The "Custom" chip is a normal chip whose dot previews the current custom
  // gradient. Clicking it (via the row's delegated handler) activates custom
  // and reveals the Theme Studio panel below the picker.
  function injectCustomSwatch(row) {
    if (row.querySelector('.accent-swatch[data-accent-choice="custom"]')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'accent-swatch accent-swatch-custom';
    btn.setAttribute('data-accent-choice', 'custom');
    btn.setAttribute('aria-label', _t('ui.aurora_custom_swatch_aria'));
    btn.setAttribute('aria-pressed', 'false');
    var dot = document.createElement('span');
    dot.className = 'accent-swatch-dot';
    dot.setAttribute('aria-hidden', 'true');
    var baseP = readBaseP();
    if (baseP) {
      dot.style.background = 'linear-gradient(135deg, ' + baseP + ' 0%, '
        + (readBaseS() || autoSecondaryBase(baseP)) + ' 100%)';
    }
    var text = document.createElement('span');
    text.textContent = _t('ui.aurora_custom_label');
    btn.appendChild(dot);
    btn.appendChild(text);
    row.appendChild(btn);
    chipDot = dot;
  }

  // ---- THEME STUDIO: live preview + two swatch/hex fields + AA + actions ---
  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }

  // one colour control = a swatch (hosting a hidden native picker) + a hex field
  // ariaBase is now the full translated aria-label for the colour picker;
  // ariaHex is the full translated aria-label for the hex input.
  function buildStudioField(labelText, ariaBase, onPick, ariaHex) {
    var field = el('label', 'ts-field');
    field.appendChild(el('span', 'ts-field-label', labelText));
    var wrap = el('span', 'ts-input-wrap');
    var swatch = el('span', 'ts-swatch');
    var color = document.createElement('input');
    color.type = 'color';
    color.className = 'ts-color';
    color.setAttribute('aria-label', ariaBase);
    var hex = document.createElement('input');
    hex.type = 'text';
    hex.className = 'ts-hex';
    hex.maxLength = 7;
    hex.spellcheck = false;
    hex.placeholder = '#rrggbb';
    hex.setAttribute('aria-label', ariaHex || ariaBase);
    swatch.appendChild(color);
    wrap.appendChild(swatch);
    wrap.appendChild(hex);
    field.appendChild(wrap);
    color.addEventListener('input', function () { onPick(color.value); });
    hex.addEventListener('input', function () { var n = normalizeHex(hex.value); if (n) onPick(n); });
    hex.addEventListener('blur', function () { var n = normalizeHex(hex.value); if (n) hex.value = n; });
    return { field: field, swatch: swatch, color: color, hex: hex };
  }

  function injectThemeStudio(row) {
    if (document.querySelector('.aur-theme-studio')) return;
    var anchor = row.closest ? row.closest('.settings-block') : null;
    var pane = document.getElementById('settings-pane-appearance');
    if (!anchor && !pane) return;

    var studio = el('div', 'settings-block aur-theme-studio');
    studio.hidden = true;
    studio.appendChild(el('h3', 'settings-block-title', _t('ui.aurora_studio_title')));

    // live preview — every node uses the live accent tokens, so it recolours
    // instantly as the user edits (inline vars feed the grouped CSS rule).
    var prev = el('div', 'ts-preview');
    prev.setAttribute('aria-hidden', 'true');
    prev.appendChild(el('span', 'ts-preview-title', _t('ui.aurora_studio_preview_title')));
    prev.appendChild(el('span', 'ts-preview-bar'));
    var actions = el('span', 'ts-preview-actions');
    actions.appendChild(el('span', 'ts-preview-btn', _t('ui.aurora_studio_primary_btn')));
    actions.appendChild(el('span', 'ts-preview-soft', 'aA'));
    prev.appendChild(actions);
    studio.appendChild(prev);

    var controls = el('div', 'ts-controls');
    var fP = buildStudioField(_t('ui.aurora_studio_primary_field'), _t('ui.aurora_studio_primary_aria'), chosePrimary, _t('ui.aurora_studio_primary_hex_aria'));
    var fS = buildStudioField(_t('ui.aurora_studio_secondary_field'), _t('ui.aurora_studio_secondary_aria'), choseSecondary, _t('ui.aurora_studio_secondary_hex_aria'));
    controls.appendChild(fP.field);
    controls.appendChild(fS.field);
    studio.appendChild(controls);

    var foot = el('div', 'ts-foot');
    var aa = el('span', 'ts-aa');
    aa.setAttribute('role', 'status');
    var footActions = el('span', 'ts-foot-actions');
    var surprise = el('button', 'btn-ghost btn-sm ts-surprise', _t('ui.aurora_studio_surprise'));
    surprise.type = 'button';
    var reset = el('button', 'btn-ghost btn-sm ts-reset', _t('ui.aurora_studio_reset'));
    reset.type = 'button';
    footActions.appendChild(surprise);
    footActions.appendChild(reset);
    foot.appendChild(aa);
    foot.appendChild(footActions);
    studio.appendChild(foot);

    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(studio, anchor.nextSibling);
    else pane.appendChild(studio);

    studioEl = studio; studioAA = aa;
    studioSwatchP = fP.swatch; studioHexP = fP.hex; studioColorP = fP.color;
    studioSwatchS = fS.swatch; studioHexS = fS.hex; studioColorS = fS.color;
    surprise.addEventListener('click', surpriseTheme);
    reset.addEventListener('click', resetCustom);

    // seed the controls from any saved theme so they aren't empty on first open
    var baseP = readBaseP();
    if (baseP) {
      var baseS = readBaseS() || autoSecondaryBase(baseP);
      fP.color.value = baseP; fS.color.value = baseS;
      fP.hex.value = baseP; fS.hex.value = baseS;
      fP.swatch.style.background = baseP; fS.swatch.style.background = baseS;
    }
  }

  // Re-derive the custom family when the theme flips — dark glass and light
  // surfaces want different lightness for the same chosen hue.
  function watchThemeForCustom() {
    var obs = new MutationObserver(function () {
      if (root.getAttribute('data-accent') !== 'custom') return;
      var p = readBaseP();
      if (p) renderCustom(p, readBaseS() || autoSecondaryBase(p));
    });
    obs.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  }

  // On boot, re-apply a persisted custom theme (works app-wide, even on pages
  // without the picker — the inline vars + grouped rule do the recolouring).
  function applyPersistedCustomAccent() {
    if (readPref('accent', '') !== 'custom') return;
    var p = readBaseP();
    if (p) renderCustom(p, readBaseS() || autoSecondaryBase(p));
  }

  // Re-append every known chip in ACCENT_ORDER so the row reads as a spectrum.
  // appendChild moves existing nodes (no clone), so this only reorders.
  function reorderSwatches(row) {
    ACCENT_ORDER.forEach(function (choice) {
      var sw = row.querySelector('.accent-swatch[data-accent-choice="' + choice + '"]');
      if (sw) row.appendChild(sw);
    });
  }

  function syncAccentPressed() {
    var active = currentAccent();
    var swatches = document.querySelectorAll('.accent-swatch[data-accent-choice]');
    Array.prototype.forEach.call(swatches, function (sw) {
      var choice = sw.getAttribute('data-accent-choice');
      sw.setAttribute('aria-pressed', choice === active ? 'true' : 'false');
    });
    // the studio panel is visible only while the custom theme is active
    if (studioEl) studioEl.hidden = active !== 'custom';
  }

  function wireAccentPicker() {
    var row = document.querySelector('.accent-swatch-row');
    if (!row) return; // not on this page — no-op
    injectExtraSwatches(row);
    injectCustomSwatch(row);
    injectThemeStudio(row);
    reorderSwatches(row);
    watchThemeForCustom();

    // Single delegated listener — does not touch any existing listener.
    row.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest
        ? ev.target.closest('.accent-swatch[data-accent-choice]')
        : null;
      if (!btn || !row.contains(btn)) return;
      var choice = btn.getAttribute('data-accent-choice');
      if (!choice) return;
      if (choice === 'custom') {
        // re-activate the saved theme; if none yet, a bead's picker opens it
        reactivateCustom();
        return;
      }
      clearCustomVars(); // leaving custom → drop inline overrides so presets win
      root.setAttribute('data-accent', choice);
      writePref('accent', choice);
      syncAccentPressed();
    });

    syncAccentPressed();
  }

  // ---- 2. APPEARANCE TWEAKS (Atmosphere / Shape / Density) --------------
  var SEGMENTS = [
    {
      key: 'atmos', prop: 'atmos', titleKey: 'ui.aurora_seg_atmos_title', def: 'default',
      hintKey: 'ui.aurora_seg_atmos_hint',
      opts: [
        { value: 'calm',     labelKey: 'ui.aurora_seg_calm' },
        { value: 'default',  labelKey: 'ui.aurora_seg_balanced' },
        { value: 'cinematic',labelKey: 'ui.aurora_seg_cinematic' }
      ]
    },
    {
      key: 'shape', prop: 'shape', titleKey: 'ui.aurora_seg_shape_title', def: 'default',
      hintKey: 'ui.aurora_seg_shape_hint',
      opts: [
        { value: 'soft',    labelKey: 'ui.aurora_seg_soft' },
        { value: 'default', labelKey: 'ui.aurora_seg_balanced' },
        { value: 'sharp',   labelKey: 'ui.aurora_seg_sharp' }
      ]
    },
    {
      key: 'density', prop: 'density', titleKey: 'ui.aurora_seg_density_title', def: 'default',
      hintKey: 'ui.aurora_seg_density_hint',
      opts: [
        { value: 'default', labelKey: 'ui.aurora_seg_spacious' },
        { value: 'compact', labelKey: 'ui.aurora_seg_compact' }
      ]
    }
  ];

  function applySegment(seg, value) {
    if (value && value !== seg.def) {
      root.dataset[seg.prop] = value;
    } else {
      // default → remove attribute entirely (defaults must look unchanged)
      delete root.dataset[seg.prop];
    }
    writePref(seg.key, value || seg.def);
  }

  function applyPersistedSegments() {
    SEGMENTS.forEach(function (seg) {
      var v = readPref(seg.key, seg.def);
      if (v && v !== seg.def) {
        root.dataset[seg.prop] = v;
      }
    });
  }

  function buildSegmentBlock(seg) {
    var block = document.createElement('div');
    block.className = 'settings-block aur-tweak-block';

    var segTitle = _t(seg.titleKey || seg.key);
    var title = document.createElement('h3');
    title.className = 'settings-block-title';
    title.textContent = segTitle;
    block.appendChild(title);

    var group = document.createElement('div');
    group.className = 'aur-seg';
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', segTitle);

    var current = readPref(seg.key, seg.def) || seg.def;

    seg.opts.forEach(function (opt) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'aur-seg-btn';
      btn.setAttribute('data-aur-seg', seg.key);
      btn.setAttribute('data-aur-value', opt.value);
      btn.setAttribute('aria-pressed', opt.value === current ? 'true' : 'false');
      btn.textContent = _t(opt.labelKey || opt.value);
      group.appendChild(btn);
    });

    group.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest
        ? ev.target.closest('.aur-seg-btn[data-aur-value]')
        : null;
      if (!btn || !group.contains(btn)) return;
      var value = btn.getAttribute('data-aur-value');
      applySegment(seg, value);
      var btns = group.querySelectorAll('.aur-seg-btn');
      Array.prototype.forEach.call(btns, function (b) {
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
    });

    block.appendChild(group);

    if (seg.hintKey) {
      var hint = document.createElement('p');
      hint.className = 'field-hint';
      hint.textContent = _t(seg.hintKey);
      block.appendChild(hint);
    }

    return block;
  }

  function injectAppearanceTweaks() {
    var pane = document.getElementById('settings-pane-appearance');
    if (!pane) return; // not on this page — no-op
    if (pane.querySelector('.aur-tweak-block')) return; // already injected

    // Anchor after the "Arayüz" (UI) settings block (the one holding #settings-compact-log).
    var anchor = null;
    var compactCb = pane.querySelector('#settings-compact-log');
    if (compactCb && compactCb.closest) anchor = compactCb.closest('.settings-block');
    if (!anchor) {
      // fall back to the accent-swatch-row's block, else pane end
      var accentRow = pane.querySelector('.accent-swatch-row');
      if (accentRow && accentRow.closest) anchor = accentRow.closest('.settings-block');
    }

    SEGMENTS.forEach(function (seg) {
      var block = buildSegmentBlock(seg);
      if (anchor && anchor.parentNode) {
        anchor.parentNode.insertBefore(block, anchor.nextSibling);
        anchor = block; // keep appending in order after the previous insert
      } else {
        pane.appendChild(block);
      }
    });
  }

  // ---- boot --------------------------------------------------------------
  function init() {
    // Persisted appearance tweaks must apply even if the settings pane is
    // absent on this page (e.g. memory studio).
    applyPersistedSegments();
    wireAccentPicker();
    applyPersistedCustomAccent();
    injectAppearanceTweaks();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
