import { mkdir, readFile, writeFile, open } from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const remoteURL = process.argv[2]?.startsWith("https://") ? process.argv[2] : null;
const remoteVersion = process.argv[3];
if (remoteURL && !remoteVersion) throw new Error("Supply the exact remote release version.");
const packagePath = process.argv[2] ? resolve(process.argv[2]) : root;
const prefix = remoteURL ? "remote-documentation" : process.argv[2] ? "binary-documentation" : "documentation";
const packageIdentity = remoteURL ? "attentive-ios-sdk" : "AttentiveVoice";
const dependency = remoteURL
  ? `.package(url: ${JSON.stringify(remoteURL)}, exact: ${JSON.stringify(remoteVersion)})`
  : `.package(name: "AttentiveVoice", path: ${JSON.stringify(packagePath)})`;
const consumer = join(root, ".build", `${prefix}-consumer`);
const markdown = await readFile(join(root, "GETTING_STARTED.md"), "utf8");
const snippets = new Map([...markdown.matchAll(
  /<!-- snippet: ([\w-]+) -->\n```swift\n([\s\S]*?)\n```/g
)].map((match) => [match[1], match[2]]));
const targets = {
  DocsCore: ["session", "custom-controls"],
  DocsCallerUI: ["session", "caller-ui", "entry-button", "configure-entry"],
};

for (const [target, names] of Object.entries(targets)) {
  const source = names.map((name) => {
    if (!snippets.has(name)) throw new Error(`Missing documented snippet: ${name}`);
    return snippets.get(name);
  }).join("\n\n");
  const directory = join(consumer, "Sources", target);
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "Examples.swift"), source + "\n");
}

await writeFile(join(consumer, "Package.swift"), `// swift-tools-version: 6.1
import PackageDescription
let package = Package(
    name: "DocumentationConsumer",
    platforms: [.iOS(.v16)],
    products: [
        .library(name: "DocsCore", targets: ["DocsCore"]),
        .library(name: "DocsCallerUI", targets: ["DocsCallerUI"])
    ],
    dependencies: [${dependency}],
    targets: [
        .target(name: "DocsCore", dependencies: [
            .product(name: "AttentiveVoice", package: "${packageIdentity}")
        ]),
        .target(name: "DocsCallerUI", dependencies: [
            .product(name: "AttentiveVoice", package: "${packageIdentity}"),
            .product(name: "AttentiveVoiceUI", package: "${packageIdentity}")
        ])
    ],
    swiftLanguageModes: [.v5]
)
`);

for (const scheme of Object.keys(targets)) {
  const logPath = join(consumer, `${scheme}.log`);
  const log = await open(logPath, "w");
  console.log(`Compiling ${scheme}; log: ${logPath}`);
  try {
    const exitCode = await new Promise((resolveExit, reject) => {
      const build = spawn("xcodebuild", [
        "-scheme", scheme, "-destination", "generic/platform=iOS Simulator",
        "-derivedDataPath", join(root, ".build", `${prefix}-build`),
        "-clonedSourcePackagesDirPath", join(consumer, "dependencies"),
        ...(remoteURL ? ["-scmProvider", "system"] : []),
        "-jobs", "4", "CODE_SIGNING_ALLOWED=NO", "build",
      ], { cwd: consumer, stdio: ["ignore", log.fd, log.fd] });
      build.once("error", reject);
      build.once("exit", resolveExit);
    });
    if (exitCode !== 0) throw new Error(`${scheme} failed; inspect ${logPath}`);
  } finally {
    await log.close();
  }
}
console.log("Both documentation consumers compiled. No app or call was launched.");
