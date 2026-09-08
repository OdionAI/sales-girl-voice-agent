class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.head = 0;
    this.offset = 0;
    this.samples = 0;
    this.started = false;
    this.ended = false;
    this.prebuffer = Math.round(sampleRate * 0.12);
    this.port.onmessage = ({data}) => {
      if (data && data.type === 'clear') {
        this.queue = []; this.head = 0; this.offset = 0; this.samples = 0; this.started = false; this.ended = false;
      } else if (data && data.type === 'end') {
        this.ended = true;
      } else {
        this.queue.push(data); this.samples += data.length;
      }
    };
  }
  process(_inputs, outputs) {
    const output = outputs[0][0]; output.fill(0);
    if (!this.started && this.samples < this.prebuffer && !this.ended) return true;
    if (!this.started && this.samples > 0) { this.started = true; this.port.postMessage({type:'started'}); }
    let index = 0;
    while (index < output.length && this.head < this.queue.length) {
      const chunk = this.queue[this.head];
      output[index++] = chunk[this.offset++] / 32768;
      this.samples--;
      if (this.offset === chunk.length) {
        this.head++; this.offset = 0;
        if (this.head > 256 && this.head * 2 > this.queue.length) {
          this.queue = this.queue.slice(this.head); this.head = 0;
        }
      }
    }
    if (this.started && this.samples === 0 && this.ended) {
      this.started = false; this.ended = false; this.queue = []; this.head = 0; this.offset = 0;
      this.port.postMessage({type:'stopped'});
    }
    return true;
  }
}
registerProcessor('rvc-lab-playback', PlaybackProcessor);
