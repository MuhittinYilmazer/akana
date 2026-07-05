# Voice

Akana ships wake word, speech-to-text and text-to-speech behind a single language toggle. This page covers the wake path, the STT/TTS engines, voice selection and the full-duplex bridges. For a short overview, see the [Voice section in the README](../README.md#voice).

> **Privacy caveat:** the **default** TTS engine (`edge-tts`) and the **default** browser STT are **online**: synthesized text goes to Microsoft, recognized audio goes to Google. Akana keeps your *data* local, but the zero-setup *voice* path is not. A fully local voice pipeline requires installing `voice-full` (for local STT + wake) and choosing **Piper** for TTS. See [Security & privacy](../README.md#security-and-privacy).

## Wake word

A custom-trained **"Hey Akana"** openWakeWord ONNX model is committed at `akana_server/voice/wake_models/hey_akana.onnx` (~823 KB) and is the server default when `WAKE_MODEL` is unset. Which path actually detects the wake phrase depends on what you have installed.

- **Local acoustic scoring** runs on the server when the voice extras are installed:

  ```sh
  python akana.py add voice-full
  ```

  That pulls `openwakeword` and `faster-whisper` (both listed in `requirements-voice.txt`, not core). Two settings tune it: `wake_threshold` (default 0.5, hard cap 1.0) and `wake_min_frames` (default 3, a sustain gate that requires the score to stay above threshold across N consecutive frames before firing; it is the main lever for cutting false wakes).

- **Browser fallback:** if `openwakeword` is not installed, `/voice/wake/config` reports `enabled=false` and the frontend uses the browser's `SpeechRecognition` API to phrase-match "Hey Akana". This requires a Chromium-based browser (Chrome, Edge, Brave). There is no threshold to tune. In this path, **audio goes to Google's speech service**.

The two paths are mutually exclusive at the browser layer. Wake autostart is on by default in the UI (`akana.wakeAutostart`), but the user still has to grant microphone permission on first run.

## Speech-to-text

Two options:

- **Browser `SpeechRecognition`** — default, no install, Chromium-only, **cloud** (audio goes to Google).
- **Server-side `faster-whisper`** — opt-in, works **offline**, part of the `voice-full` extra.

## Text-to-speech

Akana ships three TTS engines and picks one based on `AKANA_TTS_ENGINE` plus a runtime preference file, with a live fallback if the preferred engine fails at synth time.

| Engine | Locality | License | Install | Notes |
| --- | --- | --- | --- | --- |
| **edge-tts** | **Online (default)**, text goes to Microsoft | **GPL-3.0** (installed from PyPI as a runtime dependency; not vendored) | In core `requirements.txt` | Microsoft's free neural voices. Zero setup. If edge fails at synth time (unreachable), the runtime falls back to Piper for that utterance rather than dropping the reply. See the GPL/cloud disclosure in [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md). |
| **piper** | **Offline** | MIT | `python akana.py add voice-piper` (`requirements-piper.txt`) | Permissive, fully local. Piper is the last-resort fallback engine; when Piper itself fails, the error surfaces. |
| **XTTS-v2** (`coqui-tts`) | **Offline**, heavy | Code MPL-2.0; **model weights CPML — NON-COMMERCIAL** | `python akana.py add xtts` (`requirements-xtts.txt`) | Downloads a ~2 GB model on first synth, benefits from a GPU (~4 GB VRAM). Supports voice cloning. **Do not use in commercial deployments.** |

> **Why edge-tts is the default despite being cloud + GPL:** it needs no download and no local model, so voice works immediately on any machine with an internet connection. If you want an offline or permissive path, install Piper and select it. Piper (MIT, offline) is the recommended alternative for local-only or commercial use; XTTS adds voice cloning but is non-commercial.

## Voice selection

Voice picking lives in **Settings → Voice** in the web UI, backed by `GET /api/v1/voice/config` and `PATCH /api/v1/voice/preferences`. The picker lists every registered voice tagged `ENGINE · LANG · name` and marks missing Piper files as disabled so you cannot silently lock yourself to a voice that has not been downloaded. Selections are persisted to `<data_dir>/voice_preferences.json`.

- **Engine choice.** Set it in the picker or force it with `AKANA_TTS_ENGINE=auto|edge|piper|xtts`. `auto` (the default) tries edge, then falls back to piper. XTTS is never in the auto chain; you have to select it explicitly.
- **Edge voices.** Per-language selection is persisted. Defaults: `en-US-JennyNeural` (English) and `tr-TR-EmelNeural` (Turkish). Other shipped options: `en-US-AriaNeural`, `en-US-GuyNeural`, `en-GB-SoniaNeural`, `tr-TR-AhmetNeural`.
- **Piper voices.** Downloaded by the setup wizard. `akana setup` (or `akana add voice-piper`) opens an interactive checklist preselected to the two shipped defaults (`en_US-amy-medium` for English and `tr_TR-dfki-medium` for Turkish), plus optional `en_US-lessac-medium`, `en_US-ryan-high`, `en_GB-alba-medium`, `tr_TR-fahrettin-medium`. Files land in `AKANA_VOICES_DIR` (env) or `<data_dir>/voices` (default), pulled from `huggingface.co/rhasspy/piper-voices`. To switch, either pick the file in Settings → Voice or point `PIPER_VOICE_EN` / `PIPER_VOICE_TR` at the `.onnx` you want.
- **XTTS voice cloning.** Drop a short reference recording at `<data_dir>/voices/xtts_ref.wav`; when XTTS is the active engine and no explicit speaker is set, that WAV is used as the clone reference automatically. Advanced callers can pass a voice id of the form `<lang>|<path/to/ref.wav>` (cloning) or `<lang>|<speaker_name>` (built-in). Supported languages: `tr, en, es, fr, de, it, pt, pl, ru, nl, cs, ar, zh-cn, hu, ko, ja, hi`.
- **Language follows the UI language.** The spoken language is always the unified `language` setting from Settings → General. There is no separate voice-language knob; flipping to English switches both the UI and the voice.

Related env vars: `AKANA_TTS_ENGINE`, `AKANA_VOICES_DIR`, `PIPER_VOICE_EN`, `PIPER_VOICE_TR`, `VOICE_TTS_MAX_CHARS` (default 5000), `AKANA_TTS_EDGE_TIMEOUT_S` (default 10s).

## Conversation mode and barge-in

Conversation mode (hands-free listen-respond-listen) is wired and is entered on wake by default. Barge-in (interrupting Akana mid-reply by speaking) is implemented but defaults **off**. Enabling it uses a separate AEC microphone stream and `AnalyserNode` to detect user speech without picking up Akana's own audio through the speakers.

## Full-duplex bridges

Gemini Live (`/ws/voice/live`) and OpenAI Realtime (`/ws/voice/realtime`) full-duplex voice bridges are implemented over WebSocket but are off by default; each requires enabling the corresponding provider flag (`gemini_live_enabled`, `openai_realtime_enabled`) plus a valid API key.
