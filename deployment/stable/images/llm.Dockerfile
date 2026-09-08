FROM quay.io/ascend/vllm-ascend@sha256:4ee78def8f33d59d48f116d1dfa793332c23c99ecab4f0d7dd5cd62d0fb4e6c1

LABEL org.opencontainers.image.title="Odion stable Qwen 3.8 27B runtime"
LABEL org.opencontainers.image.description="Pinned Ascend vLLM image for the verified Qwen 3.8 27B profile"
LABEL ai.odion.vllm.revision="0fc695fc6d1d82e9a5ac6835ac8e4e1c83703665"
LABEL ai.odion.vllm-ascend.revision="5cb98caaadeff42b5b62b996e34bb2aaa29d20fd"

COPY deployment/stable/npu/llm/start-vllm.sh /opt/odion/start-vllm.sh

RUN chmod 0755 /opt/odion/start-vllm.sh

CMD ["bash"]
