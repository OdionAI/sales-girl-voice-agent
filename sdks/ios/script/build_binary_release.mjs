import { cp, mkdir, readFile, writeFile, open, readdir, rm, rename, lstat, readlink, realpath } from "node:fs/promises";
import { spawn, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const sdk = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const version = process.argv[2];
if (!/^\d+\.\d+\.\d+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?$/.test(version || "")) {
  throw new Error("Supply the new SDK release version, for example: 0.1.0");
}
const root = join(sdk, ".build", `binary-release-${version}`);
const source = join(root, "source");
const distribution = join(root, "package");
const frameworks = join(distribution, "Frameworks");
const nativeFrameworks = new Map([
  ["LiveKitWebRTC", "AttentiveMedia"],
  ["RustLiveKitUniFFI", "AttentiveBindings"],
]);
const nativeNames = [...nativeFrameworks.values()];
const nativeTargetList = nativeNames.map((name) => JSON.stringify(name)).join(", ");
const xcodeTool = (name, args) => execFileSync("xcrun", [name, ...args], { encoding: "utf8" });
if (process.argv.includes("--ui-only")) throw new Error("A clean release requires a full build.");
await rm(distribution, { recursive: true, force: true });
await mkdir(source, { recursive: true });
await mkdir(frameworks, { recursive: true });
{
  for (const directory of ["Sources", "Vendor"]) {
    await rm(join(source, directory), { recursive: true, force: true });
    await cp(join(sdk, directory), join(source, directory), { recursive: true });
  }
  for (const file of ["Package.swift", "Package.resolved"]) {
    let contents = await readFile(join(sdk, file), "utf8");
    if (file === "Package.swift") {
      contents = contents.replace('.library(name: "AttentiveVoice", targets:',
        '.library(name: "AttentiveVoice", type: .dynamic, targets:');
      contents = contents.split("\n").filter((line) => !line.includes(".testTarget(")).join("\n");
    }
    await writeFile(join(source, file), contents);
  }
}

async function run(command, args, logName, cwd = source) {
  const logPath = join(root, logName);
  const log = await open(logPath, "w");
  console.log(`${command} ${args.slice(0, 4).join(" ")}; log: ${logPath}`);
  try {
    const code = await new Promise((resolveExit, reject) => {
      const child = spawn(command, args, { cwd, stdio: ["ignore", log.fd, log.fd] });
      child.once("error", reject);
      child.once("exit", resolveExit);
    });
    if (code !== 0) throw new Error(`Failed (${code}); inspect ${logPath}`);
  } finally { await log.close(); }
}

async function archiveLibrary(name, cwd, bundles) {
  const slices = [];
  for (const [variant, destination, platform] of [
    ["ios", "generic/platform=iOS", "iphoneos"],
    ["simulator", "generic/platform=iOS Simulator", "iphonesimulator"],
  ]) {
    const archive = join(root, `${name}-${variant}.xcarchive`);
    const derived = join(root, `${name}-derived`);
    await run("xcodebuild", [
      "archive", "-scheme", name, "-configuration", "Release",
      "-destination", destination, "-archivePath", archive,
      "-derivedDataPath", derived,
      "-clonedSourcePackagesDirPath", join(root, `dependencies-${name}`), "-jobs", "4",
      "SKIP_INSTALL=NO", "BUILD_LIBRARY_FOR_DISTRIBUTION=YES", "CODE_SIGNING_ALLOWED=NO",
    ], `archive-${name}-${variant}.log`, cwd);

    const framework = join(archive, "Products/usr/local/lib", `${name}.framework`);
    const products = join(derived, "Build/Intermediates.noindex/ArchiveIntermediates", name,
      "BuildProductsPath", `Release-${platform}`);
    const modules = join(framework, "Modules", `${name}.swiftmodule`);
    await mkdir(modules, { recursive: true });
    // Ship stable public interfaces, not private or compiler-specific Swift modules.
    for (const file of await readdir(join(products, `${name}.swiftmodule`))) {
      if (!file.endsWith(".swiftinterface") || /\.(private|package)\./.test(file)) continue;
      const contents = await readFile(join(products, `${name}.swiftmodule`, file), "utf8");
      if (/^import (AttentiveRTC|LiveKit|SwiftProtobuf|RustLiveKit)/m.test(contents)) {
        throw new Error(`Private transport import leaked into ${name}/${file}`);
      }
      await cp(join(products, `${name}.swiftmodule`, file), join(modules, file));
    }
    for (const bundle of bundles) {
      await rm(join(framework, bundle), { recursive: true, force: true });
      await cp(join(products, bundle), join(framework, bundle), { recursive: true, dereference: true });
    }
    await rebindFramework(framework, name);
    slices.push(framework);
  }
  const output = join(frameworks, `${name}.xcframework`);
  await rm(output, { recursive: true, force: true });
  await run("xcodebuild", ["-create-xcframework", ...slices.flatMap((path) => ["-framework", path]),
    "-output", output], `package-${name}.log`, cwd);
}

async function rebindFramework(framework, name) {
  const binary = join(framework, name);
  // Change only dyld paths. Exported symbols, media code and native ABI stay intact.
  const links = xcodeTool("otool", ["-L", binary]);
  const changes = [...nativeFrameworks].flatMap(([upstream, packaged]) => {
    const oldPath = `@rpath/${upstream}.framework/${upstream}`;
    return links.includes(oldPath)
      ? ["-change", oldPath, `@rpath/${packaged}.framework/${packaged}`] : [];
  });
  xcodeTool("install_name_tool", ["-id", `@rpath/${name}.framework/${name}`, ...changes, binary]);
  await rm(join(framework, "_CodeSignature"), { recursive: true, force: true });
  execFileSync("codesign", ["--force", "--sign", "-", "--timestamp=none", framework]);
}

await archiveLibrary("AttentiveVoice", source, [
  "AttentiveVoice_AttentiveRTC.bundle", "SwiftProtobuf_SwiftProtobuf.bundle",
]);
for (const [upstream, name] of nativeFrameworks) {
  const artifact = join(root, "dependencies-AttentiveVoice/artifacts/source", upstream, `${upstream}.xcframework`);
  const info = JSON.parse(execFileSync("plutil", ["-convert", "json", "-o", "-", join(artifact, "Info.plist")], { encoding: "utf8" }));
  const libraries = info.AvailableLibraries.filter((library) => library.SupportedPlatform === "ios"
    && (!library.SupportedPlatformVariant || library.SupportedPlatformVariant === "simulator"));
  if (libraries.length !== 2) throw new Error(`Expected device and simulator slices for ${upstream}`);
  const slices = [];
  for (const library of libraries) {
    const framework = join(root, "native", name, library.LibraryIdentifier, `${name}.framework`);
    await rm(framework, { recursive: true, force: true });
    await cp(join(artifact, library.LibraryIdentifier, library.LibraryPath), framework, { recursive: true });
    await rename(join(framework, upstream), join(framework, name));
    for (const key of ["CFBundleExecutable", "CFBundleName"]) {
      execFileSync("plutil", ["-replace", key, "-string", name, join(framework, "Info.plist")]);
    }
    execFileSync("plutil", ["-replace", "CFBundleIdentifier", "-string", `ai.odion.${name}`, join(framework, "Info.plist")]);
    // Consumers link these runtimes; they do not compile against their private headers.
    await rm(join(framework, "Headers"), { recursive: true, force: true });
    await rm(join(framework, "Modules"), { recursive: true, force: true });
    await mkdir(join(framework, "Modules"));
    await writeFile(join(framework, "Modules/module.modulemap"), `framework module ${name} {}\n`);
    await rebindFramework(framework, name);
    for (const arch of library.SupportedArchitectures) {
      const symbols = (path) => xcodeTool("nm", ["-arch", arch, "-gUj", path]);
      if (symbols(join(framework, name)) !== symbols(join(artifact, library.LibraryIdentifier, library.LibraryPath, upstream))) {
        throw new Error(`Native exports changed: ${name}/${arch}`);
      }
    }
    slices.push(framework);
  }
  await run("xcodebuild", ["-create-xcframework", ...slices.flatMap((path) => ["-framework", path]),
    "-output", join(frameworks, `${name}.xcframework`)], `package-${name}.log`);
}

// Compile the optional UI against the binary core, avoiding a second embedded core.
const uiSource = join(root, "ui-source");
await mkdir(uiSource, { recursive: true });
await rm(join(uiSource, "Sources"), { recursive: true, force: true });
await cp(join(sdk, "Sources/AttentiveVoiceUI"), join(uiSource, "Sources/AttentiveVoiceUI"),
  { recursive: true });
for (const name of ["AttentiveVoice", ...nativeNames]) {
  await rm(join(uiSource, "Frameworks", `${name}.xcframework`), { recursive: true, force: true });
  await cp(join(frameworks, `${name}.xcframework`),
    join(uiSource, "Frameworks", `${name}.xcframework`), { recursive: true, verbatimSymlinks: true });
}
const binaryTargets = (names) => names.map((name) =>
  `.binaryTarget(name: "${name}", path: "Frameworks/${name}.xcframework")`).join(",\n        ");
await writeFile(join(uiSource, "Package.swift"), `// swift-tools-version: 6.1
import PackageDescription
let package = Package(
    name: "AttentiveVoiceUI",
    platforms: [.iOS(.v16)],
    products: [.library(name: "AttentiveVoiceUI", type: .dynamic, targets: ["AttentiveVoiceUI"])],
    targets: [
        .target(name: "AttentiveVoiceUI", dependencies: ["AttentiveVoice", ${nativeTargetList}],
            resources: [.process("Resources")]),
        ${binaryTargets(["AttentiveVoice", ...nativeNames])}
    ],
    swiftLanguageModes: [.v5]
)
`);
await archiveLibrary("AttentiveVoiceUI", uiSource, ["AttentiveVoiceUI_AttentiveVoiceUI.bundle"]);
await writeFile(join(distribution, "Package.swift"), `// swift-tools-version: 6.1
import PackageDescription
let package = Package(
    name: "AttentiveVoice",
    platforms: [.iOS(.v16)],
    products: [
        .library(name: "AttentiveVoice", targets: ["AttentiveVoice", ${nativeTargetList}]),
        .library(name: "AttentiveVoiceUI", targets: ["AttentiveVoiceUI", "AttentiveVoice", ${nativeTargetList}])
    ],
    targets: [
        ${binaryTargets(["AttentiveVoice", "AttentiveVoiceUI", ...nativeNames])}
    ]
)
`);
await cp(join(sdk, "Vendor/AttentiveRTC/Sources/AttentiveRTC/ThirdPartyNotices"),
  join(distribution, "ThirdPartyNotices"), { recursive: true });
await writeFile(join(distribution, "ThirdPartyNotices/Packaging-NOTICE"),
  "Attentive packaging modifications: LiveKitWebRTC is distributed as AttentiveMedia; " +
  "RustLiveKitUniFFI is distributed as AttentiveBindings. Framework names, bundle identifiers, " +
  "dynamic-loader paths and signing were updated; private headers and unused platform slices " +
  "were omitted. The upstream executable implementation and exported ABI are unchanged.\n");
const commandOutput = (command, args) => execFileSync(command, args, { cwd: sdk, encoding: "utf8" }).trim();
await writeFile(join(root, "BUILD_INFO.json"), JSON.stringify({
  version,
  sourceRepository: "https://github.com/OdionAI/sales-girl-voice-agent",
  sourceCommit: commandOutput("git", ["rev-parse", "HEAD"]),
  xcode: commandOutput("xcodebuild", ["-version"]),
  swift: commandOutput("xcrun", ["swift", "--version"]),
  builtAt: new Date().toISOString(),
  architectures: { iOS: ["arm64"], iOSSimulator: ["arm64", "x86_64"] },
  attentiveSigning: "ad-hoc-staging; host application must sign embedded frameworks",
  upstream: JSON.parse(await readFile(join(sdk, "Vendor/AttentiveRTC/upstream.json"), "utf8")),
}, null, 2) + "\n");
await writeFile(join(distribution, ".gitattributes"), "* -text\n");
const hashes = [];
async function inventory(directory, prefix = "") {
  for (const name of (await readdir(directory)).sort()) {
    if (name === "SHA256SUMS" || name === ".git") continue;
    const path = join(directory, name);
    const relative = prefix + name;
    const stat = await lstat(path);
    if (stat.isSymbolicLink()) {
      const target = await readlink(path);
      if (target.startsWith("/") || !(await realpath(path)).startsWith(distribution + "/")) {
        throw new Error(`Nonportable binary symlink: ${relative}`);
      }
    } else if (stat.isDirectory()) {
      await inventory(path, relative + "/");
    } else {
      if (stat.size >= 100 * 1024 * 1024) throw new Error(`Exceeds GitHub file limit: ${relative}`);
      if (/\.md$/i.test(name)) throw new Error(`Documentation in runtime package: ${relative}`);
      if (/LiveKit/i.test(relative)) throw new Error(`Upstream framework name in package path: ${relative}`);
      if (name.endsWith(".swift") && name !== "Package.swift") throw new Error(`Implementation source in release: ${relative}`);
      hashes.push(`${createHash("sha256").update(await readFile(path)).digest("hex")}  ${relative}`);
    }
  }
}
await inventory(distribution);
await writeFile(join(root, "SHA256SUMS"), hashes.join("\n") + "\n");
const archiveName = `attentive-ios-sdk-${version}.zip`;
const archivePath = join(root, archiveName);
await rm(archivePath, { force: true });
execFileSync("ditto", ["-c", "-k", "--norsrc", distribution, archivePath]);
const releaseURL = `https://github.com/OdionAI/attentive-ios-sdk/releases/tag/${version}`;
const metadata = {
  schemaVersion: 1, version, status: version.includes("-") ? "prerelease" : "stable",
  packageURL: "https://github.com/OdionAI/attentive-ios-sdk.git", releaseURL,
  minimumIOS: "17.0", swiftToolsVersion: "6.1",
  buildToolchain: {
    xcode: commandOutput("xcodebuild", ["-version"]),
    swift: commandOutput("xcrun", ["swift", "--version"]),
  },
  archive: {
    name: archiveName,
    url: `https://github.com/OdionAI/attentive-ios-sdk/releases/download/${version}/${archiveName}`,
    sha256: createHash("sha256").update(await readFile(archivePath)).digest("hex"),
  },
};
await writeFile(join(distribution, "release.json"), JSON.stringify(metadata, null, 2) + "\n");
console.log(`Binary Swift package: ${distribution}`);
