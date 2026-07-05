/* AudioWorklet — Gemini Live 24kHz PCM16 playback queue + barge-in flush.
 *
 * The main thread forwards raw PCM16 (little-endian, 24kHz mono) ArrayBuffers
 * received from the WebSocket via `port.postMessage(buf, [buf])` (transfer);
 * the worklet converts them into a Float32 queue and streams them to the
 * `process` output. A `{type:"flush"}` message (model `interrupted` → barge-in)
 * flushes the queue IMMEDIATELY: when the user interrupts, the assistant's
 * in-progress audio is cut off.
 *
 * NOTE: The playback AudioContext is created at 24000 Hz (akana-voice-live.js) →
 * no resampling here; incoming 24k samples are written one-to-one to the output.
 */
class AkanaPcmPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._queue = []; // Float32Array chunks (FIFO)
    this._cur = null;
    this._pos = 0;
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d && d.type === "flush") {
        this._queue = [];
        this._cur = null;
        this._pos = 0;
        return;
      }
      if (d instanceof ArrayBuffer && d.byteLength) {
        const i16 = new Int16Array(d);
        const f32 = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;
        this._queue.push(f32);
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0] && outputs[0][0];
    if (!out) return true;
    let i = 0;
    while (i < out.length) {
      if (!this._cur || this._pos >= this._cur.length) {
        this._cur = this._queue.shift() || null;
        this._pos = 0;
        if (!this._cur) {
          while (i < out.length) out[i++] = 0; // queue empty → silence
          break;
        }
      }
      out[i++] = this._cur[this._pos++];
    }
    return true; // keep worklet alive (for the duration of the session)
  }
}

registerProcessor("akana-pcm-playback", AkanaPcmPlaybackProcessor);
