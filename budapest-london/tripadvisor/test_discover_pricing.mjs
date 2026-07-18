import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source=readFileSync(new URL("./discover-pricing.js",import.meta.url),"utf8");
const sandbox=Object.assign(Object.create(null),{URL});
vm.runInNewContext(source,sandbox,{filename:"discover-pricing.js"});
const api=sandbox.DISCOVER_PRICING;

const packageAt=(name,kind,amount,unit="adult")=>({
  name,
  description:`What the ${name} option includes.`,
  availability:"available",
  price:{kind,amount,currency:"USD",unit},
});

test("formats exact, from, range and free prices without losing source precision",()=>{
  assert.equal(api.formatMoney("034.90","usd"),"USD 34.90");
  assert.equal(api.formatMoney("5000","HUF"),"HUF 5,000");
  assert.equal(api.formatPrice({kind:"exact",amount:"34.97",currency:"USD",unit:"adult"}),"USD 34.97 per adult");
  assert.equal(api.formatPrice({kind:"from",amount:"34.97",currency:"USD",unit:"person"}),"From USD 34.97 per person");
  assert.equal(api.formatPrice({kind:"range",minAmount:"20",maxAmount:"35.50",currency:"EUR",unit:"ticket"}),"EUR 20–35.50 per ticket");
  assert.equal(api.formatPrice({kind:"free"}),"Free");
  assert.equal(api.formatPrice({kind:"exact",amount:"3.51",currency:"USD",unit:"adult",scope:"booking-fee"}),"Booking fee USD 3.51 per adult");
  assert.equal(api.formatPrice({kind:"from",amount:"20",currency:"EUR",scope:"deposit"}),"Deposit from EUR 20");
});

test("summarizes every supported non-priced state explicitly",()=>{
  assert.equal(api.pricingSummary({status:"free",packages:[]}),"Free");
  assert.equal(api.pricingSummary({status:"date-required",packages:[]}),"Select booking details to see prices");
  assert.equal(api.pricingSummary({status:"not-published",packages:[]}),"No public price found");
  assert.equal(api.pricingSummary({status:"unavailable",packages:[]}),"Currently unavailable");
});

test("uses one compact quote for one package and a comparable floor for multiple packages",()=>{
  assert.equal(
    api.pricingSummary({status:"priced",packages:[packageAt("Standard","exact","34.97")]}),
    "USD 34.97 per adult",
  );
  assert.equal(
    api.pricingSummary({status:"priced",packages:[packageAt("Standard","exact","34.97"),packageAt("Basic","exact","24.50")]}),
    "From USD 24.50 per adult · 2 options",
  );
  assert.equal(
    api.pricingSummary({status:"priced",packages:[packageAt("Adult","exact","20","adult"),packageAt("Private group","exact","80","group")]}),
    "Prices vary · 2 options",
  );
  assert.equal(
    api.pricingSummary({
      status:"priced",
      startingPrice:{kind:"from",amount:"57.25",currency:"USD"},
      packages:[
        {name:"Buffet",description:"Named dinner option.",availability:"date-required",price:{kind:"date-required"}},
        {name:"Wine tasting",description:"Named tasting option.",availability:"date-required",price:{kind:"date-required"}},
      ],
    }),
    "Advertised from USD 57.25 · 2 options",
  );
  assert.equal(
    api.pricingSummary({status:"priced",packages:[
      {name:"Free",description:"Free option.",availability:"available",price:{kind:"free"}},
      {name:"Check dates",description:"Price appears after date selection.",availability:"date-required",price:{kind:"date-required"}},
    ]}),
    "Free and date-dependent options · 2 options",
  );
  assert.equal(
    api.pricingSummary({status:"priced",packages:[
      {...packageAt("Sold out bargain","exact","10"),availability:"sold-out"},
      packageAt("Available ticket","exact","35"),
    ]}),
    "From USD 35 per adult · 2 options",
  );
  assert.equal(
    api.pricingSummary({
      status:"priced",
      startingPrice:{kind:"from",amount:"20",currency:"USD"},
      packages:[{name:"General admission",description:"The venue says entry is free.",availability:"available",price:{kind:"free"}}],
    }),
    "Free admission · paid options advertised from USD 20",
  );
});

test("ignores unavailable packages before describing free and mixed options",()=>{
  assert.equal(
    api.pricingSummary({status:"priced",packages:[
      {...packageAt("Sold-out free ticket","free"),availability:"sold-out"},
      packageAt("Available ticket","exact","35"),
    ]}),
    "From USD 35 per adult · 2 options",
  );
  assert.equal(
    api.pricingSummary({status:"priced",packages:[
      {...packageAt("Unavailable paid ticket","exact","20"),availability:"unavailable"},
      {...packageAt("Available free ticket","free"),availability:"available"},
    ]}),
    "Free · 2 options",
  );
});

test("distinguishes an advertised floor from selected option quotes",()=>{
  assert.equal(
    api.pricingSummary({
      status:"priced",
      startingPrice:{kind:"from",amount:"76",currency:"USD"},
      context:{date:"2026-07-18",travellers:"2 travellers"},
      packages:[
        packageAt("Available food tour","exact","79"),
        {name:"Sold-out tour",description:"Another option.",availability:"sold-out",price:{kind:"date-required"}},
      ],
    }),
    "Advertised from USD 76 · 2 options",
  );
  assert.equal(
    api.pricingSummary({status:"priced",startingPrice:{kind:"from",amount:"76",currency:"USD"},packages:[]}),
    "From USD 76",
  );
  assert.equal(
    api.pricingSummary({
      status:"priced",
      startingPrice:{kind:"from",amount:"3.51",currency:"USD",scope:"booking-fee"},
      packages:[{...packageAt("Reservation","exact","3.51"),price:{kind:"exact",amount:"3.51",currency:"USD",unit:"adult",scope:"booking-fee"}}],
    }),
    "Advertised booking fee from USD 3.51",
  );
});

test("shows an advertised floor without claiming a failed package lookup is unavailable",()=>{
  const pricing={
    status:"priced",
    startingPrice:{kind:"from",amount:"19.86",currency:"USD"},
    packageAvailability:"unknown",
    checkedAt:"2026-07-17",
    source:"https://www.tripadvisor.com/example",
    sourceLabel:"Tripadvisor booking page",
    context:{date:"2026-07-18",currencyShown:"USD"},
    note:"Tripadvisor advertised this starting price, but its package lookup did not confirm the choices for the selected date. Treat it as an advertised floor, not a confirmed bookable option.",
    packages:[],
  };

  assert.equal(
    api.pricingSummary(pricing),
    "Advertised from USD 19.86 · package details not confirmed",
  );
  const html=api.renderPricing(pricing,{activityName:"Live GraphQL product"});
  assert.match(html,/Advertised from USD 19\.86 · package details not confirmed/);
  assert.match(html,/not a confirmed bookable option/);
  assert.doesNotMatch(html,/Available when checked/);
  assert.doesNotMatch(html,/price-package-amount/);
});

test("never renders numeric zero as an advertised or exact price",()=>{
  const invalid={
    status:"priced",
    startingPrice:{kind:"from",amount:"0.00",currency:"USD"},
    packageAvailability:"unknown",
    packages:[],
  };

  assert.equal(api.formatPrice({kind:"exact",amount:"0",currency:"USD"}),"Price not available");
  assert.equal(api.formatPrice({kind:"range",minAmount:"0",maxAmount:"10",currency:"USD"}),"Price not available");
  assert.equal(api.pricingSummary(invalid),"Package details not confirmed");
  assert.doesNotMatch(api.renderPricing(invalid),/USD 0/);
});

test("single-package markup stays compact and includes context, source and checked date",()=>{
  const html=api.renderPricing({
    status:"priced",
    startingPrice:{kind:"from",amount:"25",currency:"USD",unit:"adult"},
    checkedAt:"2026-07-16",
    source:"https://www.tripadvisor.com/example?x=1&y=2",
    sourceLabel:"Tripadvisor booking page",
    context:{date:"2026-07-17",travellers:"1 adult",currencyShown:"USD"},
    packages:[packageAt("Standard admission","from","34.97")],
  },{activityName:"Nightmare in Budapest"});

  assert.match(html,/^<section class="activity-price price-single/);
  assert.doesNotMatch(html,/<details/);
  assert.match(html,/From USD 34\.97 per adult/);
  assert.match(html,/Advertised from USD 25 per adult/);
  assert.match(html,/Options checked:/);
  assert.match(html,/<time datetime="2026-07-17">17 Jul 2026<\/time>/);
  assert.match(html,/Checked <time datetime="2026-07-16">16 Jul 2026<\/time>/);
  assert.match(html,/target="_blank" rel="noopener noreferrer"/);
  assert.match(html,/Open checked source: Tripadvisor booking page/);
  assert.match(html,/x=1&amp;y=2/);
});

test("multiple packages render as native details with named, explained options",()=>{
  const html=api.renderPricing({
    status:"priced",
    startingPrice:{kind:"from",amount:"25",currency:"USD",unit:"adult"},
    checkedAt:"2026-07-16",
    source:"https://example.com/prices",
    sourceLabel:"Official price list",
    packages:[
      {...packageAt("Standard","exact","30"),conditions:["Weekdays","90 minutes"]},
      {...packageAt("Premium","exact","45"),availability:"date-required"},
    ],
  },{activityName:"Example activity"});

  assert.match(html,/^<details class="activity-price price-multi/);
  assert.match(html,/<summary aria-label="Price information for Example activity:/);
  assert.match(html,/<span class="price-option-count">2 options<\/span>/);
  assert.match(html,/Advertised from USD 25 per adult/);
  assert.match(html,/<ul class="price-options">/);
  assert.equal((html.match(/class="price-package"/g)||[]).length,2);
  assert.match(html,/What the Standard option includes\./);
  assert.match(html,/Weekdays · 90 minutes/);
  assert.match(html,/Select booking details to confirm availability/);
});

test("renderer escapes untrusted text and omits unsafe source URLs",()=>{
  const html=api.renderPricing({
    status:"not-published",
    checkedAt:"2026-07-16",
    source:"javascript:alert(1)",
    sourceLabel:'bad" onclick="alert(1)',
    note:"No price <script>alert(1)</script> was published.",
    packages:[],
  },{activityName:'Bad <img src=x onerror="alert(1)">'});

  assert.doesNotMatch(html,/<script|<img|javascript:|onclick=/i);
  assert.match(html,/&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html,/aria-label="Price information for Bad &lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;:/);
  assert.doesNotMatch(html,/<a /);
});

test("free and unavailable states render their status in visible text",()=>{
  assert.match(api.renderPricing({status:"free",packages:[]}),/<strong class="price-summary-main">Free<\/strong>/);
  assert.match(api.renderPricing({status:"unavailable",packages:[]}),/<strong class="price-summary-main">Currently unavailable<\/strong>/);
});
