(function renderTripLocationCards() {
  "use strict";

  const data = window.TRIP_LOCATION_DATA;
  const containers = {
    A: document.querySelector("#loopAStops"),
    B: document.querySelector("#loopBStops"),
  };

  const escapeHtml = (value) => String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const sourceLink = (source) => (
    `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.label)}</a>`
  );

  function renderPriceDetails(price) {
    const packages = Array.isArray(price.packages) ? price.packages : [];
    if (!packages.length) return "";

    const list = `<ul class="price-list">${packages.map((option) => (
      `<li><b>${escapeHtml(option.label)}:</b> ${escapeHtml(option.price)}${option.note ? ` · ${escapeHtml(option.note)}` : ""}</li>`
    )).join("")}</ul>`;

    if (packages.length === 1) {
      return `<div class="price-details price-single">${list}</div>`;
    }

    return `<details class="price-details"><summary>See ${packages.length} price options <span aria-hidden="true">⌄</span></summary>${list}</details>`;
  }

  function renderCard(stop) {
    const rating = `${stop.rating.platform} ${stop.rating.value}/5 · ${stop.rating.reviews} reviews`;
    const sources = stop.sources.map(sourceLink).join("");
    return `
      <details class="stop-card" id="place-${escapeHtml(stop.id)}"${stop.open ? " open" : ""}>
        <summary>
          <span class="stop-title">
            <span class="stop-icon" aria-hidden="true">${escapeHtml(stop.icon)}</span>
            <span><span class="stop-name" role="heading" aria-level="4">${escapeHtml(stop.name)}</span><span class="stop-area">${escapeHtml(stop.area)}</span></span>
          </span>
          <span class="stop-summary-facts">
            <span class="fit-pill ${escapeHtml(stop.fit.tone)}">${escapeHtml(stop.fit.label)}</span>
            <span class="fact-pill">★ ${escapeHtml(stop.rating.value)} · ${escapeHtml(stop.rating.platform)}</span>
            <span class="fact-pill">${escapeHtml(stop.price.summary)}</span>
            <span class="stop-chev" aria-hidden="true">⌄</span>
          </span>
          <span class="stop-hook"><b>Memorable bit:</b> ${escapeHtml(stop.hook)}</span>
        </summary>
        <div class="stop-body">
          <div class="stop-sections">
            <section class="stop-section"><h4>What is it?</h4><p>${escapeHtml(stop.what)}</p></section>
            <section class="stop-section"><h4>Why Matija may like it</h4><p>${escapeHtml(stop.matija)}</p></section>
            <section class="stop-section"><h4>Why Tündi may like it</h4><p>${escapeHtml(stop.tundi)}</p></section>
          </div>
          <dl class="stop-facts">
            <div><dt>Traveler rating</dt><dd>${escapeHtml(rating)}${stop.rating.note ? `<br><small>${escapeHtml(stop.rating.note)}</small>` : ""}</dd></div>
            <div><dt>Adult price</dt><dd>${escapeHtml(stop.price.summary)}</dd></div>
            <div><dt>Time to allow</dt><dd>${escapeHtml(stop.duration)}</dd></div>
          </dl>
          ${renderPriceDetails(stop.price)}
          <p class="caveat"><b>Know before deciding:</b> ${escapeHtml(stop.caveat)}</p>
          <div class="stop-sources" aria-label="Sources for ${escapeHtml(stop.name)}">${sources}</div>
        </div>
      </details>`;
  }

  function showError(message) {
    Object.values(containers).forEach((container) => {
      if (container) container.innerHTML = `<p class="stop-empty">${escapeHtml(message)}</p>`;
    });
  }

  if (!data || !data.loops) {
    showError("The researched stop guide could not load. Refresh the page to try again.");
    return;
  }

  Object.entries(containers).forEach(([loop, container]) => {
    if (!container) return;
    const stops = data.loops[loop];
    container.innerHTML = Array.isArray(stops) && stops.length
      ? stops.map(renderCard).join("")
      : '<p class="stop-empty">No researched stops are available for this loop yet.</p>';
  });

  function revealHashTarget(hash = location.hash) {
    const id = hash.slice(1);
    if (!/^place-[a-z0-9-]+$/.test(id)) return;
    const target = document.getElementById(id);
    if (!(target instanceof HTMLDetailsElement) || !target.classList.contains("stop-card")) return;
    target.open = true;
    requestAnimationFrame(() => {
      target.scrollIntoView({ block: "start" });
      target.querySelector("summary")?.focus({ preventScroll: true });
    });
  }

  window.addEventListener("hashchange", () => revealHashTarget());
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const link = event.target.closest('a[href^="#place-"]');
    if (link instanceof HTMLAnchorElement && link.hash === location.hash) revealHashTarget(link.hash);
  });

  let printDisclosureState = [];
  window.addEventListener("beforeprint", () => {
    printDisclosureState = [...document.querySelectorAll(".details, .stop-card, .price-details")]
      .filter((element) => element instanceof HTMLDetailsElement)
      .map((element) => ({ element, open: element.open }));
    printDisclosureState.forEach(({ element }) => { element.open = true; });
  });
  window.addEventListener("afterprint", () => {
    printDisclosureState.forEach(({ element, open }) => { element.open = open; });
    printDisclosureState = [];
  });
  revealHashTarget();
}());
