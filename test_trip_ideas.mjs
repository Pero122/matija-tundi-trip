import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const ideasData = require("./trip-ideas-data.js");
const locationData = require("./trip-location-data.js");
const photos = require("./trip-map-photos.js");
const ideasUi = require("./trip-ideas.js");
const page = readFileSync(new URL("./trip-ideas.html", import.meta.url), "utf8");
const script = readFileSync(new URL("./trip-ideas.js", import.meta.url), "utf8");
const build = readFileSync(new URL("./deploy/build.sh", import.meta.url), "utf8");
const serve = readFileSync(new URL("./scripts/serve-local-site.sh", import.meta.url), "utf8");
const launchAgent = readFileSync(new URL("./scripts/local-site-launchagent.sh", import.meta.url), "utf8");
const navPages = [
  "trip-plan.html",
  "trip-map.html",
  "saved-places.html",
  "budapest-london/tripadvisor/index.html",
].map((path) => [path, readFileSync(new URL(`./${path}`, import.meta.url), "utf8")]);

test("five editorial ideas validate against the shared guides and photo manifest", () => {
  assert.equal(ideasData.schemaVersion, 1);
  assert.equal(ideasData.ideas.length, 5);
  assert.equal(ideasUi.validateIdeas(ideasData, locationData, photos), true);
  assert.deepEqual([...ideasData.ideas].sort((a, b) => a.rank - b.rank).map((idea) => idea.id), [
    "balaton-balanced",
    "balaton-chill",
    "northeast-expedition",
    "balaton-adrenaline",
    "pecs-thermal",
  ]);
  assert.equal(ideasData.defaultIdeaId, "balaton-balanced");
  assert.match(ideasData.rankingMethod, /confidence/i);
  assert.match(ideasData.rankingMethod, /memorable/i);
  assert.match(ideasData.rankingMethod, /couple fit/i);
  assert.match(ideasData.rankingMethod, /feasibility/i);
  const ranked = [...ideasData.ideas].sort((a, b) => a.rank - b.rank);
  for (const idea of ranked) {
    assert.ok(idea.score >= 0 && idea.score <= 100);
    for (const dimension of Object.values(idea.scores)) assert.ok(dimension >= 0 && dimension <= 100);
    assert.match(idea.metrics.drive, /(?:km|h)/);
    assert.ok(idea.bookNow.length > 12);
    assert.ok(idea.tradeoff.length > 20);
    assert.ok(idea.skip.length > 12);
  }
  assert.deepEqual(ranked.map((idea) => idea.score), [...ranked.map((idea) => idea.score)].sort((a, b) => b - a));
});

test("only 15 curated existing stops are illustrated, each with exactly five verified photos", () => {
  const stopIds = new Set(ideasData.ideas.flatMap(ideasUi.routeStopIds));
  assert.equal(stopIds.size, 15);
  assert.ok(!stopIds.has("danube-bend-cruise"));
  assert.ok(!page.includes("needs-5-verified"));
  for (const stopId of stopIds) {
    assert.equal(photos.stops[stopId].length, 5, `${stopId}: missing five-photo gallery`);
    for (const photo of photos.stops[stopId]) {
      assert.match(photo.src, /^images\/trip-map\/20260722\.3\//);
      assert.match(photo.sourceUrl, /^https:\/\//);
      assert.ok(photo.verification.length >= 30);
    }
  }
});

test("alternative days do not become fake sequential map legs", () => {
  const northeast = ideasData.ideas.find((idea) => idea.id === "northeast-expedition");
  const topology = ideasUi.routeTopology(northeast);
  assert.deepEqual(topology.primaryIds, [
    "eger-castle-bath",
    "szalajka-valley",
    "aggtelek-baradla",
    "zemplen-adventure-park",
  ]);
  assert.deepEqual(topology.branches, [["eger-castle-bath", "lillafured-bukk", "aggtelek-baradla"]]);
  const friday = northeast.days[1];
  assert.equal(friday.stops[0].choiceGroup, "friday-forest");
  assert.equal(friday.stops[1].choiceGroup, "friday-forest");
  assert.match(friday.note, /Do not stack both/);

  const balanced = ideasData.ideas.find((idea) => idea.id === "balaton-balanced");
  const balancedTopology = ideasUi.routeTopology(balanced);
  assert.ok(balancedTopology.primaryIds.includes("csodabogyos-adventure-cave"));
  assert.deepEqual(balancedTopology.branches, [["szigliget-castle", "heviz-thermal-lake"]]);
  assert.equal(balanced.days[2].stops[0].choiceGroup, "saturday-energy");
  assert.equal(balanced.days[2].stops[1].choiceGroup, "saturday-energy");
});

test("deep links are strict, stable and constrained to each route", () => {
  assert.equal(ideasUi.formatHash("balaton-balanced", "tapolca-lake-cave"), "#idea/balaton-balanced/stop/tapolca-lake-cave");
  assert.deepEqual(ideasUi.parseHash("#idea/balaton-balanced/stop/tapolca-lake-cave", ideasData), { ideaId: "balaton-balanced", stopId: "tapolca-lake-cave" });
  assert.deepEqual(ideasUi.parseHash("#idea/balaton-chill/stop/zemplen-adventure-park", ideasData), { ideaId: "balaton-chill", stopId: "" });
  assert.deepEqual(ideasUi.parseHash("#javascript:alert(1)", ideasData), { ideaId: "balaton-balanced", stopId: "" });
  assert.throws(() => ideasUi.formatHash("../bad"), /invalid/);
});

test("gallery renders five in-page lightbox triggers with escaped copy", () => {
  const stop = locationData.loops.A.find((entry) => entry.id === "tapolca-lake-cave");
  const gallery = ideasUi.renderPhotoGallery(stop, photos.stops[stop.id]);
  assert.equal((gallery.match(/class="photo-card/g) || []).length, 5);
  assert.equal((gallery.match(/data-photo-index=/g) || []).length, 5);
  assert.equal((gallery.match(/aria-controls="photoLightbox"/g) || []).length, 5);
  assert.ok(!/class="photo-open"[^>]*target=/.test(gallery));
  const hostile = { ...stop, name: '<img src=x onerror="bad">' };
  const escaped = ideasUi.renderPhotoGallery(hostile, photos.stops[stop.id]);
  assert.ok(!escaped.includes('<img src=x onerror="bad">'));
  assert.match(escaped, /&lt;img src=x onerror=&quot;bad&quot;&gt;/);
});

test("page exposes comparison, map, timeline, full guide and accessible lightbox", () => {
  assert.match(page, /<title>Trip ideas/);
  assert.match(page, /id="ideaComparison"/);
  assert.match(page, /id="ideasMap"/);
  assert.match(page, /id="ideaTimeline"/);
  assert.match(page, /id="ideaStopDetail"/);
  assert.match(page, /id="photoLightbox"/);
  assert.match(page, /Use ← and → · swipe on mobile · Esc to close/);
  assert.match(page, /sha256-p4NxAoJBhIIN\+hmNHrzRCf9tD\/miZyoHS5obTRR9BMY=/);
  assert.match(page, /Lines show planning order, not turn-by-turn roads/);
  assert.equal((page.match(/aria-live="polite"/g) || []).length, 2, "only concise status regions should announce dynamic changes");
  assert.match(script, /sort\(\(a, b\) => a\.rank - b\.rank\)/);
  assert.match(script, /event\.key === "ArrowLeft"/);
  assert.match(script, /touchend/);
  assert.match(script, /options\.pan !== false/);
  assert.match(script, /openPopup: false, pan: false/);
});

test("Trip ideas is visible in every requested site navigation", () => {
  assert.match(page, /class="here" href="trip-ideas\.html" aria-current="page">✨ Trip ideas/);
  for (const [path, html] of navPages) {
    assert.match(html, /trip-ideas\.html">✨ Trip ideas/, `${path}: Trip ideas nav link is missing`);
  }
});

test("build inlines all four idea modules in dependency order and health checks the page", () => {
  assert.match(build, /cp \.\.\/trip-plan\.html \.\.\/trip-map\.html \.\.\/trip-ideas\.html/);
  const dataRule = build.indexOf("trip-location-data\\.js");
  const photosRule = build.indexOf("trip-map-photos\\.js", dataRule + 1);
  const ideaDataRule = build.indexOf("trip-ideas-data\\.js", photosRule + 1);
  const ideaRule = build.indexOf("trip-ideas\\.js", ideaDataRule + 1);
  assert.ok(dataRule >= 0 && dataRule < photosRule && photosRule < ideaDataRule && ideaDataRule < ideaRule);
  assert.match(build, /test_trip_ideas\.mjs/);
  assert.match(build, /failed to inline all four Trip ideas modules/);
  assert.match(serve, /trip-ideas\.html/);
  assert.match(launchAgent, /TRIP_IDEAS_URL=.*trip-ideas\.html/);
  assert.match(launchAgent, /site_is_healthy/);
});
