#!/usr/bin/env python
"""Train a custom openWakeWord "Hey Akana" model, fully locally.

Pipeline (see scripts/wake/README.md for the full write-up):

    audio (16 kHz)
      -> melspectrogram.onnx        (openWakeWord, bundled)
      -> embedding_model.onnx       (openWakeWord, bundled)  -> [frames, 96]
      -> slide 16-frame windows     -> model input [1, 16, 96]
      -> tiny classifier (this file) -> [1, 1] score in (0,1)

The exported ``hey_akana.onnx`` has the SAME input/output contract as the shipped
``hey_jarvis_v0.1.onnx`` ([1,16,96] -> [1,1]), so the Akana server's existing
``/voice/wake`` scoring path consumes it unchanged — set ``WAKE_MODEL`` to its path.

Positives are synthesized with Piper (multiple EN + TR voices, varied prosody, then
audio-domain augmentation). Negatives come from openWakeWord's precomputed real-audio
feature set plus synthesized hard-negative distractors (Hakan, kanka, banana, ...).

This is a *candidate* trainer: validate the result on a real microphone before
shipping it as the default (synthetic-only training can drift from real hardware).

Usage:
    python scripts/wake/build_wake_model.py all           # data + train + eval + export
    python scripts/wake/build_wake_model.py data          # just (re)build feature arrays
    python scripts/wake/build_wake_model.py train         # train + eval + export from cached data

Env:
    WAKE_WORK   working dir for artifacts (default: ./.wake_work)
    WAKE_VOICES dir with piper <voice>.onnx (+ .onnx.json) files (default: $WAKE_WORK/voices)
    WAKE_NEGFEAT path to openWakeWord negative feature .npy (default: $WAKE_WORK/negative_features.npy)
"""
from __future__ import annotations

import os
import sys
import glob
import time
import argparse

import numpy as np

SR = 16000              # openWakeWord operates at 16 kHz mono
N_CTX = 16              # embedding frames per model input window
EMB = 96               # embedding dim
TOTAL_MS = 2400         # positive/negative buffer length fed to the embedder
RNG = np.random.default_rng(1337)

WORK = os.environ.get("WAKE_WORK", os.path.join(os.getcwd(), ".wake_work"))
VOICES_DIR = os.environ.get("WAKE_VOICES", os.path.join(WORK, "voices"))
NEGFEAT = os.environ.get("WAKE_NEGFEAT", os.path.join(WORK, "negative_features.npy"))
os.makedirs(WORK, exist_ok=True)

# --- phrase inventory -------------------------------------------------------
# Positives: the wake phrase and natural variants, in both shipped UI languages.
POSITIVE_TEXTS = [
    "Hey Akana", "Hey, Akana", "Akana", "Hey Akana.", "Hi Akana",
    "Hey Akanaa", "Ey Akana", "Hey Akana!",
]
# Hard negatives: phonetic near-misses + common words that must NOT wake it.
NEGATIVE_TEXTS = [
    # Turkish RHYMING near-misses — real words that sound close to "akana" and WILL
    # occur in normal Turkish speech; these are the most important negatives.
    "arkana", "bakana", "yakana", "takana", "çakana", "kına", "kana", "kanı",
    "makina", "makine", "yakına", "arkanda", "bakında", "akına", "hakana geldi",
    # Turkish common / other near-misses
    "Hakan", "kanka", "kanal", "kanepe", "makarna", "Ankara", "anlarsın",
    "akıl", "akın", "hakem", "kavanoz", "Adana", "kabak", "nasılsın", "kanat",
    "anahtar", "akademi", "Furkan", "kansız", "hangi", "bir dakika", "tamam",
    "teşekkürler", "günaydın", "naber",
    # English near-misses / common
    "banana", "arcana", "a corner", "cabana", "Havana", "a can of soda",
    "open the app", "hey there", "cannot", "America", "the answer", "okay then",
    "a kind of", "our corner", "a comma",
]


def log(msg: str) -> None:
    print(f"[wake] {msg}", flush=True)


# --- audio helpers ----------------------------------------------------------
def _resample_to_16k(audio_f32: np.ndarray, src_sr: int) -> np.ndarray:
    if src_sr == SR:
        return audio_f32
    from scipy.signal import resample_poly
    from math import gcd
    g = gcd(src_sr, SR)
    return resample_poly(audio_f32, SR // g, src_sr // g).astype(np.float32)


def _trim_silence(x: np.ndarray, thresh: float = 0.01) -> np.ndarray:
    idx = np.where(np.abs(x) > thresh)[0]
    if idx.size == 0:
        return x
    return x[max(0, idx[0] - 320): idx[-1] + 320]


def _place_in_buffer(phrase: np.ndarray, end_lo_ms: int, end_hi_ms: int) -> np.ndarray:
    """Put the phrase into a fixed TOTAL_MS buffer, phrase END near the buffer end."""
    total = int(TOTAL_MS * SR / 1000)
    buf = np.zeros(total, dtype=np.float32)
    p = phrase[:total]
    end = int(RNG.integers(end_lo_ms, end_hi_ms) * SR / 1000)
    end = min(end, total)
    start = max(0, end - len(p))
    buf[start:start + len(p)] = p[: total - start]
    return buf


def _augment(x: np.ndarray) -> np.ndarray:
    y = x.copy()
    # gain
    y *= float(RNG.uniform(0.45, 1.0))
    # additive noise at a random SNR
    if RNG.random() < 0.85:
        sig = np.mean(y ** 2) + 1e-9
        snr = float(RNG.uniform(5, 25))
        npow = sig / (10 ** (snr / 10))
        noise = RNG.normal(0, np.sqrt(npow), size=y.shape).astype(np.float32)
        if RNG.random() < 0.5:  # pink-ish: cumulative-sum then renormalize
            noise = np.cumsum(noise)
            noise = noise / (np.std(noise) + 1e-9) * np.sqrt(npow)
        y = y + noise
    # light synthetic reverb (exp-decay IR)
    if RNG.random() < 0.4:
        ir_len = int(RNG.uniform(0.02, 0.12) * SR)
        ir = (RNG.normal(0, 1, ir_len) * np.exp(-np.linspace(0, 5, ir_len))).astype(np.float32)
        ir[0] = 1.0
        y = np.convolve(y, ir)[: len(x)].astype(np.float32)
    m = np.max(np.abs(y)) + 1e-9
    if m > 1.0:
        y = y / m
    return y.astype(np.float32)


# --- piper synthesis --------------------------------------------------------
def _load_voices():
    from piper import PiperVoice
    paths = sorted(glob.glob(os.path.join(VOICES_DIR, "*.onnx")))
    if not paths:
        log(f"NO piper voices in {VOICES_DIR} — run the fetch step first.")
        sys.exit(2)
    voices = []
    for p in paths:
        try:
            v = PiperVoice.load(p)
            voices.append((os.path.basename(p), v))
        except Exception as e:  # noqa: BLE001
            log(f"skip voice {os.path.basename(p)}: {e}")
    log(f"loaded {len(voices)} piper voices: {[n for n, _ in voices]}")
    return voices


def _synth(voice, text: str, length_scale: float, noise_scale: float) -> np.ndarray:
    from piper import SynthesisConfig
    cfg = SynthesisConfig(
        length_scale=length_scale, noise_scale=noise_scale,
        noise_w_scale=float(RNG.uniform(0.6, 1.0)), normalize_audio=True,
    )
    chunks = list(voice.synthesize(text, syn_config=cfg))
    if not chunks:
        return np.zeros(1, dtype=np.float32)
    audio = np.concatenate([c.audio_int16_array.astype(np.float32) / 32768.0 for c in chunks])
    return _resample_to_16k(audio, voice.config.sample_rate)


def _windows_from_clip(af, clip16: np.ndarray, take: str) -> np.ndarray:
    """Embed a fixed-length clip and return [k, N_CTX, 96] windows.

    take='end'    -> the last few windows (positive: phrase just completed)
    take='stride' -> all windows, stride 2 (negatives: reject throughout)
    """
    emb = af.embed_clips(clip16[None, :].astype(np.int16), batch_size=1)[0]  # [frames, 96]
    frames = emb.shape[0]
    if frames < N_CTX:
        return np.zeros((0, N_CTX, EMB), dtype=np.float32)
    if take == "end":
        starts = [s for s in (frames - N_CTX - d for d in (0, 1, 2, 3)) if s >= 0]
    else:
        starts = list(range(0, frames - N_CTX + 1, 2))
    return np.stack([emb[s:s + N_CTX] for s in starts]).astype(np.float32)


def build_dataset() -> None:
    import openwakeword.utils as u
    af = u.AudioFeatures()
    voices = _load_voices()

    log("synthesizing + embedding POSITIVES ...")
    pos = []
    for text in POSITIVE_TEXTS:
        for vname, v in voices:
            for ls in (0.85, 1.0, 1.18):
                base = _trim_silence(_synth(v, text, ls, float(RNG.uniform(0.5, 0.85))))
                if base.size < SR // 4:
                    continue
                # Turkish is the primary spoken language here but piper-voices ships only
                # one TR speaker (dfki), so double its augmentation to balance the set.
                n_aug = 12 if vname.startswith("tr_") else 6
                for _ in range(n_aug):  # audio-domain augmentation copies
                    clip = _augment(_place_in_buffer(base, 1850, 2350))
                    clip16 = np.clip(clip * 32768.0, -32768, 32767).astype(np.int16)
                    w = _windows_from_clip(af, clip16, take="end")
                    if len(w):
                        pos.append(w)
    pos = np.concatenate(pos, axis=0) if pos else np.zeros((0, N_CTX, EMB), np.float32)
    log(f"positive windows: {pos.shape}")

    log("synthesizing + embedding HARD NEGATIVES ...")
    hneg = []
    for text in NEGATIVE_TEXTS:
        for vname, v in voices:
            for ls in (0.9, 1.05, 1.2):
                base = _trim_silence(_synth(v, text, ls, float(RNG.uniform(0.5, 0.85))))
                if base.size < SR // 4:
                    continue
                for _ in range(3):
                    clip = _augment(_place_in_buffer(base, 1600, 2350))
                    clip16 = np.clip(clip * 32768.0, -32768, 32767).astype(np.int16)
                    w = _windows_from_clip(af, clip16, take="stride")
                    if len(w):
                        hneg.append(w)
    hneg = np.concatenate(hneg, axis=0) if hneg else np.zeros((0, N_CTX, EMB), np.float32)
    log(f"hard-negative windows: {hneg.shape}")

    np.save(os.path.join(WORK, "pos.npy"), pos)
    np.save(os.path.join(WORK, "hardneg.npy"), hneg)
    log(f"saved pos.npy / hardneg.npy to {WORK}")


def _load_negatives(n_pos: int) -> np.ndarray:
    """Real-audio negatives from openWakeWord's precomputed feature file."""
    if not os.path.exists(NEGFEAT):
        log(f"WARNING: {NEGFEAT} missing — training on hard-negatives only (weaker).")
        return np.zeros((0, N_CTX, EMB), np.float32)
    a = np.asarray(np.load(NEGFEAT, mmap_mode="r"))
    if a.ndim == 2 and a.shape[1] == EMB:
        # continuous embedding stream (frames, 96) -> sliding 16-frame windows (a VIEW)
        sw = np.lib.stride_tricks.sliding_window_view(a, (N_CTX, EMB))[:, 0, :, :]
    elif a.ndim == 3 and a.shape[1] == N_CTX and a.shape[2] == EMB:
        sw = a  # already windowed (M, 16, 96)
    elif a.ndim == 3 and a.shape[2] == EMB:
        # (M, frames, 96) -> concat per-row sliding windows
        sw = np.concatenate(
            [np.lib.stride_tricks.sliding_window_view(row, (N_CTX, EMB))[:, 0, :, :]
             for row in a[:: max(1, a.shape[0] // 4000)]], axis=0)
    else:
        log(f"unexpected negative-feature shape {a.shape} — skipping real negatives.")
        return np.zeros((0, N_CTX, EMB), np.float32)
    # cap so training stays balanced-ish (BCE pos_weight handles the rest);
    # index-then-copy so only the capped subset is materialized.
    cap = int(min(len(sw), max(20000, n_pos * 40)))
    idx = RNG.choice(len(sw), size=cap, replace=False)
    log(f"real negatives used: {cap} (from {len(sw)} windows)")
    return np.ascontiguousarray(sw[idx]).astype(np.float32)


def train_and_export() -> None:
    import torch
    from torch import nn

    pos = np.load(os.path.join(WORK, "pos.npy"))
    hneg = np.load(os.path.join(WORK, "hardneg.npy"))
    rneg = _load_negatives(len(pos))
    log(f"dataset: {len(pos)} pos / {len(hneg)} hard-neg / {len(rneg)} real-neg")
    if len(pos) < 50 or len(rneg) < 50 or len(hneg) < 20:
        log("not enough data — aborting.")
        sys.exit(3)

    def _split(a, frac=0.15):
        idx = RNG.permutation(len(a))
        n = int(frac * len(a))
        return a[idx[n:]], a[idx[:n]]

    pos_tr, pos_va = _split(pos)
    hn_tr, hn_va = _split(hneg)
    rn_tr, rn_va = _split(rneg)

    # Training pool tagged by group: 0=positive, 1=hard-negative, 2=real-negative.
    Xtr = np.concatenate([pos_tr, hn_tr, rn_tr]).astype(np.float32)
    gtr = np.concatenate([np.zeros(len(pos_tr)), np.ones(len(hn_tr)),
                          np.full(len(rn_tr), 2)]).astype(np.int64)
    ytr = (gtr == 0).astype(np.float32)
    # Group-balanced sampling: force the model to work the positive-vs-hard-negative
    # boundary (a global pos_weight instead just biased it to fire on phonetic
    # near-misses like "bakana"/"Hakan"). Real negatives stay as background.
    GROUP_P = np.array([0.42, 0.42, 0.16])  # pos / hard-neg / real-neg mass per batch
    counts = np.array([len(pos_tr), len(hn_tr), len(rn_tr)], dtype=np.float64)
    per_sample_p = (GROUP_P / counts)[gtr]
    per_sample_p = per_sample_p / per_sample_p.sum()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"training on {dev}")

    class WakeNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.net = nn.Sequential(
                nn.Flatten(),
                nn.Linear(N_CTX * EMB, 128), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(128, 64), nn.ReLU(), nn.Dropout(0.2),
                nn.Linear(64, 1),
            )

        def forward(self, x):
            return self.net(x)

    model = WakeNet().to(dev)
    lossf = nn.BCEWithLogitsLoss()  # balance comes from the sampler, not pos_weight
    opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)

    Xtr_t = torch.tensor(Xtr, device=dev)
    ytr_t = torch.tensor(ytr, device=dev)[:, None]

    def _scores(a):
        with torch.no_grad():
            return torch.sigmoid(model(torch.tensor(a.astype(np.float32), device=dev))).cpu().numpy().ravel()

    bs = 512
    steps = max(1, len(Xtr) // bs)
    best_state, best_score = None, -1.0
    for epoch in range(35):
        model.train()
        for _ in range(steps):
            b = RNG.choice(len(Xtr), size=bs, p=per_sample_p)
            bt = torch.as_tensor(b, device=dev)
            opt.zero_grad()
            loss = lossf(model(Xtr_t[bt]), ytr_t[bt])
            loss.backward()
            opt.step()
        model.eval()
        sp, sh, sr = _scores(pos_va), _scores(hn_va), _scores(rn_va)
        # selection score = true-accept at the threshold that holds hard-neg FA <= 3%
        thr = float(np.quantile(sh, 0.97))
        ta = float(np.mean(sp >= thr))
        if ta > best_score:
            best_score = ta
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        log(f"epoch {epoch:2d}  TA@hardFA3% {ta:.3f}  thr {thr:.2f}  "
            f"pos_mu {sp.mean():.2f}  hn_mu {sh.mean():.2f}  rn_mu {sr.mean():.2f}")
    model.load_state_dict(best_state)

    # --- per-group threshold sweep (hard-neg FA is the metric that matters) ---
    sp, sh, sr = _scores(pos_va), _scores(hn_va), _scores(rn_va)
    log("threshold sweep:  thr  true-accept  hardneg-FA  realneg-FA")
    best = None
    for thr in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        ta = float(np.mean(sp >= thr))
        fah = float(np.mean(sh >= thr))
        far = float(np.mean(sr >= thr))
        log(f"                  {thr:.2f}    {ta:6.3f}     {fah:6.3f}     {far:6.4f}")
        if best is None and fah <= 0.03 and far <= 0.01:
            best = (thr, ta, fah, far)
    if best:
        log(f"SUGGESTED wake_threshold = {best[0]:.2f}  (true-accept {best[1]:.2f}, "
            f"hardneg-FA {best[2]:.3f}, realneg-FA {best[3]:.4f})")
    else:
        log("No threshold met hardneg-FA<=3% & realneg-FA<=1% — inspect the sweep above.")

    # --- export ONNX with sigmoid baked in: [1,16,96] -> [1,1] ---
    class Exportable(nn.Module):
        def __init__(self, core):
            super().__init__()
            self.core = core

        def forward(self, x):
            return torch.sigmoid(self.core(x))

    exp = Exportable(model).to("cpu").eval()
    dummy = torch.zeros(1, N_CTX, EMB)
    out_path = os.path.join(WORK, "hey_akana.onnx")
    torch.onnx.export(
        exp, dummy, out_path,
        input_names=["x"], output_names=["score"],
        opset_version=13, dynamic_axes=None,
    )
    # The exporter can split weights into a .onnx.data sidecar; inline them so the
    # model is a single self-contained file that ships/copies safely.
    import onnx
    _m = onnx.load(out_path, load_external_data=True)
    onnx.save_model(_m, out_path, save_as_external_data=False)
    if os.path.exists(out_path + ".data"):
        os.remove(out_path + ".data")
    log(f"exported {out_path} (self-contained, {os.path.getsize(out_path)} bytes)")
    _verify_onnx(out_path)


def _recall_at_low_fpr(scores: np.ndarray, y: np.ndarray, max_fpr: float = 0.01) -> float:
    neg = scores[y == 0]
    pos = scores[y == 1]
    if len(neg) == 0 or len(pos) == 0:
        return 0.0
    thr = np.quantile(neg, 1 - max_fpr)
    return float(np.mean(pos >= thr))


def _report_thresholds(scores: np.ndarray, y: np.ndarray) -> None:
    pos = scores[y == 1]
    neg = scores[y == 0]
    log("threshold sweep (val):  thr   true-accept   false-accept-rate")
    best = None
    for thr in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        ta = float(np.mean(pos >= thr)) if len(pos) else 0.0
        fa = float(np.mean(neg >= thr)) if len(neg) else 0.0
        log(f"                        {thr:.2f}   {ta:6.3f}        {fa:6.4f}")
        if best is None and fa <= 0.005:
            best = (thr, ta, fa)
    if best:
        log(f"SUGGESTED wake_threshold = {best[0]:.2f}  (true-accept {best[1]:.2f}, "
            f"false-accept {best[2]:.4f} on val negatives)")
    else:
        log("No threshold hit <=0.5% false-accept on val — need more/better negatives.")


def _verify_onnx(path: str) -> None:
    import onnxruntime as ort
    s = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    i, o = s.get_inputs()[0], s.get_outputs()[0]
    log(f"onnx verify: in {i.name}{i.shape} -> out {o.name}{o.shape}")
    r = s.run(None, {i.name: np.zeros((1, N_CTX, EMB), np.float32)})[0]
    assert r.shape == (1, 1), r.shape
    log(f"onnx smoke score on silence: {float(r.ravel()[0]):.4f}  (expected low)")


def main() -> None:
    # torch.onnx's exporter prints status with emoji; a non-UTF8 Windows console
    # (e.g. cp1254) would crash on it. Force UTF-8 so export never dies on a print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["data", "train", "all"], nargs="?", default="all")
    args = ap.parse_args()
    t0 = time.time()
    if args.stage in ("data", "all"):
        build_dataset()
    if args.stage in ("train", "all"):
        train_and_export()
    log(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
