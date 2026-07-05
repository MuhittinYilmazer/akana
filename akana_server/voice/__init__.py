"""Voice helpers: pluggable TTS engines (edge/Piper), browser/Whisper STT, openWakeWord."""

from __future__ import annotations

from akana_server.voice.engines import (
    TtsEngine,
    VoiceSelection,
)
from akana_server.voice.streaming_tts import (
    resolve_voice_selection,
    split_first_sentence,
    stream_text_to_tts_chunks,
    strip_markdown_for_tts,
    synthesize_with_fallback,
)
from akana_server.voice.stt import (
    SttError,
    decode_wav_to_float_mono16k,
    transcribe_wav_bytes,
)
from akana_server.voice.tts import (
    TtsError,
    list_available_voices,
    resolve_tts_lang,
    resolve_tts_voice_path,
)
from akana_server.voice.wake import (
    WakeError,
    WakeScoreResult,
    score_wake_wav_bytes,
)

__all__ = [
    "SttError",
    "TtsEngine",
    "TtsError",
    "VoiceSelection",
    "WakeError",
    "WakeScoreResult",
    "decode_wav_to_float_mono16k",
    "list_available_voices",
    "resolve_tts_lang",
    "resolve_tts_voice_path",
    "resolve_voice_selection",
    "score_wake_wav_bytes",
    "split_first_sentence",
    "stream_text_to_tts_chunks",
    "strip_markdown_for_tts",
    "synthesize_with_fallback",
    "transcribe_wav_bytes",
]
