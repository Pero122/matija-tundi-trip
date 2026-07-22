import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

function parseArguments(argv) {
  const options = {
    allowPartialResearch: false,
    inventoryOnly: false,
    printVisibleJson: false,
    printVisibleRefs: false,
    siteRoot: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--allow-partial-research") {
      options.allowPartialResearch = true;
    } else if (argument === "--inventory-only") {
      options.inventoryOnly = true;
    } else if (argument === "--print-visible-json") {
      options.printVisibleJson = true;
    } else if (argument === "--print-visible-refs") {
      options.printVisibleRefs = true;
    } else if (argument === "--site-root") {
      if (options.siteRoot !== null) {
        throw new Error("--site-root may only be provided once");
      }
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new Error("--site-root requires a directory argument");
      }
      options.siteRoot = resolve(value);
      index += 1;
    } else {
      throw new Error(`unknown argument: ${argument}`);
    }
  }
  if (options.printVisibleJson && options.printVisibleRefs) {
    throw new Error("--print-visible-json and --print-visible-refs are mutually exclusive");
  }
  return options;
}

function failArgument(message) {
  console.error(`error: ${message}`);
  process.exit(2);
}

let options;
try {
  options = parseArguments(process.argv.slice(2));
} catch (error) {
  failArgument(error instanceof Error ? error.message : String(error));
}

const sourceRoot = fileURLToPath(new URL(".", import.meta.url));
const siteRoot = options.siteRoot || sourceRoot;
try {
  if (!statSync(siteRoot).isDirectory()) {
    failArgument(`--site-root is not a directory: ${siteRoot}`);
  }
} catch (error) {
  failArgument(`cannot read --site-root ${siteRoot}: ${error.message}`);
}

const html = readFileSync(resolve(siteRoot, "index.html"), "utf8");
const setupSql = readFileSync(new URL("./supabase_setup.sql", import.meta.url), "utf8");
const briefsSource = readFileSync(resolve(siteRoot, "activity-briefs.js"), "utf8");
const pricingSource = readFileSync(resolve(siteRoot, "activity-pricing.js"), "utf8");
const allowPartialResearch = options.allowPartialResearch || options.inventoryOnly;
const appScript = html.slice(html.lastIndexOf("<script>") + "<script>".length, html.lastIndexOf("</script>"));
assert.doesNotThrow(() => new Function(appScript), "Discover inline script must parse");
assert.match(html, /const UI_FIELDS=\[[^\]]*"reviewed"/);
assert.match(html, /const UI_FIELDS=\[[^\]]*"reviewed_revision"/);
assert.match(html, /const GROUP_KEY_PREFIX="@discover-group:v1\|"/);
const optionalRevisionQuery = String.raw`(?:\?v=[0-9a-f]{64})?`;
for (const script of [
  "activity-briefs",
  "activity-pricing",
  "discover-collaboration",
  "discover-pricing",
]) {
  assert.match(
    html,
    new RegExp(`<script src="\\./${script}\\.js${optionalRevisionQuery}"></script>`),
  );
}
assert.match(html, /const COLLAB=window\.DISCOVER_COLLABORATION/);
assert.match(html, /const PRICE_UI=window\.DISCOVER_PRICING/);
assert.match(html, /window\.ACTIVITY_BRIEFS_REVISION===window\.ACTIVITY_PRICING_REVISION/);
assert.match(html, /Discover release bundle mismatch/);
assert.match(html, /function setGroupReviewed\([^]*COLLAB\.reviewedPatch/);
assert.match(html, /schemaVersion:3,activities,categories/);
assert.match(setupSql, /add column if not exists reviewed boolean/i);
assert.match(setupSql, /add column if not exists reviewed_revision text/i);
const BRIEFS_PREFIX = "// Grounded activity briefs, keyed by route-qualified TripAdvisor ID or editorial idea ID.\n"
  + "// Raw source descriptions and review text stay local; only concise synthesis ships.\n"
  + "window.ACTIVITY_BRIEFS=";
const PRICING_PREFIX = "// Researched price states, keyed by route-qualified Tripadvisor ID or editorial idea ID.\n"
  + "// Numeric prices are copied from the source evidence; generated text only explains packages.\n"
  + "window.ACTIVITY_PRICING=";
const REVISION_RE = /^[0-9a-f]{64}$/;

function parseResearchBundle(source, { prefix, revisionGlobal, fileName }) {
  if (!source.startsWith(prefix)) {
    throw new Error(`${fileName} must start with its approved comments and JSON assignment`);
  }

  const revisionMarker = `;\nwindow.${revisionGlobal}=`;
  const markerAt = source.lastIndexOf(revisionMarker);
  let json;
  let revision = null;
  if (markerAt >= 0) {
    json = source.slice(prefix.length, markerAt);
    const revisionTail = source.slice(markerAt + 2);
    const match = revisionTail.match(
      new RegExp(`^window\\.${revisionGlobal}=\"([0-9a-f]{64})\";\\n$`),
    );
    if (!match || !REVISION_RE.test(match[1])) {
      throw new Error(`${fileName} has an invalid or executable revision suffix`);
    }
    revision = match[1];
  } else {
    if (!source.endsWith(";\n")) {
      throw new Error(`${fileName} must end after its JSON assignment`);
    }
    json = source.slice(prefix.length, -2);
  }

  JSON.parse(json);
  return { json, revision };
}

function researchRevisionErrors(briefsRevision, pricingRevision, allowPartial) {
  const issues = [];
  if (!allowPartial && !briefsRevision) {
    issues.push("activity-briefs.js is missing a valid ACTIVITY_BRIEFS_REVISION");
  }
  if (!allowPartial && !pricingRevision) {
    issues.push("activity-pricing.js is missing a valid ACTIVITY_PRICING_REVISION");
  }
  if (briefsRevision && pricingRevision && briefsRevision !== pricingRevision) {
    issues.push(
      `Research bundle revision mismatch: briefs ${briefsRevision}, pricing ${pricingRevision}`,
    );
  }
  return issues;
}

const briefsBundle = parseResearchBundle(briefsSource, {
  prefix: BRIEFS_PREFIX,
  revisionGlobal: "ACTIVITY_BRIEFS_REVISION",
  fileName: "activity-briefs.js",
});
const briefsJson = briefsBundle.json;
assert.throws(
  () => parseResearchBundle("globalThis.unexpected=true;\n" + briefsSource, {
    prefix: BRIEFS_PREFIX,
    revisionGlobal: "ACTIVITY_BRIEFS_REVISION",
    fileName: "activity-briefs.js",
  }),
  /must start/,
);
assert.throws(
  () => parseResearchBundle(briefsSource + "globalThis.unexpected=true;\n", {
    prefix: BRIEFS_PREFIX,
    revisionGlobal: "ACTIVITY_BRIEFS_REVISION",
    fileName: "activity-briefs.js",
  }),
);
const pricingBundle = parseResearchBundle(pricingSource, {
  prefix: PRICING_PREFIX,
  revisionGlobal: "ACTIVITY_PRICING_REVISION",
  fileName: "activity-pricing.js",
});
const pricingJson = pricingBundle.json;
assert.throws(
  () => parseResearchBundle("globalThis.unexpected=true;\n" + pricingSource, {
    prefix: PRICING_PREFIX,
    revisionGlobal: "ACTIVITY_PRICING_REVISION",
    fileName: "activity-pricing.js",
  }),
  /must start/,
);
assert.throws(
  () => parseResearchBundle(pricingSource + "globalThis.unexpected=true;\n", {
    prefix: PRICING_PREFIX,
    revisionGlobal: "ACTIVITY_PRICING_REVISION",
    fileName: "activity-pricing.js",
  }),
);

// Bundle-format regressions run even during an active partial crawl. Missing
// revisions are temporarily tolerated there, but malformed or mismatched
// revisions can never become a deployable mixed generation.
const revisionA = "a".repeat(64);
const revisionB = "b".repeat(64);
const revisionFixture = `${BRIEFS_PREFIX}{};\nwindow.ACTIVITY_BRIEFS_REVISION="${revisionA}";\n`;
assert.deepEqual(
  parseResearchBundle(revisionFixture, {
    prefix: BRIEFS_PREFIX,
    revisionGlobal: "ACTIVITY_BRIEFS_REVISION",
    fileName: "activity-briefs.js",
  }),
  { json: "{}", revision: revisionA },
);
assert.throws(
  () => parseResearchBundle(
    `${BRIEFS_PREFIX}{};\nwindow.ACTIVITY_BRIEFS_REVISION="short";\n`,
    {
      prefix: BRIEFS_PREFIX,
      revisionGlobal: "ACTIVITY_BRIEFS_REVISION",
      fileName: "activity-briefs.js",
    },
  ),
  /invalid or executable revision suffix/,
);
assert.throws(
  () => parseResearchBundle(revisionFixture + "globalThis.unexpected=true;\n", {
    prefix: BRIEFS_PREFIX,
    revisionGlobal: "ACTIVITY_BRIEFS_REVISION",
    fileName: "activity-briefs.js",
  }),
  /invalid or executable revision suffix/,
);
assert.deepEqual(researchRevisionErrors(null, null, true), []);
assert.equal(researchRevisionErrors(null, null, false).length, 2);
assert.match(researchRevisionErrors(revisionA, revisionB, true)[0], /revision mismatch/);

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
  creative: "11458125",
  photoshoots: "27151671",
  water: "11452776",
  partycruises: "10753631",
  shows: "276857",
  events: "10407622",
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
  creative: 5,
  photoshoots: 3,
  water: 20,
  partycruises: 9,
  shows: 12,
  events: 4,
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
  const editorialMarker = "const EDITORIAL_IDEAS=";
  const editorialStart = uniqueMarker(inline, editorialMarker, dataEnd + dataEndMarker.length) + editorialMarker.length;
  const editorialEnd = uniqueMarker(inline, ";\nDATA.push(...EDITORIAL_IDEAS);", editorialStart);
  return {
    dataJson: inline.slice(dataStart, dataEnd),
    editorialJson: inline.slice(editorialStart, editorialEnd),
    classifier: inline.slice(classifierStart, classifierEnd),
  };
}

// Regression probe: classifier-looking scraped text must stay inside DATA.
const markerProbe = `<script>\nconst DATA=[{"n":"const GROUPS=[]; // ---------- dropdowns ----------"}];\nconst SB_URL="x";\nconst EDITORIAL_IDEAS=[{"id":"idea:probe"}];\nDATA.push(...EDITORIAL_IDEAS);\nconst GROUPS=[{id:"real"}];\n// ---------- dropdowns ----------\n</script>`;
assert.match(extractPageParts(markerProbe).classifier, /id:"real"/);

const { dataJson, editorialJson, classifier } = extractPageParts(html);
const sandbox = Object.assign(Object.create(null), {
  DATA_JSON: dataJson,
  EDITORIAL_JSON: editorialJson,
  BRIEFS_JSON: briefsJson,
  PRICING_JSON: pricingJson,
  ALLOW_PARTIAL_RESEARCH: allowPartialResearch,
});
const context = vm.createContext(sandbox, {
  codeGeneration: { strings: false, wasm: false },
});
const auditScript = new vm.Script(
  `
    "use strict";
    const SOURCE_DATA=JSON.parse(DATA_JSON);
    const DATA=[...SOURCE_DATA,...JSON.parse(EDITORIAL_JSON)];
    const ACTIVITY_BRIEFS=JSON.parse(BRIEFS_JSON);
    const ACTIVITY_PRICING=JSON.parse(PRICING_JSON);
    const listingId=d=>(d.url||"").match(/-d(\\d+)-/)?.[1]||"";
    const itemRef=d=>d.id||listingId(d);
    const routeIdentity=d=>(d.url||"").match(/\\/(AttractionProductReview|Attraction_Review)-g\\d+-d(\\d+)-/);
    const activityRef=d=>{if(d.id)return d.id;const m=routeIdentity(d);return m?m[1]+":"+m[2]:""};
    const activityKey=d=>{if(d.id)return d.city+"|"+d.id;const m=routeIdentity(d);return m?d.city+"|ta:"+m[1]+":"+m[2]:d.city+"|"+d.n};
    const GROUPS=${classifier}
    ;(() => {
      const groupIds=GROUPS.map(group=>group.id);
      const buckets=Object.fromEntries(groupIds.map(id=>[id,[]]));
      const visible=DATA.filter(d=>d.city==="budapest"&&d.origin!=="foreign-origin"&&!isHiddenListing(d));
      const assignments={};
      visible.forEach(d=>{
        const group=activityGroup(d);
        (buckets[group]||buckets.review).push(d);
        assignments[itemRef(d)]=group;
      });

      const allIds=new Set(DATA.map(listingId).filter(Boolean));
      const itemRefs=DATA.map(itemRef).filter(Boolean);
      const seenItemRefs=new Set(),duplicateItemRefs=[];
      itemRefs.forEach(id=>seenItemRefs.has(id)?duplicateItemRefs.push(id):seenItemRefs.add(id));
      const curatedOwners=new Map(),duplicateCurated=[];
      Object.entries(CURATED_GROUP_IDS).forEach(([group,ids])=>{
        ids.trim().split(/\\s+/).filter(Boolean).forEach(id=>{
          if(curatedOwners.has(id)) duplicateCurated.push({id,first:curatedOwners.get(id),second:group});
          curatedOwners.set(id,group);
        });
      });
      const hiddenEntries=Object.entries(HIDDEN_LISTING_REASONS);
      const allById=new Map(DATA.map(d=>[activityRef(d),d]));
      const requiredBriefFields=["what","do","why","source","sourceLabel","checkedAt"];
      const briefIssues=Object.entries(ACTIVITY_BRIEFS).flatMap(([id,brief])=>{
        const issues=[];
        if(!allById.has(id)) issues.push("does not match a listing or editorial idea");
        if(!brief||typeof brief!=="object"||Array.isArray(brief)) issues.push("must be an object");
        else {
          requiredBriefFields.forEach(field=>{
            if(typeof brief[field]!=="string"||!brief[field].trim()) issues.push(field+" is required");
          });
          if((brief.evidenceHash!==undefined||!ALLOW_PARTIAL_RESEARCH)
            && !/^[0-9a-f]{64}$/.test(brief.evidenceHash||"")) issues.push("evidenceHash must be a lowercase SHA-256 digest");
          for(const field of ["what","do"]){if((brief[field]||"").length>320)issues.push(field+" is too long");}
          if((brief.why||"").length>420) issues.push("why is too long");
          if(!(brief.source||"").startsWith("https://")) issues.push("source must be HTTPS");
          if(!/^\\d{4}-\\d{2}-\\d{2}$/.test(brief.checkedAt||"")) issues.push("checkedAt must be YYYY-MM-DD");
          const hasReviewContext=[brief.reviewSummary,brief.reviewsUsed,brief.reviewSource].some(value=>value!==undefined);
          if(hasReviewContext){
            if(typeof brief.reviewSummary!=="string"||!brief.reviewSummary.trim()) issues.push("reviewSummary is required when review context is present");
            if((brief.reviewSummary||"").length>420) issues.push("reviewSummary is too long");
            if(!Number.isInteger(brief.reviewsUsed)||brief.reviewsUsed<1||brief.reviewsUsed>10) issues.push("reviewsUsed must be an integer from 1 to 10");
            if(typeof brief.reviewSource!=="string"||!brief.reviewSource.startsWith("https://")) issues.push("reviewSource must be HTTPS");
          }
        }
        return issues.length?[{id,issues}]:[];
      });
      const briefForId=id=>allById.has(id)?activityBrief(allById.get(id)):null;
      const moneyPattern=/^(?:0|[1-9]\\d*)(?:\\.\\d+)?$/;
      const allowedStatuses=new Set(["priced","free","date-required","not-published","unavailable"]);
      const allowedKinds=new Set(["exact","from","range","free","date-required"]);
      const allowedAvailability=new Set(["available","date-required","sold-out","unavailable","unknown"]);
      const allowedPriceScopes=new Set(["booking-fee","deposit"]);
      const numericKinds=new Set(["exact","from","range"]);
      const pricingIssues=Object.entries(ACTIVITY_PRICING).flatMap(([id,pricing])=>{
        const issues=[];
        if(!allById.has(id)) issues.push("does not match a listing or editorial idea");
        if(!pricing||typeof pricing!=="object"||Array.isArray(pricing)) issues.push("must be an object");
        else {
          if(!allowedStatuses.has(pricing.status)) issues.push("status is invalid");
          if(!/^\\d{4}-\\d{2}-\\d{2}$/.test(pricing.checkedAt||"")) issues.push("checkedAt must be YYYY-MM-DD");
          if(typeof pricing.source!=="string"||!pricing.source.startsWith("https://")) issues.push("source must be HTTPS");
          if(typeof pricing.sourceLabel!=="string"||!pricing.sourceLabel.trim()) issues.push("sourceLabel is required");
          if((pricing.evidenceHash!==undefined||!ALLOW_PARTIAL_RESEARCH)
            && !/^[0-9a-f]{64}$/.test(pricing.evidenceHash||"")) issues.push("evidenceHash must be a lowercase SHA-256 digest");
          const starting=pricing.startingPrice;
          let hasStartingNumeric=false;
          if(starting!==undefined){
            if(!starting||typeof starting!=="object"||Array.isArray(starting)) issues.push("startingPrice must be an object");
            else {
              if(!numericKinds.has(starting.kind)) issues.push("startingPrice kind must be numeric");
              else hasStartingNumeric=true;
              if(["exact","from"].includes(starting.kind)&&!moneyPattern.test(starting.amount||"")) issues.push("startingPrice amount must be a decimal string");
              if(starting.kind==="range"&&(!moneyPattern.test(starting.minAmount||"")||!moneyPattern.test(starting.maxAmount||""))) issues.push("startingPrice range amounts must be decimal strings");
              if(!/^[A-Z]{3}$/.test(starting.currency||"")) issues.push("startingPrice currency must be an ISO-style code");
              if(starting.unit!==undefined&&(typeof starting.unit!=="string"||!starting.unit.trim())) issues.push("startingPrice unit must be non-empty when supplied");
              if(starting.scope!==undefined&&!allowedPriceScopes.has(starting.scope)) issues.push("startingPrice scope is invalid");
            }
          }
          if(pricing.status!=="priced"&&starting!==undefined) issues.push("only priced status may include startingPrice");
          if(pricing.packageAvailability!==undefined&&!new Set(["unknown","unavailable"]).has(pricing.packageAvailability)) issues.push("packageAvailability must be unknown or unavailable");
          if(!Array.isArray(pricing.packages)) issues.push("packages must be an array");
          else {
            if(pricing.status==="not-published"&&pricing.packages.length) issues.push("not-published status cannot include packages");
            pricing.packages.forEach((pkg,index)=>{
              const prefix="packages["+index+"] ";
              if(!pkg||typeof pkg!=="object"||Array.isArray(pkg)){issues.push(prefix+"must be an object");return;}
              if(typeof pkg.name!=="string"||!pkg.name.trim()) issues.push(prefix+"name is required");
              if(typeof pkg.description!=="string"||!pkg.description.trim()) issues.push(prefix+"description is required");
              if(!allowedAvailability.has(pkg.availability)) issues.push(prefix+"availability is invalid");
              if(pkg.conditions!==undefined&&(!Array.isArray(pkg.conditions)||pkg.conditions.some(value=>typeof value!=="string"||!value.trim()))) issues.push(prefix+"conditions must contain non-empty strings");
              const price=pkg.price;
              if(!price||typeof price!=="object"||Array.isArray(price)){issues.push(prefix+"price is required");return;}
              if(!allowedKinds.has(price.kind)) issues.push(prefix+"price kind is invalid");
              if(["exact","from"].includes(price.kind)&&!moneyPattern.test(price.amount||"")) issues.push(prefix+"amount must be a decimal string");
              if(price.kind==="range"&&(!moneyPattern.test(price.minAmount||"")||!moneyPattern.test(price.maxAmount||""))) issues.push(prefix+"range amounts must be decimal strings");
              if(["exact","from","range"].includes(price.kind)&&!/^[A-Z]{3}$/.test(price.currency||"")) issues.push(prefix+"currency must be an ISO-style code");
              if(price.unit!==undefined&&(typeof price.unit!=="string"||!price.unit.trim())) issues.push(prefix+"unit must be non-empty when supplied");
              if(price.scope!==undefined&&!allowedPriceScopes.has(price.scope)) issues.push(prefix+"scope is invalid");
            });
            const priceKinds=pricing.packages.map(pkg=>pkg&&pkg.price&&pkg.price.kind).filter(Boolean);
            const hasSelectableNumeric=pricing.packages.some(pkg=>pkg&&pkg.price&&numericKinds.has(pkg.price.kind)&&!["sold-out","unavailable"].includes(pkg.availability));
            if(pricing.status==="priced"&&!hasStartingNumeric&&!hasSelectableNumeric) issues.push("priced status requires startingPrice or at least one selectable numeric package");
            if(pricing.status==="free"&&priceKinds.some(kind=>kind!=="free")) issues.push("free status may contain only free packages");
            if(pricing.status==="date-required"&&priceKinds.some(kind=>kind!=="date-required")) issues.push("date-required status may contain only date-required packages");
            if(pricing.status==="unavailable"&&pricing.packages.some(pkg=>pkg&&pkg.availability==="available")) issues.push("unavailable status cannot contain available packages");
          }
          if(["date-required","not-published","unavailable"].includes(pricing.status)&&(typeof pricing.note!=="string"||!pricing.note.trim())) issues.push("non-priced status requires an explanation note");
        }
        return issues.length?[{id,issues}]:[];
      });
      const visibleRefs=visible.map(activityRef).filter(Boolean);

      globalThis.AUDIT_JSON=JSON.stringify({
        groupIds,
        invalidGroupIds:groupIds.filter(id=>!/^[a-z0-9-]+$/.test(id)),
        reservedKeyCollisions:DATA.map(activityKey).filter(key=>key.startsWith("@discover-group:")),
        visible:visible.length,
        visibleRefs,
        rawBlurbRefs:SOURCE_DATA
          .filter(d=>Object.prototype.hasOwnProperty.call(d,"blurb"))
          .map(activityRef),
        visibleItems:visible.map(d=>({
          key:activityRef(d),name:d.n,category:d.cat,subtype:d.sub||"",rating:d.r,
          reviewCount:d.rv||0,url:d.url||"",type:d.type,group:activityGroup(d),
        })),
        assignments,
        counts:Object.fromEntries(groupIds.map(id=>[id,buckets[id].length])),
        duplicateItemRefs,
        duplicateCurated,
        missingCurated:[...curatedOwners.keys()].filter(id=>!allIds.has(id)),
        curatedHidden:[...curatedOwners.keys()].filter(id=>HIDDEN_LISTING_IDS.has(id)),
        missingHidden:hiddenEntries.filter(([id])=>!allIds.has(id)).map(([id])=>id),
        hiddenWithoutReason:hiddenEntries.filter(([,reason])=>!String(reason).trim()).map(([id])=>id),
        briefIssues,
        briefCount:Object.keys(ACTIVITY_BRIEFS).length,
        missingBriefRefs:visibleRefs.filter(id=>!ACTIVITY_BRIEFS[id]),
        unexpectedBriefRefs:Object.keys(ACTIVITY_BRIEFS).filter(id=>!visibleRefs.includes(id)),
        pricingIssues,
        pricingCount:Object.keys(ACTIVITY_PRICING).length,
        missingPricingRefs:visibleRefs.filter(id=>!ACTIVITY_PRICING[id]),
        unexpectedPricingRefs:Object.keys(ACTIVITY_PRICING).filter(id=>!visibleRefs.includes(id)),
        routeBriefRefs:[
          activityRef({url:"https://www.tripadvisor.com/Attraction_Review-g1-d123-Reviews-X.html"}),
          activityRef({url:"https://www.tripadvisor.com/AttractionProductReview-g1-d123-X.html"}),
        ],
        briefSentinels:{
          freddie:briefForId("Attraction_Review:34405806"),
          geocaching:briefForId("idea:geocaching"),
          londonArchive:briefForId("Attraction_Review:1526046"),
        },
        pricingSentinels:{
          geocaching:ACTIVITY_PRICING["idea:geocaching"]||null,
          nightmare:ACTIVITY_PRICING["AttractionProductReview:20971891"]||null,
          londonArchive:ACTIVITY_PRICING["Attraction_Review:1526046"]||null,
        },
        editorialIdeas:DATA.filter(d=>d.type==="idea").map(d=>({id:itemRef(d),name:d.n,group:activityGroup(d),brief:activityBrief(d)})),
        review:buckets.review
          .filter(d=>curatedOwners.get(listingId(d))!=="review")
          .map(d=>({id:itemRef(d),name:d.n})),
        reviewQueue:buckets.review
          .filter(d=>curatedOwners.get(listingId(d))==="review")
          .map(d=>({id:itemRef(d),name:d.n})),
        waterParksInSpas:buckets.spas
          .filter(d=>/water parks?|public baths?|thermal baths?|swimming pools?|\\blidos?\\b/.test(
            ((d.n||"")+" "+(d.sub||"")).toLowerCase(),
          ))
          .map(d=>({id:itemRef(d),name:d.n})),
      });
    })();
  `,
  { filename: "discover-group-audit.vm.js" },
);
auditScript.runInContext(context, { timeout: 2000 });
const audit = JSON.parse(sandbox.AUDIT_JSON);

const errors = options.inventoryOnly ? [] : researchRevisionErrors(
  briefsBundle.revision,
  pricingBundle.revision,
  allowPartialResearch,
);
if (new Set(audit.groupIds).size !== audit.groupIds.length) errors.push("Duplicate GROUPS id");
if (audit.invalidGroupIds.length) errors.push(`Unsafe GROUPS ids: ${audit.invalidGroupIds.join(", ")}`);
if (audit.reservedKeyCollisions.length) errors.push(`Activity keys use the reserved group namespace: ${audit.reservedKeyCollisions.join(", ")}`);
if (audit.visible < MIN_VISIBLE_LISTINGS) {
  errors.push(`Visible inventory collapsed: ${audit.visible} < ${MIN_VISIBLE_LISTINGS}`);
}
if (audit.rawBlurbRefs.length) {
  errors.push(`Raw listing blurbs must not ship: ${audit.rawBlurbRefs.slice(0,12).join(", ")}`);
}
if (audit.duplicateCurated.length) {
  errors.push(`Curated IDs assigned twice: ${JSON.stringify(audit.duplicateCurated)}`);
}
if (audit.duplicateItemRefs.length) {
  errors.push(`Duplicate listing/editorial IDs: ${audit.duplicateItemRefs.join(", ")}`);
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
const geocachingIdea = audit.editorialIdeas.find(item=>item.id==="idea:geocaching");
if (!geocachingIdea || geocachingIdea.group!=="quests") {
  errors.push(`Geocaching editorial idea must stay in quests: ${JSON.stringify(geocachingIdea)}`);
}
if (!options.inventoryOnly) {
  if (audit.briefIssues.length) {
    errors.push(`Invalid grounded activity briefs: ${JSON.stringify(audit.briefIssues)}`);
  }
  if (audit.pricingIssues.length) {
    errors.push(`Invalid activity pricing: ${JSON.stringify(audit.pricingIssues)}`);
  }
  if (!audit.briefCount) errors.push("At least one grounded activity brief is required");
  if (!audit.pricingCount) errors.push("At least one researched price state is required");
  if (audit.routeBriefRefs[0]===audit.routeBriefRefs[1]) {
    errors.push("Venue and product brief identities collided: "+JSON.stringify(audit.routeBriefRefs));
  }
  if (!audit.briefSentinels.freddie?.what?.includes("Freddie Mercury")
    || !audit.briefSentinels.freddie?.do || !audit.briefSentinels.freddie?.why
    || audit.briefSentinels.freddie?.reviewsUsed < 3 || !audit.briefSentinels.freddie?.reviewSummary) {
    errors.push(`FREDDIE must keep a specific what/do/why brief: ${JSON.stringify(audit.briefSentinels.freddie)}`);
  }
  if (audit.briefSentinels.londonArchive !== null) {
    errors.push(`London archive must not receive Hungary briefs: ${JSON.stringify(audit.briefSentinels.londonArchive)}`);
  }
  if (audit.pricingSentinels.londonArchive !== null) {
    errors.push(`London archive must not receive Hungary pricing: ${JSON.stringify(audit.pricingSentinels.londonArchive)}`);
  }
  if (!audit.briefSentinels.geocaching?.what || !audit.briefSentinels.geocaching?.do
    || !audit.briefSentinels.geocaching?.why?.includes("Matija")) {
    errors.push(`Geocaching suggestion brief regressed: ${JSON.stringify(audit.briefSentinels.geocaching)}`);
  }
  if (audit.pricingSentinels.geocaching?.status!=="free") {
    errors.push(`Geocaching must keep its explicit free price state: ${JSON.stringify(audit.pricingSentinels.geocaching)}`);
  }
}
const semanticAssignmentRegressions = Object.freeze({
  "8539546":"shows",
  "23608263":"review",
  "11447085":"tours",
  "13117895":"tours",
  "13117896":"tours",
  "23873098":"shows",
  "33230135":"rides",
  "2026197":"review",
  "27151671":"photoshoots",
  "32789559":"photoshoots",
  "25077094":"photoshoots",
  "27428722":"photoshoots",
  "10407622":"events",
  "26024640":"culture",
});
for (const [id,group] of Object.entries(semanticAssignmentRegressions)) {
  if (audit.assignments[id]!==group) {
    errors.push(`Semantically reviewed listing ${id} expected ${group}, got ${audit.assignments[id]||"missing"}`);
  }
}
if (!allowPartialResearch && !options.inventoryOnly) {
  if (audit.missingBriefRefs.length) {
    errors.push(`Research coverage incomplete: ${audit.missingBriefRefs.length} visible listings lack briefs; first: ${audit.missingBriefRefs.slice(0,12).join(", ")}`);
  }
  if (audit.missingPricingRefs.length) {
    errors.push(`Pricing coverage incomplete: ${audit.missingPricingRefs.length} visible listings lack price states; first: ${audit.missingPricingRefs.slice(0,12).join(", ")}`);
  }
  if (audit.unexpectedBriefRefs.length) {
    errors.push(`Research bundle has ${audit.unexpectedBriefRefs.length} non-visible keys; first: ${audit.unexpectedBriefRefs.slice(0,12).join(", ")}`);
  }
  if (audit.unexpectedPricingRefs.length) {
    errors.push(`Pricing bundle has ${audit.unexpectedPricingRefs.length} non-visible keys; first: ${audit.unexpectedPricingRefs.slice(0,12).join(", ")}`);
  }
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
  if (options.printVisibleJson) {
    console.log(JSON.stringify(audit.visibleItems));
  } else if (options.printVisibleRefs) {
    console.log(audit.visibleRefs.join("\n"));
  } else {
    const populated = Object.values(audit.counts).filter(Boolean).length;
    console.log(`Discover groups valid: ${audit.visible} listings across ${populated} populated groups`);
  }
}
