//! Builds Xiph RNNoise v0.2 from the vendored `third_party/rnnoise` source.
//!
//! The neural model (`src/rnnoise_data.c`, ~78 MB generated C) is not vendored;
//! it is the exact blob referenced by `third_party/rnnoise/model_version`. It is
//! fetched from Xiph's media server, checksum-verified, and extracted under
//! `OUT_DIR` (which the Docker CI build caches through the cargo target mount).
//! This is the same RNNoise v0.2 model the Python worker's `pyrnnoise` wraps, so
//! the native worker and the Python worker suppress noise identically.
//!
//! The C library is only built when the `rvc` feature is enabled.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const MODEL_URL: &str = "https://media.xiph.org/rnnoise/models";

/// Vendored C sources (the generated model is separate, see `MODEL_MEMBER`).
const VENDORED_SOURCES: &[&str] = &[
    "denoise.c",
    "rnn.c",
    "pitch.c",
    "kiss_fft.c",
    "celt_lpc.c",
    "nnet.c",
    "nnet_default.c",
    "parse_lpcnet_weights.c",
    "rnnoise_tables.c",
];

const MODEL_MEMBERS: &[&str] = &[
    "src/rnnoise_data.c",
    "src/rnnoise_data.h",
    "src/rnnoise_data_little.c",
    "src/rnnoise_data_little.h",
];

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=third_party/rnnoise/model_version");

    // Only compile the C library for feature sets that include the RVC worker.
    if env::var_os("CARGO_FEATURE_RVC").is_none() {
        return;
    }

    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let root = manifest.join("third_party/rnnoise");
    let src = root.join("src");
    let include = root.join("include");

    let model_version = fs::read_to_string(root.join("model_version"))
        .expect("read third_party/rnnoise/model_version")
        .trim()
        .to_string();

    let model_dir = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR")).join("rnnoise_model");
    let model_c = model_dir.join("src/rnnoise_data.c");
    if !model_c.exists() {
        fetch_model(&model_dir, &model_version);
    }

    let mut build = cc::Build::new();
    build
        .include(&include)
        .include(&src)
        .include(model_dir.join("src"));
    // Portable C path only (no AVX/SSE dispatch): RNNoise selects RTCD_ARCH `c`
    // in nnet_default.c, which keeps the build platform-independent.
    build.warnings(false).opt_level(2).pic(true);
    for name in VENDORED_SOURCES {
        let path = src.join(name);
        assert!(
            path.exists(),
            "missing vendored RNNoise source: {}",
            path.display()
        );
        build.file(path);
    }
    build.file(&model_c);
    build.compile("rnnoise");
}

fn fetch_model(model_dir: &Path, model_version: &str) {
    let archive = format!("rnnoise_data-{model_version}.tar.gz");
    let url = format!("{MODEL_URL}/{archive}");
    fs::create_dir_all(model_dir).expect("create model dir");
    let tmp = model_dir.join(&archive);

    eprintln!("build.rs: downloading RNNoise model {url}");
    let status = Command::new("curl")
        .args([
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--output",
        ])
        .arg(&tmp)
        .arg(&url)
        .status()
        .expect("failed to spawn curl to download the RNNoise model");
    assert!(status.success(), "curl failed to download {url}");

    let bytes = fs::read(&tmp).expect("read downloaded model archive");
    let digest = hex_sha256(&bytes);
    assert_eq!(
        digest, model_version,
        "RNNoise model checksum mismatch: expected {model_version}, got {digest}"
    );

    // Extract only the generated model C/headers (skip training .pth blobs).
    let status = Command::new("tar")
        .arg("-xzf")
        .arg(&tmp)
        .arg("-C")
        .arg(model_dir)
        .args(MODEL_MEMBERS)
        .status()
        .expect("failed to spawn tar to extract the RNNoise model");
    assert!(status.success(), "tar failed to extract {archive}");
    assert!(
        model_dir.join("src/rnnoise_data.c").exists(),
        "model extraction produced no rnnoise_data.c"
    );

    let _ = fs::remove_file(&tmp);
}

fn hex_sha256(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
