"""Tailscale integration — detect-and-guide + Serve/Funnel control.

Tailscale is an OPTIONAL, out-of-band dependency: it is a separate binary the
user installs, logs into, and runs. This module never assumes it is present.
Every entry point degrades to an ``installed: false`` (or a specific error)
payload with an actionable ``guidance`` string instead of raising, so the
settings panel can render a step-by-step state machine:

    not installed        → install link (platform-aware)
    installed, logged out → "run `tailscale up`"
    logged in             → mode selector off / serve / funnel + https URL + QR

All subprocess calls use argument LISTS (never ``shell=True``), a hard 5s
timeout, and capture stderr so common failure modes (not logged in, funnel not
enabled on the tailnet) map to a concrete ``guidance`` string.

Parsing is DEFENSIVE — the JSON/text shape of ``tailscale status`` and
``tailscale serve status`` has drifted across CLI versions, so every field is
read with ``.get`` and missing/garbage values fall back rather than throw.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["get_status", "set_serve", "find_binary"]

#: Hard ceiling for any single tailscale CLI call. The CLI talks to a local
#: daemon, so it is normally instant; a hung/absent daemon must not wedge a
#: request thread.
_CMD_TIMEOUT = 5.0

#: Windows default install path, used when ``tailscale`` is not on PATH (the
#: installer does not always add it, especially for a non-elevated shell).
_WINDOWS_DEFAULT = r"C:\Program Files\Tailscale\tailscale.exe"

#: Platform-aware install/help URLs surfaced in ``guidance`` when the CLI is
#: absent. The generic download page covers every platform.
_INSTALL_URL = "https://tailscale.com/download"
_FUNNEL_DOCS_URL = "https://tailscale.com/kb/1223/funnel"


def find_binary() -> str | None:
    """Locate the ``tailscale`` CLI, or ``None`` if it is not installed.

    Prefers PATH (``shutil.which``); on Windows falls back to the default
    installer location, which the installer does not always add to PATH.
    """
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform.startswith("win"):
        import os

        if os.path.isfile(_WINDOWS_DEFAULT):
            return _WINDOWS_DEFAULT
    return None


def _install_guidance() -> str:
    """Platform-aware 'how to install' line for the not-installed state."""
    if sys.platform.startswith("win"):
        plat = "Windows"
    elif sys.platform == "darwin":
        plat = "macOS"
    else:
        plat = "Linux"
    return f"Tailscale is not installed. Install it for {plat}: {_INSTALL_URL}"


async def _run(binary: str, *args: str) -> tuple[int, str, str]:
    """Run ``binary`` with ``args`` (list form, no shell), 5s hard timeout.

    Returns ``(returncode, stdout, stderr)``. On timeout the child is killed and
    ``(-1, "", "<timeout message>")`` is returned so callers map it to guidance
    rather than hanging. NEVER uses ``shell=True`` (arg list only).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:  # binary vanished between checks
        return (-1, "", str(e))
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_CMD_TIMEOUT
        )
    except (TimeoutError, asyncio.TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Reap so we don't leak a zombie / unawaited transport.
        try:
            await proc.wait()
        except Exception:  # pragma: no cover - best-effort reap
            pass
        return (-1, "", f"tailscale {' '.join(args)} timed out after {_CMD_TIMEOUT}s")
    out = (stdout_b or b"").decode("utf-8", errors="replace")
    err = (stderr_b or b"").decode("utf-8", errors="replace")
    return (proc.returncode if proc.returncode is not None else -1, out, err)


def _parse_status_json(raw: str) -> dict[str, Any]:
    """Parse ``tailscale status --json`` defensively into the fields we surface.

    Returns a partial dict; unknown/garbage JSON yields empty defaults rather
    than raising. Fields extracted: ``backend_state``, ``logged_in``,
    ``self_dns_name`` (trailing dot stripped), ``tailscale_ips``.
    """
    result: dict[str, Any] = {
        "backend_state": None,
        "logged_in": False,
        "self_dns_name": None,
        "tailscale_ips": [],
    }
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return result
    if not isinstance(data, dict):
        return result

    backend = data.get("BackendState")
    if isinstance(backend, str):
        result["backend_state"] = backend
        # "Running" is the only state where the node is up + authenticated.
        result["logged_in"] = backend == "Running"

    self_node = data.get("Self")
    if isinstance(self_node, dict):
        dns = self_node.get("DNSName")
        if isinstance(dns, str) and dns:
            result["self_dns_name"] = dns.rstrip(".")
        ips = self_node.get("TailscaleIPs")
        if isinstance(ips, list):
            result["tailscale_ips"] = [ip for ip in ips if isinstance(ip, str)]
    return result


def _map_serve_error(stderr: str) -> str | None:
    """Map a known ``tailscale serve/funnel`` stderr to actionable guidance.

    Returns ``None`` when the stderr is not one of the recognised cases (the
    caller then surfaces the raw stderr). Case-insensitive substring matching —
    the exact wording drifts between CLI versions.
    """
    low = (stderr or "").lower()
    if not low.strip():
        return None
    if "not logged in" in low or "logged out" in low or "needs login" in low:
        return "Not logged in to Tailscale. Run `tailscale up` first."
    if "funnel" in low and ("not enabled" in low or "not allowed" in low or "denied" in low):
        return (
            "Funnel is not enabled for this tailnet. Enable the Funnel node "
            f"attribute in the admin console, then retry: {_FUNNEL_DOCS_URL}"
        )
    if "https" in low and "disabled" in low:
        return (
            "HTTPS certificates are disabled for this tailnet. Enable HTTPS in "
            "the Tailscale admin console (DNS → HTTPS Certificates)."
        )
    if "permission denied" in low or "access is denied" in low:
        return (
            "Permission denied talking to the Tailscale daemon. On Windows run "
            "the app elevated; on Linux ensure your user can reach tailscaled."
        )
    return None


async def get_status() -> dict[str, Any]:
    """Full Tailscale state for the settings panel.

    Shape (always present keys)::

        installed        bool
        backend_state    str | None   ("Running", "NeedsLogin", "Stopped", …)
        logged_in        bool
        self_dns_name    str | None    (MagicDNS name, no trailing dot)
        tailscale_ips    list[str]
        serve_active     bool
        funnel_active    bool
        https_url        str | None    ("https://<self_dns_name>")
        error            str | None
        guidance         str | None    (actionable next step, may be None)

    Never raises: an absent CLI returns ``installed: false`` with install
    guidance; a daemon/parse failure returns ``error`` + guidance.
    """
    base: dict[str, Any] = {
        "installed": False,
        "backend_state": None,
        "logged_in": False,
        "self_dns_name": None,
        "tailscale_ips": [],
        "serve_active": False,
        "funnel_active": False,
        "https_url": None,
        "error": None,
        "guidance": None,
    }

    binary = find_binary()
    if not binary:
        base["guidance"] = _install_guidance()
        return base
    base["installed"] = True

    rc, out, err = await _run(binary, "status", "--json")
    if rc != 0 or not out.strip():
        # A non-zero exit here is usually a stopped/never-configured daemon.
        base["error"] = (err or out or "tailscale status failed").strip()
        base["guidance"] = (
            _map_serve_error(err)
            or "Tailscale is installed but not running. Run `tailscale up` to log in."
        )
        return base

    parsed = _parse_status_json(out)
    base.update(
        {
            "backend_state": parsed["backend_state"],
            "logged_in": parsed["logged_in"],
            "self_dns_name": parsed["self_dns_name"],
            "tailscale_ips": parsed["tailscale_ips"],
        }
    )

    if not base["logged_in"]:
        base["guidance"] = "Tailscale is installed but not logged in. Run `tailscale up`."
        return base

    if base["self_dns_name"]:
        base["https_url"] = f"https://{base['self_dns_name']}"

    # Serve/Funnel state is best-effort: a missing/older CLI subcommand must not
    # blank out the rest of the (already valid) status.
    serve = await _serve_state(binary)
    base["serve_active"] = serve["serve_active"]
    base["funnel_active"] = serve["funnel_active"]
    return base


async def _serve_state(binary: str) -> dict[str, bool]:
    """Best-effort read of whether Serve/Funnel is currently active.

    Tries ``tailscale serve status --json`` first (newer CLIs), then falls back
    to the plain-text ``tailscale serve status``. Any failure → both False.
    """
    result = {"serve_active": False, "funnel_active": False}

    rc, out, _err = await _run(binary, "serve", "status", "--json")
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            # TCP/Web entries present → something is being served.
            web = data.get("Web")
            tcp = data.get("TCP")
            if (isinstance(web, dict) and web) or (isinstance(tcp, dict) and tcp):
                result["serve_active"] = True
            funnel = data.get("AllowFunnel")
            if isinstance(funnel, dict) and any(bool(v) for v in funnel.values()):
                result["funnel_active"] = True
            return result

    # Text fallback: `tailscale serve status` prints the current config; an empty
    # config prints a "No serve config"-style line.
    _rc2, out2, _e2 = await _run(binary, "serve", "status")
    text = (out2 or "").strip()
    low = text.lower()
    if text and "no serve config" not in low and "no config" not in low:
        result["serve_active"] = True
        if "funnel" in low or "(funnel)" in low:
            result["funnel_active"] = True
    return result


async def set_serve(port: int, mode: str) -> dict[str, Any]:
    """Turn Tailscale Serve/Funnel on or off in front of ``127.0.0.1:<port>``.

    ``mode``:
        * ``"off"``    — reset BOTH serve and funnel.
        * ``"serve"``  — expose on the tailnet (private, HTTPS) via ``serve``.
        * ``"funnel"`` — expose on the PUBLIC internet via ``funnel``.

    Uses the persistent ``--bg`` form so the proxy survives the CLI call.
    Returns ``{ok, mode, error, guidance}``; never raises. Known stderr cases
    (not logged in, funnel not enabled) map to a concrete ``guidance``.
    """
    out: dict[str, Any] = {"ok": False, "mode": mode, "error": None, "guidance": None}

    if mode not in ("off", "serve", "funnel"):
        out["error"] = f"invalid mode: {mode!r}"
        return out

    binary = find_binary()
    if not binary:
        out["error"] = "tailscale is not installed"
        out["guidance"] = _install_guidance()
        return out

    try:
        port = int(port)
    except (TypeError, ValueError):
        out["error"] = f"invalid port: {port!r}"
        return out
    if not (1 <= port <= 65535):
        out["error"] = f"port out of range: {port}"
        return out

    target = f"http://127.0.0.1:{port}"

    if mode == "off":
        # Reset BOTH surfaces — a prior funnel and a prior serve are independent.
        rc_s, _o_s, err_s = await _run(binary, "serve", "--bg", "reset")
        rc_f, _o_f, err_f = await _run(binary, "funnel", "--bg", "reset")
        # `funnel reset` may not exist on older CLIs; a serve reset alone already
        # tears the proxy down, so tolerate a funnel-reset failure.
        if rc_s != 0:
            out["error"] = (err_s or "serve reset failed").strip()
            out["guidance"] = _map_serve_error(err_s)
            return out
        _ = (rc_f, err_f)
        out["ok"] = True
        return out

    subcmd = "serve" if mode == "serve" else "funnel"
    rc, _o, err = await _run(binary, subcmd, "--bg", target)
    if rc != 0:
        out["error"] = (err or f"tailscale {subcmd} failed").strip()
        out["guidance"] = _map_serve_error(err)
        return out
    out["ok"] = True
    return out
