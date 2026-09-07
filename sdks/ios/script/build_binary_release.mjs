import { cp, mkdir, readFile, writeFile, open, readdir, rm, lstat, readlink, realpath } from "node:fs/promises";
import { spawn, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const sdk = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = join(sdk, ".build", "binary-release");
const source = join(root, "source");
const distribution = join(root, "package");
const frameworks = join(distribution, "Frameworks");
const nativeNames = ["LiveKitWebRTC", "RustLiveKitUniFFI"];
const uiOnly = process.argv.includes("--ui-only");
await mkdir(source, { recursive: true });
await mkdir(frameworks, { recursive: true });
if (!uiOnly) {
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
    slices.push(framework);
  }
  const output = join(frameworks, `${name}.xcframework`);
  await rm(output, { recursive: true, force: true });
  await run("xcodebuild", ["-create-xcframework", ...slices.flatMap((path) => ["-framework", path]),
    "-output", output], `package-${name}.log`, cwd);
}

if (!uiOnly) {
  await archiveLibrary("AttentiveVoice", source, [
    "AttentiveVoice_AttentiveRTC.bundle", "SwiftProtobuf_SwiftProtobuf.bundle",
  ]);
  for (const name of nativeNames) {
    await rm(join(frameworks, `${name}.xcframework`), { recursive: true, force: true });
    await cp(join(root, "dependencies-AttentiveVoice/artifacts/source", name, `${name}.xcframework`),
      join(frameworks, `${name}.xcframework`), { recursive: true, verbatimSymlinks: true });
  }
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
        .target(name: "AttentiveVoiceUI", dependencies: ["AttentiveVoice", "LiveKitWebRTC", "RustLiveKitUniFFI"],
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
        .library(name: "AttentiveVoice", targets: ["AttentiveVoice", "LiveKitWebRTC", "RustLiveKitUniFFI"]),
        .library(name: "AttentiveVoiceUI", targets: ["AttentiveVoiceUI", "AttentiveVoice", "LiveKitWebRTC", "RustLiveKitUniFFI"])
    ],
    targets: [
        ${binaryTargets(["AttentiveVoice", "AttentiveVoiceUI", ...nativeNames])}
    ]
)
`);
await cp(join(sdk, "release/README.md"), join(distribution, "README.md"));
for (const file of ["GETTING_STARTED.md", "CALLER_UI.md", "INTEGRATION_AGENT.md"]) {
  const contents = (await readFile(join(sdk, file), "utf8"))
    .replaceAll("../../docs/attentive/ROADMAP.md", "README.md#roadmap")
    .replace(/\.\.\/\.\.\/docs\/attentive\/call-api\.md(?:#[\w-]+)?/g, "README.md#staging-backend")
    .replaceAll("Examples/AttentiveSample/README.md", "GETTING_STARTED.md")
    .replaceAll("DISTRIBUTION.md", "README.md#installation")
    .replaceAll("VERIFICATION.md", "README.md#verification")
    .replaceAll("README.md#call-from-your-ui", "GETTING_STARTED.md#3b-use-your-own-ui-instead");
  await writeFile(join(distribution, file), contents);
}
await cp(join(sdk, "Vendor/AttentiveRTC/Sources/AttentiveRTC/ThirdPartyNotices"),
  join(distribution, "ThirdPartyNotices"), { recursive: true });
const commandOutput = (command, args) => execFileSync(command, args, { cwd: sdk, encoding: "utf8" }).trim();
await writeFile(join(distribution, "BUILD_INFO.json"), JSON.stringify({
  version: "0.1.0-staging.1",
  sourceRepository: "https://github.com/OdionAI/sales-girl-voice-agent",
  sourceCommit: commandOutput("git", ["rev-parse", "HEAD"]),
  xcode: commandOutput("xcodebuild", ["-version"]),
  swift: commandOutput("xcrun", ["swift", "--version"]),
  builtAt: new Date().toISOString(),
  architectures: { iOS: ["arm64"], iOSSimulator: ["arm64", "x86_64"] },
  attentiveSigning: "unsigned-staging",
  upstream: JSON.parse(await readFile(join(sdk, "Vendor/AttentiveRTC/upstream.json"), "utf8")),
}, null, 2) + "\n");
await writeFile(join(distribution, ".gitignore"), ".DS_Store\n.build/\n.swiftpm/\n");
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
      if (name.endsWith(".swift") && name !== "Package.swift") throw new Error(`Implementation source in release: ${relative}`);
      hashes.push(`${createHash("sha256").update(await readFile(path)).digest("hex")}  ${relative}`);
    }
  }
}
await inventory(distribution);
await writeFile(join(distribution, "SHA256SUMS"), hashes.join("\n") + "\n");
console.log(`Binary Swift package: ${distribution}`);
