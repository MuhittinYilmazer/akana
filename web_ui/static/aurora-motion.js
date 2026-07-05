/* =================================================================== */
/*  aurora-motion.js — pointer-driven micro-interaction layer          */
/*  ---------------------------------------------------------------    */
/*  JS counterpart to aurora-motion.css. Adds two compositor-only      */
/*  behaviours, both gated on (pointer:fine) + no (prefers-reduced-    */
/*  motion), both rAF-coalesced, both no-op if their targets/inputs    */
/*  are absent:                                                        */
/*    1. Ambient parallax — the cursor gently shifts the background    */
/*       nebula field. We write --par-x/--par-y (px) onto .app-bg;     */
/*       aurora-motion.css maps them onto the orbs + aurora curtain    */
/*       via the INDEPENDENT `translate` property, which composes with */
/*       each layer's drift `transform` animation (no conflict).       */
/*    2. Magnetic chips — prompt-chips ease toward the cursor while    */
/*       hovered and spring back on leave, via an inline `translate`   */
/*       that composes with their CSS `transform` hover lift.          */
/*                                                                     */
/*  CONTRACT: touches NO markup/class/id/--j-* names; only reads       */
/*  existing nodes and writes inline `translate` + two --par-* vars.   */
/*  Degrades to a perfectly static scene if JS/capabilities absent.    */
/* =================================================================== */
(function () {
  'use strict';

  var fineMQ = window.matchMedia('(pointer: fine)');
  var reduceMQ = window.matchMedia('(prefers-reduced-motion: reduce)');
  function motionOK() { return fineMQ.matches && !reduceMQ.matches; }

  var bg = document.querySelector('.app-bg');

  // ---- tunables ----------------------------------------------------------
  var PAR_AMP = 14;     // px — max background field shift at the viewport edge
  var PAR_EASE = 0.12;  // parallax follow (lower = lazier trail)
  var MAG_X = 8;        // px — max chip horizontal pull
  var MAG_Y = 6;        // px — max chip vertical pull
  var MAG_EASE = 0.18;  // magnetic follow / spring-back rate

  // ---- state -------------------------------------------------------------
  var pTar = { x: 0, y: 0 }, pCur = { x: 0, y: 0 };          // parallax target/current
  var mEl = null, mActive = false;                          // hovered chip + held?
  var mTar = { x: 0, y: 0 }, mCur = { x: 0, y: 0 };          // magnetic target/current
  var rafId = 0, running = false;

  function lerp(a, b, t) { return a + (b - a) * t; }

  function tick() {
    rafId = 0;

    // — parallax —
    pCur.x = lerp(pCur.x, pTar.x, PAR_EASE);
    pCur.y = lerp(pCur.y, pTar.y, PAR_EASE);
    if (bg) {
      bg.style.setProperty('--par-x', pCur.x.toFixed(2) + 'px');
      bg.style.setProperty('--par-y', pCur.y.toFixed(2) + 'px');
    }

    // — magnetic —
    mCur.x = lerp(mCur.x, mTar.x, MAG_EASE);
    mCur.y = lerp(mCur.y, mTar.y, MAG_EASE);
    if (mEl) {
      if (!mActive && Math.abs(mCur.x) < 0.06 && Math.abs(mCur.y) < 0.06) {
        mEl.style.translate = '';        // settled back home → drop inline style
        mEl = null;
      } else {
        mEl.style.translate = mCur.x.toFixed(2) + 'px ' + mCur.y.toFixed(2) + 'px';
      }
    }

    var pSettled = Math.abs(pCur.x - pTar.x) < 0.1 && Math.abs(pCur.y - pTar.y) < 0.1;
    if (pSettled && !mEl) { running = false; return; }   // fully idle → stop loop
    rafId = requestAnimationFrame(tick);
  }

  function kick() {
    if (!running && motionOK()) { running = true; rafId = requestAnimationFrame(tick); }
  }

  function onMove(e) {
    if (!motionOK()) return;
    var w = window.innerWidth || 1, h = window.innerHeight || 1;
    pTar.x = ((e.clientX / w) * 2 - 1) * PAR_AMP;
    pTar.y = ((e.clientY / h) * 2 - 1) * PAR_AMP;

    var chip = (e.target && e.target.closest) ? e.target.closest('.prompt-chip') : null;
    if (chip) {
      if (mEl && mEl !== chip) mEl.style.translate = '';   // hopped chips → reset old
      mEl = chip; mActive = true;
      var r = chip.getBoundingClientRect();
      var rx = (e.clientX - (r.left + r.width / 2)) / ((r.width / 2) || 1);
      var ry = (e.clientY - (r.top + r.height / 2)) / ((r.height / 2) || 1);
      mTar.x = Math.max(-1, Math.min(1, rx)) * MAG_X;
      mTar.y = Math.max(-1, Math.min(1, ry)) * MAG_Y;
    } else if (mEl) {
      mActive = false; mTar.x = 0; mTar.y = 0;             // left chip → spring home
    }
    kick();
  }

  function recenter() {            // cursor left the window → ease everything home
    pTar.x = 0; pTar.y = 0;
    if (mEl) { mActive = false; mTar.x = 0; mTar.y = 0; }
    kick();
  }

  function stopLoop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  }

  function hardReset() {           // reduced-motion / coarse → wipe inline state
    stopLoop();
    pTar.x = pTar.y = pCur.x = pCur.y = 0;
    mTar.x = mTar.y = mCur.x = mCur.y = 0; mActive = false;
    if (bg) { bg.style.removeProperty('--par-x'); bg.style.removeProperty('--par-y'); }
    if (mEl) { mEl.style.translate = ''; mEl = null; }
  }

  document.addEventListener('pointermove', onMove, { passive: true });
  document.documentElement.addEventListener('pointerleave', recenter);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopLoop();
  });

  function onCap() { if (!motionOK()) hardReset(); }
  if (fineMQ.addEventListener) {        // modern
    fineMQ.addEventListener('change', onCap);
    reduceMQ.addEventListener('change', onCap);
  } else if (fineMQ.addListener) {      // Safari < 14
    fineMQ.addListener(onCap);
    reduceMQ.addListener(onCap);
  }
})();
