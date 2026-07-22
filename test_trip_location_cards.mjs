import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const data = require("./trip-location-data.js");
const html = readFileSync(new URL("./trip-plan.html", import.meta.url), "utf8");
const renderer = readFileSync(new URL("./trip-location-cards.js", import.meta.url), "utf8");
const build = readFileSync(new URL("./deploy/build.sh", import.meta.url), "utf8");
const stops = [...data.loops.A, ...data.loops.B];
const expectedLoopAIds = [
  "zamardi-adventure-park",
  "balatonfured-tagore",
  "tihany-abbey",
  "tapolca-lake-cave",
  "szigliget-castle",
  "heviz-thermal-lake",
  "hegyestu-kapolcs",
  "pecs-zsolnay",
];
const expectedLoopBIds = [
  "eger-castle-bath",
  "lillafured-bukk",
  "szalajka-valley",
  "aggtelek-baradla",
  "boldogko-castle",
  "sarospatak-rakoczi",
  "fuzer-castle",
  "regec-castle",
  "zemplen-adventure-park",
];

test("location guide has complete, current data for both loops", () => {
  assert.equal(data.schemaVersion, 2);
  assert.equal(data.checkedAt, "2026-07-22");
  assert.deepEqual(data.loops.A.map((stop) => stop.id), expectedLoopAIds);
  assert.deepEqual(data.loops.B.map((stop) => stop.id), expectedLoopBIds);

  const ids = new Set();
  for (const stop of stops) {
    for (const field of ["id", "name", "area", "hook", "what", "matija", "tundi", "duration", "caveat"]) {
      assert.equal(typeof stop[field], "string", `${stop.id || stop.name}: ${field} must be text`);
      assert.ok(stop[field].trim().length > 2, `${stop.id || stop.name}: ${field} cannot be empty`);
    }
    assert.equal(typeof stop.icon, "string");
    assert.ok(stop.icon.length > 0, `${stop.id}: icon cannot be empty`);
    assert.ok(Array.isArray(stop.visualStory) && stop.visualStory.length >= 2, `${stop.id}: visual story is incomplete`);
    assert.equal(new Set(stop.visualStory).size, stop.visualStory.length, `${stop.id}: visual story contains duplicates`);
    for (const subject of stop.visualStory) assert.ok(subject.length >= 8, `${stop.id}: visual story subject is too vague`);
    assert.ok(Number.isFinite(stop.geo?.lat) && Number.isFinite(stop.geo?.lng), `${stop.id}: mapped coordinates are required`);
    assert.match(stop.geo.sourceUrl, /^https:\/\//, `${stop.id}: coordinate source must use HTTPS`);
    assert.match(stop.id, /^[a-z0-9-]+$/);
    assert.ok(!ids.has(stop.id), `duplicate stop id: ${stop.id}`);
    ids.add(stop.id);

    assert.ok(["", "optional", "later"].includes(stop.fit.tone));
    assert.ok(stop.fit.label.length > 2);
    assert.ok(["Google", "Google snapshot", "Tripadvisor"].includes(stop.rating.platform));
    assert.ok(Number(stop.rating.value) >= 1 && Number(stop.rating.value) <= 5);
    assert.match(stop.rating.reviews, /^[0-9,]+$/);

    assert.ok(stop.price.summary.length > 1);
    assert.ok(Array.isArray(stop.price.packages) && stop.price.packages.length > 0);
    for (const option of stop.price.packages) {
      assert.ok(option.label && option.price);
      assert.ok(/HUF|Free/i.test(option.price), `${stop.id}: price must be explicit in HUF or marked free`);
    }

    assert.ok(Array.isArray(stop.sources) && stop.sources.length >= 2);
    assert.ok(stop.sources.some((source) => /rating/i.test(source.label)), `${stop.id}: missing attributed rating link`);
    for (const source of stop.sources) {
      assert.match(source.url, /^https:\/\//, `${stop.id}: source URL must use HTTPS`);
      assert.ok(source.label.length > 2);
    }
  }
});

test("trip page loads the data and accessible renderer", () => {
  assert.match(html, /id="loopAStops"/);
  assert.match(html, /id="loopBStops"/);
  assert.match(html, /src="trip-location-data\.js\?v=20260722\.2"/);
  assert.match(html, /src="trip-location-cards\.js(?:\?v=[a-f0-9]{64})?"/);
  assert.match(html, /Thu 23 → Sun 26 July/);
  assert.match(html, /checked 22 July 2026/i);
  assert.match(html, /Crowd strategy:/);
  assert.match(html, /leave Budapest early Thursday/);
  assert.doesNotMatch(html, /start tomorrow|for tomorrow|closed today/i);
  assert.match(renderer, /What is it\?/);
  assert.match(renderer, /Why Matija may like it/);
  assert.match(renderer, /Why Tündi may like it/);
  assert.match(renderer, /Traveler rating/);
  assert.match(renderer, /Adult price/);
  assert.match(renderer, /target="_blank" rel="noopener noreferrer"/);
  assert.match(renderer, /target\.scrollIntoView\(\{ block: "start" \}\)/);
  assert.match(renderer, /document\.getElementById\(id\)/);
  assert.doesNotMatch(renderer, /document\.querySelector\(location\.hash\)/);
  assert.match(renderer, /a\[href\^="#place-"\]/);
  assert.match(renderer, /addEventListener\("hashchange", \(\) => revealHashTarget\(\)\)/);
  assert.match(renderer, /beforeprint/);
  assert.match(renderer, /afterprint/);
  assert.match(renderer, /trip-map\.html#place-/);

  const linkedIds = new Set([...html.matchAll(/href="#(place-[a-z0-9-]+)"/g)].map((match) => match[1]));
  const expectedLinkedIds = new Set(stops.map((stop) => `place-${stop.id}`));
  assert.deepEqual([...linkedIds].sort(), [...expectedLinkedIds].sort());
});

test("release build syntax-checks and inlines the guide as one atomic response", () => {
  assert.match(build, /node --check \.\.\/trip-location-data\.js/);
  assert.match(build, /node --check \.\.\/trip-location-cards\.js/);
  assert.match(build, /trip_plan_inline/);
  assert.match(build, /failed to inline both trip-location modules/);
  assert.doesNotMatch(build, /TRIP_REVISION/);
});

test("current-window advice is honest about the closed Zemplén zipline", () => {
  const zemplen = data.loops.B.find((stop) => stop.id === "zemplen-adventure-park");
  assert.ok(zemplen);
  assert.match(zemplen.fit.label, /closed/i);
  assert.match(zemplen.caveat, /temporarily closed/i);
  assert.match(zemplen.caveat, /live page confirms reopening/i);
});
