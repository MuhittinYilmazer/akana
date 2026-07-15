"""Cross-platform accessibility PERCEPTION for computer-control — the a11y tree + ref layer.

Turns the OS accessibility API into a compact, model-sized element tree with stable,
clickable refs, so the model can ``read_screen`` and then ``click_ref("w1e5")`` instead
of eyeballing pixel coordinates off a screenshot PNG (which, additionally, only the
Claude CLI path can even see — the text tree works on EVERY provider).

Ported from the browser-pack ``read_page``/ref pattern (Playwright MCP), adapted to the
desktop: only INTERACTABLE nodes get a ref, refs are minted per element and memoized by
identity so the same control keeps its ref across snapshots, the tree renders as indented
text lines ending in ``[ref=wNeM]``, and a per-snapshot registry maps ``ref -> rect``.

Staleness model (weaker than the browser's live DOM re-resolve — be honest about it): a
ref resolves only while it belongs to the CURRENT snapshot, so a ref from a SUPERSEDED
``read_screen`` is refused with a "re-snapshot" error. But between snapshots the registry
holds a captured RECT, not a live element handle — if the UI changes and the model acts
WITHOUT re-reading, the click lands on that remembered rect. The operating rule (enforced
in SKILL.md, not by the server) is therefore: re-run ``read_screen`` after any action that
alters the UI. A future revision may re-locate the element by identity at action time.

Backends are platform-selected and LAZY-imported (Windows → UI Automation via the
``uiautomation`` package; Linux → AT-SPI via ``pyatspi``), mirroring ``__main__``'s
``_BackendMissing``/``_INSTALL_HINT`` discipline: import + ``tools/list`` stay green on a
machine without an accessibility backend; the missing backend only surfaces when a
perception tool is actually called.

Coordinate space: element rectangles from UIA (``BoundingRectangle``) and AT-SPI
(``get_extents``) are ABSOLUTE virtual-desktop physical pixels — the SAME space pyautogui
clicks in — so a ref's stored rect is clicked directly, WITHOUT the screenshot-relative
``_abs_xy`` rebase. The process is made DPI-aware at startup (``enable_dpi_awareness``) so
those rects and pyautogui/mss agree on scaled multi-monitor Windows setups.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable

#: Install hint for the accessibility backends (distinct from __main__'s GUI-backend hint
#: because a machine can have pyautogui/mss yet lack the a11y layer).
A11Y_INSTALL_HINT = (
    "computer-control PERCEPTION backend is not installed. Install the accessibility "
    "layer for this OS: Windows → `pip install uiautomation` (or add it via "
    "requirements-computer.txt); Linux → the AT-SPI Python bindings "
    "(`python3-pyatspi` / `gir1.2-atspi-2.0`, plus a running at-spi bus and "
    "'Enable accessibility' in the desktop). Perception is optional; the pixel tools "
    "(screenshot + click x,y) keep working without it."
)

#: The maximum number of interactable refs a single snapshot mints. A full desktop tree
#: is enormous; the browser-pack pattern keeps snapshots model-sized by scoping + capping.
#: Beyond this the walk stops adding refs (the tree is truncated with a note).
_MAX_REFS = 400

#: Hard cap on TOTAL nodes visited by a single snapshot walk. A browser window's AT-SPI
#: tree can be tens of thousands of accessibles, each visit a D-Bus round-trip AND, since
#: mcp runs sync tools inline on the event loop, a freeze of the whole MCP server. The
#: walk stops descending once this many nodes have been visited (the tree is truncated
#: with a note) — the single most important guard for the Linux/AT-SPI path.
_MAX_NODES = 2500

#: Default depth cap for the tree walk (root window = depth 0). Deep control hierarchies
#: (nested panes) rarely add addressable value past this and blow up the token count.
_DEFAULT_MAX_DEPTH = 40

#: Cap on the rendered tree text (chars). Even under the node budget a wide tree can be
#: large; keep read_screen's payload model-sized.
_MAX_TREE_CHARS = 24000

#: An identity's ref is forgotten if it has not appeared in this many consecutive
#: snapshots — bounds the memo on a long-running stdio server (identities churn, esp. on
#: AT-SPI where index-chain identities shift as apps launch/quit).
_MEMO_TTL_SNAPSHOTS = 8

#: Safety cap on the AT-SPI parent-chain walk — a misbehaving toolkit can expose a parent
#: cycle (a known AT-SPI hazard), which would otherwise hang the event loop forever.
_MAX_ANCESTRY = 64


class A11yUnavailable(RuntimeError):
    """The OS accessibility backend could not be loaded/used — surfaced as a tool error."""


class A11yWindowNotFound(RuntimeError):
    """A ``window=`` scope was given but no matching window is open (distinct from an
    unavailable backend so read_screen can say 'no window matched' rather than suggest a
    screenshot fallback or return a misleading empty tree)."""


@dataclass(slots=True)
class A11yNode:
    """One accessibility element. ``ref`` is set only for INTERACTABLE, on-screen nodes.

    ``rect`` is ``(x, y, w, h)`` in absolute virtual-desktop physical pixels (or ``None``
    when the backend cannot resolve bounds). ``identity`` is a backend-stable key (UIA
    RuntimeId / AT-SPI path) used to memoize a ref across snapshots.
    """

    role: str
    name: str = ""
    states: tuple[str, ...] = ()
    rect: tuple[int, int, int, int] | None = None
    identity: str = ""
    interactable: bool = False
    ref: str | None = None
    children: list["A11yNode"] = field(default_factory=list)

    def center(self) -> tuple[int, int] | None:
        if not self.rect:
            return None
        x, y, w, h = self.rect
        return int(x + w / 2), int(y + h / 2)


@dataclass(slots=True)
class RefEntry:
    """Registry record for a live ref: enough to click it + re-verify it later."""

    ref: str
    role: str
    name: str
    rect: tuple[int, int, int, int]
    identity: str
    window_id: int
    snapshot_id: int


class RefRegistry:
    """Assigns + resolves ``wNeM`` refs across snapshots.

    A ref is ``w{window_index}e{element_counter}``. Refs are memoized by ``identity`` so a
    control that survives into the next snapshot keeps the same ref (stable addressing,
    like the browser pattern's ``element._ariaRef``). ``resolve`` returns the entry only
    while it belongs to the CURRENT snapshot — a ref from a superseded snapshot is treated
    as stale (the desktop may have changed under it), forcing an explicit re-``read_screen``.
    """

    def __init__(self) -> None:
        self._by_ref: dict[str, RefEntry] = {}
        self._ref_for_identity: dict[str, str] = {}
        self._identity_seen: dict[str, int] = {}  # identity → last snapshot_id it appeared in
        self._used_this_snapshot: set[str] = set()
        self._elem_counter = 0
        self._snapshot_id = 0

    def begin_snapshot(self) -> int:
        self._snapshot_id += 1
        self._by_ref.clear()  # only the current snapshot's refs are resolvable
        self._used_this_snapshot.clear()
        # Evict memo entries not seen within the TTL window → the identity memo stays
        # bounded on a long-running stdio server (finding: unbounded growth).
        cutoff = self._snapshot_id - _MEMO_TTL_SNAPSHOTS
        stale = [idn for idn, seen in self._identity_seen.items() if seen < cutoff]
        for idn in stale:
            self._identity_seen.pop(idn, None)
            self._ref_for_identity.pop(idn, None)
        return self._snapshot_id

    def assign(self, node: A11yNode, window_id: int) -> str:
        """Mint (or reuse) a ref for ``node`` and record it for the current snapshot."""
        idn = node.identity
        ref = self._ref_for_identity.get(idn) if idn else None
        # Duplicate identity (two DISTINCT elements sharing a fallback identity like
        # role+name) must NOT collapse to one ref — that would let resolve() return the
        # second element's rect for a ref the model thinks points at the first. If the
        # memoized ref was already claimed this snapshot, mint a fresh one instead.
        if ref is not None and ref in self._used_this_snapshot:
            ref = None
        if ref is None:
            self._elem_counter += 1
            ref = f"w{window_id}e{self._elem_counter}"
            if idn and idn not in self._ref_for_identity:
                self._ref_for_identity[idn] = ref
        if idn:
            self._identity_seen[idn] = self._snapshot_id
        self._used_this_snapshot.add(ref)
        self._by_ref[ref] = RefEntry(
            ref=ref,
            role=node.role,
            name=node.name,
            rect=node.rect or (0, 0, 0, 0),
            identity=idn,
            window_id=window_id,
            snapshot_id=self._snapshot_id,
        )
        return ref

    def resolve(self, ref: str) -> RefEntry | None:
        """Return the entry IFF it is from the current snapshot, else None (stale/unknown)."""
        entry = self._by_ref.get(str(ref or "").strip())
        if entry is None or entry.snapshot_id != self._snapshot_id:
            return None
        return entry


# ── Tree rendering (compact, indented, model-sized) ─────────────────────────────

def render_tree(root: A11yNode, *, include_box: bool = False) -> str:
    """Render an ``A11yNode`` tree as indented text lines (the ``read_page`` analogue).

    ``- role "name" [state] [state] [ref=wNeM] [box=x,y,w,h]``. Nodes without a name and
    without a ref that carry no addressable children are omitted to keep the tree lean.
    """
    lines: list[str] = []

    total = [0]  # rendered char count (list = mutable across the closure)
    truncated = [False]

    def walk(node: A11yNode, depth: int) -> None:
        if total[0] >= _MAX_TREE_CHARS:
            truncated[0] = True
            return
        parts = [f"- {node.role}"]
        if node.name:
            nm = node.name if len(node.name) <= 80 else node.name[:77] + "…"
            parts.append(f'"{nm}"')
        for st in node.states:
            parts.append(f"[{st}]")
        if node.ref:
            parts.append(f"[ref={node.ref}]")
        if include_box and node.rect:
            x, y, w, h = node.rect
            parts.append(f"[box={x},{y},{w},{h}]")
        line = "  " * depth + " ".join(parts)
        lines.append(line)
        total[0] += len(line) + 1
        for child in node.children:
            walk(child, depth + 1)

    walk(root, 0)
    if truncated[0]:
        lines.append("  … (tree truncated — narrow with `window` or a smaller max_depth)")
    return "\n".join(lines)


def flatten_refs(root: A11yNode) -> list[A11yNode]:
    """All ref-bearing nodes, pre-order (for find_element + counts)."""
    out: list[A11yNode] = []

    def walk(n: A11yNode) -> None:
        if n.ref:
            out.append(n)
        for c in n.children:
            walk(c)

    walk(root)
    return out


# ── DPI awareness (Windows) ─────────────────────────────────────────────────────

def enable_dpi_awareness() -> None:
    """Make this process per-monitor DPI-aware on Windows (no-op elsewhere).

    Without it, on a >100% scaled monitor UIA reports physical-pixel rects while a
    non-DPI-aware pyautogui/mss see virtualized coordinates — ref clicks would drift.
    Best-effort: any failure (older Windows, already set) is swallowed.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()  # legacy fallback
    except Exception:
        pass


# ── Backend selection ───────────────────────────────────────────────────────────

#: Test seam: when set, snapshot() uses this instead of the real OS backend. A backend is
#: any callable ``(scope: str | None, max_depth: int, include_offscreen: bool) -> A11yNode``
#: returning the (unref'd) element tree root. Kept module-level so tests can inject a
#: headless-safe fake without an accessibility bus.
_BACKEND_OVERRIDE: Callable[..., A11yNode] | None = None


def set_backend_override(fn: Callable[..., A11yNode] | None) -> None:
    global _BACKEND_OVERRIDE
    _BACKEND_OVERRIDE = fn


def _select_backend() -> Callable[..., A11yNode]:
    """Return the platform accessibility walker, or raise A11yUnavailable with the hint."""
    if _BACKEND_OVERRIDE is not None:
        return _BACKEND_OVERRIDE
    if sys.platform == "win32":
        return _uia_snapshot
    if sys.platform.startswith("linux"):
        return _atspi_snapshot
    raise A11yUnavailable(
        f"{A11Y_INSTALL_HINT} (no accessibility backend for platform {sys.platform!r})"
    )


def snapshot(
    scope: str | None = None,
    *,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    include_offscreen: bool = False,
    registry: RefRegistry,
) -> A11yNode:
    """Capture the a11y tree for ``scope`` (None/"foreground" = active window) and assign refs.

    Returns the root ``A11yNode`` with ``ref`` set on interactable, on-screen elements
    (registered in ``registry`` under a fresh snapshot id). Raises ``A11yUnavailable`` if
    the OS backend cannot be loaded/used.
    """
    walker = _select_backend()
    enable_dpi_awareness()
    root = walker(scope, max_depth, include_offscreen)
    _assign_refs(root, registry)
    return root


def _assign_refs(root: A11yNode, registry: RefRegistry) -> None:
    """Walk the raw tree; mint refs for interactable nodes, capping at _MAX_REFS."""
    registry.begin_snapshot()
    minted = 0
    # window_id increments per top-level window child so refs read wNeM across windows.
    def walk(node: A11yNode, window_id: int) -> None:
        nonlocal minted
        if node.interactable and node.rect and minted < _MAX_REFS:
            node.ref = registry.assign(node, window_id)
            minted += 1
        for child in node.children:
            walk(child, window_id)

    # If the root has multiple top-level window children, number them; else everything is w1.
    top = root.children if root.role in ("desktop", "root", "") and root.children else [root]
    for i, win in enumerate(top, start=1):
        walk(win, i)


# ── Windows UI Automation backend (uiautomation package) ────────────────────────

def _uia_snapshot(scope: str | None, max_depth: int, include_offscreen: bool) -> A11yNode:
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise A11yUnavailable(f"{A11Y_INSTALL_HINT} (uiautomation: {exc})") from exc

    # Interactable UIA control types (mirrors the browser 'receives pointer events' filter).
    interactable_types = {
        "ButtonControl", "CheckBoxControl", "RadioButtonControl", "ComboBoxControl",
        "EditControl", "HyperlinkControl", "ListItemControl", "MenuItemControl",
        "TabItemControl", "TreeItemControl", "SliderControl", "SplitButtonControl",
        "DocumentControl", "TextControl",
    }

    budget = [0]  # total nodes visited this snapshot (freeze guard; see _MAX_NODES)

    def state_flags(ctl: Any) -> tuple[str, ...]:
        flags: list[str] = []
        try:
            if not ctl.IsEnabled:
                flags.append("disabled")
        except Exception:
            pass
        try:
            sp = ctl.GetSelectionItemPattern()  # the real "selected" surface (not IsSelected)
            if sp is not None and sp.IsSelected:
                flags.append("selected")
        except Exception:
            pass
        try:
            tp = ctl.GetTogglePattern()
            if tp is not None:
                flags.append("checked" if tp.ToggleState == auto.ToggleState.On else "unchecked")
        except Exception:
            pass
        return tuple(flags)

    def rect_of(ctl: Any) -> tuple[int, int, int, int] | None:
        try:
            r = ctl.BoundingRectangle
            # OR, not AND: a 0-width OR 0-height element has no clickable point.
            if r is None or r.width() <= 0 or r.height() <= 0:
                return None
            return int(r.left), int(r.top), int(r.width()), int(r.height())
        except Exception:
            return None

    def convert(ctl: Any, depth: int) -> A11yNode | None:
        if budget[0] >= _MAX_NODES:
            return None
        budget[0] += 1
        try:
            ct = ctl.ControlTypeName
        except Exception:
            return None
        try:
            offscreen = bool(ctl.IsOffscreen)
        except Exception:
            offscreen = False
        if offscreen and not include_offscreen:
            return None
        name = ""
        try:
            name = (ctl.Name or "").strip()
        except Exception:
            pass
        rect = rect_of(ctl)
        interactable = (ct in interactable_types) and rect is not None
        node = A11yNode(
            role=ct.replace("Control", "") or "Element",
            name=name,
            states=state_flags(ctl),
            rect=rect,
            # Identity is only USED to memoize a ref, and only interactable nodes get refs;
            # skip the (COM) identity call for the vast majority of non-interactable nodes.
            identity=_uia_identity(ctl) if interactable else "",
            interactable=interactable,
        )
        if depth < max_depth and budget[0] < _MAX_NODES:
            try:
                for child in ctl.GetChildren():
                    cn = convert(child, depth + 1)
                    if cn is not None:
                        node.children.append(cn)
            except Exception:
                pass
        # Prune noise: an unnamed, non-interactable node with no useful descendants.
        if not node.interactable and not node.name and not node.children:
            return None
        return node

    root = A11yNode(role="desktop", name="")
    # Future-proof COM: mcp 1.28.1 runs sync tools inline on the (already CoInitialized)
    # event-loop thread, but a future FastMCP dispatching to a worker-thread pool would
    # raise CO_E_NOTINITIALIZED. The per-thread UIA initializer makes this survive that
    # (a harmless no-op on the already-initialized thread today).
    try:
        _com: Any = auto.UIAutomationInitializerInThread()
    except Exception:
        import contextlib

        _com = contextlib.nullcontext()
    with _com:
        try:
            if scope and scope != "foreground":
                # Case-insensitive top-level window match (UIA SubName is case-SENSITIVE);
                # no match → a distinct error, not a misleading empty "- desktop" tree.
                needle = scope.lower()
                targets = [
                    w for w in auto.GetRootControl().GetChildren()
                    if needle in (getattr(w, "Name", "") or "").lower()
                ]
            else:
                # GetForegroundControl() is already the top-level foreground WINDOW
                # (ControlFromHandle(GetForegroundWindow())) — no parent-climb needed.
                fg = auto.GetForegroundControl()
                targets = [fg] if fg is not None else []
        except Exception as exc:  # noqa: BLE001
            raise A11yUnavailable(f"UIA snapshot failed: {exc}") from exc
        if scope and scope != "foreground" and not targets:
            raise A11yWindowNotFound(f"no window matched {scope!r}")
        for win in targets:
            cn = convert(win, 0)
            if cn is not None:
                root.children.append(cn)
    return root


def _uia_identity(ctl: Any) -> str:
    try:
        rid = ctl.GetRuntimeId()
        if rid:
            return "uia:" + ",".join(str(x) for x in rid)
    except Exception:
        pass
    # Fallback identity: role + name + position (less stable but better than nothing).
    try:
        return f"uia:{ctl.ControlTypeName}:{ctl.Name}"
    except Exception:
        return ""


# ── Linux AT-SPI backend (pyatspi) ──────────────────────────────────────────────

def _atspi_snapshot(scope: str | None, max_depth: int, include_offscreen: bool) -> A11yNode:
    try:
        import pyatspi  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise A11yUnavailable(f"{A11Y_INSTALL_HINT} (pyatspi: {exc})") from exc

    interactable_roles = {
        "push button", "toggle button", "check box", "radio button", "combo box",
        "text", "entry", "link", "list item", "menu item", "tab", "tree item",
        "slider", "spin button", "password text",
    }

    budget = [0]  # total nodes visited this snapshot — THE Linux freeze guard (_MAX_NODES)

    def rect_of(acc: Any) -> tuple[int, int, int, int] | None:
        try:
            comp = acc.queryComponent()
            ext = comp.getExtents(pyatspi.DESKTOP_COORDS)
            # OR, not AND: a 0-width OR 0-height element has no clickable point.
            if ext.width <= 0 or ext.height <= 0:
                return None
            return int(ext.x), int(ext.y), int(ext.width), int(ext.height)
        except Exception:
            return None

    def state_flags(acc: Any) -> tuple[str, ...]:
        flags: list[str] = []
        try:
            ss = acc.getState()
            if not ss.contains(pyatspi.STATE_ENABLED) or not ss.contains(pyatspi.STATE_SENSITIVE):
                flags.append("disabled")
            if ss.contains(pyatspi.STATE_CHECKED):
                flags.append("checked")
            if ss.contains(pyatspi.STATE_SELECTED):
                flags.append("selected")
            if ss.contains(pyatspi.STATE_EXPANDED):
                flags.append("expanded")
        except Exception:
            pass
        return tuple(flags)

    def is_showing(acc: Any) -> bool:
        try:
            ss = acc.getState()
            return ss.contains(pyatspi.STATE_SHOWING) and ss.contains(pyatspi.STATE_VISIBLE)
        except Exception:
            return True

    def convert(acc: Any, depth: int) -> A11yNode | None:
        if budget[0] >= _MAX_NODES:
            return None
        budget[0] += 1
        if not include_offscreen and not is_showing(acc):
            return None
        try:
            role = acc.getRoleName()
        except Exception:
            role = "element"
        try:
            name = (acc.name or "").strip()
        except Exception:
            name = ""
        rect = rect_of(acc)
        interactable = (role in interactable_roles) and rect is not None
        node = A11yNode(
            role=role,
            name=name,
            states=state_flags(acc),
            rect=rect,
            # Identity is only used to memoize a ref, and only interactable nodes get one;
            # skip the (expensive, per-ancestor D-Bus) identity walk for the rest.
            identity=_atspi_identity(acc) if interactable else "",
            interactable=interactable,
        )
        if depth < max_depth and budget[0] < _MAX_NODES:
            try:
                for i in range(acc.childCount):
                    child = acc.getChildAtIndex(i)
                    if child is None:
                        continue
                    cn = convert(child, depth + 1)
                    if cn is not None:
                        node.children.append(cn)
                    if budget[0] >= _MAX_NODES:
                        break
            except Exception:
                pass
        if not node.interactable and not node.name and not node.children:
            return None
        return node

    root = A11yNode(role="desktop", name="")
    try:
        desktop = pyatspi.Registry.getDesktop(0)
    except Exception as exc:  # noqa: BLE001
        raise A11yUnavailable(f"AT-SPI snapshot failed (is the a11y bus running?): {exc}") from exc
    matched = False
    for i in range(desktop.childCount):
        app = desktop.getChildAtIndex(i)
        if app is None:
            continue
        try:
            for j in range(app.childCount):
                frame = app.getChildAtIndex(j)
                if frame is None:
                    continue
                # Scope filter: active frame only unless a title substring is given.
                if scope and scope != "foreground":
                    if scope.lower() not in (getattr(frame, "name", "") or "").lower():
                        continue
                    matched = True
                else:
                    try:
                        if not frame.getState().contains(pyatspi.STATE_ACTIVE):
                            continue
                    except Exception:
                        pass
                cn = convert(frame, 0)
                if cn is not None:
                    root.children.append(cn)
        except Exception:
            continue
        if budget[0] >= _MAX_NODES:
            break
    if scope and scope != "foreground" and not matched:
        raise A11yWindowNotFound(f"no window matched {scope!r}")
    return root


def _atspi_identity(acc: Any) -> str:
    """A stable-ish identity: <application name>:<child-index chain from the app root>.

    A bare index chain from the DESKTOP shifts whenever any app launches/quits (a
    different element would inherit an old ref); anchoring to the APPLICATION name keeps
    the chain meaningful across such churn. The walk is bounded (_MAX_ANCESTRY) because a
    misbehaving toolkit can expose a parent cycle, which would otherwise hang forever.
    """
    try:
        app = ""
        try:
            application = acc.get_application()
            app = str(getattr(application, "name", "") or application.getRoleName())
        except Exception:
            app = ""
        chain = [str(acc.getIndexInParent())]
        p = acc.parent
        hops = 0
        while p is not None and hops < _MAX_ANCESTRY:
            chain.append(str(p.getIndexInParent()))
            p = p.parent
            hops += 1
        return f"atspi:{app}:" + "/".join(reversed(chain))
    except Exception:
        try:
            return f"atspi:{acc.getRoleName()}:{acc.name}"
        except Exception:
            return ""
