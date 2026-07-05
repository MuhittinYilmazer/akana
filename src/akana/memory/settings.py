"""Memory settings — the owner's knobs, persisted per data dir.

The orchestrator's safety posture (K30 ``allow_direct``) and the vector recall
mode are *user* decisions, not LLM decisions: they live in
``<data_dir>/memory_settings.yaml`` and may be overridden per-process with
environment variables (``AKANA_MEMORY_ALLOW_DIRECT``, ``AKANA_MEMORY_VECTOR``).
Both the MCP server process and the HTTP API read through this module, so the
Studio toggle and the tool behaviour can't drift apart.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

__all__ = ["MemorySettings", "load_memory_settings", "save_memory_settings"]

log = logging.getLogger(__name__)

_FILE_NAME = "memory_settings.yaml"
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
_VECTOR_MODES = ("auto", "on", "off")
_EMBED_BACKENDS = ("local", "ollama", "off")


@dataclass(slots=True)
class MemorySettings:
    """Owner-controlled memory behaviour (see module docstring)."""

    #: K30: when False, LLM ``policy="direct"``/``supersedes`` degrade to the
    #: staging inbox. The owner may flip this to skip approvals entirely.
    allow_direct: bool = False
    #: Background auto-capture: after each turn a 2nd pass proposes facts to remember (they
    #: land in the Inbox). When False, only what the model explicitly saves via the memory tool
    #: is captured. (Auto-capture is always skipped for a turn where the model already wrote
    #: memory itself, so a fact is never captured twice.) No longer a Memory Studio toggle —
    #: it defaults ON for everyone; ``AKANA_MEMORY_LLM_CAPTURE=0`` is the env kill switch.
    auto_capture: bool = True
    #: Session summarization (SessionCloser M3.2 + cross-session consolidation M3.3). When OFF,
    #: BOTH the idle/long-chat summary cron AND the summary-consolidation cron are skipped — so
    #: the flag cleanly means "no session-summary inbox activity". No longer a Memory Studio
    #: toggle — it defaults ON for everyone; the runtime ``session_closer_enabled`` (env
    #: ``AKANA_SESSION_CLOSER_ENABLED``) remains the env/advanced master switch for the closer,
    #: and both must be on for summaries to run.
    session_summary: bool = True
    #: Vector recall: "auto" probes Ollama and enables when reachable;
    #: "on" requires it (fails loudly to log); "off" keeps keyword-only.
    vector: str = "auto"
    #: Embedding backend (when vector is on/auto): "local" (fastembed/ONNX, NO
    #: Ollama — the default; the owner does not want Ollama), "ollama" (local
    #: daemon), "off".
    embed_backend: str = "local"
    local_embed_model: str = ""  # empty → fastembed default (e5-small, multilingual)
    ollama_url: str = "http://localhost:11434"
    embed_model: str = "bge-m3"


def _settings_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / _FILE_NAME


def load_memory_settings(data_dir: Path) -> MemorySettings:
    """YAML file (if any) + env overrides; unknown fields are ignored."""
    s = MemorySettings()
    path = _settings_path(data_dir)
    try:
        if path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                if isinstance(raw.get("allow_direct"), bool):
                    s.allow_direct = raw["allow_direct"]
                if isinstance(raw.get("auto_capture"), bool):
                    s.auto_capture = raw["auto_capture"]
                if isinstance(raw.get("session_summary"), bool):
                    s.session_summary = raw["session_summary"]
                if str(raw.get("vector", "")).strip().lower() in _VECTOR_MODES:
                    s.vector = str(raw["vector"]).strip().lower()
                if isinstance(raw.get("ollama_url"), str) and raw["ollama_url"].strip():
                    s.ollama_url = raw["ollama_url"].strip()
                if isinstance(raw.get("embed_model"), str) and raw["embed_model"].strip():
                    s.embed_model = raw["embed_model"].strip()
                if str(raw.get("embed_backend", "")).strip().lower() in _EMBED_BACKENDS:
                    s.embed_backend = str(raw["embed_backend"]).strip().lower()
                if isinstance(raw.get("local_embed_model"), str) and raw["local_embed_model"].strip():
                    s.local_embed_model = raw["local_embed_model"].strip()
    except Exception:  # a corrupt settings file must never block memory
        log.exception("memory settings read failed (%s); defaults in effect", path)

    env_direct = os.environ.get("AKANA_MEMORY_ALLOW_DIRECT", "").strip().lower()
    if env_direct in _TRUTHY:
        s.allow_direct = True
    elif env_direct in _FALSY:
        s.allow_direct = False
    env_capture = os.environ.get("AKANA_MEMORY_LLM_CAPTURE", "").strip().lower()
    if env_capture in _TRUTHY:
        s.auto_capture = True
    elif env_capture in _FALSY:
        s.auto_capture = False
    env_vector = os.environ.get("AKANA_MEMORY_VECTOR", "").strip().lower()
    if env_vector in _VECTOR_MODES:
        s.vector = env_vector
    env_backend = os.environ.get("AKANA_MEMORY_EMBED_BACKEND", "").strip().lower()
    if env_backend in _EMBED_BACKENDS:
        s.embed_backend = env_backend
    return s


def save_memory_settings(data_dir: Path, settings: MemorySettings) -> Path:
    """Persist to YAML (env overrides still win on next load)."""
    path = _settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(asdict(settings), allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    return path
