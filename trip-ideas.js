(function exposeTripIdeas(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TRIP_IDEAS = api;
  if (root && root.document) {
    const start = () => api.initialize(root);
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start, { once: true });
    else start();
  }
}(typeof window !== "undefined" ? window : globalThis, function buildTripIdeas() {
  "use strict";

  const BUDAPEST = Object.freeze({ id: "budapest", name: "Budapest", lat: 47.497879, lng: 19.040238 });
  const REQUIRED_ID = /^[a-z0-9-]+$/;
  const REQUIRED_COLOR = /^#[0-9a-f]{6}$/i;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

  function wrapPhotoIndex(index, count) {
    if (!Number.isInteger(index) || !Number.isInteger(count) || count < 1) throw new Error("Photo index and count must be valid integers.");
    return ((index % count) + count) % count;
  }

  function buildStopIndex(locationData) {
    if (!locationData?.loops) throw new Error("Trip location data is missing.");
    const stops = new Map();
    for (const [loop, entries] of Object.entries(locationData.loops)) {
      if (!Array.isArray(entries)) throw new Error(`Loop ${loop} is not a stop list.`);
      for (const stop of entries) {
        if (!REQUIRED_ID.test(stop?.id || "")) throw new Error("A trip stop has an invalid ID.");
        if (stops.has(stop.id)) throw new Error(`Duplicate trip stop: ${stop.id}`);
        if (!Number.isFinite(stop.geo?.lat) || !Number.isFinite(stop.geo?.lng)) throw new Error(`Stop has no valid coordinates: ${stop.id}`);
        stops.set(stop.id, Object.freeze({ ...stop, loop }));
      }
    }
    return stops;
  }

  function routeStopIds(idea) {
    const ids = [];
    const seen = new Set();
    for (const day of idea?.days || []) {
      for (const visit of day.stops || []) {
        if (!seen.has(visit.id)) {
          ids.push(visit.id);
          seen.add(visit.id);
        }
      }
    }
    return ids;
  }

  function routeTopology(idea) {
    const visits = (idea?.days || []).flatMap((day) => day.stops || []);
    const primaryIds = [];
    const seenPrimary = new Set();
    const groupIndexes = new Map();
    visits.forEach((visit, index) => {
      if (visit.choiceGroup) {
        if (!groupIndexes.has(visit.choiceGroup)) groupIndexes.set(visit.choiceGroup, []);
        groupIndexes.get(visit.choiceGroup).push(index);
      }
    });
    const firstInGroup = new Map([...groupIndexes].map(([group, indexes]) => [group, indexes[0]]));
    visits.forEach((visit, index) => {
      const isAlternative = visit.choiceGroup && firstInGroup.get(visit.choiceGroup) !== index;
      if (!isAlternative && !seenPrimary.has(visit.id)) {
        primaryIds.push(visit.id);
        seenPrimary.add(visit.id);
      }
    });
    const branches = [];
    for (const indexes of groupIndexes.values()) {
      if (indexes.length < 2) continue;
      const start = indexes[0];
      const end = indexes.at(-1);
      const alternatives = indexes.slice(1).map((index) => visits[index].id);
      let previous = "";
      let next = "";
      for (let index = start - 1; index >= 0; index -= 1) {
        if (primaryIds.includes(visits[index].id)) { previous = visits[index].id; break; }
      }
      for (let index = end + 1; index < visits.length; index += 1) {
        if (primaryIds.includes(visits[index].id) && !alternatives.includes(visits[index].id)) { next = visits[index].id; break; }
      }
      const ids = [previous, ...alternatives, next].filter(Boolean).filter((id, index, all) => index === 0 || id !== all[index - 1]);
      if (ids.length >= 2) branches.push(ids);
    }
    return { primaryIds, branches };
  }

  function validateIdeas(ideasData, locationData, photoManifest) {
    if (ideasData?.schemaVersion !== 1 || !Array.isArray(ideasData.ideas) || ideasData.ideas.length < 4) throw new Error("At least four supported trip ideas are required.");
    const stopById = buildStopIndex(locationData);
    const ideaIds = new Set();
    const ranks = new Set();
    const routeSignatures = new Set();
    for (const idea of ideasData.ideas) {
      if (!REQUIRED_ID.test(idea.id || "") || ideaIds.has(idea.id)) throw new Error(`Invalid or duplicate trip idea: ${idea.id || "unknown"}`);
      ideaIds.add(idea.id);
      if (!Number.isInteger(idea.rank) || ranks.has(idea.rank)) throw new Error(`${idea.id} has an invalid or duplicate rank.`);
      ranks.add(idea.rank);
      if (!REQUIRED_COLOR.test(idea.color || "")) throw new Error(`${idea.id} has an invalid map color.`);
      if (!Number.isFinite(idea.score) || idea.score < 0 || idea.score > 100) throw new Error(`${idea.id} has an invalid editorial score.`);
      for (const field of ["confidence", "memorable", "coupleFit", "feasibility"]) {
        if (!Number.isFinite(idea.scores?.[field]) || idea.scores[field] < 0 || idea.scores[field] > 100) throw new Error(`${idea.id} has an invalid score dimension: ${field}.`);
      }
      for (const field of ["title", "shortTitle", "subtitle", "verdict", "bestFor", "compromise", "whyMatija", "whyTundi", "tradeoff", "bookNow", "skip"]) {
        if (typeof idea[field] !== "string" || idea[field].trim().length < 8) throw new Error(`${idea.id} is missing ${field}.`);
      }
      for (const field of ["days", "drive", "energy", "bookingRisk"]) {
        if (typeof idea.metrics?.[field] !== "string" || !idea.metrics[field].trim()) throw new Error(`${idea.id} is missing the ${field} metric.`);
      }
      if (!Array.isArray(idea.days) || idea.days.length < 2) throw new Error(`${idea.id} needs a day-by-day plan.`);
      idea.days.forEach((day, index) => {
        if (day.day !== index + 1 || !Array.isArray(day.stops) || day.stops.length < 1) throw new Error(`${idea.id} has an invalid day ${index + 1}.`);
        for (const visit of day.stops) {
          if (!stopById.has(visit.id)) throw new Error(`${idea.id} references unknown stop: ${visit.id}`);
          for (const field of ["timing", "role", "note"]) if (typeof visit[field] !== "string" || visit[field].trim().length < 3) throw new Error(`${idea.id}/${visit.id} is missing ${field}.`);
        }
      });
      const stopIds = routeStopIds(idea);
      if (stopIds.length < 3) throw new Error(`${idea.id} needs at least three distinct stops.`);
      const signature = [...stopIds].sort().join("|");
      if (routeSignatures.has(signature)) throw new Error(`${idea.id} duplicates another route.`);
      routeSignatures.add(signature);
      for (const stopId of stopIds) {
        const photos = photoManifest?.stops?.[stopId];
        if (!Array.isArray(photos) || photos.length !== 5) throw new Error(`${idea.id}/${stopId} must have exactly five verified photos.`);
        for (const [index, photo] of photos.entries()) {
          if (!photo.src?.startsWith("images/trip-map/") || !photo.alt || !photo.subject || !/^https:\/\//.test(photo.sourceUrl || "")) throw new Error(`${stopId} photo ${index + 1} is incomplete.`);
        }
      }
    }
    const expectedRanks = Array.from({ length: ideasData.ideas.length }, (_, index) => index + 1);
    if (expectedRanks.some((rank) => !ranks.has(rank))) throw new Error("Trip idea ranks must be contiguous.");
    const ranked = [...ideasData.ideas].sort((a, b) => a.rank - b.rank);
    for (let index = 1; index < ranked.length; index += 1) {
      if (ranked[index - 1].score <= ranked[index].score) throw new Error("Editorial scores must descend with the published rank.");
    }
    if (!ideaIds.has(ideasData.defaultIdeaId)) throw new Error("The default trip idea does not exist.");
    return true;
  }

  function formatHash(ideaId, stopId = "") {
    if (!REQUIRED_ID.test(ideaId || "")) throw new Error("Cannot format an invalid trip idea ID.");
    if (stopId && !REQUIRED_ID.test(stopId)) throw new Error("Cannot format an invalid stop ID.");
    return stopId ? `#idea/${ideaId}/stop/${stopId}` : `#idea/${ideaId}`;
  }

  function parseHash(hash, ideasData) {
    const match = /^#idea\/([a-z0-9-]+)(?:\/stop\/([a-z0-9-]+))?$/.exec(hash || "");
    const ideaById = new Map((ideasData?.ideas || []).map((idea) => [idea.id, idea]));
    const fallback = ideaById.has(ideasData?.defaultIdeaId) ? ideasData.defaultIdeaId : ideasData?.ideas?.[0]?.id || "";
    if (!match || !ideaById.has(match[1])) return { ideaId: fallback, stopId: "" };
    const stopId = match[2] || "";
    return { ideaId: match[1], stopId: routeStopIds(ideaById.get(match[1])).includes(stopId) ? stopId : "" };
  }

  function haversineKm(from, to) {
    const radians = (degrees) => degrees * Math.PI / 180;
    const earthKm = 6371;
    const dLat = radians(to.lat - from.lat);
    const dLng = radians(to.lng - from.lng);
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(radians(from.lat)) * Math.cos(radians(to.lat)) * Math.sin(dLng / 2) ** 2;
    return earthKm * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function googleDirectionsUrl(stop) {
    const params = new URLSearchParams({ api: "1", origin: `${BUDAPEST.lat},${BUDAPEST.lng}`, destination: `${stop.geo.lat},${stop.geo.lng}`, travelmode: "driving" });
    return `https://www.google.com/maps/dir/?${params}`;
  }

  function osmPinUrl(stop) {
    return `https://www.openstreetmap.org/?mlat=${encodeURIComponent(stop.geo.lat)}&mlon=${encodeURIComponent(stop.geo.lng)}#map=15/${encodeURIComponent(stop.geo.lat)}/${encodeURIComponent(stop.geo.lng)}`;
  }

  function renderPhotoGallery(stop, photos) {
    if (!Array.isArray(photos) || photos.length !== 5) return `<p class="gallery-error">Five verified photos are not available for ${escapeHtml(stop.name)}.</p>`;
    const cards = photos.map((photo, index) => `<figure class="photo-card${index === 0 ? " hero-photo" : ""}">
      <button type="button" class="photo-open" data-photo-stop="${escapeHtml(stop.id)}" data-photo-index="${index}" aria-haspopup="dialog" aria-controls="photoLightbox" aria-label="Open photo ${index + 1} of 5: ${escapeHtml(photo.subject)}">
        <img src="${escapeHtml(photo.src)}" alt="${escapeHtml(photo.alt)}" width="960" height="720" loading="lazy" decoding="async">
      </button>
      <figcaption><span>${escapeHtml(photo.subject)}</span><small>${index + 1}/5</small></figcaption>
    </figure>`).join("");
    const credits = photos.map((photo, index) => `<li><b>${index + 1}. ${escapeHtml(photo.subject)}</b><span>${escapeHtml(photo.credit)} · <a href="${escapeHtml(photo.sourceUrl)}" target="_blank" rel="noopener noreferrer">source</a></span></li>`).join("");
    return `<section class="stop-gallery" aria-label="Five verified photos of ${escapeHtml(stop.name)}">
      <div class="gallery-heading"><div><span>Five relevant views</span><h3>See the actual payoff</h3></div><p>If the guide promises an activity or distinctive view, the gallery shows it.</p></div>
      <div class="photo-grid">${cards}</div>
      <details class="photo-credits"><summary>Photo sources &amp; credits</summary><ol>${credits}</ol></details>
    </section>`;
  }

  function initialize(root) {
    const document = root.document;
    const ideasData = root.TRIP_IDEAS_DATA;
    const locationData = root.TRIP_LOCATION_DATA;
    const photoManifest = root.TRIP_MAP_PHOTOS;
    const errorBox = document.querySelector("#tripIdeasError");
    try {
      validateIdeas(ideasData, locationData, photoManifest);
    } catch (error) {
      if (errorBox) {
        errorBox.hidden = false;
        errorBox.textContent = `Trip ideas could not load safely: ${error.message}`;
      }
      return;
    }

    const stopById = buildStopIndex(locationData);
    const ideas = [...ideasData.ideas].sort((a, b) => a.rank - b.rank);
    const ideaById = new Map(ideas.map((idea) => [idea.id, idea]));
    const dom = {
      comparison: document.querySelector("#ideaComparison"),
      activeSummary: document.querySelector("#activeIdeaSummary"),
      timeline: document.querySelector("#ideaTimeline"),
      routeMap: document.querySelector("#ideasMap"),
      mapLegend: document.querySelector("#ideasMapLegend"),
      mapStatus: document.querySelector("#ideasStatus"),
      fitAll: document.querySelector("#fitAllIdeas"),
      fitSelected: document.querySelector("#fitSelectedIdea"),
      detail: document.querySelector("#ideaStopDetail"),
      lightbox: document.querySelector("#photoLightbox"),
      lightboxImage: document.querySelector("#lightboxImage"),
      lightboxTitle: document.querySelector("#lightboxTitle"),
      lightboxDescription: document.querySelector("#lightboxDescription"),
      lightboxStop: document.querySelector("#lightboxStop"),
      lightboxCount: document.querySelector("#lightboxCount"),
      lightboxCredit: document.querySelector("#lightboxCredit"),
      lightboxStatus: document.querySelector("#lightboxStatus"),
      lightboxStrip: document.querySelector("#lightboxStrip"),
      lightboxMedia: document.querySelector("#lightboxMedia"),
      lightboxError: document.querySelector("#lightboxError"),
      lightboxClose: document.querySelector("#lightboxClose"),
    };
    const state = {
      ideaId: "",
      stopId: "",
      map: null,
      routeLines: new Map(),
      stopLayer: null,
      stopMarkers: new Map(),
      lightboxStopId: "",
      lightboxIndex: 0,
      lightboxOpener: null,
      lightboxRequest: 0,
      lightboxCloseTimer: 0,
      lightboxTouchStart: null,
    };

    const activeIdea = () => ideaById.get(state.ideaId);
    const currentPhotos = () => photoManifest.stops[state.lightboxStopId] || [];

    function renderComparison() {
      dom.comparison.innerHTML = ideas.map((idea) => `<button class="idea-card" type="button" data-idea-id="${escapeHtml(idea.id)}" aria-pressed="false" style="--idea:${escapeHtml(idea.color)}">
        <span class="idea-rank">#${idea.rank}</span>
        <span class="idea-emoji" aria-hidden="true">${escapeHtml(idea.emoji)}</span>
        <span class="idea-card-copy"><strong>${escapeHtml(idea.shortTitle)}</strong><small>${escapeHtml(idea.verdict)}</small></span>
        <span class="idea-score"><b>${idea.score}</b><small>fit</small></span>
        <span class="idea-mini-metrics"><span>${escapeHtml(idea.metrics.days)}</span><span>${escapeHtml(idea.metrics.drive)}</span><span>${escapeHtml(idea.metrics.energy)}</span></span>
      </button>`).join("");
    }

    function metric(label, value, icon) {
      return `<div class="metric"><span aria-hidden="true">${icon}</span><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`;
    }

    function renderActiveSummary(idea) {
      const dimensions = [
        ["Evidence", idea.scores.confidence],
        ["Memory", idea.scores.memorable],
        ["Couple fit", idea.scores.coupleFit],
        ["Doable", idea.scores.feasibility],
      ];
      dom.activeSummary.style.setProperty("--idea", idea.color);
      dom.activeSummary.innerHTML = `<div class="active-title">
          <div><p class="eyebrow">#${idea.rank} · ${escapeHtml(idea.verdict)}</p><h2>${escapeHtml(idea.emoji)} ${escapeHtml(idea.title)}</h2><p>${escapeHtml(idea.subtitle)}</p></div>
          <div class="hero-score"><b>${idea.score}</b><span>/100</span><small>editorial fit</small></div>
        </div>
        <div class="metrics">${metric("Length", idea.metrics.days, "🗓️")}${metric("Road time", idea.metrics.drive, "🚗")}${metric("Energy", idea.metrics.energy, "⚡")}${metric("Booking risk", idea.metrics.bookingRisk, "🎟️")}</div>
        <div class="idea-why-grid">
          <section><span>🧑 Why Matija</span><p>${escapeHtml(idea.whyMatija)}</p></section>
          <section><span>👩 Why Tündi</span><p>${escapeHtml(idea.whyTundi)}</p></section>
          <section class="tradeoff"><span>⚖️ Honest trade-off</span><p>${escapeHtml(idea.tradeoff)}</p></section>
        </div>
        <div class="decision-strip"><p><b>Book now:</b> ${escapeHtml(idea.bookNow)}</p><p><b>Skip:</b> ${escapeHtml(idea.skip)}</p></div>
        <details class="score-method"><summary>Why this rank—not just stars</summary><p>${escapeHtml(ideasData.rankingMethod)}</p>${dimensions.map(([label, value]) => `<div><span>${label}</span><meter min="0" max="100" value="${value}">${value}/100</meter><b>${value}</b></div>`).join("")}</details>`;
    }

    function renderTimeline(idea) {
      dom.timeline.innerHTML = idea.days.map((day) => {
        const visits = day.stops.map((visit, index) => {
          const stop = stopById.get(visit.id);
          const previous = day.stops[index - 1];
          const isAlternative = Boolean(visit.choiceGroup && previous?.choiceGroup === visit.choiceGroup);
          return `${isAlternative ? '<div class="or-divider"><span>OR</span></div>' : ""}<button class="timeline-stop" type="button" data-stop-id="${escapeHtml(stop.id)}" data-idea-stop="${escapeHtml(idea.id)}" aria-pressed="false">
            <span class="stop-icon">${escapeHtml(stop.icon)}</span>
            <span class="stop-copy"><small>${escapeHtml(visit.role)} · ${escapeHtml(visit.timing)}</small><strong>${escapeHtml(stop.name)}</strong><span>${escapeHtml(visit.note)}</span></span>
            <span class="stop-rating">★ ${escapeHtml(stop.rating.value)}<small>${escapeHtml(stop.rating.reviews)} reviews</small></span>
          </button>`;
        }).join("");
        return `<article class="day-card"><header><span class="day-number">${day.day}</span><div><small>${escapeHtml(day.label)}</small><h3>${escapeHtml(day.headline)}</h3></div></header>
          <div class="day-facts"><span>🛏️ ${escapeHtml(day.base)}</span><span>🚗 ${escapeHtml(day.drive)}</span><span>⚡ ${escapeHtml(day.energy)}</span></div>
          <p class="day-note">${escapeHtml(day.note)}</p><div class="day-stops">${visits}</div></article>`;
      }).join("");
    }

    function renderStopDetail(stop) {
      const idea = activeIdea();
      const packages = stop.price.packages.map((option) => `<li><div><b>${escapeHtml(option.label)}</b><strong>${escapeHtml(option.price)}</strong></div>${option.note ? `<small>${escapeHtml(option.note)}</small>` : ""}</li>`).join("");
      const sources = stop.sources.map((source) => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.label)} ↗</a>`).join("");
      const km = Math.round(haversineKm(BUDAPEST, stop.geo));
      dom.detail.style.setProperty("--idea", idea.color);
      dom.detail.innerHTML = `<header class="detail-header"><div><p class="eyebrow">Full stop guide · ${escapeHtml(idea.shortTitle)}</p><h2>${escapeHtml(stop.icon)} ${escapeHtml(stop.name)}</h2><p>${escapeHtml(stop.area)}</p></div><div class="detail-rating"><b>★ ${escapeHtml(stop.rating.value)}</b><span>${escapeHtml(stop.rating.platform)}</span><small>${escapeHtml(stop.rating.reviews)} reviews</small></div></header>
        <p class="stop-hook">${escapeHtml(stop.hook)}</p>
        ${renderPhotoGallery(stop, photoManifest.stops[stop.id])}
        <div class="guide-grid">
          <section><h3>What even is this?</h3><p>${escapeHtml(stop.what)}</p></section>
          <section><h3>Why Matija might like it</h3><p>${escapeHtml(stop.matija)}</p></section>
          <section><h3>Why Tündi might like it</h3><p>${escapeHtml(stop.tundi)}</p></section>
        </div>
        <div class="stop-facts"><span><small>Time</small><b>${escapeHtml(stop.duration)}</b></span><span><small>Trip fit</small><b>${escapeHtml(stop.fit.label)}</b></span><span><small>From Budapest</small><b>≈ ${km} km straight-line</b></span></div>
        <details class="price-options"${stop.price.packages.length === 1 ? " open" : ""}><summary><span>💳 Price options</span><b>${escapeHtml(stop.price.summary)} · ${stop.price.packages.length} ${stop.price.packages.length === 1 ? "option" : "options"} ▾</b></summary><ul>${packages}</ul></details>
        <p class="stop-caveat"><b>Before you commit:</b> ${escapeHtml(stop.caveat)}</p>
        <div class="detail-links"><a href="${escapeHtml(googleDirectionsUrl(stop))}" target="_blank" rel="noopener noreferrer">Driving directions ↗</a><a href="${escapeHtml(osmPinUrl(stop))}" target="_blank" rel="noopener noreferrer">Exact map pin ↗</a>${sources}</div>`;
    }

    function routeCoordinates(idea) {
      return [BUDAPEST, ...routeTopology(idea).primaryIds.map((id) => stopById.get(id).geo), BUDAPEST].map((point) => [point.lat, point.lng]);
    }

    function branchCoordinates(idea) {
      return routeTopology(idea).branches.map((branch) => branch.map((id) => {
        const point = stopById.get(id).geo;
        return [point.lat, point.lng];
      }));
    }

    function makePin(label, color, budapest = false) {
      return root.L.divIcon({ className: `idea-map-pin${budapest ? " budapest-pin" : ""}`, html: `<span style="--pin:${escapeHtml(color)}">${escapeHtml(label)}</span>`, iconSize: budapest ? [40, 40] : [32, 32], iconAnchor: budapest ? [20, 20] : [16, 16] });
    }

    function addTiles(map) {
      root.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>' }).addTo(map);
    }

    function allBounds() {
      return root.L.latLngBounds([BUDAPEST, ...ideas.flatMap((idea) => routeStopIds(idea).map((id) => stopById.get(id).geo))].map((point) => [point.lat, point.lng]));
    }

    function selectedBounds() {
      return root.L.latLngBounds(routeCoordinates(activeIdea()));
    }

    function fitAllIdeas() {
      if (state.map) state.map.fitBounds(allBounds(), { padding: [28, 28], maxZoom: 8 });
    }

    function fitSelectedIdea() {
      if (state.map) state.map.fitBounds(selectedBounds(), { padding: [28, 28], maxZoom: 9 });
    }

    function renderSelectedMarkers() {
      if (!state.map || !state.stopLayer) return;
      state.stopLayer.clearLayers();
      state.stopMarkers.clear();
      const idea = activeIdea();
      routeStopIds(idea).forEach((id, index) => {
        const stop = stopById.get(id);
        const marker = root.L.marker([stop.geo.lat, stop.geo.lng], { icon: makePin(String(index + 1), idea.color), keyboard: true, title: stop.name, alt: `${index + 1}. ${stop.name}` });
        marker.bindPopup(`<b>${escapeHtml(stop.name)}</b><br><span>★ ${escapeHtml(stop.rating.value)} · ${escapeHtml(stop.price.summary)}</span>`);
        marker.on("click", () => selectStop(id, { updateHash: true, scroll: false, openPopup: false }));
        marker.addTo(state.stopLayer);
        state.stopMarkers.set(id, marker);
      });
    }

    function updateMapIdea() {
      if (!state.map) return;
      for (const idea of ideas) {
        const selected = idea.id === state.ideaId;
        const lines = state.routeLines.get(idea.id) || [];
        lines.forEach((line, index) => line.setStyle(index === 0
          ? { weight: selected ? 6 : 3, opacity: selected ? .92 : .2, dashArray: selected ? null : "7 8" }
          : { weight: selected ? 4 : 2, opacity: selected ? .62 : .12, dashArray: "6 8" }));
      }
      renderSelectedMarkers();
      fitSelectedIdea();
    }

    function initializeMap() {
      dom.mapLegend.innerHTML = ideas.map((idea) => `<button type="button" data-idea-id="${escapeHtml(idea.id)}" aria-pressed="false"><i style="--idea:${escapeHtml(idea.color)}"></i>${escapeHtml(idea.shortTitle)}</button>`).join("");
      if (!root.L) {
        dom.routeMap.innerHTML = '<div class="map-fallback">The interactive map could not load. Every stop still has exact-map and driving links in its guide.</div>';
        dom.fitAll.hidden = true;
        dom.fitSelected.hidden = true;
        return;
      }
      dom.routeMap.replaceChildren();
      state.map = root.L.map(dom.routeMap, { scrollWheelZoom: false });
      addTiles(state.map);
      root.L.marker([BUDAPEST.lat, BUDAPEST.lng], { icon: makePin("B", "#f6c85f", true), keyboard: true, title: "Budapest", alt: "Budapest, start and finish" }).bindPopup("<b>Budapest</b><br>Start and finish").addTo(state.map);
      for (const idea of ideas) {
        const mainLine = root.L.polyline(routeCoordinates(idea), { color: idea.color, weight: 3, opacity: .2, dashArray: "7 8", lineJoin: "round" }).bindTooltip(escapeHtml(idea.shortTitle));
        mainLine.addTo(state.map);
        const lines = [mainLine];
        for (const coordinates of branchCoordinates(idea)) {
          const branch = root.L.polyline(coordinates, { color: idea.color, weight: 2, opacity: .12, dashArray: "6 8", lineJoin: "round" }).bindTooltip(`${escapeHtml(idea.shortTitle)} · alternative`);
          branch.addTo(state.map);
          lines.push(branch);
        }
        state.routeLines.set(idea.id, lines);
      }
      state.stopLayer = root.L.layerGroup().addTo(state.map);
      fitAllIdeas();
    }

    function updateHash() {
      if (root.history?.replaceState) root.history.replaceState(null, "", formatHash(state.ideaId, state.stopId));
    }

    function selectStop(stopId, options = {}) {
      const idea = activeIdea();
      if (!idea || !routeStopIds(idea).includes(stopId)) return;
      state.stopId = stopId;
      const stop = stopById.get(stopId);
      renderStopDetail(stop);
      document.querySelectorAll("[data-stop-id]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.stopId === stopId)));
      if (options.pan !== false && state.stopMarkers.has(stopId)) {
        const marker = state.stopMarkers.get(stopId);
        state.map.panTo(marker.getLatLng());
        if (options.openPopup !== false) marker.openPopup();
      }
      if (options.updateHash !== false) updateHash();
      dom.mapStatus.textContent = `${stop.name} selected in ${idea.shortTitle}.`;
      if (options.scroll && root.matchMedia("(max-width: 900px)").matches) dom.detail.scrollIntoView({ block: "start", behavior: root.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }

    function selectIdea(ideaId, options = {}) {
      const idea = ideaById.get(ideaId);
      if (!idea) return;
      state.ideaId = ideaId;
      renderActiveSummary(idea);
      renderTimeline(idea);
      document.querySelectorAll("[data-idea-id]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.ideaId === ideaId)));
      updateMapIdea();
      const preferredStop = options.stopId && routeStopIds(idea).includes(options.stopId) ? options.stopId : routeStopIds(idea)[0];
      selectStop(preferredStop, { updateHash: options.updateHash !== false, scroll: false, openPopup: false, pan: false });
      dom.mapStatus.textContent = `${idea.title} selected. The map, timeline and stop guide are updated.`;
      if (options.scroll && root.matchMedia("(max-width: 760px)").matches) dom.activeSummary.scrollIntoView({ block: "start", behavior: root.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }

    function renderLightboxStrip(photos) {
      dom.lightboxStrip.innerHTML = photos.map((photo, index) => `<button type="button" data-lightbox-index="${index}" aria-current="false" aria-label="View photo ${index + 1}: ${escapeHtml(photo.subject)}"><img src="${escapeHtml(photo.src)}" alt="" width="96" height="68"></button>`).join("");
    }

    function animateLightbox(direction) {
      if (root.matchMedia("(prefers-reduced-motion: reduce)").matches || typeof dom.lightboxImage.animate !== "function") return;
      dom.lightboxImage.getAnimations?.().forEach((animation) => animation.cancel());
      dom.lightboxImage.animate([{ opacity: 0, transform: `translateX(${direction > 0 ? 34 : direction < 0 ? -34 : 0}px) scale(.98)` }, { opacity: 1, transform: "none" }], { duration: 250, easing: "cubic-bezier(.2,.8,.2,1)" });
    }

    function showLightboxPhoto(index, direction = 0) {
      const photos = currentPhotos();
      if (!photos.length || !dom.lightbox.open) return;
      const next = wrapPhotoIndex(index, photos.length);
      const photo = photos[next];
      const request = ++state.lightboxRequest;
      state.lightboxIndex = next;
      dom.lightboxStop.textContent = stopById.get(state.lightboxStopId)?.name || "Trip photo";
      dom.lightboxTitle.textContent = photo.subject;
      dom.lightboxDescription.textContent = photo.alt;
      dom.lightboxCount.textContent = `Photo ${next + 1} of ${photos.length}`;
      dom.lightboxCredit.textContent = `Photo: ${photo.credit}`;
      dom.lightboxStatus.textContent = `${photo.subject}, photo ${next + 1} of ${photos.length}`;
      dom.lightboxError.hidden = true;
      dom.lightboxMedia.classList.add("loading");
      dom.lightboxImage.removeAttribute("src");
      dom.lightboxStrip.querySelectorAll("[data-lightbox-index]").forEach((button) => button.setAttribute("aria-current", String(Number(button.dataset.lightboxIndex) === next)));
      const candidate = new root.Image();
      candidate.onload = () => {
        if (request !== state.lightboxRequest || !dom.lightbox.open) return;
        dom.lightboxImage.src = photo.src;
        dom.lightboxImage.alt = photo.alt;
        dom.lightboxMedia.classList.remove("loading");
        animateLightbox(direction);
      };
      candidate.onerror = () => {
        if (request !== state.lightboxRequest || !dom.lightbox.open) return;
        dom.lightboxMedia.classList.remove("loading");
        dom.lightboxError.hidden = false;
      };
      candidate.src = photo.src;
    }

    function openLightbox(stopId, index, opener) {
      if (!dom.lightbox || typeof dom.lightbox.showModal !== "function" || !photoManifest.stops[stopId]) return;
      state.lightboxStopId = stopId;
      state.lightboxOpener = opener;
      renderLightboxStrip(currentPhotos());
      dom.lightbox.classList.remove("closing");
      dom.lightbox.showModal();
      document.body.classList.add("lightbox-open");
      showLightboxPhoto(index);
      dom.lightboxClose.focus({ preventScroll: true });
    }

    function closeLightbox(immediate = false) {
      if (!dom.lightbox?.open) return;
      ++state.lightboxRequest;
      const finish = () => dom.lightbox.open && dom.lightbox.close();
      if (immediate || root.matchMedia("(prefers-reduced-motion: reduce)").matches) finish();
      else {
        dom.lightbox.classList.add("closing");
        state.lightboxCloseTimer = root.setTimeout(finish, 180);
      }
    }

    function stepLightbox(delta) {
      showLightboxPhoto(state.lightboxIndex + delta, delta);
    }

    renderComparison();
    initializeMap();
    const initial = parseHash(root.location.hash, ideasData);
    selectIdea(initial.ideaId, { stopId: initial.stopId, updateHash: false, scroll: false });

    document.addEventListener("click", (event) => {
      if (!(event.target instanceof root.Element)) return;
      const photoButton = event.target.closest("[data-photo-stop][data-photo-index]");
      if (photoButton) return openLightbox(photoButton.dataset.photoStop, Number(photoButton.dataset.photoIndex), photoButton);
      const stopButton = event.target.closest("[data-stop-id]");
      if (stopButton) return selectStop(stopButton.dataset.stopId, { updateHash: true, scroll: true });
      const ideaButton = event.target.closest("[data-idea-id]");
      if (ideaButton) selectIdea(ideaButton.dataset.ideaId, { updateHash: true, scroll: true });
    });
    dom.fitAll.addEventListener("click", fitAllIdeas);
    dom.fitSelected.addEventListener("click", fitSelectedIdea);
    root.addEventListener("hashchange", () => {
      const next = parseHash(root.location.hash, ideasData);
      selectIdea(next.ideaId, { stopId: next.stopId, updateHash: false, scroll: false });
    });
    dom.lightbox.addEventListener("click", (event) => {
      if (!(event.target instanceof root.Element)) return;
      if (event.target === dom.lightbox || event.target.closest('[data-lightbox-action="close"]')) closeLightbox();
      else if (event.target.closest('[data-lightbox-action="previous"]')) stepLightbox(-1);
      else if (event.target.closest('[data-lightbox-action="next"]')) stepLightbox(1);
      else {
        const thumbnail = event.target.closest("[data-lightbox-index]");
        if (thumbnail) showLightboxPhoto(Number(thumbnail.dataset.lightboxIndex), Number(thumbnail.dataset.lightboxIndex) >= state.lightboxIndex ? 1 : -1);
      }
    });
    dom.lightbox.addEventListener("cancel", (event) => { event.preventDefault(); closeLightbox(); });
    dom.lightbox.addEventListener("close", () => {
      if (state.lightboxCloseTimer) root.clearTimeout(state.lightboxCloseTimer);
      dom.lightbox.classList.remove("closing");
      document.body.classList.remove("lightbox-open");
      dom.lightboxImage.removeAttribute("src");
      dom.lightboxStrip.replaceChildren();
      const opener = state.lightboxOpener;
      state.lightboxStopId = "";
      state.lightboxOpener = null;
      if (opener?.isConnected) root.requestAnimationFrame(() => opener.focus({ preventScroll: true }));
    });
    dom.lightbox.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft") { event.preventDefault(); stepLightbox(-1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); stepLightbox(1); }
      else if (event.key === "Home") { event.preventDefault(); showLightboxPhoto(0, -1); }
      else if (event.key === "End") { event.preventDefault(); showLightboxPhoto(currentPhotos().length - 1, 1); }
    });
    dom.lightboxMedia.addEventListener("touchstart", (event) => {
      state.lightboxTouchStart = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
    }, { passive: true });
    dom.lightboxMedia.addEventListener("touchend", (event) => {
      const start = state.lightboxTouchStart;
      state.lightboxTouchStart = null;
      if (!start || event.changedTouches.length !== 1) return;
      const dx = event.changedTouches[0].clientX - start.x;
      const dy = event.changedTouches[0].clientY - start.y;
      if (Math.abs(dx) >= 52 && Math.abs(dx) > Math.abs(dy) * 1.15) stepLightbox(dx < 0 ? 1 : -1);
    }, { passive: true });
  }

  return Object.freeze({ BUDAPEST, buildStopIndex, escapeHtml, formatHash, googleDirectionsUrl, haversineKm, initialize, osmPinUrl, parseHash, renderPhotoGallery, routeStopIds, routeTopology, validateIdeas, wrapPhotoIndex });
}));
