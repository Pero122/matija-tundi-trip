import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const pageUrl = new URL("./index.html", import.meta.url);
const html = readFileSync(pageUrl, "utf8");

const GROUP_SENTINELS = Object.freeze({
  baths: "526980",
  spas: "1908676",
  fitness: "10206379",
  food: "25283313",
  markets: "3560261",
  shopping: "6731691",
  beer: "20100566",
  wine: "3548341",
  nightlife: "15238035",
  nighttours: "12087624",
  clubs: "4187519",
  escape: "14004742",
  quests: "15802944",
  games: "13237730",
  shooting: "23492767",
  creative: "27151671",
  water: "11452776",
  partycruises: "10753631",
  shows: "276857",
  galleries: "10761239",
  culture: "33233594",
  landmarks: "276817",
  tours: "17745528",
  rides: "12866758",
  bikes: "11446184",
  outdoor: "16848331",
  nature: "14145483",
  parks: "298971",
  family: "8358120",
  transport: "279012",
  drivers: "13307665",
  travel: "24859352",
  daytrips: "11466755",
  crossborder: "17330448",
});

// Conservative floors catch collapsed or accidentally bypassed classifier branches
// while still allowing normal crawler churn above the reviewed July 2026 inventory.
const MIN_VISIBLE_LISTINGS = 1100;
const MIN_GROUP_COUNTS = Object.freeze({
  baths: 10,
  spas: 30,
  fitness: 5,
  food: 20,
  markets: 10,
  shopping: 25,
  beer: 5,
  wine: 15,
  nightlife: 20,
  nighttours: 12,
  clubs: 10,
  escape: 10,
  quests: 3,
  games: 15,
  shooting: 8,
  creative: 8,
  water: 20,
  partycruises: 9,
  shows: 12,
  galleries: 20,
  culture: 25,
  landmarks: 35,
  tours: 40,
  rides: 25,
  bikes: 12,
  outdoor: 10,
  nature: 12,
  parks: 20,
  family: 20,
  transport: 10,
  drivers: 35,
  travel: 4,
  daytrips: 25,
  crossborder: 9,
});

function uniqueMarker(source, marker, from = 0) {
  const found = source.indexOf(marker, from);
  if (found < 0) throw new Error(`Missing marker: ${marker}`);
  if (source.indexOf(marker, found + marker.length) >= 0) {
    throw new Error(`Duplicate marker after DATA: ${marker}`);
  }
  return found;
}

function extractPageParts(source) {
  const scriptOpen = source.lastIndexOf("<script>");
  const scriptClose = source.lastIndexOf("</script>");
  if (scriptOpen < 0 || scriptClose <= scriptOpen) throw new Error("Missing inline app script");
  const inline = source.slice(scriptOpen + "<script>".length, scriptClose);

  const dataMarker = "const DATA=";
  const dataMarkerAt = inline.startsWith(`\n${dataMarker}`) ? 1 : inline.startsWith(dataMarker) ? 0 : -1;
  if (dataMarkerAt < 0) throw new Error("DATA must start the inline app script");
  const dataStart = dataMarkerAt + dataMarker.length;
  const dataEndMarker = ";\nconst SB_URL";
  const dataEnd = inline.indexOf(dataEndMarker, dataStart);
  if (dataEnd < 0) throw new Error(`Missing marker: ${dataEndMarker}`);

  // Search only after the complete JSON block. Marker-like scraped names or
  // blurbs can therefore never become executable validator source.
  const classifierMarker = "const GROUPS=";
  const classifierAt = uniqueMarker(inline, classifierMarker, dataEnd + dataEndMarker.length);
  const classifierStart = classifierAt + classifierMarker.length;
  const classifierEnd = uniqueMarker(inline, "// ---------- dropdowns ----------", classifierStart);
  return {
    dataJson: inline.slice(dataStart, dataEnd),
    classifier: inline.slice(classifierStart, classifierEnd),
  };
}

// Regression probe: classifier-looking scraped text must stay inside DATA.
const markerProbe = `<script>\nconst DATA=[{"n":"const GROUPS=[]; // ---------- dropdowns ----------"}];\nconst SB_URL="x";\nconst GROUPS=[{id:"real"}];\n// ---------- dropdowns ----------\n</script>`;
assert.match(extractPageParts(markerProbe).classifier, /id:"real"/);

const { dataJson, classifier } = extractPageParts(html);
const sandbox = Object.assign(Object.create(null), { DATA_JSON: dataJson });
const context = vm.createContext(sandbox, {
  codeGeneration: { strings: false, wasm: false },
});
const auditScript = new vm.Script(
  `
    "use strict";
    const DATA=JSON.parse(DATA_JSON);
    const listingId=d=>(d.url||"").match(/-d(\\d+)-/)?.[1]||"";
    const GROUPS=${classifier}
    ;(() => {
      const groupIds=GROUPS.map(group=>group.id);
      const buckets=Object.fromEntries(groupIds.map(id=>[id,[]]));
      const visible=DATA.filter(d=>d.city==="budapest"&&d.origin!=="foreign-origin"&&!isHiddenListing(d));
      const assignments={};
      visible.forEach(d=>{
        const group=activityGroup(d);
        (buckets[group]||buckets.review).push(d);
        assignments[listingId(d)]=group;
      });

      const allIds=new Set(DATA.map(listingId).filter(Boolean));
      const curatedOwners=new Map(),duplicateCurated=[];
      Object.entries(CURATED_GROUP_IDS).forEach(([group,ids])=>{
        ids.trim().split(/\\s+/).filter(Boolean).forEach(id=>{
          if(curatedOwners.has(id)) duplicateCurated.push({id,first:curatedOwners.get(id),second:group});
          curatedOwners.set(id,group);
        });
      });
      const hiddenEntries=Object.entries(HIDDEN_LISTING_REASONS);
      const allById=new Map(DATA.map(d=>[listingId(d),d]));
      const validClaudeTones=new Set(["yes","maybe","skip","utility"]);
      const claudeEligible=DATA.filter(hasClaudeTake);
      const claudeTakeIssues=claudeEligible.map(d=>({d,take:claudeTake(d)})).filter(({take})=>
        !take||!validClaudeTones.has(take.tone)||typeof take.label!=="string"
        ||!take.label.trim()||take.label.length>40||typeof take.text!=="string"
        ||!take.text.trim()||take.text.length>280
      ).map(({d,take})=>({id:listingId(d),name:d.n,take}));
      const takeForId=id=>allById.has(id)?claudeTake(allById.get(id)):null;

      globalThis.AUDIT_JSON=JSON.stringify({
        groupIds,
        visible:visible.length,
        assignments,
        counts:Object.fromEntries(groupIds.map(id=>[id,buckets[id].length])),
        duplicateCurated,
        missingCurated:[...curatedOwners.keys()].filter(id=>!allIds.has(id)),
        curatedHidden:[...curatedOwners.keys()].filter(id=>HIDDEN_LISTING_IDS.has(id)),
        missingHidden:hiddenEntries.filter(([id])=>!allIds.has(id)).map(([id])=>id),
        hiddenWithoutReason:hiddenEntries.filter(([,reason])=>!String(reason).trim()).map(([id])=>id),
        missingClaudeGuidance:groupIds.filter(id=>!Object.hasOwn(CLAUDE_GROUP_GUIDANCE,id)),
        invalidClaudeOverrideIds:Object.keys(CLAUDE_ITEM_TAKES).filter(id=>!/^\\d+$/.test(id)||!allIds.has(id)),
        claudeTakeIssues,
        claudeEligibilityIssues:DATA.filter(d=>!hasClaudeTake(d)&&claudeTake(d)!==null)
          .map(d=>({id:listingId(d),name:d.n,city:d.city,origin:d.origin,take:claudeTake(d)})),
        wineTakeIssues:claudeEligible.filter(d=>activityGroup(d)==="wine"&&claudeTake(d).tone!=="skip")
          .map(d=>({id:listingId(d),name:d.n,take:claudeTake(d)})),
        utilityTakeIssues:claudeEligible.filter(d=>CLAUDE_UTILITY_GROUPS.has(activityGroup(d))&&claudeTake(d).tone!=="utility")
          .map(d=>({id:listingId(d),name:d.n,take:claudeTake(d)})),
        claudeSentinels:{
          palatinus:takeForId("526980"),
          thinLowRating:takeForId("279410"),
          unrated:takeForId("32877475"),
          foodWithWines:takeForId("25283313"),
          roseCruise:takeForId("34312653"),
          wine:takeForId("3548341"),
          driver:takeForId("13307665"),
          londonArchive:takeForId("1526046"),
        },
        review:buckets.review.map(d=>({id:listingId(d),name:d.n})),
        waterParksInSpas:buckets.spas
          .filter(d=>/water parks?|public baths?|thermal baths?|swimming pools?|\\blidos?\\b/.test(
            ((d.n||"")+" "+(d.sub||"")).toLowerCase(),
          ))
          .map(d=>({id:listingId(d),name:d.n})),
      });
    })();
  `,
  { filename: "discover-group-audit.vm.js" },
);
auditScript.runInContext(context, { timeout: 2000 });
const audit = JSON.parse(sandbox.AUDIT_JSON);

const errors = [];
if (new Set(audit.groupIds).size !== audit.groupIds.length) errors.push("Duplicate GROUPS id");
if (audit.visible < MIN_VISIBLE_LISTINGS) {
  errors.push(`Visible inventory collapsed: ${audit.visible} < ${MIN_VISIBLE_LISTINGS}`);
}
if (audit.duplicateCurated.length) {
  errors.push(`Curated IDs assigned twice: ${JSON.stringify(audit.duplicateCurated)}`);
}
if (audit.missingCurated.length) {
  errors.push(`Curated IDs missing from DATA: ${audit.missingCurated.join(", ")}`);
}
if (audit.curatedHidden.length) {
  errors.push(`Listings cannot be both curated and hidden: ${audit.curatedHidden.join(", ")}`);
}
if (audit.missingHidden.length) {
  errors.push(`Hidden IDs missing from DATA: ${audit.missingHidden.join(", ")}`);
}
if (audit.hiddenWithoutReason.length) {
  errors.push(`Hidden IDs need a reason: ${audit.hiddenWithoutReason.join(", ")}`);
}
if (audit.missingClaudeGuidance.length) {
  errors.push(`Groups need Claude guidance: ${audit.missingClaudeGuidance.join(", ")}`);
}
if (audit.invalidClaudeOverrideIds.length) {
  errors.push(`Claude overrides must reference existing numeric IDs: ${audit.invalidClaudeOverrideIds.join(", ")}`);
}
if (audit.claudeTakeIssues.length) {
  errors.push(`Invalid Claude takes: ${JSON.stringify(audit.claudeTakeIssues)}`);
}
if (audit.claudeEligibilityIssues.length) {
  errors.push(`Ineligible listings must not receive Claude takes: ${JSON.stringify(audit.claudeEligibilityIssues)}`);
}
if (audit.wineTakeIssues.length) {
  errors.push(`Wine listings must be marked low-fit: ${JSON.stringify(audit.wineTakeIssues)}`);
}
if (audit.utilityTakeIssues.length) {
  errors.push(`Utility listings must stay logistics-only: ${JSON.stringify(audit.utilityTakeIssues)}`);
}
if (audit.claudeSentinels.palatinus?.label !== "Warm-day yes") {
  errors.push(`Palatinus Claude note regressed: ${JSON.stringify(audit.claudeSentinels.palatinus)}`);
}
if (audit.claudeSentinels.thinLowRating?.label !== "Unproven") {
  errors.push(`Thin low-rating Claude note regressed: ${JSON.stringify(audit.claudeSentinels.thinLowRating)}`);
}
if (audit.claudeSentinels.unrated?.label !== "Needs research") {
  errors.push(`Unrated Claude note regressed: ${JSON.stringify(audit.claudeSentinels.unrated)}`);
}
if (audit.claudeSentinels.foodWithWines?.label !== "Mixed fit") {
  errors.push(`Plural-wines preference note regressed: ${JSON.stringify(audit.claudeSentinels.foodWithWines)}`);
}
if (audit.claudeSentinels.roseCruise?.label !== "Mixed fit") {
  errors.push(`Rosé preference note regressed: ${JSON.stringify(audit.claudeSentinels.roseCruise)}`);
}
if (audit.claudeSentinels.wine?.label !== "Low fit for Matija") {
  errors.push(`Wine preference override regressed: ${JSON.stringify(audit.claudeSentinels.wine)}`);
}
if (audit.claudeSentinels.driver?.label !== "Logistics only") {
  errors.push(`Driver utility note regressed: ${JSON.stringify(audit.claudeSentinels.driver)}`);
}
if (audit.claudeSentinels.londonArchive !== null) {
  errors.push(`London archive must not receive Hungary guidance: ${JSON.stringify(audit.claudeSentinels.londonArchive)}`);
}
if (audit.review.length) errors.push(`Unreviewed listings: ${JSON.stringify(audit.review)}`);
if (audit.waterParksInSpas.length) {
  errors.push(`Bath/pool listings still in spas: ${JSON.stringify(audit.waterParksInSpas)}`);
}

const expectedGroups = Object.keys(GROUP_SENTINELS);
const groupsWithoutSentinels = audit.groupIds.filter(id=>id!=="review"&&!GROUP_SENTINELS[id]);
if (groupsWithoutSentinels.length) {
  errors.push(`Groups need independent sentinels: ${groupsWithoutSentinels.join(", ")}`);
}
for (const group of expectedGroups) {
  const id = GROUP_SENTINELS[group];
  if (!(id in audit.assignments)) errors.push(`Sentinel ${id} for ${group} is missing or hidden`);
  else if (audit.assignments[id] !== group) {
    errors.push(`Sentinel ${id} expected ${group}, got ${audit.assignments[id]}`);
  }
  if ((audit.counts[group] || 0) < MIN_GROUP_COUNTS[group]) {
    errors.push(`Group ${group} collapsed: ${audit.counts[group] || 0} < ${MIN_GROUP_COUNTS[group]}`);
  }
}

if (errors.length) {
  console.error(errors.join("\n"));
  process.exitCode = 1;
} else {
  const populated = Object.values(audit.counts).filter(Boolean).length;
  console.log(`Discover groups valid: ${audit.visible} listings across ${populated} populated groups`);
}
