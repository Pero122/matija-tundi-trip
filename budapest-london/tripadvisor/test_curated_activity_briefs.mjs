import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";

const source=readFileSync(new URL("./activity-briefs.js",import.meta.url),"utf8");
const pricingSource=readFileSync(new URL("./activity-pricing.js",import.meta.url),"utf8");
const briefsPrefix="// Grounded activity briefs, keyed by route-qualified TripAdvisor ID or editorial idea ID.\n"
  +"// Raw source descriptions and review text stay local; only concise synthesis ships.\n"
  +"window.ACTIVITY_BRIEFS=";
const pricingPrefix="// Researched price states, keyed by route-qualified Tripadvisor ID or editorial idea ID.\n"
  +"// Numeric prices are copied from the source evidence; generated text only explains packages.\n"
  +"window.ACTIVITY_PRICING=";

function parseBundle(value,{prefix,revisionGlobal}){
  assert.ok(value.startsWith(prefix),`${revisionGlobal} bundle must keep its approved prefix`);
  const marker=`;\nwindow.${revisionGlobal}=`;
  const markerAt=value.lastIndexOf(marker);
  let json,revision=null;
  if(markerAt>=0){
    json=value.slice(prefix.length,markerAt);
    const tail=value.slice(markerAt+2);
    const match=tail.match(new RegExp(`^window\\.${revisionGlobal}=\"([0-9a-f]{64})\";\\n$`));
    assert.ok(match,`${revisionGlobal} must be the only executable bundle suffix`);
    revision=match[1];
  }else{
    assert.ok(value.endsWith(";\n"),`${revisionGlobal} bundle must end after its JSON assignment`);
    json=value.slice(prefix.length,-2);
  }
  return {value:JSON.parse(json),revision};
}

function assertRevisionPair(briefsRevision,pricingRevision,{allowPartial=false}={}){
  if(!allowPartial){
    assert.match(briefsRevision||"",/^[0-9a-f]{64}$/,"brief revision is required");
    assert.match(pricingRevision||"",/^[0-9a-f]{64}$/,"pricing revision is required");
  }
  if(briefsRevision&&pricingRevision){
    assert.equal(briefsRevision,pricingRevision,"research bundle revision mismatch");
  }
}

const briefsBundle=parseBundle(source,{
  prefix:briefsPrefix,
  revisionGlobal:"ACTIVITY_BRIEFS_REVISION",
});
const pricingBundle=parseBundle(pricingSource,{
  prefix:pricingPrefix,
  revisionGlobal:"ACTIVITY_PRICING_REVISION",
});
const briefs=briefsBundle.value;

test("bundle revisions reject mixed generations and executable suffixes",()=>{
  assertRevisionPair(briefsBundle.revision,pricingBundle.revision,{allowPartial:true});
  assert.doesNotThrow(()=>assertRevisionPair(null,null,{allowPartial:true}));
  assert.throws(()=>assertRevisionPair(null,null),/brief revision is required/);
  assert.throws(
    ()=>assertRevisionPair("a".repeat(64),"b".repeat(64),{allowPartial:true}),
    /revision mismatch/,
  );
  const valid=`${briefsPrefix}{};\nwindow.ACTIVITY_BRIEFS_REVISION="${"a".repeat(64)}";\n`;
  assert.throws(
    ()=>parseBundle(valid+"globalThis.unexpected=true;\n",{
      prefix:briefsPrefix,
      revisionGlobal:"ACTIVITY_BRIEFS_REVISION",
    }),
    /only executable bundle suffix/,
  );
});

test("Nightmare uses the current all-language sample without universal claims",()=>{
  const brief=briefs["AttractionProductReview:20971891"];
  assert.equal(brief.checkedAt,"2026-07-16");
  assert.equal(brief.reviewsUsed,10);
  assert.match(brief.sourceLabel,/all-language sampled reviews/i);
  assert.match(brief.reviewSummary,/(?:ten|10) reviews/i);
  assert.doesNotMatch(
    JSON.stringify(brief),
    /all 10 visible reviews praise|lighting and sound/i,
  );
});

test("Pix uses current Hungarian and English reviews, not stale use cases",()=>{
  const brief=briefs["Attraction_Review:27151671"];
  assert.equal(brief.checkedAt,"2026-07-16");
  assert.equal(brief.reviewsUsed,10);
  assert.match(brief.sourceLabel,/all-language sampled reviews/i);
  assert.match(brief.reviewSummary,/Hungarian-language accounts/i);
  assert.doesNotMatch(
    JSON.stringify(brief),
    /birthdays?|business portraits?|friend activity/i,
  );
});

test("Kaa uses the refreshed all-language sample",()=>{
  const brief=briefs["Attraction_Review:26822818"];
  assert.equal(brief.checkedAt,"2026-07-16");
  assert.equal(brief.reviewsUsed,10);
  assert.match(brief.sourceLabel,/all-language sampled reviews/i);
  assert.match(brief.reviewSummary,/all (?:ten|10) supplied reviews/i);
  assert.match(brief.why,/(?:unmarked|discreet) entrance/i);
});

test("stable curated reviews identify their current all-language samples",()=>{
  for(const key of [
    "Attraction_Review:24132782",
    "Attraction_Review:6936772",
  ]){
    const brief=briefs[key];
    assert.equal(brief.checkedAt,"2026-07-16");
    assert.equal(brief.reviewsUsed,10);
    assert.match(brief.sourceLabel,/all-language sampled reviews/i);
  }
});

test("geocaching cites the official English site without inventing a cost",()=>{
  const brief=briefs["idea:geocaching"];
  assert.equal(brief.source,"https://geocaching.hu/?lang=en");
  assert.match(brief.sourceLabel,/official English site/i);
  assert.equal(brief.checkedAt,"2026-07-16");
  assert.doesNotMatch(brief.why,/\bcheap\b/i);
});
