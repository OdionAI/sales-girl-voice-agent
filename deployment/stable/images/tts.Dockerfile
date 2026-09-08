FROM quay.io/ascend/vllm-omni@sha256:acba66221a39170cbc405c7e7aa69b73b1905e04ef92f04943a61bb3878d6761

LABEL org.opencontainers.image.title="Odion stable Qwen3-TTS runtime"
LABEL org.opencontainers.image.description="Pinned Ascend vLLM Omni image with the verified low-latency TTS configuration"
LABEL ai.odion.vllm.revision="e5588e49bc2642670116664a7fc4096e27adb179"
LABEL ai.odion.vllm-ascend.revision="8092d3f66599ce07cd0aca2bcc99d14b8a9192f8"
LABEL ai.odion.vllm-omni.revision="3f4abedbda281302b28f7885c270fce3e342c924"

COPY deployment/stable/npu/tts/qwen3_tts_fidelity.yaml /opt/odion/qwen3_tts_fidelity.yaml
COPY deployment/stable/npu/tts/start-tts.sh /opt/odion/start-tts.sh

RUN chmod 0755 /opt/odion/start-tts.sh

CMD ["bash"]
