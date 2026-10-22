# Lagos voice worker image — built from this repo (main).
#
# Mirrors the release-dir recipe (/opt/attentive/releases/*/Python.Dockerfile)
# so an image built here is equivalent to one built on the box.
FROM python:3.12.7-slim-bookworm@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libsndfile1 libportaudio2 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The frozen dependency set is the source of truth for the worker runtime.
# NOTE: it must include every runtime import the worker makes, e.g.
#   pyrnnoise  (RNNoise input-noise suppression)
#   webauthn   (passkey confirmation demo path)
COPY latency_lab/handoff/hybrid-python-freeze.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && pip check

COPY . /app/

LABEL org.opencontainers.image.vendor="OdionAI"

# The Lagos Compose service supplies the command:
#   python -m latency_lab.livekit_worker start
