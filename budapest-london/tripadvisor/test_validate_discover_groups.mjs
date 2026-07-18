import assert from "node:assert/strict";
import {
  copyFileSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = dirname(fileURLToPath(import.meta.url));
const validator = join(root, "validate_discover_groups.mjs");
const briefsSource = readFileSync(join(root, "activity-briefs.js"), "utf8");
const pricingSource = readFileSync(join(root, "activity-pricing.js"), "utf8");
const buildSource = readFileSync(join(root, "..", "..", "deploy", "build.sh"), "utf8");

function withRevision(source, globalName, revision) {
  const revisionSuffix = new RegExp(
    `window\\.${globalName}="[0-9a-f]{64}";\\n$`,
  );
  const sourceWithoutRevision = source.replace(revisionSuffix, "");
  assert.match(sourceWithoutRevision, /;\n$/);
  return `${sourceWithoutRevision}window.${globalName}="${revision}";\n`;
}

function runValidator(arguments_) {
  return spawnSync(process.execPath, [validator, ...arguments_], {
    cwd: root,
    encoding: "utf8",
  });
}

function stagedSite(briefsRevision, pricingRevision) {
  const siteRoot = mkdtempSync(join(tmpdir(), "discover-validator-"));
  copyFileSync(join(root, "index.html"), join(siteRoot, "index.html"));
  writeFileSync(
    join(siteRoot, "activity-briefs.js"),
    withRevision(briefsSource, "ACTIVITY_BRIEFS_REVISION", briefsRevision),
  );
  writeFileSync(
    join(siteRoot, "activity-pricing.js"),
    withRevision(pricingSource, "ACTIVITY_PRICING_REVISION", pricingRevision),
  );
  return siteRoot;
}

test("--site-root validates the staged HTML and matching research bundles", (context) => {
  const revision = "a".repeat(64);
  const siteRoot = stagedSite(revision, revision);
  context.after(() => rmSync(siteRoot, { recursive: true, force: true }));

  const result = runValidator([
    "--allow-partial-research",
    "--site-root",
    siteRoot,
  ]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Discover groups valid/);
});

test("a mixed pair in the staged snapshot fails before activation", (context) => {
  const siteRoot = stagedSite("a".repeat(64), "b".repeat(64));
  context.after(() => rmSync(siteRoot, { recursive: true, force: true }));

  const result = runValidator([
    "--allow-partial-research",
    "--site-root",
    siteRoot,
  ]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Research bundle revision mismatch/);
});

test("inventory-only mode can recover visibility from a mixed pair", (context) => {
  const siteRoot = stagedSite("a".repeat(64), "b".repeat(64));
  context.after(() => rmSync(siteRoot, { recursive: true, force: true }));

  const result = runValidator([
    "--inventory-only",
    "--print-visible-json",
    "--site-root",
    siteRoot,
  ]);
  assert.equal(result.status, 0, result.stderr);
  const visible = JSON.parse(result.stdout);
  assert.equal(visible.length, 1180);
});

test("raw listing blurbs are rejected even in inventory-only mode", (context) => {
  const revision = "a".repeat(64);
  const siteRoot = stagedSite(revision, revision);
  context.after(() => rmSync(siteRoot, { recursive: true, force: true }));
  const htmlPath = join(siteRoot, "index.html");
  const html = readFileSync(htmlPath, "utf8").replace(
    "const DATA=[",
    'const DATA=[{"n":"Raw","url":"https://www.tripadvisor.com/Attraction_Review-g1-d999999-Reviews-Raw.html","city":"london","cat":"Test","type":"venue","blurb":"raw review"},',
  );
  writeFileSync(htmlPath, html);

  const result = runValidator([
    "--inventory-only",
    "--print-visible-json",
    "--site-root",
    siteRoot,
  ]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Raw listing blurbs must not ship/);
});

test("--site-root reads the staged HTML instead of the source checkout", (context) => {
  const revision = "a".repeat(64);
  const siteRoot = stagedSite(revision, revision);
  context.after(() => rmSync(siteRoot, { recursive: true, force: true }));
  writeFileSync(join(siteRoot, "index.html"), "<script>const broken = ;</script>\n");

  const result = runValidator([
    "--allow-partial-research",
    "--site-root",
    siteRoot,
  ]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Discover inline script must parse/);
});

test("build validates the staged snapshot after copying and before activation", () => {
  const briefsCopy = buildSource.indexOf(
    "cp ../budapest-london/tripadvisor/activity-briefs.js",
  );
  const pricingCopy = buildSource.indexOf(
    "cp ../budapest-london/tripadvisor/activity-pricing.js",
  );
  const stagedValidation = buildSource.indexOf(
    '--site-root "$release/budapest-london/tripadvisor"',
  );
  const activation = buildSource.indexOf('/bin/mv -fh "$link_tmp" public');
  assert.ok(briefsCopy >= 0, "build must copy the briefs bundle");
  assert.ok(pricingCopy > briefsCopy, "build must copy the pricing bundle");
  assert.ok(
    stagedValidation > pricingCopy,
    "staged validation must happen after both generated bundles are copied",
  );
  assert.ok(
    activation > stagedValidation,
    "public activation must happen only after staged validation",
  );
  assert.match(buildSource, /test_validate_discover_groups\.mjs/);
  assert.match(
    buildSource,
    /audit_detail_context\.py[^]*--site-root "\$release\/budapest-london\/tripadvisor"/,
  );
  assert.match(buildSource, /BUNDLE_REVISION=.*perl -0pi/s);
});

test("validator rejects malformed command-line arguments", () => {
  const missingRoot = runValidator(["--site-root"]);
  assert.equal(missingRoot.status, 2);
  assert.match(missingRoot.stderr, /--site-root requires a directory argument/);

  const duplicateRoot = runValidator([
    "--site-root",
    root,
    "--site-root",
    root,
  ]);
  assert.equal(duplicateRoot.status, 2);
  assert.match(duplicateRoot.stderr, /--site-root may only be provided once/);

  const unknown = runValidator(["--unexpected"]);
  assert.equal(unknown.status, 2);
  assert.match(unknown.stderr, /unknown argument: --unexpected/);

  const missingDirectory = runValidator([
    "--site-root",
    join(tmpdir(), `discover-validator-does-not-exist-${process.pid}`),
  ]);
  assert.equal(missingDirectory.status, 2);
  assert.match(missingDirectory.stderr, /cannot read --site-root/);

  const conflictingOutput = runValidator([
    "--print-visible-json",
    "--print-visible-refs",
  ]);
  assert.equal(conflictingOutput.status, 2);
  assert.match(conflictingOutput.stderr, /mutually exclusive/);
});
