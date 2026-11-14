#!/usr/bin/env python3
"""Select voice-agent CI suites from files changed in a PR or branch push."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable


FULL_SUITE_PATHS = {
    ".github/workflows/ci.yml",
    ".github/scripts/detect_ci_scope.py",
}


def classify(changed_paths: Iterable[str]) -> dict[str, bool]:
    python_tests = False
    passkey_tests = False
    native_tests = False

    for path in changed_paths:
        if path.endswith((".md", ".rst", ".adoc")) or path.startswith("docs/"):
            continue
        if path in FULL_SUITE_PATHS:
            python_tests = passkey_tests = native_tests = True
            continue
        if path == ".github/workflows/rust-unit-tests.yml":
            native_tests = True
            continue
        if path.startswith(".github/workflows/"):
            # GitHub parses workflow files; only CI orchestration changes opt
            # into the full suite above.
            continue
        if path.startswith("rust/"):
            if path in {
                "rust/Cargo.toml",
                "rust/Cargo.lock",
                "rust/Dockerfile",
                "rust/build.rs",
                "rust/.cargo/config.toml",
            } or path.startswith(("rust/src/", "rust/fixtures/")):
                native_tests = True
            continue

        if path.endswith(".py") or path in {
            "Dockerfile",
            "requirements.txt",
            "pyproject.toml",
            "setup.cfg",
        } or path.startswith("tests/") or (
            path.startswith("deployment/lagos/") and path.endswith((".py", ".txt", ".json"))
        ):
            python_tests = True
        if path.startswith(("latency_lab/", "agent/")) and path.endswith(
            (".py", ".txt", ".json", ".wav", ".yaml", ".yml")
        ):
            passkey_tests = True
        if path in {
            "deployment/lagos/Dockerfile",
            "deployment/lagos/test-native-runtime.sh",
        } or (path.startswith("deployment/lagos/") and path.endswith((".yaml", ".yml"))):
            native_tests = True

    return {"python": python_tests, "passkey": passkey_tests, "native": native_tests}


def changed_paths(base: str, head: str) -> list[str]:
    base_exists = bool(base) and set(base) != {"0"} and subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"], check=False
    ).returncode == 0
    if base_exists:
        command = ["git", "diff", "--name-only", "--no-renames", base, head]
    else:
        command = ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", head]
    return subprocess.check_output(command, text=True).splitlines()


def main() -> None:
    paths = changed_paths(os.environ.get("BASE_SHA", ""), os.environ["HEAD_SHA"])
    scope = classify(paths)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for name, enabled in scope.items():
            output.write(f"{name}={str(enabled).lower()}\n")
    print(f"Changed files: {paths}")
    print("CI scope: " + ", ".join(f"{name}={enabled}" for name, enabled in scope.items()))


if __name__ == "__main__":
    main()
