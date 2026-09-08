FROM quay.io/ascend/vllm-ascend@sha256:9008b47081282612abfe4d28069ce34436752c980fd06f7599343213205ce64d

LABEL org.opencontainers.image.title="Odion stable Qwen3-ASR runtime"
LABEL org.opencontainers.image.description="Pinned Ascend vLLM image with the verified realtime cumulative ASR overlays"
LABEL ai.odion.vllm.revision="0decac0d96c42b49572498019f0a0e3600f50398"
LABEL ai.odion.vllm-ascend.revision="5f6faa0cb8830f667266f3b8121cd1383606f2a1"

COPY deployment/stable/npu/asr/qwen3_asr_realtime.py /workspace/vllm/vllm/model_executor/models/qwen3_asr_realtime.py
COPY deployment/stable/npu/asr/connection.py /workspace/vllm/vllm/entrypoints/speech_to_text/realtime/connection.py
COPY deployment/stable/npu/asr/run-realtime.sh /opt/odion/run-realtime.sh

RUN chmod 0755 /opt/odion/run-realtime.sh

CMD ["bash"]
