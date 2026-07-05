/* AudioWorklet — mic capture without deprecated ScriptProcessorNode.
   PERF: process() is called in 128-sample quanta; one postMessage per quantum
   = ~375 msg/s, plus appendRawToBuffer on the main thread realloc+copies the
   whole ring-buffer each time (GC jank even at idle). Fix: batch samples into
   ~2048-sample blocks (~43 ms @48k) before posting → ~23 msg/s (16× fewer).
   VAD/silence timing is unaffected because downstream computes chunkMs from the
   real chunk length (functionally transparent). */
const _BATCH = 2048;

class AkanaCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(_BATCH);
    this._n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) {
      let i = 0;
      while (i < ch.length) {
        const room = this._buf.length - this._n;
        const take = room < ch.length - i ? room : ch.length - i;
        this._buf.set(ch.subarray(i, i + take), this._n);
        this._n += take;
        i += take;
        if (this._n >= this._buf.length) {
          this.port.postMessage(this._buf.slice(0, this._n));
          this._n = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("akana-capture", AkanaCaptureProcessor);
