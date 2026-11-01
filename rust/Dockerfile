# Local validation image only. Never publishes, deploys, or loads production env.
FROM rust:1.99.0-bookworm@sha256:114c7a4425406451c2866b6aafe69fe29b1b298832db1277d411ac73c82d04d6 AS build
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    curl --fail --silent --show-error https://apt.llvm.org/llvm-snapshot.gpg.key -o /tmp/llvm-key.asc && \
    gpg --show-keys --with-colons /tmp/llvm-key.asc | grep '^fpr:.*:6084F3CF814B57C1CF12EFD515CF4D18AF4F7421:' && \
    gpg --dearmor -o /usr/share/keyrings/llvm.gpg /tmp/llvm-key.asc && \
    echo 'deb [signed-by=/usr/share/keyrings/llvm.gpg] https://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-21 main' > /etc/apt/sources.list.d/llvm.list && \
    apt-get update && apt-get install -y --no-install-recommends cmake clang-21 libclang-21-dev pkg-config libssl-dev unzip && \
    rm -rf /var/lib/apt/lists/* /tmp/llvm-key.asc
WORKDIR /src
ENV CARGO_BUILD_JOBS=2 CC=clang-21 CXX=clang++-21 LIBCLANG_PATH=/usr/lib/llvm-21/lib \
    RNNOISE_MODEL_PATH=/opt/rnnoise/rnnoise_data-0a8755f8e2d834eff6a54714ecc7d75f9932e845df35f8b59bc52a7cfe6e8b37.tar.gz
# Fetch the pinned RNNoise model once with curl (retries + checksum), so
# rnnoise2-sys unpacks it via RNNOISE_MODEL_PATH instead of downloading it with
# its build-time reqwest client, which times out in this build environment.
RUN mkdir -p /opt/rnnoise && \
    curl --fail --location --retry 8 --retry-delay 5 --connect-timeout 30 \
        -o "$RNNOISE_MODEL_PATH" \
        "https://media.xiph.org/rnnoise/models/rnnoise_data-0a8755f8e2d834eff6a54714ecc7d75f9932e845df35f8b59bc52a7cfe6e8b37.tar.gz" && \
    echo "0a8755f8e2d834eff6a54714ecc7d75f9932e845df35f8b59bc52a7cfe6e8b37  $RNNOISE_MODEL_PATH" | sha256sum -c -
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY fixtures ./fixtures
RUN --mount=type=cache,id=sg-rvc-cargo-registry,target=/usr/local/cargo/registry --mount=type=cache,id=sg-rvc-linux-target,target=/src/target \
    cargo test --locked --release --all-features --lib && \
    cargo build --locked --release --all-features --bins && \
    mkdir /out && cp target/release/rvc-worker target/release/native-worker-smoke target/release/worker-control-smoke /out/
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/rvc-worker /usr/local/bin/rvc-worker
COPY --from=build /out/native-worker-smoke /usr/local/bin/native-worker-smoke
COPY --from=build /out/worker-control-smoke /usr/local/bin/worker-control-smoke
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/rvc-worker"]
