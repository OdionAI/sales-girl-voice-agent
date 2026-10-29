# Extend the model-bearing Lagos image until its ONNX assets are versioned
# independently. The application sources below are tracked in this repo.
FROM attentive/voice-auth:wema-20261023-1

WORKDIR /app
RUN pip install --no-cache-dir 'azure-storage-blob>=12.22,<13' && pip check
COPY agent/voice_auth.py agent/voice_enroll_http.py agent/wema_store.py agent/wema_voiceprint.py /app/agent/
COPY scripts/run_voice_auth_http.py /app/scripts/run_voice_auth_http.py

LABEL org.opencontainers.image.vendor="OdionAI"
