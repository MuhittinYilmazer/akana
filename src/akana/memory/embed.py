"""Embedders — turn text into vectors for the vector recall milestone.

A tiny :class:`Embedder` protocol keeps the vector layer model-agnostic
(P-extensibility): swapping bge-m3 for another model, or Ollama for an API,
is a new implementation, not a refactor. Two implementations ship today:

* :class:`OllamaEmbedder` — the real one, talking to a local Ollama daemon.
  It never touches the network at import time; the first ``embed()`` call is
  where connectivity is exercised, and failure raises a clear error.
* :class:`HashingEmbedder` — a deterministic pure-python fallback so tests
  (and offline machines) get real vector behaviour without a model.
"""

from __future__ import annotations

import math
import zlib
from typing import Protocol, runtime_checkable

import httpx

__all__ = [
    "Embedder",
    "OllamaEmbedder",
    "LocalEmbedder",
    "HashingEmbedder",
    "EmbeddingError",
    "ModelNotFoundError",
    "is_available",
    "has_model",
]

_OLLAMA_URL = "http://localhost:11434/api/embed"
_OLLAMA_MODEL = "bge-m3"
_OLLAMA_TIMEOUT_S = 10.0
_PROBE_TIMEOUT_S = 1.5

# Local (Ollama-free) embedding default: small, multilingual (Turkish), SYMMETRIC
# (no query/passage prefix needed), registered with fastembed. Downloaded + cached
# on the first embed (~220MB). Can be overridden via local_embed_model.
_LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def is_available(url: str, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """Cheap reachability probe for an Ollama daemon at base ``url``.

    GETs ``<url>/api/tags`` (Ollama's lightest list endpoint) with a short
    timeout. Any connection error, timeout or non-2xx answer is ``False`` —
    callers use this to decide *whether* to wire an :class:`OllamaEmbedder`,
    so unavailability must be a value, never an exception. Runs only when
    called; importing this module still touches no network.
    """
    try:
        resp = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
    except Exception:
        return False
    return resp.is_success


def has_model(url: str, model: str, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """Is ``model`` actually installed on the Ollama daemon at ``url``?

    A reachable daemon without the embed model is the classic silent failure:
    the probe passes, then every ``/api/embed`` call 404s. This check reads
    ``/api/tags`` and matches the model name — an untagged ``model`` (e.g.
    ``bge-m3``) matches any tag of it (``bge-m3:latest``), a tagged one must
    match exactly. Like :func:`is_available`, any error is ``False``, never
    an exception.
    """
    try:
        resp = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        if not resp.is_success:
            return False
        models = resp.json().get("models") or []
    except Exception:
        return False
    for entry in models:
        name = str(entry.get("name", "")) if isinstance(entry, dict) else ""
        if name == model:
            return True
        if ":" not in model and name.split(":", 1)[0] == model:
            return True
    return False


class EmbeddingError(RuntimeError):
    """Embedding backend unreachable or returned an unusable response."""


class ModelNotFoundError(EmbeddingError):
    """The daemon is up but the embed model is not installed (HTTP 404).

    Distinct from a transient :class:`EmbeddingError` because retrying cannot
    help — the fix is operator action (``ollama pull <model>``), so callers
    may disable the vector layer permanently instead of cooling down.
    """


@runtime_checkable
class Embedder(Protocol):
    """Anything that maps texts to fixed-size float vectors."""

    @property
    def name(self) -> str:
        """Stable identifier stored next to vectors (e.g. ``ollama:bge-m3``)."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per input text, all of the same dimension."""
        ...


class OllamaEmbedder:
    """Embeddings via a local Ollama daemon (``POST /api/embed``).

    Connectivity is attempted per call, never at import/construction time, so
    merely wiring this class up costs nothing when Ollama is down. When the
    daemon is unreachable or answers garbage, :class:`EmbeddingError` carries
    a message the caller can surface.
    """

    def __init__(
        self,
        *,
        model: str = _OLLAMA_MODEL,
        url: str = _OLLAMA_URL,
        timeout_s: float = _OLLAMA_TIMEOUT_S,
    ) -> None:
        self._model = model
        self._url = url
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = httpx.post(
                self._url,
                json={"model": self._model, "input": texts},
                timeout=self._timeout_s,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ModelNotFoundError(
                    f"Ollama embed model {self._model!r} not installed (404); "
                    f"run: ollama pull {self._model}"
                ) from e
            raise EmbeddingError(
                f"Ollama embed call failed ({self._url}, model={self._model!r}): {e}"
            ) from e
        except httpx.HTTPError as e:
            raise EmbeddingError(
                f"Ollama embed call failed ({self._url}, model={self._model!r}): {e}"
            ) from e
        payload = resp.json()
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError(
                f"Ollama returned {len(vectors) if isinstance(vectors, list) else 'no'} "
                f"embeddings for {len(texts)} inputs (model={self._model!r})"
            )
        return [[float(x) for x in vec] for vec in vectors]


class LocalEmbedder:
    """In-process semantic embeddings via fastembed (ONNX) — NO Ollama, NO torch.

    fastembed is a light ONNX-runtime wrapper; the model is downloaded + cached
    on the FIRST :meth:`embed` call (import here stays cheap, lazy). The default
    is a small multilingual model (Turkish-capable) that is SYMMETRIC — no
    ``query:``/``passage:`` prefix needed, so the single :meth:`embed` serves both
    indexing and recall directly. Missing dependency or a load failure raises
    :class:`ModelNotFoundError`/:class:`EmbeddingError` with the fix, and the
    caller degrades to keyword recall (never fatal).
    """

    def __init__(self, *, model: str = _LOCAL_MODEL) -> None:
        self._model_name = model
        self._model: object | None = None  # lazy: load on first embed

    @property
    def name(self) -> str:
        return f"fastembed:{self._model_name}"

    def _ensure_model(self) -> object:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise ModelNotFoundError(
                    "fastembed is not installed — local vector recall is disabled. "
                    "Install: python akana.py add embeddings (installs into the venv; a "
                    "bare 'pip install' can land in user-site where the server won't see it)"
                ) from e
            try:
                self._model = TextEmbedding(model_name=self._model_name)
            except Exception as e:  # unknown model name / download / ONNX failure
                raise EmbeddingError(
                    f"fastembed model failed to load ({self._model_name!r}): {e}"
                ) from e
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        try:
            # the paraphrase-multilingual model is SYMMETRIC → NO query/passage prefix
            vectors = [[float(x) for x in v] for v in model.embed(list(texts))]  # type: ignore[attr-defined]
        except Exception as e:
            raise EmbeddingError(
                f"fastembed embed failed ({self._model_name!r}): {e}"
            ) from e
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"fastembed returned {len(vectors)} vectors, expected {len(texts)}"
            )
        return vectors


class HashingEmbedder:
    """Deterministic character 3-gram hashing embedder (pure python).

    **Test-quality only** — this is a feature-hashing trick, not a semantic
    model: overlapping character 3-grams of the lowercased text are hashed
    (crc32, salt-free → stable across processes) into a fixed-size signed
    bucket vector, then L2-normalized. Texts sharing surface n-grams come out
    cosine-similar; true paraphrases do not. It exists so the vector pipeline
    is exercisable offline and in unit tests; production uses
    :class:`OllamaEmbedder`.
    """

    def __init__(self, *, dim: int = 256) -> None:
        if dim < 8:
            raise ValueError("dim must be >= 8")
        self._dim = dim

    @property
    def name(self) -> str:
        return f"hashing:3gram-{self._dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        normalized = " ".join(text.lower().split())
        padded = f" {normalized} "  # boundary grams anchor word starts/ends
        grams = (
            [padded[i : i + 3] for i in range(len(padded) - 2)]
            if len(padded) >= 3
            else [padded]
        )
        for gram in grams:
            h = zlib.crc32(gram.encode("utf-8"))
            sign = 1.0 if (h & 0x80000000) == 0 else -1.0
            vec[h % self._dim] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        return vec
