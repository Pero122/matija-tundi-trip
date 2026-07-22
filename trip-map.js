(function exposeTripMap(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TRIP_MAP = api;
  if (root && root.document) {
    const start = () => api.initialize(root);
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start, { once: true });
    else start();
  }
}(typeof window !== "undefined" ? window : globalThis, function buildTripMap() {
  "use strict";

  const BUDAPEST = Object.freeze({ id: "budapest", name: "Budapest", lat: 47.497879, lng: 19.040238 });
  const ROUTES = Object.freeze({
    A: Object.freeze({
      label: "Loop A · Balaton, spa & culture",
      color: "#c8492a",
      className: "loop-a",
      stopOrder: Object.freeze([
        "zamardi-adventure-park",
        "balatonfured-tagore",
        "tihany-abbey",
        "hegyestu-kapolcs",
        "tapolca-lake-cave",
        "szigliget-castle",
        "heviz-thermal-lake",
        "pecs-zsolnay",
      ]),
      mainOrder: Object.freeze([
        "zamardi-adventure-park",
        "balatonfured-tagore",
        "tihany-abbey",
        "hegyestu-kapolcs",
        "tapolca-lake-cave",
        "szigliget-castle",
        "heviz-thermal-lake",
      ]),
      optionalPaths: Object.freeze([
        Object.freeze(["budapest", "pecs-zsolnay", "zamardi-adventure-park"]),
      ]),
    }),
    B: Object.freeze({
      label: "Loop B · Caves, castles & forests",
      color: "#1f4a40",
      className: "loop-b",
      stopOrder: Object.freeze([
        "eger-castle-bath",
        "lillafured-bukk",
        "szalajka-valley",
        "aggtelek-baradla",
        "boldogko-castle",
        "sarospatak-rakoczi",
        "fuzer-castle",
        "regec-castle",
        "zemplen-adventure-park",
      ]),
      mainOrder: Object.freeze([
        "eger-castle-bath",
        "lillafured-bukk",
        "szalajka-valley",
        "aggtelek-baradla",
        "boldogko-castle",
        "sarospatak-rakoczi",
        "fuzer-castle",
        "zemplen-adventure-park",
      ]),
      optionalPaths: Object.freeze([
        Object.freeze(["sarospatak-rakoczi", "regec-castle", "zemplen-adventure-park"]),
      ]),
    }),
  });

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

  function haversineKm(from, to) {
    const radians = (degrees) => degrees * Math.PI / 180;
    const earthKm = 6371;
    const latDelta = radians(to.lat - from.lat);
    const lngDelta = radians(to.lng - from.lng);
    const a = Math.sin(latDelta / 2) ** 2
      + Math.cos(radians(from.lat)) * Math.cos(radians(to.lat)) * Math.sin(lngDelta / 2) ** 2;
    return earthKm * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function googleDirectionsUrl(stop) {
    const params = new URLSearchParams({
      api: "1",
      origin: `${BUDAPEST.lat},${BUDAPEST.lng}`,
      destination: `${stop.geo.lat},${stop.geo.lng}`,
      travelmode: "driving",
    });
    return `https://www.google.com/maps/dir/?${params}`;
  }

  function osmPinUrl(stop) {
    const zoom = 15;
    return `https://www.openstreetmap.org/?mlat=${encodeURIComponent(stop.geo.lat)}&mlon=${encodeURIComponent(stop.geo.lng)}#map=${zoom}/${encodeURIComponent(stop.geo.lat)}/${encodeURIComponent(stop.geo.lng)}`;
  }

  function buildStopIndex(data) {
    const stopById = new Map();
    for (const loop of ["A", "B"]) {
      for (const stop of data.loops[loop] || []) {
        if (stopById.has(stop.id)) throw new Error(`Duplicate trip stop: ${stop.id}`);
        stopById.set(stop.id, { ...stop, loop });
      }
    }
    for (const [loop, route] of Object.entries(ROUTES)) {
      const topologyIds = [...route.stopOrder, ...route.mainOrder, ...route.optionalPaths.flat()];
      for (const id of topologyIds) {
        if (id === "budapest") continue;
        const stop = stopById.get(id);
        if (!stop || stop.loop !== loop) throw new Error(`Route ${loop} references missing stop: ${id}`);
        if (!Number.isFinite(stop.geo?.lat) || !Number.isFinite(stop.geo?.lng)) throw new Error(`Stop has no valid coordinates: ${id}`);
      }
    }
    return stopById;
  }

  function routeCoordinates(route, stopById, closeLoop = true) {
    const coordinates = route.mainOrder.map((id) => stopById.get(id).geo);
    return closeLoop ? [BUDAPEST, ...coordinates, BUDAPEST] : coordinates;
  }

  function initialize(root) {
    const document = root.document;
    const data = root.TRIP_LOCATION_DATA;
    if (!data?.loops) return;

    let stopById;
    try {
      stopById = buildStopIndex(data);
    } catch (error) {
      document.querySelector("#routeMap").innerHTML = `<div class="map-fallback">The route data could not be loaded. ${escapeHtml(error.message)}</div>`;
      return;
    }

    const dom = {
      routeMap: document.querySelector("#routeMap"),
      orientationMap: document.querySelector("#orientationMap"),
      detailLoop: document.querySelector("#detailLoop"),
      detailTitle: document.querySelector("#detailTitle"),
      detailArea: document.querySelector("#detailArea"),
      detailRating: document.querySelector("#detailRating"),
      detailBody: document.querySelector("#detailBody"),
      distanceCopy: document.querySelector("#distanceCopy"),
      directions: document.querySelector("#directions"),
      mapStatus: document.querySelector("#mapStatus"),
      fitRoutes: document.querySelector("#fitRoutes"),
    };
    const state = {
      mode: "all",
      selectedId: "",
      routeMap: null,
      orientationMap: null,
      orientationLayers: null,
      routeLayers: {},
      markerById: new Map(),
    };

    function renderStopButtons() {
      for (const loop of ["A", "B"]) {
        const container = document.querySelector(`#loop${loop}StopButtons`);
        container.innerHTML = ROUTES[loop].stopOrder.map((id, index) => {
          const stop = stopById.get(id);
          return `<button class="stop-button" type="button" data-stop-id="${escapeHtml(id)}" data-loop="${loop}" aria-pressed="false">
            <span class="stop-number">${loop}${index + 1}</span>
            <span class="stop-button-name">${escapeHtml(stop.name)}</span>
            <span class="stop-button-rating">★ ${escapeHtml(stop.rating.value)}</span>
          </button>`;
        }).join("");
      }
    }

    function renderDetail(stop) {
      const route = ROUTES[stop.loop];
      dom.detailLoop.dataset.loop = stop.loop;
      dom.detailLoop.textContent = route.label;
      dom.detailTitle.textContent = stop.name;
      dom.detailArea.textContent = stop.area;
      dom.detailRating.innerHTML = `<b>★ ${escapeHtml(stop.rating.value)}</b><span>${escapeHtml(stop.rating.platform)} · ${escapeHtml(stop.rating.reviews)} reviews</span>`;

      const packages = stop.price.packages.map((option) => `<li><b>${escapeHtml(option.label)}</b><strong>${escapeHtml(option.price)}</strong>${option.note ? `<small>${escapeHtml(option.note)}</small>` : ""}</li>`).join("");
      const sources = stop.sources.map((source) => `<a class="source-link" href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.label)}</a>`).join("");
      const ratingNote = stop.rating.note ? `<small>${escapeHtml(stop.rating.note)}</small>` : "";
      dom.detailBody.innerHTML = `
        <p class="detail-hook">${escapeHtml(stop.hook)}</p>
        <div class="detail-sections">
          <section class="detail-section"><h4>What is it?</h4><p>${escapeHtml(stop.what)}</p></section>
          <section class="detail-section"><h4>Why Matija may like it</h4><p>${escapeHtml(stop.matija)}</p></section>
          <section class="detail-section"><h4>Why Tündi may like it</h4><p>${escapeHtml(stop.tundi)}</p></section>
        </div>
        <dl class="facts-row">
          <div class="fact-box"><b>Traveler rating</b><span>${escapeHtml(stop.rating.value)}/5 · ${escapeHtml(stop.rating.reviews)} reviews</span>${ratingNote}</div>
          <div class="fact-box"><b>Time to allow</b><span>${escapeHtml(stop.duration)}</span></div>
          <div class="fact-box"><b>Trip fit</b><span>${escapeHtml(stop.fit.label)}</span></div>
        </dl>
        <details class="prices"${stop.price.packages.length === 1 ? " open" : ""}>
          <summary><span>💳 Price options</span><span>${escapeHtml(stop.price.summary)} · ${stop.price.packages.length} ${stop.price.packages.length === 1 ? "option" : "options"} ▾</span></summary>
          <ul class="price-list">${packages}</ul>
        </details>
        <p class="caveat"><b>Know before deciding:</b> ${escapeHtml(stop.caveat)}</p>
        <div class="sources">${sources}</div>`;

      const km = Math.round(haversineKm(BUDAPEST, stop.geo));
      dom.distanceCopy.textContent = `Pin: ${stop.geo.label}. It is about ${km} km from Budapest in a straight line. Use driving directions for the real road distance and travel time.`;
      dom.directions.innerHTML = `
        <a class="directions-link" href="${escapeHtml(googleDirectionsUrl(stop))}" target="_blank" rel="noopener noreferrer">Open driving directions ↗</a>
        <a class="directions-link" href="${escapeHtml(osmPinUrl(stop))}" target="_blank" rel="noopener noreferrer">Open exact pin in OSM ↗</a>`;
    }

    function makePinIcon(loop, label, isBudapest = false) {
      return root.L.divIcon({
        className: `map-pin${isBudapest ? " budapest" : loop === "B" ? " loop-b" : ""}`,
        html: escapeHtml(label),
        iconSize: isBudapest ? [38, 38] : [32, 32],
        iconAnchor: isBudapest ? [19, 19] : [16, 16],
      });
    }

    function addTiles(map) {
      root.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
      }).addTo(map);
    }

    function initializeMaps() {
      if (!root.L) {
        dom.routeMap.innerHTML = '<div class="map-fallback">The live map could not load. Use the stop list and exact OpenStreetMap links below.</div>';
        dom.orientationMap.innerHTML = '<div class="map-fallback">The orientation map could not load. Exact-map and driving links still work.</div>';
        dom.fitRoutes.hidden = true;
        return;
      }

      dom.routeMap.replaceChildren();
      dom.orientationMap.replaceChildren();
      state.routeMap = root.L.map(dom.routeMap, { scrollWheelZoom: false, zoomControl: true });
      state.orientationMap = root.L.map(dom.orientationMap, { scrollWheelZoom: false, zoomControl: true });
      addTiles(state.routeMap);
      addTiles(state.orientationMap);
      state.orientationLayers = root.L.layerGroup().addTo(state.orientationMap);

      root.L.marker([BUDAPEST.lat, BUDAPEST.lng], { alt: "Budapest, shared start and finish", icon: makePinIcon("", "B", true), keyboard: true, title: "Budapest" })
        .bindPopup('<div class="popup-name">Budapest</div><div class="popup-meta">Shared start and finish</div>')
        .addTo(state.routeMap);

      for (const loop of ["A", "B"]) {
        const route = ROUTES[loop];
        const layer = root.L.layerGroup();
        state.routeLayers[loop] = layer;
        root.L.polyline(routeCoordinates(route, stopById), { color: route.color, weight: 5, opacity: .84, lineJoin: "round" }).addTo(layer);
        for (const optionalPath of route.optionalPaths) {
          const optionalCoordinates = optionalPath.map((id) => id === "budapest" ? BUDAPEST : stopById.get(id).geo);
          root.L.polyline(optionalCoordinates, { color: route.color, weight: 4, opacity: .72, dashArray: "9 9", lineJoin: "round" }).addTo(layer);
        }
        route.stopOrder.forEach((id, index) => {
          const stop = stopById.get(id);
          const marker = root.L.marker([stop.geo.lat, stop.geo.lng], {
            icon: makePinIcon(loop, `${loop}${index + 1}`),
            keyboard: true,
            alt: `${loop}${index + 1}, ${stop.name}`,
            title: `${loop}${index + 1} · ${stop.name}`,
          });
          marker.bindPopup(`<div class="popup-name">${escapeHtml(stop.name)}</div><div class="popup-meta">${escapeHtml(stop.area)} · ★ ${escapeHtml(stop.rating.value)}</div><div class="popup-action">Full guide updated below ↓</div>`);
          marker.on("click", () => selectStop(id, { focusMainMap: false, revealOnMobile: false, updateHash: true }));
          marker.addTo(layer);
          state.markerById.set(id, marker);
        });
        layer.addTo(state.routeMap);
      }
      fitVisibleRoutes();
    }

    function visibleStops() {
      const loops = state.mode === "all" ? ["A", "B"] : [state.mode];
      return loops.flatMap((loop) => ROUTES[loop].stopOrder.map((id) => stopById.get(id)));
    }

    function fitVisibleRoutes() {
      if (!state.routeMap) return;
      const points = [BUDAPEST, ...visibleStops()].map((item) => [item.geo?.lat ?? item.lat, item.geo?.lng ?? item.lng]);
      state.routeMap.fitBounds(root.L.latLngBounds(points), { padding: [28, 28], maxZoom: 9 });
    }

    function setMapMode(mode) {
      if (!state.routeMap || !["all", "A", "B"].includes(mode)) return;
      state.mode = mode;
      for (const loop of ["A", "B"]) {
        const shouldShow = mode === "all" || mode === loop;
        const layer = state.routeLayers[loop];
        if (shouldShow && !state.routeMap.hasLayer(layer)) layer.addTo(state.routeMap);
        if (!shouldShow && state.routeMap.hasLayer(layer)) state.routeMap.removeLayer(layer);
      }
      document.querySelectorAll("[data-map-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mapMode === mode)));
      fitVisibleRoutes();
      dom.mapStatus.textContent = mode === "all" ? "Both routes are visible." : `Only Loop ${mode} is visible.`;
    }

    function updateOrientationMap(stop) {
      if (!state.orientationMap) return;
      state.orientationLayers.clearLayers();
      root.L.marker([BUDAPEST.lat, BUDAPEST.lng], { alt: "Budapest", icon: makePinIcon("", "B", true), keyboard: true, title: "Budapest" }).addTo(state.orientationLayers);
      root.L.marker([stop.geo.lat, stop.geo.lng], { alt: stop.name, icon: makePinIcon(stop.loop, stop.loop), keyboard: true, title: stop.name }).addTo(state.orientationLayers);
      root.L.polyline([[BUDAPEST.lat, BUDAPEST.lng], [stop.geo.lat, stop.geo.lng]], { color: ROUTES[stop.loop].color, weight: 4, opacity: .8, dashArray: "8 8" }).addTo(state.orientationLayers);
      state.orientationMap.fitBounds([[BUDAPEST.lat, BUDAPEST.lng], [stop.geo.lat, stop.geo.lng]], { padding: [35, 35], maxZoom: 9 });
      root.requestAnimationFrame(() => state.orientationMap.invalidateSize());
    }

    function selectStop(id, options = {}) {
      if (!/^[a-z0-9-]+$/.test(id)) return;
      const stop = stopById.get(id);
      if (!stop) return;
      state.selectedId = id;
      renderDetail(stop);
      updateOrientationMap(stop);
      document.querySelectorAll("[data-stop-id]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.stopId === id)));
      if (state.routeMap && state.markerById.has(id) && options.focusMainMap !== false) {
        const marker = state.markerById.get(id);
        if (!state.routeMap.hasLayer(state.routeLayers[stop.loop])) setMapMode(stop.loop);
        state.routeMap.panTo(marker.getLatLng());
        marker.openPopup();
      }
      if (options.updateHash !== false && root.history?.replaceState) root.history.replaceState(null, "", `#place-${id}`);
      dom.mapStatus.textContent = `${stop.name} selected. The guide and Budapest orientation map are updated.`;
      if (options.revealOnMobile && root.matchMedia("(max-width: 940px)").matches) {
        document.querySelector("#stopDetail").scrollIntoView({ block: "start", behavior: root.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      }
    }

    renderStopButtons();
    initializeMaps();
    document.addEventListener("click", (event) => {
      if (!(event.target instanceof root.Element)) return;
      const stopButton = event.target.closest("[data-stop-id]");
      if (stopButton) selectStop(stopButton.dataset.stopId, { revealOnMobile: true, updateHash: true });
      const modeButton = event.target.closest("[data-map-mode]");
      if (modeButton) setMapMode(modeButton.dataset.mapMode);
    });
    dom.fitRoutes.addEventListener("click", fitVisibleRoutes);
    root.addEventListener("hashchange", () => {
      const match = /^#place-([a-z0-9-]+)$/.exec(root.location.hash);
      if (match) selectStop(match[1], { focusMainMap: true, revealOnMobile: false, updateHash: false });
    });

    const initialMatch = /^#place-([a-z0-9-]+)$/.exec(root.location.hash);
    const initialId = initialMatch && stopById.has(initialMatch[1]) ? initialMatch[1] : ROUTES.A.stopOrder[0];
    selectStop(initialId, { focusMainMap: Boolean(initialMatch), revealOnMobile: false, updateHash: false });
  }

  return Object.freeze({ BUDAPEST, ROUTES, buildStopIndex, escapeHtml, googleDirectionsUrl, haversineKm, initialize, osmPinUrl, routeCoordinates });
}));
