ARG BASE_IMAGE=sha256:4300d9c34beeff15b37ef30cd87a426e3230b613e76fb48ef5f10a84e5673a31
FROM ${BASE_IMAGE}

COPY deployment/stable/npu/yinka/warmup.patch deployment/stable/npu/yinka/predictor-prefix.patch /opt/odion/
COPY deployment/stable/npu/yinka/warmup.py /vllm-workspace/vllm-omni/vllm_omni/entrypoints/openai/yinka_warmup.py
RUN cd /vllm-workspace/vllm-omni \
    && echo 'dc458bd42ab49cd15c70eaac432c4e071a0763ce28a47dc9da82399a1fbd8e1f  vllm_omni/entrypoints/openai/serving_speech.py' | sha256sum -c - \
    && echo '8d1047c251273912c24fccef0380dc8a558eb248bcc28bfa3101b88cf5c67467  vllm_omni/model_executor/models/common/qwen3_code_predictor.py' | sha256sum -c - \
    && patch --batch --fuzz=0 -p1 < /opt/odion/warmup.patch \
    && patch --batch --fuzz=0 -p1 < /opt/odion/predictor-prefix.patch
ENV ODION_YINKA_WARMUP=1
