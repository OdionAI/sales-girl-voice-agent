class MicProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    const pcm = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const value = Math.max(-1, Math.min(1, input[i]));
      pcm[i] = value < 0 ? value * 32768 : value * 32767;
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}
registerProcessor('rvc-lab-mic', MicProcessor);
