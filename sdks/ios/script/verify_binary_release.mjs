import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdir, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";

const root = resolve(process.argv[2]);
const names = ["AttentiveVoice", "AttentiveVoiceUI", "AttentiveMedia", "AttentiveBindings"];
const output = (command, args) => execFileSync(command, args, { encoding: "utf8" });
const plist = (path) => JSON.parse(output("plutil", ["-convert", "json", "-o", "-", path]));
assert.deepEqual((await readdir(root)).filter((name) => name !== ".git").sort(),
  [".gitattributes", "Frameworks", "Package.swift", "ThirdPartyNotices", "release.json"]);
const release = JSON.parse(await readFile(join(root, "release.json"), "utf8"));
assert.equal(release.schemaVersion, 1);
assert.match(release.version, /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/);
assert.equal(release.status, release.version.includes("-") ? "prerelease" : "stable");
assert.equal(release.packageURL, "https://github.com/OdionAI/attentive-ios-sdk.git");
assert.equal(release.releaseURL, `https://github.com/OdionAI/attentive-ios-sdk/releases/tag/${release.version}`);
assert.equal(release.archive.name, `attentive-ios-sdk-${release.version}.zip`);
assert.equal(release.archive.url, `https://github.com/OdionAI/attentive-ios-sdk/releases/download/${release.version}/${release.archive.name}`);
assert.match(release.archive.sha256, /^[a-f0-9]{64}$/);
assert.deepEqual((await readdir(join(root, "Frameworks"))).sort(), names.map((name) => `${name}.xcframework`).sort());
for (const entry of await readdir(root, { recursive: true })) {
  assert(!/\.md$/i.test(entry), `Documentation shipped: ${entry}`);
  assert(!/LiveKit/i.test(entry), `Upstream framework name exposed in path: ${entry}`);
  assert(!entry.endsWith(".swift") || entry === "Package.swift", `Implementation source shipped: ${entry}`);
  assert(!/\.private\.swiftinterface$|\.package\.swiftinterface$/.test(entry), `Private interface shipped: ${entry}`);
}
const manifest = await readFile(join(root, "Package.swift"), "utf8");
assert(!/LiveKit|https?:/.test(manifest), "Customer manifest exposes an upstream dependency");
for (const name of names) {
  const xc = join(root, "Frameworks", `${name}.xcframework`);
  const slices = plist(join(xc, "Info.plist")).AvailableLibraries;
  assert.equal(slices.length, 2, `${name}: expected iPhone and Simulator only`);
  assert.deepEqual(slices.map((slice) => slice.SupportedPlatformVariant ?? "device").sort(), ["device", "simulator"]);
  for (const slice of slices) {
    assert.equal(slice.SupportedPlatform, "ios");
    assert.deepEqual([...slice.SupportedArchitectures].sort(), slice.SupportedPlatformVariant ? ["arm64", "x86_64"] : ["arm64"]);
    const framework = join(xc, slice.LibraryIdentifier, slice.LibraryPath);
    assert.equal(plist(join(framework, "Info.plist")).CFBundleExecutable, name);
    const links = output("xcrun", ["otool", "-L", join(framework, name)]);
    assert(!/LiveKit/.test(links), `Old dyld dependency: ${name}`);
    const dependencies = links.split("\n").filter((line) => line.startsWith("\t"));
    assert(!/\/Users\/|\.build\//.test(dependencies.join("\n")), `Nonportable link: ${name}`);
    if (name === "AttentiveVoice") {
      for (const dependency of names.slice(2)) assert(links.includes(`@rpath/${dependency}.framework/${dependency}`));
    }
    if (name === "AttentiveVoiceUI") assert(links.includes("@rpath/AttentiveVoice.framework/AttentiveVoice"));
    output("codesign", ["--verify", "--strict", framework]);
    for (const entry of await readdir(framework, { recursive: true })) {
      if (!entry.endsWith(".swiftinterface")) continue;
      const contents = await readFile(join(framework, entry), "utf8");
      assert(!/^import (AttentiveRTC|AttentiveMedia|AttentiveBindings|LiveKit|RustLiveKit|SwiftProtobuf)/m.test(contents), `Transport type leaks into public interface: ${entry}`);
    }
  }
}
assert((await readdir(join(root, "ThirdPartyNotices"))).includes("Packaging-NOTICE"));
console.log("PASS: runtime-only package, four Attentive frameworks, portable links, signatures, and public interfaces.");
