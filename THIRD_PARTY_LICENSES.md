# Third-Party Licenses and Attributions

Akana itself is licensed under the [MIT License](LICENSE), Copyright (c) 2026
Muhittin Yılmazer.

This document lists the notable third-party components Akana relies on, so that
users and downstream distributors can understand the licensing that applies. It
distinguishes between:

- **Vendored** — third-party code shipped inside this repository. Akana
  redistributes it, so its license notice must travel with it (see the files
  under `web_ui/static/vendor/` and `akana_server/voice/wake_models/`).
- **Runtime (user-installed)** — Python packages that Akana imports but does
  **not** bundle. They are pulled in by `pip` (via `requirements*.txt`) on the
  user's own machine. Akana's own source distribution contains none of their
  code, so Akana's MIT license is unaffected; the third party's license governs
  that package once the user installs and runs it.

Nothing in this list changes the license of Akana's own source, which remains
MIT. Where a dependency carries a copyleft or non-commercial license, that is
called out explicitly below.

---

## Important note on edge-tts (GPL-3.0)

`edge-tts` is Akana's **default online TTS engine** and is listed in the core
`requirements.txt`. It is licensed under the **GNU General Public License v3.0
(GPL-3.0)**.

Akana does **not** vendor, copy, or redistribute any part of `edge-tts`. It is
installed separately by the user through `pip` (as part of
`python akana.py setup`), and Akana calls it as an ordinary runtime dependency.
Because Akana's own source is not a derivative of `edge-tts` and does not
distribute its code, Akana's own MIT license continues to apply to Akana's code.

When a user installs and runs `edge-tts`, the terms of the GPL-3.0 apply to that
package on their machine. Users and downstream distributors who need to avoid a
GPL runtime dependency have permissive alternatives already supported by Akana:

- **Piper TTS** (MIT) — offline engine, `requirements-piper.txt`
  (`python akana.py setup --voice piper`).
- Any other configured TTS backend.

If `edge-tts` is not installed, Akana still boots and degrades gracefully to the
configured fallback (the runtime import is guarded).

---

## Vendored components (shipped in this repository)

| Component | License | Location | Notes |
|-----------|---------|----------|-------|
| **pdf.js** (Mozilla Foundation) | Apache-2.0 | `web_ui/static/vendor/pdfjs/pdf.min.js` | In-browser PDF rendering. The upstream `@licstart` Apache-2.0 notice is retained verbatim in the file header, satisfying the redistribution terms. |
| **qrcodejs** (davidshimjs) | MIT | `web_ui/static/vendor/qrcode/qrcode.min.js` | Offline QR-code generation for the pairing/remote-access flow. A header crediting `github.com/davidshimjs/qrcodejs` (MIT) is added to the vendored file. Kept offline on purpose so the bearer token is never sent to a remote QR service. |
| **"Hey Akana" wake-word model** (`hey_akana.onnx`) | Apache-2.0 (openWakeWord framework) | `akana_server/voice/wake_models/hey_akana.onnx` | A custom wake-word model trained with the openWakeWord pipeline (Apache-2.0) and shipped as the default wake model. The shared feature models (melspectrogram + audio embedding) are downloaded at runtime, not vendored. |

---

## Runtime dependencies (user-installed via pip, not vendored)

### Core (`requirements.txt`)

| Package | License | How it is used |
|---------|---------|----------------|
| **edge-tts** | **GPL-3.0** | **Default online TTS engine.** User-installed, not vendored — see the note above. |
| fastapi | MIT | Web framework / HTTP API. |
| starlette | BSD-3-Clause | ASGI toolkit underlying FastAPI. |
| uvicorn[standard] | BSD-3-Clause | ASGI server. |
| httpx | BSD-3-Clause | Async HTTP client (provider calls). |
| pydantic | MIT | Settings and data-model validation. |
| cryptography | Apache-2.0 OR BSD-3-Clause | Fernet encryption for the credential vault. |
| python-dotenv | BSD-3-Clause | `.env` loading. |
| ulid-py | MIT | ULID identifiers. |
| pyyaml | MIT | YAML config parsing (e.g. `mcp_servers.yaml`). |
| python-multipart | Apache-2.0 | Multipart/form-data (file uploads). |
| anyio | MIT | Async compatibility layer. |
| numpy | BSD-3-Clause | Numeric/audio array handling. |
| mcp | MIT | Model Context Protocol client (in-process MCP bridge). Soft dependency; degrades gracefully if absent. |

### Voice input — STT + wake word (`requirements-voice.txt`, optional)

| Package | License | How it is used |
|---------|---------|----------------|
| faster-whisper | MIT | Local speech-to-text. |
| openwakeword | Apache-2.0 | "Hey Akana" wake-word detection (driven via ONNX). |
| onnxruntime | MIT | ONNX inference runtime for wake models. |
| scipy | BSD-3-Clause | Signal processing (openWakeWord transitive dep). |
| scikit-learn | BSD-3-Clause | openWakeWord transitive dep. |
| requests | Apache-2.0 | openWakeWord transitive dep. |
| tqdm | MPL-2.0 AND MIT | openWakeWord transitive dep (progress bars). |

### TTS — Piper (`requirements-piper.txt`, optional)

| Package | License | How it is used |
|---------|---------|----------------|
| piper-tts | MIT | **Offline TTS engine** — permissive alternative to edge-tts. |

### TTS — XTTS-v2 (`requirements-xtts.txt`, optional, heavy)

| Package | License | How it is used |
|---------|---------|----------------|
| coqui-tts | MPL-2.0 (code) | Local XTTS-v2 TTS (Turkish + voice cloning). |
| **XTTS-v2 model weights** | **CPML — Coqui Public Model License (NON-COMMERCIAL)** | Downloaded by the user on first synthesis (~2 GB); **not** shipped. The CPML restricts use to non-commercial purposes. Disclosed in `requirements-xtts.txt` and the README. |
| transformers | Apache-2.0 | Model loading for coqui-tts. |
| torch | BSD-3-Clause | Tensor backend for XTTS. |
| torchaudio | BSD-2-Clause | Audio I/O for XTTS. |

### Semantic vector recall (`requirements-vector.txt`, optional)

| Package | License | How it is used |
|---------|---------|----------------|
| fastembed | Apache-2.0 | Local ONNX embeddings for semantic memory recall. The embedding model is downloaded on first use (~220 MB), not shipped. |

### Gemini provider (`requirements-gemini.txt`, optional)

| Package | License | How it is used |
|---------|---------|----------------|
| google-genai | Apache-2.0 | Google Gemini provider (text chat + Live audio). Opt-in cloud feature; the user supplies their own key. |

### Computer-control pack (`requirements-computer.txt`, optional)

| Package | License | How it is used |
|---------|---------|----------------|
| pyautogui | BSD-3-Clause | Mouse/keyboard control for the `computer` MCP tools. |
| mss | MIT | Fast screen capture. |
| pygetwindow | BSD-3-Clause | Window enumeration/focus. |
| pyperclip | BSD-3-Clause | Clipboard read/write. |
| pyscreeze | MIT | pyautogui transitive dep (screenshots). |
| pytweening | MIT | pyautogui transitive dep (motion easing). |
| pymsgbox | BSD-3-Clause | pyautogui transitive dep. |
| mouseinfo | BSD-3-Clause | pyautogui transitive dep. |
| pyrect | BSD-3-Clause | pyautogui transitive dep. |

### Development / test (`requirements-dev.txt`, optional)

| Package | License | How it is used |
|---------|---------|----------------|
| pytest | MIT | Test runner. |
| pytest-asyncio | Apache-2.0 | Async test support. |

---

## Notes

- License identifiers reflect the license each upstream project declares at the
  time of writing. Where a project offers a dual license (e.g. `cryptography`),
  both options are listed. Always consult the upstream project for the
  authoritative and current license text.
- Models downloaded at runtime (XTTS-v2 weights, the fastembed embedding model,
  the openWakeWord feature models, provider models) are fetched onto the user's
  machine and are **not** redistributed by Akana. Their respective licenses
  apply on download; the XTTS-v2 CPML non-commercial restriction is the one to
  note.
- This list covers the notable runtime and vendored components. Transitively
  pulled sub-dependencies of the packages above carry their own (predominantly
  permissive) licenses; run a license scanner over your installed environment
  for a complete, machine-generated inventory.
