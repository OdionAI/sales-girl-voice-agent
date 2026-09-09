import { cp, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Generates a source-development project without editing the published binary sample.
const sdk = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const target = join(sdk, ".build/quickstart");
const project = join(target, "AttentiveQuickStart.xcodeproj");
await mkdir(project, { recursive: true });
const template = join(sdk, "Examples/AttentiveSample/AttentiveSample.xcodeproj/project.pbxproj");
const data = JSON.parse(execFileSync("plutil", ["-convert", "json", "-o", "-", template], { encoding: "utf8" }));
const objects = data.objects;
const appTarget = objects.A50000000000000000000001;
appTarget.name = "AttentiveQuickStart";
objects.A60000000000000000000001.targets = ["A50000000000000000000001"];
objects.A30000000000000000000001.children = ["A30000000000000000000002", "A30000000000000000000004"];
objects.A30000000000000000000002.children = ["A20000000000000000000001", "A20000000000000000000004"];
objects.A40000000000000000000001.files = ["A10000000000000000000001"];
objects.A30000000000000000000004.children = ["A20000000000000000000006"];
objects.A20000000000000000000001.path = "AttentiveExampleApp.swift";
objects.A20000000000000000000006.path = "AttentiveQuickStart.app";
for (const id of ["B10000000000000000000003", "B10000000000000000000004"]) {
  objects[id].buildSettings.PRODUCT_BUNDLE_IDENTIFIER = "ai.odion.attentive.quickstart";
}
objects.A90000000000000000000001 = { isa: "XCLocalSwiftPackageReference", relativePath: sdk };
// Remove unreachable objects, including legacy SampleModel/settings/test file references.
const reachable = new Set();
function visit(value) {
  if (typeof value === "string" && objects[value] && !reachable.has(value)) {
    reachable.add(value);
    visit(objects[value]);
  } else if (value && typeof value === "object") Object.values(value).forEach(visit);
}
delete objects.A60000000000000000000001.attributes.TargetAttributes;
visit(data.rootObject);
for (const id of Object.keys(objects)) if (!reachable.has(id)) delete objects[id];
execFileSync("plutil", ["-convert", "xml1", "-o", join(project, "project.pbxproj"), "-"], { input: JSON.stringify(data) });
await mkdir(join(target, "AttentiveSample"), { recursive: true });
await cp(join(sdk, "Examples/QuickStart/AttentiveExampleApp.swift"), join(target, "AttentiveSample/AttentiveExampleApp.swift"));
await cp(join(sdk, "Examples/AttentiveSample/AttentiveSample/Info.plist"), join(target, "AttentiveSample/Info.plist"));
console.log(project);
