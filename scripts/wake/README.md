# Wake-word model trainer — custom "Hey Akana" (openWakeWord)

Trains a local acoustic wake-word model so "Hey Akana" is detected on-device instead of
by the browser `SpeechRecognition` phrase-match. Fully local; no cloud, no third-party key.
The browser-SR phrase-match was the quick-win Plan A; this trainer is **Plan B** — a bundled, on-device model with no cloud dependency.

## Why the output "just works" with the server

The exported `hey_akana.onnx` has the exact input/output contract of openWakeWord's shipped
models — input `x` `[1, 16, 96]` (16 speech-embedding frames), output `score` `[1, 1]` in
`(0, 1)`. The Akana server's `/voice/wake` scoring path (see `akana_server/api/routes/voice.py`)
already consumes that format; you only set `WAKE_MODEL` to the file's path.

## Pipeline

```
audio 16 kHz ─► melspectrogram.onnx ─► embedding_model.onnx ─► [frames,96]
             (both bundled with openWakeWord, run via onnxruntime)
   ─► slide 16-frame windows ─► classifier (trained here) ─► score
```

- **Positives**: `Hey Akana` / `Akana` variants synthesized with Piper across several EN + TR
  voices and prosody settings, then audio-domain augmentation (gain, noise at random SNR,
  light reverb). The last few 16-frame windows of each clip (phrase just completed) are the
  positive examples.
- **Negatives**: openWakeWord's precomputed real-audio feature set
  (`validation_set_features.npy`, ~180 MB) **plus** synthesized hard-negative distractors
  (`Hakan`, `kanka`, `banana`, `a corner`, …) so the model learns to reject near-misses.
- **Classifier**: a small MLP (`[16,96] → 128 → 64 → 1`), BCE with `pos_weight`, early-stopped
  on recall at ≤1 % false-positive rate. Sigmoid is baked into the exported ONNX.

## Requirements

Installed into the project venv (already present after setup, plus training extras):

```
openwakeword  piper (piper-tts)  onnxruntime  numpy  scipy        # runtime + features
torch (CPU is fine)  onnx                                          # train + export
```

Assets fetched once (a helper `fetch.py` in the work dir does this):
- Piper voices `<voice>.onnx` (+ `.onnx.json`) under `$WAKE_VOICES`
  (EN: lessac, amy, ryan · TR: dfki, fahrettin, fettah).
- `negative_features.npy` — openWakeWord `validation_set_features.npy`.

## Run

```bash
export WAKE_WORK=/path/to/work_dir           # holds voices/, features, artifacts
python scripts/wake/build_wake_model.py all  # data → train → eval → export
# → $WAKE_WORK/hey_akana.onnx
```

`data` and `train` can be run separately (regenerate features once, retrain many times).

The trainer prints a **threshold sweep**; pick `wake_threshold` at an acceptable
false-accept rate. It also prints a smoke score on silence (should be low).

## Ship it

1. Copy `hey_akana.onnx` into the repo, e.g. `akana_server/voice/wake_models/hey_akana.onnx`
   (a checked-in candidate already lives there).
2. Set `WAKE_MODEL=<path>` and `WAKE_THRESHOLD` (env / settings). **Start at `0.5`** and
   tune on your mic — see the validation below.
3. `GET /voice/wake/config` now reports enabled; the browser prefers the server wake path
   and SpeechRecognition stays as fallback.

## Candidate validation (checked-in model)

Trained on 2880 positive / 5859 hard-negative / 115 200 real-negative embedding windows
(3 EN + Turkish dfki voices, TR double-weighted; group-balanced sampling):

| threshold | true-accept | hard-neg false-accept | real-neg false-accept |
|-----------|-------------|-----------------------|-----------------------|
| 0.50      | 0.956       | 0.015                 | 0.0009                |
| 0.60      | 0.951       | 0.013                 | 0.0008                |
| 0.70      | 0.938       | 0.010                 | 0.0006                |

Standalone check: positive score mean 0.994, hard-negative ("Hakan", "banana", "bakana",
"arkana", …) mean 0.045 — the near-miss words are strongly rejected. `0.5` clears the
all-zeros artifact (pure digital silence scores ~0.44, but a real mic never sends zeros);
raise toward `0.6–0.7` if you hear any false wakes, lower toward `0.4` if it misses you.

## Validate on real hardware first

Synthetic-only training can drift from your actual mic/room. Before making it the default,
say "Hey Akana" into the real microphone across distances/noise and confirm the trigger rate
and false-wakes are acceptable; nudge `WAKE_THRESHOLD` as needed. For higher quality, add
[`piper-sample-generator`](https://github.com/rhasspy/piper-sample-generator) for more speaker
variety and/or download the larger openWakeWord negative feature set (17 GB) instead of the
180 MB validation slice.
