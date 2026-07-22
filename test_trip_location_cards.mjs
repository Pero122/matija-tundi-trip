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
  "balatonibob-leisure-park",
  "balatonfured-tagore",
  "tihany-abbey",
  "tapolca-lake-cave",
  "szigliget-castle",
  "csodabogyos-adventure-cave",
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
  assert.equal(data.schemaVersion, 4);
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
    assert.ok(["must", "maybe", "chill", "skip"].includes(stop.worth?.verdict), `${stop.id}: invalid worth-it verdict`);
    assert.ok(["high", "medium", "low"].includes(stop.worth?.payoff), `${stop.id}: invalid payoff`);
    assert.ok(["low", "medium", "high"].includes(stop.worth?.effort), `${stop.id}: invalid effort`);
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
  assert.match(html, /src="trip-location-data\.js\?v=20260722\.3"/);
  assert.match(html, /src="trip-location-cards\.js\?v=20260722\.3"/);
  assert.match(html, /Thu 23 → Sun 26 July/);
  assert.match(html, /checked 22 July 2026/i);
  assert.match(html, /Crowd strategy:/);
  assert.match(html, /leave Budapest early Thursday/i);
  assert.doesNotMatch(html, /start tomorrow|closed today/i);
  assert.match(renderer, /What is it\?/);
  assert.match(renderer, /Why Matija may like it/);
  assert.match(renderer, /Why Tündi may like it/);
  assert.match(renderer, /Why it earns the stop:/);
  assert.match(renderer, /class="worth-pill/);
  assert.match(renderer, /class="effort-pill/);
  assert.match(renderer, /🔥 Must-do/);
  assert.match(renderer, /↪ Easy skip/);
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

test("trip page includes a sourced worth-it Hungary shortlist", () => {
  assert.match(html, /id="mustDoTitle"/);
  assert.match(html, /The ones with a real payoff/);
  assert.match(html, /href="#place-balatonibob-leisure-park"/);
  assert.match(html, /href="#place-csodabogyos-adventure-cave"/);
  assert.match(html, /hungaroring\.hu\/site\/en\/races\/2026-formula-1/);
  assert.match(html, /spartybooking\.com\/ticket\/party-ticket-25-jul/);
  assert.match(html, /tiszataviokocentrum\.hu\/en\/gps-guided-boat-trips/);
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
  const mustGridBase = html.indexOf(".must-grid{display:grid");
  const tabletQuery = html.indexOf("@media(max-width:900px)");
  assert.ok(mustGridBase >= 0 && mustGridBase < tabletQuery, "must-do base styles must precede their responsive overrides");
  assert.match(html, /@media\(max-width:900px\)\{[^\n]*\.stop-facts,\.must-grid\{grid-template-columns:1fr\}/);
  assert.match(html, /@media\(max-width:780px\)\{[^\n]*\.section-head,\.must-head\{display:block\}/);
});

test("release build syntax-checks and inlines the guide as one atomic response", () => {
  assert.match(build, /node --check \.\.\/trip-location-data\.js/);
  assert.match(build, /node --check \.\.\/trip-location-cards\.js/);
  assert.match(build, /trip_plan_inline/);
  assert.match(build, /failed to inline both trip-location modules/);
  assert.doesNotMatch(build, /TRIP_REVISION/);
});

test("current-window advice requires a fresh live check for Zemplén", () => {
  const zemplen = data.loops.B.find((stop) => stop.id === "zemplen-adventure-park");
  assert.ok(zemplen);
  assert.equal(zemplen.open, true);
  assert.equal(zemplen.worth.verdict, "must");
  assert.equal(zemplen.worth.payoff, "high");
  assert.match(zemplen.fit.label, /live status stays green/i);
  assert.match(zemplen.caveat, /headline attractions open/i);
  assert.match(zemplen.caveat, /recheck the live panel/i);
  assert.doesNotMatch(zemplen.caveat, /temporarily closed/i);
});

test("worth-it verdicts reward fun, rarity, beauty, and strong consensus", () => {
  const stopById = new Map(stops.map((stop) => [stop.id, stop]));
  for (const id of ["zamardi-adventure-park", "balatonibob-leisure-park", "tapolca-lake-cave", "csodabogyos-adventure-cave", "szalajka-valley", "zemplen-adventure-park"]) {
    assert.equal(stopById.get(id)?.worth.verdict, "must", `${id}: expected a must-do verdict`);
    assert.equal(stopById.get(id)?.worth.payoff, "high", `${id}: expected a high payoff`);
  }
  for (const id of ["szigliget-castle", "boldogko-castle", "fuzer-castle"]) {
    assert.equal(stopById.get(id)?.worth.verdict, "maybe", `${id}: exceptional castle should remain a considered option`);
    assert.equal(stopById.get(id)?.worth.payoff, "high", `${id}: beauty or traveler consensus should count as a high payoff`);
  }
  assert.match(stopById.get("szigliget-castle")?.hook, /4\.8\/5.+16,000/i);
  assert.match(stopById.get("fuzer-castle")?.hook, /fairy-tale.+4\.8\/5/i);
  assert.equal(stopById.get("sarospatak-rakoczi")?.worth.verdict, "skip");
  assert.equal(stopById.get("sarospatak-rakoczi")?.worth.payoff, "low");
  assert.equal(stopById.get("pecs-zsolnay")?.worth.verdict, "skip", "a distinctive place can still be a skip when the detour is wrong for this trip");
  assert.equal(stopById.get("pecs-zsolnay")?.worth.payoff, "medium");
});
