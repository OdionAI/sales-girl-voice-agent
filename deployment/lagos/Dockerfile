# Native Lagos worker; existing environment and mounted data supply runtime state.
FROM rust:1.99.0-bookworm@sha256:114c7a4425406451c2866b6aafe69fe29b1b298832db1277d411ac73c82d04d6 AS build
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    curl --fail --silent --show-error https://apt.llvm.org/llvm-snapshot.gpg.key -o /tmp/llvm-key.asc && \
    gpg --show-keys --with-colons /tmp/llvm-key.asc | grep '^fpr:.*:6084F3CF814B57C1CF12EFD515CF4D18AF4F7421:' && \
    gpg --dearmor -o /usr/share/keyrings/llvm.gpg /tmp/llvm-key.asc && \
    echo 'deb [signed-by=/usr/share/keyrings/llvm.gpg] https://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-21 main' > /etc/apt/sources.list.d/llvm.list && \
    apt-get update && apt-get install -y --no-install-recommends cmake clang-21 libclang-21-dev pkg-config libssl-dev unzip && \
    rm -rf /var/lib/apt/lists/* /tmp/llvm-key.asc
WORKDIR /src
ENV CARGO_BUILD_JOBS=2 CC=clang-21 CXX=clang++-21 LIBCLANG_PATH=/usr/lib/llvm-21/lib
COPY rust/Cargo.toml rust/Cargo.lock ./
COPY rust/build.rs ./build.rs
COPY rust/src ./src
COPY rust/fixtures ./fixtures
COPY rust/third_party ./third_party
RUN --mount=type=cache,id=sg-rvc-cargo-registry,target=/usr/local/cargo/registry --mount=type=cache,id=sg-rvc-linux-target,target=/src/target \
    cargo test --locked --release --all-features --lib && \
    cargo build --locked --release --features rvc --bin rvc-worker && \
    mkdir /out && cp target/release/rvc-worker /out/
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/rvc-worker /usr/local/bin/rvc-worker
RUN mkdir -p /app/latency_lab/artifacts/traces /app/latency_lab/artifacts/reports && chown -R 65534:65534 /app
WORKDIR /app
USER 65534:65534
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s --retries=3 CMD ["/usr/local/bin/rvc-worker", "--healthcheck"]
ENTRYPOINT ["/usr/local/bin/rvc-worker"]

CMD ["--env"]
