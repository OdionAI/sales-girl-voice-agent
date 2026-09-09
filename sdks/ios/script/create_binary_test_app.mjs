import { cp, mkdir, rm } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const sdk = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dependency = process.argv[2];
if (!dependency) throw new Error("Supply the candidate package path, or its HTTPS Git URL and exact version.");
const remote = dependency.startsWith("https://");
if (remote && !process.argv[3]) throw new Error("Remote verification requires an exact version.");
const source = join(sdk, "Examples/AttentiveSample");
const target = join(sdk, ".build", remote ? "clean-release-remote-sample" : "clean-release-local-sample");
await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
for (const folder of ["AttentiveSample.xcodeproj", "AttentiveSample", "AttentiveSampleUITests"]) {
  await cp(join(source, folder), join(target, folder), { recursive: true });
}
const project = join(target, "AttentiveSample.xcodeproj");
await rm(join(project, "project.xcworkspace/xcshareddata/swiftpm/Package.resolved"), { force: true });
const path = join(project, "project.pbxproj");
const data = JSON.parse(execFileSync("plutil", ["-convert", "json", "-o", "-", path], { encoding: "utf8" }));
const reference = Object.values(data.objects).find((value) => value.isa === "XCRemoteSwiftPackageReference");
if (!reference) throw new Error("Expected the sample's single remote SDK dependency.");
if (remote) {
  reference.repositoryURL = dependency;
  reference.requirement = { kind: "exactVersion", version: process.argv[3] };
} else {
  delete reference.repositoryURL;
  delete reference.requirement;
  reference.isa = "XCLocalSwiftPackageReference";
  reference.relativePath = resolve(dependency);
}
execFileSync("plutil", ["-convert", "xml1", "-o", path, "-"], { input: JSON.stringify(data) });
console.log(project);
