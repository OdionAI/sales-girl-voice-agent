# Yinka streaming reliability and performance tests

This directory targets the deployed `yinka-v7-test` customer-service checkpoint,
not the separate 1.7B voice. Do not run these experiments on a shared live NPU.
The production baseline is image ID
`sha256:4300d9c34beeff15b37ef30cd87a426e3230b613e76fb48ef5f10a84e5673a31`.
The image includes vLLM-Omni revision `3f4abedbda281302b28f7885c270fce3e342c924`.

## Startup readiness

`warmup.patch` hooks the existing awaited speech-startup lifecycle.
`ODION_YINKA_WARMUP=1` drains short, medium and long streaming requests, including
the decoder's full left-context window and the 256-frame talker boundary,
before the HTTP service starts accepting
traffic. It also warms concurrent batches of 2 and 4 requests. The complete
speech warmup has a 180-second deadline. Empty, silent, malformed, failed, or
stalled audio fails startup, cancelling sibling requests on failure.
The toggle defaults off in upstream code and is enabled in the candidate image.
It is only for this Yinka CustomVoice deployment; other voice services are unchanged.

Build on the NPU host from the repository root, pinning the installed baseline:

```bash
DOCKER_BUILDKIT=0 docker build \
  --build-arg BASE_IMAGE=sha256:4300d9c34beeff15b37ef30cd87a426e3230b613e76fb48ef5f10a84e5673a31 \
  -f deployment/stable/npu/yinka/Dockerfile \
  -t odion/yinka-stream-candidate:local .
```

## Test isolation

Check `npu-smi info` and coordinate card availability first. Place the production
deployment YAML (unchanged) at `$TEST_ROOT/config/production-deploy.yaml`, and
`probe.py` in that same config directory. `start-isolated.sh` refuses an existing
container, the production container name, or the production port. It binds only
to loopback, does not stop any container, and does not update the public jumper.

```bash
TEST_NPU=1 TEST_ROOT=/absolute/test/directory \
  TEST_IMAGE=odion/yinka-stream-candidate:local \
  bash deployment/stable/npu/yinka/start-isolated.sh
```

Run `probe.py` inside the test container against its loopback speech endpoint.
It saves PCM, playable WAV, per-chunk timings, and a JSON result even on failure.
An HTTP 200 alone is not success. A completed stream alone does not prove that
every word was spoken: inspect audio/transcript coverage too.

```bash
python3 probe.py --url http://127.0.0.1:8919/v1/audio/speech \
  --output /test-results/candidate --sizes short medium long money --repeats 20 \
  --language English
```

## Release gates

- Preserve streaming PCM, seed 1, initial chunk 6, steady chunk 10, left context
  72, the current sampling settings, and the 4096-token generation budget.
- Prove the first request after readiness does not pay lazy graph warmup.
- Require complete long-paragraph audio and clean stream termination.
- Compare baseline/candidate PCM hashes, durations, pronunciation, speaker
  identity, and sentence coverage. Reject numerical changes that hurt quality.
- Compare warm first PCM, total synthesis time, interchunk gaps, RTF, and codec
  frame steps/s (not text-token TPS). Test concurrency, cancellation, and recovery.
- Keep an immutable rollback image and original container configuration.
- Do not deploy an unverified optimization merely because it builds or is faster.

## Predictor experiment

`predictor-prefix.patch` adapts the existing prefix graph dispatcher to NPU
capture. It does not change attention, FP32 normalization/RoPE, sampling,
per-request generators, model weights, or the codec decoder. The new connector
extra setting `code_predictor_npu_prefix_graphs` defaults off, independently of
the CUDA option. For isolated testing, enable it with
`code_predictor_prefix_graph_buckets: [1, 2, 4]`. Positions 2 through 16 get their
own warmed graph; larger batch buckets retain the original 17-position path.
There is no persistent predictor KV state shared across requests.

Compare this against the warmup-only candidate with identical English request
settings. Validate seeded audio, concurrent requests, cancellation/recovery,
startup time and graph memory before considering a production enablement.
The image contains the patch, but it stays disabled unless the deployment YAML
explicitly enables `code_predictor_npu_prefix_graphs`. Removing that flag and
restarting restores the original full-length predictor graphs. Keep the original
image and YAML for a full rollback. This local image-ID build uses Docker's
classic builder; do not substitute a mutable remote image tag.

## Long-response conditioning

The saved Yinka profile defaults to `language: English`; the provider no longer
overwrites it with `Auto`. This checkpoint repeated the long English transfer
list under Auto, extending it to 107 seconds of audio. English completed all
four items and the final question in 25.36 seconds without changing streaming,
seed, chunk sizes, sampling, or the generation budget. Explicit language
overrides remain supported. Do not use `non_streaming_mode: true` as a shortcut:
that experiment truncated this paragraph after the second item.

`service_checks.py` exercises concurrency 1/2/4, then cancels a long response
and verifies that the next short response matches its pre-cancellation PCM.
Use `--concurrency 1 2 3 4 8` to cover non-power-of-two dispatch and the larger
batch full-length graph fallback. Per-request WAV files are retained for review.

## Measured comparison

Isolated Ascend 910B4-1, Yinka v7, 24 kHz streamed PCM, English, seed 1;
20 short and 20 medium requests per variant. These are serving measurements,
not end-to-end caller latency and not text-token throughput.

| Metric | Warm baseline | NPU prefix graphs |
| --- | ---: | ---: |
| Short first PCM, median / p95 | 396.7 / 403.6 ms | 363.7 / 370.2 ms |
| Medium first PCM, median / p95 | 395.8 / 400.4 ms | 360.6 / 366.5 ms |
| Short synthesis, median | 2.481 s | 2.228 s |
| Medium synthesis, median | 5.414 s | 4.859 s |
| Medium codec frames/s | 20.87 | 23.26 |

All 40 requests completed per variant. Seeded single-request PCM was identical
between variants, including separate long and money samples. The first long
request after three-text warmup completed in 13.62 seconds, with a maximum
interchunk gap of 439 ms instead of the earlier cold gaps of 6.27 and 3.38 s.
The original cold service took 25.83 seconds to first PCM; speech warmup moves
that work before readiness rather than pretending cold initialization is free.

Raw numerical pronunciation remains a separate pre-existing quality limitation:
ASR disagreed on the monetary amount in both Auto and English samples. Do not
claim financial-number accuracy from stream completion or hash equality. Preserve
the voice worker's existing spoken-amount normalization during deployment.

Local helper tests (Python 3.11+ and `requests`):

```bash
python3 -m unittest discover -s deployment/stable/npu/yinka -p 'test_*.py' -v
```
