import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const data = require("./trip-location-data.js");
const map = require("./trip-map.js");
const page = readFileSync(new URL("./trip-map.html", import.meta.url), "utf8");
const mapScript = readFileSync(new URL("./trip-map.js", import.meta.url), "utf8");
const planPage = readFileSync(new URL("./trip-plan.html", import.meta.url), "utf8");
const savedPage = readFileSync(new URL("./saved-places.html", import.meta.url), "utf8");
const discoverPage = readFileSync(new URL("./budapest-london/tripadvisor/index.html", import.meta.url), "utf8");
const build = readFileSync(new URL("./deploy/build.sh", import.meta.url), "utf8");
const serve = readFileSync(new URL("./scripts/serve-local-site.sh", import.meta.url), "utf8");
const allStops = [...data.loops.A, ...data.loops.B];
const expectedGeo = {
  "zamardi-adventure-park": [46.885144, 17.969299, "/way/231872378"],
  "balatonfured-tagore": [46.956661, 17.902681, "/way/27016479"],
  "tihany-abbey": [46.913735, 17.889704, "/way/178673759"],
  "tapolca-lake-cave": [46.883255, 17.44346, "/way/170502522"],
  "szigliget-castle": [46.804679, 17.436359, "/relation/8167589"],
  "heviz-thermal-lake": [46.786822, 17.192817, "/way/10418191"],
  "hegyestu-kapolcs": [46.889404, 17.647343, "/node/3081517738"],
  "pecs-zsolnay": [46.077908, 18.248229, "/way/314723496"],
  "eger-castle-bath": [47.90411, 20.3795, "/relation/18318703"],
  "lillafured-bukk": [48.104626, 20.623113, "/way/156790161"],
  "szalajka-valley": [48.076014, 20.410349, "/node/1158963147"],
  "aggtelek-baradla": [48.471487, 20.495229, "https://anp.hu/en/tura/2/"],
  "boldogko-castle": [48.34441, 21.23232, "/relation/6784664"],
  "sarospatak-rakoczi": [48.315587, 21.568905, "/relation/18124569"],
  "fuzer-castle": [48.541955, 21.459392, "/way/207114467"],
  "regec-castle": [48.378626, 21.344278, "/way/360207514"],
  "zemplen-adventure-park": [48.412625, 21.638988, "/node/2982612219"],
};

test("all 17 researched stops have sourced coordinates inside Hungary", () => {
  assert.equal(data.schemaVersion, 2);
  assert.equal(allStops.length, 17);
  assert.deepEqual(new Set(Object.keys(expectedGeo)), new Set(allStops.map((stop) => stop.id)));
  for (const stop of allStops) {
    const [expectedLat, expectedLng, expectedSource] = expectedGeo[stop.id];
    assert.equal(typeof stop.geo.label, "string", `${stop.id}: missing pin label`);
    assert.ok(stop.geo.label.length > 3, `${stop.id}: pin label is too short`);
    assert.ok(stop.geo.lat >= 45.7 && stop.geo.lat <= 48.7, `${stop.id}: latitude is outside Hungary`);
    assert.ok(stop.geo.lng >= 16 && stop.geo.lng <= 23, `${stop.id}: longitude is outside Hungary`);
    assert.match(stop.geo.sourceUrl, /^https:\/\//, `${stop.id}: coordinate source must use HTTPS`);
    assert.ok(Math.abs(stop.geo.lat - expectedLat) < 0.000001, `${stop.id}: latitude changed unexpectedly`);
    assert.ok(Math.abs(stop.geo.lng - expectedLng) < 0.000001, `${stop.id}: longitude changed unexpectedly`);
    assert.ok(stop.geo.sourceUrl.includes(expectedSource), `${stop.id}: coordinate source object changed unexpectedly`);
  }
});

test("explicit route topology resolves every stop without relying on data order", () => {
  const stopById = map.buildStopIndex(data);
  assert.equal(stopById.size, 17);

  assert.deepEqual(map.ROUTES.A.mainOrder, [
    "zamardi-adventure-park",
    "balatonfured-tagore",
    "tihany-abbey",
    "hegyestu-kapolcs",
    "tapolca-lake-cave",
    "szigliget-castle",
    "heviz-thermal-lake",
  ]);
  assert.deepEqual(map.ROUTES.A.optionalPaths, [["budapest", "pecs-zsolnay", "zamardi-adventure-park"]]);
  assert.deepEqual(map.ROUTES.B.mainOrder, [
    "eger-castle-bath",
    "lillafured-bukk",
    "szalajka-valley",
    "aggtelek-baradla",
    "boldogko-castle",
    "sarospatak-rakoczi",
    "fuzer-castle",
    "zemplen-adventure-park",
  ]);
  assert.deepEqual(map.ROUTES.B.optionalPaths, [["sarospatak-rakoczi", "regec-castle", "zemplen-adventure-park"]]);

  for (const loop of ["A", "B"]) {
    const route = map.ROUTES[loop];
    assert.equal(new Set(route.stopOrder).size, route.stopOrder.length, `Loop ${loop} repeats a stop`);
    const optionalIds = route.optionalPaths.flat().filter((id) => id !== "budapest");
    assert.deepEqual(new Set([...route.mainOrder, ...optionalIds]), new Set(route.stopOrder));
    for (const id of route.stopOrder) assert.equal(stopById.get(id).loop, loop);
  }

  const loopAPath = map.routeCoordinates(map.ROUTES.A, stopById);
  const loopBPath = map.routeCoordinates(map.ROUTES.B, stopById);
  assert.equal(loopAPath.length, 9);
  assert.equal(loopBPath.length, 10);
  assert.deepEqual(loopAPath[0], map.BUDAPEST);
  assert.deepEqual(loopAPath.at(-1), map.BUDAPEST);
  assert.deepEqual(loopBPath[0], map.BUDAPEST);
  assert.deepEqual(loopBPath.at(-1), map.BUDAPEST);
  assert.ok(!map.ROUTES.A.mainOrder.includes("pecs-zsolnay"), "Pécs must remain an optional extension");
});

test("distance and external-map links use the selected coordinates", () => {
  const zamardi = data.loops.A.find((stop) => stop.id === "zamardi-adventure-park");
  const distance = map.haversineKm(map.BUDAPEST, zamardi.geo);
  assert.ok(distance > 90 && distance < 130, `unexpected Budapest–Zamárdi distance: ${distance}`);

  const directions = new URL(map.googleDirectionsUrl(zamardi));
  assert.equal(directions.origin, "https://www.google.com");
  assert.equal(directions.searchParams.get("origin"), `${map.BUDAPEST.lat},${map.BUDAPEST.lng}`);
  assert.equal(directions.searchParams.get("destination"), `${zamardi.geo.lat},${zamardi.geo.lng}`);
  assert.equal(directions.searchParams.get("travelmode"), "driving");
  assert.match(map.osmPinUrl(zamardi), /^https:\/\/www\.openstreetmap\.org\/\?mlat=/);
});

test("invalid topology and missing coordinates fail closed", () => {
  const missingGeo = structuredClone(data);
  delete missingGeo.loops.A[0].geo;
  assert.throws(() => map.buildStopIndex(missingGeo), /no valid coordinates/);

  const duplicate = structuredClone(data);
  duplicate.loops.B[0].id = duplicate.loops.A[0].id;
  assert.throws(() => map.buildStopIndex(duplicate), /Duplicate trip stop/);
});

test("map page provides two maps, route filters, legend, and accessible fallbacks", () => {
  assert.match(page, /id="routeMap"[^>]+role="region"/);
  assert.match(page, /id="orientationMap"[^>]+role="region"/);
  assert.equal([...page.matchAll(/data-map-mode="(?:all|A|B)"/g)].length, 3);
  assert.match(page, /Loop A · Balaton/);
  assert.match(page, /Loop B · north-east/);
  assert.match(page, /Lines show planning order, not turn-by-turn roads/);
  assert.match(page, /leaflet@1\.9\.4/);
  assert.match(page, /integrity="sha256-p4NxAoJBhIIN\+hmNHrzRCf9tD\/miZyoHS5obTRR9BMY="/);
  assert.match(page, /integrity="sha256-20nQCchB9co0qIjJZRGuk2\/Z9VM\+kNiyxNV1lvTlZBo="/);
  assert.match(mapScript, /https:\/\/tile\.openstreetmap\.org\/\{z\}\/\{x\}\/\{y\}\.png/);
  assert.match(mapScript, /OpenStreetMap contributors/);
  assert.match(mapScript, /scrollWheelZoom: false/);
  assert.match(mapScript, /L\.polyline/);
  assert.match(mapScript, /#place-\(\[a-z0-9-\]\+\)/);
  assert.match(mapScript, /aria-pressed/);
  assert.doesNotMatch(mapScript, /aria-selected/);
});

test("every primary page links to the route-map tab with the correct depth", () => {
  assert.match(planPage, /href="trip-map\.html">🧭 Route map/);
  assert.match(savedPage, /href="trip-map\.html">🧭 Route map/);
  assert.match(discoverPage, /href="\.\.\/\.\.\/trip-map\.html">🧭 Route map/);
  assert.match(page, /class="here" href="trip-map\.html" aria-current="page"/);
});

test("release build validates and atomically inlines the map modules", () => {
  assert.match(build, /cp \.\.\/trip-plan\.html \.\.\/trip-map\.html/);
  assert.match(build, /node --check \.\.\/trip-map\.js/);
  assert.match(build, /trip_map_inline/);
  assert.match(build, /failed to inline the route-map modules/);
  assert.match(build, /\.\.\/test_trip_map\.mjs/);
  assert.match(serve, /trip-map\.html/);
});
