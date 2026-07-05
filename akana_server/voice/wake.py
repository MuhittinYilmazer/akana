"""openWakeWord wrapper: WAV bytes → wake scores (optional dep)."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import numpy as np

from akana_server.voice.stt import SttError, decode_wav_to_float_mono16k

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

WAKE_CHUNK_SAMPLES = 1280  # 80 ms @ 16 kHz
_WAKE_MAX_DECODE_SECONDS = 5.0

_model_lock = threading.Lock()
_models: dict[tuple[str, str], Any] = {}

# The openWakeWord model carries internal audio-buffer/feature state;
# ``reset()`` + ``predict_clip()`` are stateful on the single shared cached model.
# ``score_wake_wav_bytes_sync`` runs in anyio worker threads → two concurrent wake
# requests would interleave reset/predict and corrupt each other's scores (false/
# missed trigger). We serialise inference with this lock; loading uses a separate
# ``_model_lock``.
_infer_lock = threading.Lock()

# A downloaded resource (the shared feature models, or a bare pretrained wake model)
# is retried behind a negative-cache/backoff: a failed fetch (offline first-run) must
# not re-hit the network — and re-log — on every 300 ms wake poll. Both the feature
# models and the per-name wake models share ONE primitive (``_single_flight_download``
# + ``_trigger_background`` below) so the backoff algorithm lives in a single place;
# each caller passes its own keyed ``_BackoffState`` and background-inflight registry.
_BACKOFF_MIN = 30.0
_BACKOFF_MAX = 600.0


@dataclass
class _BackoffState:
    """Per-key negative-cache for a single-flight download.

    ``next_retry`` is the monotonic time before which the download short-circuits to
    ``last_error``; ``backoff`` doubles on each failure up to ``_BACKOFF_MAX``;
    ``failures`` counts consecutive failed attempts (surfaced so a caller — e.g. the
    config GET route — can distinguish 'still downloading' from 'parked/failed')."""

    next_retry: float = 0.0
    backoff: float = 0.0
    last_error: str | None = None
    failures: int = 0

    def note_failure(self, message: str) -> None:
        self.backoff = min(max(self.backoff * 2, _BACKOFF_MIN), _BACKOFF_MAX)
        self.next_retry = time.monotonic() + self.backoff
        self.last_error = message
        self.failures += 1

    def note_success(self) -> None:
        self.next_retry = 0.0
        self.backoff = 0.0
        self.last_error = None
        self.failures = 0

    def parked(self) -> bool:
        """True while the negative-cache is holding retries back (a fetch would only re-raise)."""
        return time.monotonic() < self.next_retry


def _single_flight_download(
    state: _BackoffState,
    lock: threading.Lock,
    *,
    present: Callable[[], bool],
    fetch: Callable[[], None],
    fetch_error: Callable[[Exception], str],
    missing_after_error: str,
    parked_error: str,
    log_label: str = "resource",
) -> None:
    """Fetch a resource under ``lock`` with negative-cache/backoff (single source of truth).

    A no-op once ``present()`` is true. Otherwise, under the lock: re-check presence,
    honor the parked negative-cache (raise the last error), run ``fetch()``, and on
    failure (or a phantom success that still leaves the resource absent) park the retry
    via ``state.note_failure`` and raise a 503 ``WakeError``. On real success clear the
    negative-cache. ``fetch_error(exc)`` / ``missing_after_error`` / ``parked_error``
    supply the caller-specific friendly messages; ``log_label`` names the resource in
    the warning/info log lines."""
    if present():
        return
    with lock:
        if present():
            return
        if state.parked():
            raise WakeError(state.last_error or parked_error, status_code=503)
        try:
            fetch()
        except Exception as e:  # noqa: BLE001 - network/IO failure → backoff + friendly error
            state.note_failure(fetch_error(e))
            log.warning("openWakeWord %s download failed: %s", log_label, e)
            raise WakeError(state.last_error or "", status_code=503) from e
        if not present():
            state.note_failure(missing_after_error)
            raise WakeError(state.last_error or missing_after_error, status_code=503)
        state.note_success()
        log.info("openWakeWord: %s ready", log_label)


def _trigger_background(
    key: str,
    state: _BackoffState,
    *,
    bg_lock: threading.Lock,
    inflight: set[str],
    thread_name: str,
    run: Callable[[], None],
) -> None:
    """Fire a single background download thread for ``key`` (idempotent while in flight).

    Spawns at most ONE daemon thread per key: ``inflight`` gates re-entry and the thread
    clears it on exit. A prior failure that parked retries (``state.parked()``) is a no-op
    — a thread would only short-circuit on the negative-cache and immediately re-raise.
    ``run`` is expected to swallow its own ``WakeError`` (already negative-cached + logged)."""
    with bg_lock:
        if key in inflight:
            return
        if state.parked():
            return
        inflight.add(key)

    def _worker() -> None:
        try:
            run()
        except WakeError:
            pass  # already negative-cached + logged in the downloader
        except Exception:  # noqa: BLE001 - a background thread must never crash silently upward
            log.warning("openWakeWord background fetch failed (%s)", key, exc_info=True)
        finally:
            with bg_lock:
                inflight.discard(key)

    threading.Thread(target=_worker, name=thread_name, daemon=True).start()


# Shared feature models (melspectrogram + audio embedding) — one global backoff state.
_feature_lock = threading.Lock()
_feature_state = _BackoffState()
_feature_bg_lock = threading.Lock()
_feature_bg_inflight: set[str] = set()

# A bare pretrained WAKE_MODEL name (e.g. "hey_jarvis") whose file is not yet on disk
# needs the SAME network-download discipline as the feature models, keyed per model name.
_wake_model_lock = threading.Lock()
_wake_model_state: dict[str, _BackoffState] = defaultdict(_BackoffState)
_wake_model_bg_lock = threading.Lock()
_wake_model_bg_inflight: set[str] = set()


class WakeError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class WakeScoreResult:
    wake_model: str
    threshold: float
    max_score: float
    triggered: bool
    scores: dict[str, float]
    #: Sustain gate that produced ``triggered`` (consecutive frames required) and the
    #: longest run actually observed — surfaced so the UI/test button can explain WHY a
    #: high-scoring clip did not fire (peak crossed the threshold, but only briefly).
    min_frames: int = 1
    run_frames: int = 0


def float_mono_to_int16_pcm(audio: np.ndarray) -> np.ndarray:
    x = audio.astype(np.float32, copy=False)
    return np.clip(x * 32767.0, -32768, 32767).astype(np.int16)


#: Local model file extensions — if ``model_name`` ends with one of these it is
#: a file PATH (not a pretrained name) → no download is performed.
_LOCAL_MODEL_SUFFIXES = (".onnx", ".tflite")


def _classify_wake_model(model_name: str) -> str:
    """Classify ``model_name`` as one of ``file`` / ``missing_path`` / ``pretrained``.

      - ``file``: an existing local .onnx/.tflite file path → loader opens it as-is.
      - ``missing_path``: looks like a path (separator or .onnx/.tflite suffix) but is
        absent → cannot be downloaded (not a pretrained name).
      - ``pretrained``: a bare official name (e.g. "hey_jarvis") → downloadable.
    """
    candidate = Path(model_name)
    if candidate.exists():
        return "file"
    looks_like_path = (
        candidate.name != model_name  # contains a directory separator (absolute/relative path)
        or model_name.endswith(_LOCAL_MODEL_SUFFIXES)
    )
    return "missing_path" if looks_like_path else "pretrained"


def _pretrained_wake_present(model_name: str) -> bool:
    """True when a bare pretrained wake model is already on disk (no network).

    ``base`` is always a filename part → the glob pattern stays RELATIVE (an absolute
    pattern would raise ``NotImplementedError`` in ``Path.glob``; model_name is a bare
    name here so this is safe)."""
    import openwakeword

    models_dir = Path(openwakeword.__file__).parent / "resources" / "models"
    base = Path(model_name).name.split("_v")[0]
    return any(models_dir.glob(f"{base}_v*.onnx"))


def _missing_path_error(model_name: str) -> WakeError:
    return WakeError(
        f"wake model not found: {model_name!r} (looks like a local file path "
        "but does not exist on disk). Provide a valid custom-trained "
        "'hey_akana' .onnx/.tflite path.",
        status_code=400,
    )


def _download_pretrained_wake_model(model_name: str) -> None:
    """Fetch a bare pretrained wake model, single-flight + negative-cache/backoff.

    Delegates the backoff/negative-cache discipline to the shared
    ``_single_flight_download`` primitive (keyed per model name) so the algorithm is
    defined in ONE place (shared with the feature-model path). A no-op once the file
    is present; a failed fetch (offline first-run) is parked with exponential backoff
    so the 300 ms wake poll does not re-download and re-log."""
    try:
        import openwakeword  # noqa: F401
        from openwakeword.utils import download_models
    except ImportError as e:
        raise WakeError(
            "openwakeword is not installed — run `python akana.py add voice-full`.",
            status_code=503,
        ) from e
    _single_flight_download(
        _wake_model_state[model_name],
        _wake_model_lock,
        present=lambda: _pretrained_wake_present(model_name),
        fetch=lambda: download_models([model_name]),
        fetch_error=lambda e: (
            f"could not download the wake model {model_name!r} — check the network "
            f"and retry, or run `python akana.py add voice-full`: {e}"
        ),
        missing_after_error=(
            f"wake model {model_name!r} still missing after download — "
            "run `python akana.py add voice-full`."
        ),
        parked_error=f"wake model {model_name!r} is unavailable (retrying later).",
        log_label=f"wake model {model_name}",
    )


def trigger_wake_model_download(model_name: str) -> None:
    """Fire a single background pretrained-wake-model fetch (idempotent per name).

    The config GET route uses this (like ``trigger_feature_models_download``) so it
    never blocks on the network when a bare pretrained WAKE_MODEL is not yet on disk.
    A concurrent poll storm spawns at most ONE thread per name; the ``WakeError`` is
    swallowed there (already negative-cached + logged in the downloader)."""
    # Only a bare pretrained name is downloadable; a missing LOCAL path (or an
    # existing file) is a no-op here — the loader surfaces the right 400/uses it.
    if _classify_wake_model(model_name) != "pretrained":
        return
    _trigger_background(
        model_name,
        _wake_model_state[model_name],
        bg_lock=_wake_model_bg_lock,
        inflight=_wake_model_bg_inflight,
        thread_name="oww-wake-model-download",
        run=lambda: _download_pretrained_wake_model(model_name),
    )


def _ensure_oww_models_on_disk(model_name: str) -> None:
    """Ensure the wake model is on disk; leave local file paths untouched.

    Case ``file`` → no-op (loader opens it). Case ``missing_path`` → a clear 400.
    Case ``pretrained`` → download via the single-flight/backoff guard (no bare
    ``download_models`` on the hot scoring path). (No pretrained name is shipped by
    default; wake normally uses a custom "hey_akana" model PATH.)
    """
    kind = _classify_wake_model(model_name)
    if kind == "file":
        return
    if kind == "missing_path":
        raise _missing_path_error(model_name)
    _download_pretrained_wake_model(model_name)


#: openWakeWord's ``Model()`` also loads the SHARED feature models
#: (melspectrogram + audio embedding) regardless of which wake model is used.
#: These are NOT shipped in the wheel (`resources/models` is empty on a fresh
#: install), so ``Model()`` raises ``NO_SUCHFILE`` unless an earlier ``download_models``
#: happened to populate them — which never runs for the bundled "hey_akana" file path.
#: We fetch them here on demand. (VAD is only needed when vad_threshold>0, which the
#: scorer never sets, so we do not require silero_vad.)
_FEATURE_MODEL_STEMS = ("melspectrogram", "embedding_model")


def _feature_model_suffix(framework: str) -> str:
    return ".tflite" if framework == "tflite" else ".onnx"


def _feature_models_present(models_dir: Path, framework: str) -> bool:
    suffix = _feature_model_suffix(framework)
    return all((models_dir / f"{stem}{suffix}").is_file() for stem in _FEATURE_MODEL_STEMS)


def _ensure_oww_feature_models(framework: str) -> None:
    """Ensure the shared melspectrogram + audio-embedding feature models are on disk.

    A no-op once the files exist (the common case after first run). On a fresh install
    they are absent → downloaded via ``download_models`` with a NON-EMPTY sentinel name:
    passing ``[]`` would pull EVERY official pretrained model, whereas a name matching no
    official model triggers ONLY the feature/VAD download (openwakeword.utils.download_models
    always fetches the feature+VAD files, then matches the given names against the official
    catalogue). The backoff/negative-cache discipline is delegated to the shared
    ``_single_flight_download`` primitive so a failed fetch (offline) does not re-download
    and re-log on every 300 ms poll; the last error is re-raised meanwhile.
    """
    try:
        import openwakeword
    except ImportError as e:
        raise WakeError(
            "openwakeword is not installed — run `python akana.py add voice-full`.",
            status_code=503,
        ) from e
    models_dir = Path(openwakeword.__file__).parent / "resources" / "models"

    def _fetch() -> None:
        from openwakeword.utils import download_models

        # "__none__" matches no official model → only the shared feature (+VAD)
        # files are fetched; the official-model loop is a no-op. Do NOT pass [].
        download_models(["__none__"], target_directory=str(models_dir))

    _single_flight_download(
        _feature_state,
        _feature_lock,
        present=lambda: _feature_models_present(models_dir, framework),
        fetch=_fetch,
        fetch_error=lambda e: (
            "could not download the wake feature models (melspectrogram/embedding) — "
            f"check the network and retry, or run `python akana.py add voice-full`: {e}"
        ),
        missing_after_error=(
            f"wake feature models still missing after download ({framework}) — "
            "run `python akana.py add voice-full`."
        ),
        parked_error="wake feature models are unavailable (retrying later).",
        log_label="feature models",
    )


def _build_oww_model(model_name: str, framework: str) -> object:
    """Init and cache an openWakeWord ``Model`` for ``(model_name, framework)``.

    Assumes the wake model file AND the shared feature models are already on disk
    (the caller ensures both BEFORE taking ``_model_lock``). ``Model()`` init itself
    is serialised on ``_model_lock`` — no network is touched while it is held, so a
    slow first download can no longer pin every concurrent wake request behind the
    global load lock.
    """
    key = (model_name, framework)
    with _model_lock:
        if key not in _models:
            try:
                from openwakeword.model import Model
            except ImportError as e:
                raise WakeError(
                    "openwakeword is not installed — run `python akana.py add voice-full`.",
                    status_code=503,
                ) from e
            try:
                _models[key] = Model(
                    wakeword_models=[model_name],
                    inference_framework=framework,
                )
            except Exception as e:
                log.warning("openWakeWord init failed: %s", e)
                raise WakeError(
                    f"wake model init failed: {e}. `python akana.py add voice-full`",
                    status_code=503,
                ) from e
            log.info("openWakeWord: loaded %s (%s)", model_name, framework)
        return _models[key]


def _get_oww_model(model_name: str, framework: str) -> object:
    """Full wake-scoring load path: ensure files (may download) then init the model.

    The file/feature ensure (which may hit the network on a fresh install) runs
    OUTSIDE ``_model_lock`` so a slow download does not serialise concurrent callers
    behind the global load lock; only the fast in-memory ``Model()`` init is locked.
    """
    key = (model_name, framework)
    cached = _models.get(key)
    if cached is not None:
        return cached
    _ensure_oww_models_on_disk(model_name)
    # The wake model file may exist locally, but Model() ALSO needs the shared
    # feature models — absent on a fresh install. Ensure them before init so the
    # bundled "hey_akana" path does not fail with NO_SUCHFILE on first use. This may
    # download; it is deliberately NOT under _model_lock (see _build_oww_model).
    _ensure_oww_feature_models(framework)
    return _build_oww_model(model_name, framework)


def get_oww_model_from_disk(model_name: str, framework: str) -> object:
    """Config-gate load path: init the model WITHOUT ever touching the network.

    Used by ``GET /voice/wake/config``, which must return promptly and never wait on
    a download. If the wake model AND the shared feature models are already on disk
    (cached common case) the (cached) ``Model()`` init runs and ``enabled=true`` can
    be reported; if EITHER is absent this raises a 503 immediately — the route then
    fires a one-shot background fetch and reports ``preparing`` so the browser
    fallback stays active and a later poll flips to ``true`` once the download lands.

    Unlike ``_get_oww_model`` (the scoring path), this NEVER downloads: a bare
    pretrained wake model that is not yet on disk is reported ``preparing`` here and
    fetched off the request thread by ``trigger_wake_model_download`` — mirroring the
    feature-model branch below (a polled, offline-safe endpoint must not fetch inline).
    """
    key = (model_name, framework)
    cached = _models.get(key)
    if cached is not None:
        return cached
    try:
        import openwakeword
    except ImportError as e:
        raise WakeError(
            "openwakeword is not installed — run `python akana.py add voice-full`.",
            status_code=503,
        ) from e
    # Disk-only wake-model readiness: a missing local path is a hard 400; a bare
    # pretrained name absent from disk is a 503 "preparing" (NO inline download).
    kind = _classify_wake_model(model_name)
    if kind == "missing_path":
        raise _missing_path_error(model_name)
    if kind == "pretrained" and not _pretrained_wake_present(model_name):
        raise WakeError(
            f"wake model {model_name!r} is not on disk yet (preparing).",
            status_code=503,
        )
    models_dir = Path(openwakeword.__file__).parent / "resources" / "models"
    if not _feature_models_present(models_dir, framework):
        raise WakeError(
            "wake feature models are not on disk yet (preparing).",
            status_code=503,
        )
    return _build_oww_model(model_name, framework)


def feature_models_ready(framework: str) -> bool:
    """True when the shared feature models are already on disk (no network)."""
    try:
        import openwakeword
    except ImportError:
        return False
    models_dir = Path(openwakeword.__file__).parent / "resources" / "models"
    return _feature_models_present(models_dir, framework)


def feature_download_status(framework: str) -> tuple[int, str | None]:
    """Negative-cache state of the shared feature-model download (no network).

    Returns ``(failures, last_error)`` so a polled caller (the config GET route) can
    tell 'still downloading the first time' (``failures == 0``) from 'the download has
    failed repeatedly and is parked on backoff' (``failures > 0``) — the plain
    ``preparing`` flag alone cannot distinguish these. ``framework`` is accepted for a
    symmetric signature with ``wake_download_status``; feature state is global."""
    del framework  # feature-model backoff state is global (not per framework)
    return (_feature_state.failures, _feature_state.last_error)


def wake_download_status(model_name: str) -> tuple[int, str | None]:
    """Negative-cache state of a bare pretrained wake-model download (no network).

    Returns ``(failures, last_error)`` for ``model_name`` — the per-name twin of
    ``feature_download_status``. ``failures == 0`` means no download attempt has failed
    yet (a genuine first-time fetch is in progress); ``failures > 0`` means the fetch is
    parked on the exponential backoff. Reading a not-yet-seen name via the defaultdict
    yields a fresh zero state without mutating anything meaningful."""
    state = _wake_model_state[model_name]
    return (state.failures, state.last_error)


def wake_model_ready(model_name: str) -> bool:
    """True when the wake model file is available WITHOUT a network fetch (no network).

    A local file path that exists → ready. A bare pretrained name → ready only if its
    file is already on disk (a fresh install has neither). A missing local path is not
    ready (the loader raises 400 for it later). Used by the config GET route so a bare
    pretrained WAKE_MODEL absent on disk reports ``preparing`` instead of downloading
    inline (VB-5)."""
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        return False
    kind = _classify_wake_model(model_name)
    if kind == "file":
        return True
    if kind == "missing_path":
        return False
    return _pretrained_wake_present(model_name)


def trigger_feature_models_download(framework: str) -> None:
    """Fire a single background feature-model fetch (idempotent while in flight).

    Runs ``_ensure_oww_feature_models`` — which carries the backoff/negative-cache —
    on a daemon thread (via the shared ``_trigger_background`` primitive) so the caller
    (the config GET route) never blocks on the network. A concurrent poll storm spawns
    at most ONE thread; a prior failure that parked retries is a no-op. The background
    ``WakeError`` is swallowed there (already negative-cached + logged) so the daemon
    thread exits clean. Keyed by ``framework`` so onnx/tflite fetch independently."""
    _trigger_background(
        framework,
        _feature_state,
        bg_lock=_feature_bg_lock,
        inflight=_feature_bg_inflight,
        thread_name="oww-feature-download",
        run=lambda: _ensure_oww_feature_models(framework),
    )


def score_wake_wav_bytes_sync(wav_bytes: bytes, settings: Settings) -> WakeScoreResult:
    # Empty WAKE_MODEL = server-side openWakeWord scoring is DISABLED (the default).
    # "Hey Akana" is detected in the browser by SpeechRecognition phrase-matching, so
    # server scoring is optional and only runs when the user points WAKE_MODEL at a
    # custom-trained "hey_akana" model file. Bail out here BEFORE any model download so
    # the retired 'hey_jarvis' pretrained model is never fetched.
    if not settings.wake_model.strip():
        raise WakeError(
            "server-side wake scoring is disabled (WAKE_MODEL unset). «Hey Akana» works "
            "in the browser; set WAKE_MODEL to a custom-trained model path to enable "
            "server-side acoustic scoring.",
            status_code=503,
        )
    max_sec = min(float(settings.voice_max_record_seconds), _WAKE_MAX_DECODE_SECONDS)
    try:
        audio_f = decode_wav_to_float_mono16k(wav_bytes, max_seconds=max_sec)
    except SttError as e:
        raise WakeError(e.message, status_code=e.status_code) from e

    pcm = float_mono_to_int16_pcm(audio_f)
    if pcm.size < WAKE_CHUNK_SAMPLES:
        raise WakeError(
            f"audio too short for wake scoring (need at least {WAKE_CHUNK_SAMPLES} samples)",
            status_code=400,
        )

    model_any: Any = _get_oww_model(settings.wake_model, settings.wake_inference_framework)

    # Gentle AGC: normalize speech that is PRESENT but a touch quiet toward a target
    # level so a soft "hey akana" still scores. The floor is deliberately well above
    # digital silence — boosting near-silent ambient hiss (old floor: 50) up to speech
    # level manufactured false wakes, exactly what the user hits. Below the floor the
    # clip is left untouched (stays quiet → scores low); the gain is also capped so a
    # faint sound is never blown up into a full-scale "shout" the model over-scores.
    _AGC_FLOOR = 1000.0  # int16 peak amplitude (~3% of full scale) below which we don't touch it
    _AGC_TARGET = 6000.0
    _AGC_MAX_GAIN = 4.0
    pcm_f = pcm.astype(np.float32)
    peak_amp = float(np.max(np.abs(pcm_f))) if pcm_f.size else 0.0
    if _AGC_FLOOR < peak_amp < _AGC_TARGET:
        gain = min(_AGC_TARGET / peak_amp, _AGC_MAX_GAIN)
        pcm = np.clip(pcm_f * gain, -32768, 32767).astype(np.int16)

    # openWakeWord's predict_clip() iterates
    # ``range(0, data.shape[0] - step_size, step_size)`` — an EXCLUSIVE upper
    # bound — so with padding=0 the last full chunk (and any remainder) of the
    # newest audio is never scored, and a clip whose length is an exact
    # multiple of WAKE_CHUNK_SAMPLES yields zero predictions at all. Append one
    # chunk of trailing silence ourselves so every real chunk falls inside the
    # scored range, without pulling in the library's default 1-second padding.
    pcm_padded = np.concatenate([pcm, np.zeros(WAKE_CHUNK_SAMPLES, dtype=np.int16)])

    # reset()→predict_clip() are stateful on the shared cached model; serialise
    # concurrent wake requests (otherwise the internal feature buffer interleaves).
    # Materialise the full inference INSIDE the lock with ``list(...)``
    # (predict_clip may be lazy in some versions).
    with _infer_lock:
        model_any.reset()
        preds = list(model_any.predict_clip(pcm_padded, padding=0, chunk_size=WAKE_CHUNK_SAMPLES))
    # Keep the ORDERED per-frame score series per key (not just the peak): the sustain
    # gate needs to know how many frames stayed hot IN A ROW, which the peak alone hides.
    series: dict[str, list[float]] = defaultdict(list)
    for frame in preds:
        if not isinstance(frame, dict):
            continue
        for k, v in frame.items():
            # NOT isinstance(v, (int, float)): openWakeWord scores are np.float32
            # → not a Python float → the old check was filtering OUT ALL scores
            # (always 0.0, wake never triggered). float(v) converts np.float32/64.
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f != f or f in (float("inf"), float("-inf")):
                continue
            series[k].append(f)

    scores = {k: (max(v) if v else 0.0) for k, v in series.items()}
    label = settings.wake_model
    # When loaded via a custom model file PATH, openWakeWord keys scores by BASENAME
    # (without extension, e.g. "hey_akana"), NOT by the full path → derive the active key.
    # A bare pretrained NAME is not a file → label stays as-is.
    key = Path(label).stem if Path(label).exists() else label
    if key in scores:
        active_key: str | None = key
    elif scores:
        prefix = key.split("_v")[0]
        related = [k for k in scores if k == key or k.startswith(prefix)]
        pool = related or list(scores)
        active_key = max(pool, key=lambda k: scores[k])
    else:
        active_key = None

    active_series = series.get(active_key, []) if active_key is not None else []
    max_score = float(scores.get(active_key, 0.0)) if active_key is not None else 0.0

    thr = float(settings.wake_threshold)
    # Sustain gate: a single peak frame over a ~3 s window (≈37 frames) trips far too
    # easily, so require ``wake_min_frames`` CONSECUTIVE frames at/above the threshold.
    # We compute the longest such run on the active model's ordered series; ``max_score``
    # (the peak, for the live meter) is intentionally NOT gated by this.
    min_frames = max(1, int(settings.wake_min_frames))
    run = 0
    best_run = 0
    for f in active_series:
        if f >= thr:
            run += 1
            if run > best_run:
                best_run = run
        else:
            run = 0
    triggered = best_run >= min_frames
    return WakeScoreResult(
        wake_model=label,
        threshold=thr,
        max_score=max_score,
        triggered=triggered,
        scores=scores,
        min_frames=min_frames,
        run_frames=best_run,
    )


async def score_wake_wav_bytes(wav_bytes: bytes, settings: Settings) -> WakeScoreResult:
    try:
        return await anyio.to_thread.run_sync(score_wake_wav_bytes_sync, wav_bytes, settings)
    except WakeError:
        raise
    except Exception as e:
        log.warning("wake scoring failed: %s", e)
        raise WakeError(f"wake scoring failed: {e}", status_code=503) from e
