(function installDiscoverPricing(root){
  "use strict";

  const KNOWN_STATUSES=new Set(["priced","free","date-required","not-published","unavailable"]);
  const MONTHS=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const UNIT_LABELS=Object.freeze({
    adult:"per adult",child:"per child",person:"per person",group:"per group",
    session:"per session",entry:"per entry",ticket:"per ticket",vehicle:"per vehicle",
    package:"per package",family:"per family",
  });
  const AVAILABILITY_LABELS=Object.freeze({
    available:"Available when checked",
    "date-required":"Select booking details to confirm availability",
    "sold-out":"Sold out when checked",
    unavailable:"Unavailable when checked",
    unknown:"Availability not confirmed",
  });

  function escapeText(value){
    return String(value??"")
      .replace(/&/g,"&amp;")
      .replace(/</g,"&lt;")
      .replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;")
      .replace(/'/g,"&#39;");
  }

  function safeHttps(value){
    try{
      const url=new URL(String(value||""));
      return url.protocol==="https:"?url.href:"";
    }catch{return "";}
  }

  function decimalParts(value){
    const raw=String(value??"").trim();
    if(!/^\d+(?:\.\d+)?$/.test(raw)) return null;
    const [whole,fraction]=raw.split(".");
    const normalizedWhole=whole.replace(/^0+(?=\d)/,"")||"0";
    return {whole:normalizedWhole.replace(/\B(?=(\d{3})+(?!\d))/g,","),fraction};
  }

  function formatMoney(amount,currency){
    const parts=decimalParts(amount),code=String(currency||"").trim().toUpperCase();
    if(!parts||!/^[A-Z]{3}$/.test(code)) return "";
    return `${code} ${parts.whole}${parts.fraction===undefined?"":`.${parts.fraction}`}`;
  }

  function unitLabel(unit){
    const value=String(unit||"").trim();
    if(!value) return "";
    const normalized=value.toLowerCase();
    return UNIT_LABELS[normalized]||(/^per\s+/i.test(value)?value:`per ${value}`);
  }

  function formatPrice(price){
    if(!price||typeof price!=="object") return "Price not available";
    const kind=String(price.kind||"").toLowerCase(),unit=unitLabel(price.unit),suffix=unit?` ${unit}`:"";
    if(kind==="free") return "Free";
    if(kind==="date-required") return "Select booking details to see price";
    if(kind==="range"){
      const low=formatMoney(price.minAmount,price.currency),high=decimalParts(price.maxAmount);
      if(!low||!high||Number(price.minAmount)<=0||Number(price.maxAmount)<Number(price.minAmount)) return "Price not available";
      const shown=`${low}–${high.whole}${high.fraction===undefined?"":`.${high.fraction}`}${suffix}`;
      if(price.scope==="booking-fee") return `Booking fee ${shown}`;
      if(price.scope==="deposit") return `Deposit ${shown}`;
      return shown;
    }
    if(kind!=="exact"&&kind!=="from") return "Price not available";
    const amount=formatMoney(price.amount,price.currency);
    if(!amount||Number(price.amount)<=0) return "Price not available";
    const shown=`${kind==="from"?"From ":""}${amount}${suffix}`;
    if(price.scope==="booking-fee") return `${kind==="from"?"Booking fee from ":"Booking fee "}${amount}${suffix}`;
    if(price.scope==="deposit") return `${kind==="from"?"Deposit from ":"Deposit "}${amount}${suffix}`;
    return shown;
  }

  function numericFloor(price){
    if(!price||typeof price!=="object") return null;
    if(price.kind==="free") return 0;
    const raw=price.kind==="range"?price.minAmount:price.amount;
    const parts=decimalParts(raw);
    const value=parts?Number(String(raw)):null;
    return value!==null&&value>0?value:null;
  }

  function multiPackageSummary(packages){
    const selectable=packages.filter(option=>!["sold-out","unavailable"].includes(option?.availability));
    const prices=selectable.map(option=>option?.price).filter(Boolean);
    if(!selectable.length) return "No selectable options";
    const hasFree=prices.some(price=>price.kind==="free"),hasNumeric=prices.some(price=>numericFloor(price)!==null&&price.kind!=="free");
    if(hasFree&&hasNumeric) return "Free and paid options";
    if(hasFree&&prices.some(price=>price.kind!=="free")) return "Free and date-dependent options";
    if(prices.length&&prices.every(price=>price.kind==="free")) return "Free";
    const numericOptions=selectable.filter(option=>numericFloor(option?.price)!==null);
    const availableNumeric=numericOptions.filter(option=>option.availability==="available");
    const numeric=(availableNumeric.length?availableNumeric:numericOptions).map(option=>option.price);
    const comparable=numeric.length&&numeric.every(price=>{
      return price.currency===numeric[0].currency&&(price.unit||"")===(numeric[0].unit||"")&&(price.scope||"")===(numeric[0].scope||"");
    });
    if(!comparable) return "Prices vary";
    const cheapest=numeric.reduce((best,price)=>numericFloor(price)<numericFloor(best)?price:best);
    const amount=cheapest.kind==="range"?cheapest.minAmount:cheapest.amount;
    return formatPrice({kind:"from",amount,currency:cheapest.currency,unit:cheapest.unit,scope:cheapest.scope});
  }

  function startingPriceSummary(price,{advertised=false}={}){
    const formatted=formatPrice(price);
    if(!advertised||formatted==="Price not available") return formatted;
    if(price?.scope) return `Advertised ${formatted.charAt(0).toLowerCase()}${formatted.slice(1)}`;
    if(price?.kind==="from") return `Advertised ${formatted.replace(/^From /,"from ")}`;
    if(price?.kind==="range") return `Advertised range ${formatted}`;
    return `Advertised price ${formatted}`;
  }

  function pricingSummary(pricing){
    const value=pricing&&typeof pricing==="object"?pricing:{},status=KNOWN_STATUSES.has(value.status)?value.status:"not-published";
    const packages=Array.isArray(value.packages)?value.packages.filter(option=>option&&typeof option==="object"):[];
    const packageChoicesUnavailable=value.packageAvailability==="unavailable";
    const packageChoicesUnknown=value.packageAvailability==="unknown";
    const hasOptionContext=packageChoicesUnavailable||packageChoicesUnknown||packages.length>0||Boolean(value.context?.date||value.context?.travellers);
    let summary;
    if(status==="free") summary="Free";
    else if(status==="date-required") summary="Select booking details to see prices";
    else if(status==="not-published") summary="No public price found";
    else if(status==="unavailable") summary="Currently unavailable";
    else if(numericFloor(value.startingPrice)!==null&&packages.some(option=>option?.price?.kind==="free"&&option.availability==="available")) summary=`Free admission · paid options advertised ${formatPrice(value.startingPrice).replace(/^From /,"from ")}`;
    else if(numericFloor(value.startingPrice)!==null) summary=startingPriceSummary(value.startingPrice,{advertised:hasOptionContext});
    else if(packages.length===1) summary=formatPrice(packages[0].price);
    else if(packages.length>1) summary=multiPackageSummary(packages);
    else summary="Price not available";
    if(status==="priced"&&packageChoicesUnavailable){
      summary=summary==="Price not available"
        ?"Package choices unavailable"
        :`${summary} · package choices unavailable`;
    }
    if(status==="priced"&&packageChoicesUnknown){
      summary=summary==="Price not available"
        ?"Package details not confirmed"
        :`${summary} · package details not confirmed`;
    }
    if(packages.length>1) summary+=` · ${packages.length} options`;
    return summary;
  }

  function humanDate(value){
    const match=String(value||"").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if(!match) return "";
    const month=Number(match[2]),day=Number(match[3]);
    if(month<1||month>12||day<1||day>31) return "";
    return `${day} ${MONTHS[month-1]} ${match[1]}`;
  }

  function contextHTML(context){
    if(!context||typeof context!=="object") return "";
    const parts=[];
    const date=humanDate(context.date);
    if(date) parts.push(`for <time datetime="${escapeText(context.date)}">${escapeText(date)}</time>`);
    if(context.travellers) parts.push(escapeText(context.travellers));
    if(context.currencyShown) parts.push(`shown in ${escapeText(String(context.currencyShown).toUpperCase())}`);
    return parts.length?`<div class="price-context"><span class="price-context-label">Options checked:</span> ${parts.join(" · ")}</div>`:"";
  }

  function sourceHTML(value,className="price-source"){
    const checked=humanDate(value?.checkedAt),source=safeHttps(value?.source),label=value?.sourceLabel||"current source";
    if(!checked&&!source) return "";
    const parts=[];
    if(checked) parts.push(`Checked <time datetime="${escapeText(value.checkedAt)}">${escapeText(checked)}</time>`);
    if(source) parts.push(`<a href="${escapeText(source)}" target="_blank" rel="noopener noreferrer">Open checked source: ${escapeText(label)}</a>`);
    return `<div class="${className}">${parts.join(" · ")}</div>`;
  }

  function packageMetaHTML(option){
    const parts=[];
    const availability=AVAILABILITY_LABELS[option?.availability];
    if(availability) parts.push(escapeText(availability));
    const conditions=Array.isArray(option?.conditions)?option.conditions.filter(Boolean).join(" · "):option?.conditions;
    if(conditions) parts.push(escapeText(conditions));
    return parts.length?`<div class="price-package-meta">${parts.join(" · ")}</div>`:"";
  }

  function packageSourceHTML(option){
    const source=safeHttps(option?.source);
    if(!source) return "";
    return `<div class="price-package-source"><a href="${escapeText(source)}" target="_blank" rel="noopener noreferrer">${escapeText(option.sourceLabel||"Package source")}</a></div>`;
  }

  function packageHTML(option){
    return `<li class="price-package">
      <div class="price-package-head"><span class="price-package-name">${escapeText(option.name||"Price option")}</span><strong class="price-package-amount">${escapeText(formatPrice(option.price))}</strong></div>
      ${option.description?`<div class="price-package-description">${escapeText(option.description)}</div>`:""}
      ${packageMetaHTML(option)}${packageSourceHTML(option)}
    </li>`;
  }

  function noteHTML(value){
    return value?.note?`<div class="price-note">${escapeText(value.note)}</div>`:"";
  }

  function renderPricing(pricing,{activityName="this activity"}={}){
    if(!pricing||typeof pricing!=="object") return "";
    const status=KNOWN_STATUSES.has(pricing.status)?pricing.status:"not-published";
    const packages=Array.isArray(pricing.packages)?pricing.packages.filter(option=>option&&typeof option==="object"):[];
    const summary=pricingSummary({...pricing,status,packages});
    const label=`Price information for ${activityName}: ${summary}`;
    const common=`${contextHTML(pricing.context)}${noteHTML(pricing)}${sourceHTML(pricing)}`;
    if(packages.length>1){
      return `<details class="activity-price price-multi price-status-${status}">
        <summary aria-label="${escapeText(label)}"><span class="price-icon" aria-hidden="true">💳</span><strong class="price-summary-main">${escapeText(summary.replace(/ · \d+ options$/,""))}</strong><span class="price-option-count">${packages.length} options</span></summary>
        <div class="price-panel"><ul class="price-options">${packages.map(packageHTML).join("")}</ul>${common}</div>
      </details>`;
    }
    const option=packages[0];
    return `<section class="activity-price price-single price-status-${status}" aria-label="${escapeText(label)}">
      <div class="price-summary"><span class="price-icon" aria-hidden="true">💳</span><strong class="price-summary-main">${escapeText(summary)}</strong></div>
      ${option?`<div class="price-single-package"><div class="price-package-head"><span class="price-package-name">${escapeText(option.name||"Price option")}</span><strong class="price-package-amount">${escapeText(formatPrice(option.price))}</strong></div>${option.description?`<div class="price-package-description">${escapeText(option.description)}</div>`:""}${packageMetaHTML(option)}${packageSourceHTML(option)}</div>`:""}
      ${common}
    </section>`;
  }

  root.DISCOVER_PRICING=Object.freeze({formatMoney,formatPrice,pricingSummary,renderPricing,safeHttps});
})(globalThis);
