# Unified "Places & Activities" View — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve `saved-places.html` into one source-aware, votable, note-able grid that also absorbs the activity-picker items, then retire `activities.html`.

**Architecture:** Single self-contained HTML file (inline `<style>` + `<script>`), no build step, no dependencies — exactly like the rest of the site. New activities are appended to the existing `HU`/`UK` data arrays; voting/notes persist to `localStorage`. `activities.html` becomes a redirect.

**Tech Stack:** Vanilla HTML/CSS/JS. **No unit-test framework exists in this project and we are not adding one** (YAGNI — follows the existing pattern). Verification is done in the browser via the local preview server + Chrome MCP / DevTools console assertions, with concrete expected results given per task.

## Global Constraints

- File stays `saved-places.html` (keeps the live GitHub Pages URL stable). Page `<title>`/`<h1>` becomes **"Places & Activities"**.
- Single file, inline `<style>`/`<script>`, **no build, no external deps**.
- Persistence key: `localStorage["matidi-picks-v1"]`, schema `{ [id]: {m:0..5, t:0..5, note:""} }`; empty records pruned.
- Verdict values are exactly `must | worth | maybe | skip`.
- Sources are keys of the existing `SRCS` dict; categories are keys of `CATS` (extend only where noted).
- Do **not** break the existing photo-carousel or chip-filter behaviour.
- Per-browser persistence only (no backend, no cross-device sync) — that is intended for v1.
- Local preview: `cd <projdir> && python3 -m http.server 8799` → open `http://localhost:8799/saved-places.html` in Chrome MCP (per-site "always allow" gate applies).
- Redeploy after merge = `git push` (GitHub Pages rebuilds `main`). Do not push until the final task, and confirm first.

---

### Task 1: Foundation — slugify, pid, persistence store, star helpers

**Files:**
- Modify: `saved-places.html` — insert a helpers block at the top of the `<script>` (after line 177 `<script>`, before `const CATS`).

**Interfaces:**
- Produces:
  - `slugify(s) -> string`
  - `pid(p) -> string` (stable id for a place/activity object)
  - `let picks` (object loaded from localStorage)
  - `loadPicks() -> object`
  - `getPick(id) -> {m,t,note}` (always returns a full record, defaults 0/0/"")
  - `savePick(id, patch)` (merges patch, prunes empty, writes localStorage)
  - `stars(v) -> html` (5 `<i class="star">` spans, filled up to v)
  - `renderStars(starsEl, val)` (toggles `.on` on existing star spans)
  - `escapeHtml(s) -> string`

- [ ] **Step 1: Add the helpers block**

Insert immediately after the opening `<script>` tag (line 177):

```js
/* ---------- ids + persistence (voting & notes) ---------- */
function slugify(s){
  return (s||"").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g,"")
    .replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"");
}
function pid(p){
  return Array.isArray(p.img) ? p.img[0] : (p.img || slugify(p.n));
}
function escapeHtml(s){
  return (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
const PICKS_KEY = "matidi-picks-v1";
function loadPicks(){
  try { return JSON.parse(localStorage.getItem(PICKS_KEY)) || {}; }
  catch { return {}; }
}
let picks = loadPicks();
function getPick(id){ return {m:0, t:0, note:"", ...(picks[id]||{})}; }
function savePick(id, patch){
  const next = {...getPick(id), ...patch};
  if(!next.m && !next.t && !(next.note && next.note.trim())) delete picks[id];
  else picks[id] = next;
  localStorage.setItem(PICKS_KEY, JSON.stringify(picks));
}
function stars(v){
  let h=""; for(let i=1;i<=5;i++) h+=`<i class="star${i<=v?" on":""}" data-v="${i}">★</i>`;
  return h;
}
function renderStars(el, val){
  [...el.querySelectorAll(".star")].forEach(s => s.classList.toggle("on", (+s.dataset.v) <= val));
}
```

- [ ] **Step 2: Verify the helpers in the browser console**

Start the server, open the page in Chrome MCP, open DevTools console, paste:

```js
console.assert(slugify("Café Déli & Co!") === "cafe-deli-co", "slugify");
console.assert(pid({n:"X", img:"my-slug"}) === "my-slug", "pid string");
console.assert(pid({n:"X", img:["a","b"]}) === "a", "pid array");
console.assert(pid({n:"Big Ben & Co"}) === "big-ben-co", "pid fallback");
savePick("test-id", {m:3});
console.assert(getPick("test-id").m === 3, "savePick/getPick");
savePick("test-id", {m:0, t:0, note:""});
console.assert(JSON.parse(localStorage.getItem(PICKS_KEY))["test-id"] === undefined, "prune empty");
console.log("Task 1 helpers OK");
```
Expected: `Task 1 helpers OK` with no assertion warnings.

- [ ] **Step 3: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): add id + localStorage persistence helpers for voting/notes"
```

---

### Task 2: Merge activity-picker items into the data arrays

**Files:**
- Modify: `saved-places.html` — add one `CATS` key (after line 203 `stay:` entry); append HU activities before `];` (line 256); append UK activities before `];` (line 358).
- Reference: `activities.html` (source of the items; 9 `📍 saved` duplicates are intentionally **omitted**).

**Interfaces:**
- Consumes: `CATS`, `SRCS`, `HU`, `UK` arrays.
- Produces: HU grows 33→53, UK grows 85→107; new `CATS.waterpark` key.

- [ ] **Step 1: Add the `waterpark` category**

In `CATS`, after the `stay:` line (line 203), add:

```js
  waterpark:{e:"🌊",l:"Water parks & slides"},
```

- [ ] **Step 2: Append the 20 Hungary activities**

Inside the `HU` array, just before its closing `];` (line 256), add a separator comment and these entries:

```js
  /* ── merged from the activity picker (no photos yet) ── */
  {n:"Aquaworld Budapest", e:"🌊", v:"worth", cat:["waterpark"], src:["claude"], what:"One of Europe's biggest indoor aquaparks — 11+ slides, wave pool, lazy river.", why:"Rainy-day-proof and a blast, right by the city."},
  {n:"Annagóra Aquapark", e:"🌊", v:"worth", cat:["waterpark","daytrip"], src:["claude"], what:"Slide park right on Lake Balaton (Balatonfüred), open daily 10–7 in summer.", why:"Open during your trip and on the roadtrip route — easy to combine with Balaton."},
  {n:"Hungarospa Hajdúszoboszló", e:"🌊", v:"maybe", cat:["waterpark","daytrip"], src:["claude"], what:"'Europe's largest' spa + Aquapalace slide complex.", why:"Huge, but it's out east — only if the roadtrip swings that way."},
  {n:"Lupa Beach", e:"🏖️", v:"maybe", cat:["waterpark"], src:["claude"], what:"Lake 'beach' north of Budapest with an inflatable aqua obstacle park.", why:"Fun summer afternoon if you want sand near the city."},
  {n:"Quad / ATV tour", e:"🏍️", v:"worth", cat:["adrenaline"], src:["getyourguide"], what:"Off-road circuit + cross-country forest ride ~20km out; gear, guide & a beer included.", why:"Proper adrenaline with pickup from Heroes' Square — your kind of thing."},
  {n:"Buggy off-road tour", e:"🏎️", v:"maybe", cat:["adrenaline"], src:["getyourguide"], what:"Two-seat off-road buggy through forest tracks.", why:"Same vibe as the quads — pick one of the two."},
  {n:"Cyberjump trampoline park", e:"🤸", v:"maybe", cat:["adrenaline"], src:["claude"], what:"Big indoor trampoline & freejump arena in Budapest.", why:"Goofy fun for an hour; a good rainy-day backup."},
  {n:"Jet-ski / SUP on Balaton", e:"🌊", v:"worth", cat:["adrenaline","daytrip"], src:["claude"], what:"Get out on Lake Balaton on a jet-ski or paddleboard.", why:"The roadtrip's water moment — book a slot on the lake leg."},
  {n:"EXIT THE ROOM", e:"🔓", v:"worth", cat:["escape"], src:["claude"], what:"2025 'best Hungarian escape room' winner; 3 polished downtown locations.", why:"Budapest basically invented escape rooms — do one, make it this."},
  {n:"AROOM (ex-Locked Room)", e:"🔓", v:"maybe", cat:["escape"], src:["claude"], what:"14 cinematic, high-production rooms — one of the city's most awarded venues.", why:"Great if EXIT is booked out; just pick one room."},
  {n:"E-Exit", e:"🔓", v:"maybe", cat:["escape"], src:["claude"], what:"'Deadland' + a 75-min steampunk Jules-Verne room players rave about.", why:"For the escape-room enthusiast — otherwise one is plenty."},
  {n:"Neverland", e:"🔓", v:"maybe", cat:["escape"], src:["claude"], what:"Themed rooms (Jailbreak, Wild West, Asylum) + a bar to debrief in.", why:"The on-site bar is a nice touch; still, one room is enough."},
  {n:"Pinball Museum (Flippermúzeum)", e:"🕹️", v:"worth", cat:["attractions"], src:["claude"], what:"130+ vintage pinball & arcade machines — all on free play.", why:"Cheap, nostalgic, genuinely fun for an hour or two."},
  {n:"Museum of Illusions", e:"🌀", v:"maybe", cat:["attractions"], src:["claude"], what:"Mind-bending optical rooms, great for photos.", why:"Cute and photogenic; skip if you do the London Paradox one."},
  {n:"Pálvölgy Cave adventure", e:"🕳️", v:"worth", cat:["adrenaline","attractions"], src:["claude"], what:"Overalls-and-headlamp caving tour beneath the city.", why:"A real little adventure under Budapest — memorable and different."},
  {n:"Hospital in the Rock", e:"🏥", v:"worth", cat:["museums","attractions"], src:["claude"], what:"Secret WWII + Cold War bunker hospital in the Buda cliffs.", why:"Fascinating, atmospheric and indoors — a strong rainy-day pick."},
  {n:"House of Houdini", e:"🎩", v:"maybe", cat:["shows","museums"], src:["claude"], what:"Magic & Houdini museum with live illusion shows.", why:"Niche but charming if a show time lines up."},
  {n:"Szimpla Kert & ruin bars", e:"🍻", v:"must", cat:["bars"], src:["claude"], what:"Budapest's legendary derelict-building ruin bars.", why:"An essential Budapest night out — start at Szimpla and wander."},
  {n:"Danube night cruise", e:"🚢", v:"worth", cat:["boats"], src:["claude"], what:"The Parliament & bridges lit up from the water.", why:"Cheap, romantic, and the best view of the skyline after dark."},
  {n:"Shooting range experience", e:"🔫", v:"maybe", cat:["adrenaline"], src:["claude"], what:"Fire an AK-47, Glock, M4 & more with an instructor — no licence needed.", why:"Big adrenaline hit; you've a similar one (Alpha Guns) in London too."},
```

- [ ] **Step 3: Append the 22 London activities**

Inside the `UK` array, just before its closing `];` (line 358, after the Claude picks), add:

```js
  /* ── merged from the activity picker (no photos yet) ── */
  {n:"The Lion King", e:"🦁", v:"worth", cat:["shows"], src:["claude"], what:"The spectacle classic at the Lyceum — on in July 2026.", why:"If you want the big visual showstopper, this is it."},
  {n:"Hamilton", e:"🎤", v:"worth", cat:["shows"], src:["claude"], what:"The hip-hop history phenomenon — Victoria Palace, on in Jul 2026.", why:"The hardest ticket of the 2010s and still electric. A strong West-End pick."},
  {n:"Wicked", e:"🧹", v:"worth", cat:["shows"], src:["claude"], what:"Defying gravity at the Apollo Victoria — on in Jul 2026.", why:"Crowd-pleaser with a knockout second act."},
  {n:"Hadestown", e:"🎭", v:"worth", cat:["shows"], src:["claude"], what:"Tony-winning folk-jazz retelling of Orpheus — Lyric Theatre.", why:"The musical-lover's pick; gorgeous score."},
  {n:"Mamma Mia!", e:"💃", v:"maybe", cat:["shows"], src:["claude"], what:"The ABBA singalong crowd-pleaser.", why:"Pure fun — but for ABBA, ABBA Voyage is more of an event."},
  {n:"SIX", e:"👑", v:"worth", cat:["shows"], src:["claude"], what:"Henry VIII's wives as a pop concert — short & high-energy.", why:"80 minutes, no interval — perfect for a show without a long night."},
  {n:"The Crystal Maze LIVE Experience", e:"💎", v:"worth", cat:["escape"], src:["claude"], what:"Run the iconic TV game-show zones as a team.", why:"Daft, active, hugely fun for two — a proper laugh."},
  {n:"clueQuest", e:"🕵️", v:"maybe", cat:["escape"], src:["claude"], what:"Slick, story-driven spy-themed escape rooms.", why:"Solid escape room; do it if Crystal Maze is booked."},
  {n:"Sherlock: The Official Live Game", e:"🔎", v:"maybe", cat:["escape"], src:["claude"], what:"BBC Sherlock-themed immersive escape adventure.", why:"For the fans; otherwise one live game is enough."},
  {n:"Up at The O2", e:"🧗", v:"worth", cat:["adrenaline","views"], src:["claude"], what:"Climb up and over the roof of the O2 dome.", why:"Pairs perfectly with your Greenwich day and the cable car."},
  {n:"Lee Valley White Water Centre", e:"🛶", v:"maybe", cat:["adrenaline"], src:["claude"], what:"Raft the actual London 2012 Olympic rapids.", why:"A blast, but out in NE London — a half-day commitment."},
  {n:"Thames RIB speedboat", e:"🚤", v:"worth", cat:["adrenaline","boats"], src:["claude"], what:"High-speed thrill ride blasting down the Thames.", why:"Big grins and central — a quick adrenaline hit between sights."},
  {n:"iFLY indoor skydiving", e:"🪂", v:"worth", cat:["adrenaline"], src:["claude"], what:"Bodyflight in a vertical wind tunnel at The O2.", why:"Right next to your Greenwich day — easy to slot in."},
  {n:"Whistle Punks axe throwing", e:"🪓", v:"maybe", cat:["adrenaline"], src:["claude"], what:"Urban axe-throwing range — competitive & daft.", why:"Fun hour, but you've got shooting (Alpha Guns) covered too."},
  {n:"Frameless", e:"🖼️", v:"worth", cat:["vr"], src:["claude"], what:"Walk-through immersive digital art — rooms that move around you.", why:"Like Outernet but ticketed and deeper; great if it's wet out."},
  {n:"ABBA Voyage", e:"🕺", v:"must", cat:["shows","vr"], src:["claude"], what:"'Live' virtual ABBA concert in a purpose-built arena, Stratford.", why:"Genuinely unforgettable — the one immersive show everyone raves about."},
  {n:"Twist Museum", e:"🌀", v:"maybe", cat:["vr","museums"], src:["claude"], what:"Illusions & perception playground near Oxford Circus.", why:"Fun and photogenic; overlaps with Paradox — pick one illusion spot."},
  {n:"Swingers / Junkyard / Puttshack", e:"⛳", v:"worth", cat:["bars","attractions"], src:["claude"], what:"Crazy-golf cocktail bars — competitive & lively.", why:"A great going-out-but-doing-something night for two."},
  {n:"Clays", e:"🎯", v:"maybe", cat:["bars","attractions"], src:["claude"], what:"Virtual clay-pigeon shooting + cocktails in the City.", why:"Novel and fun; one 'competitive bar' night is probably enough."},
  {n:"Bounce ping-pong", e:"🏓", v:"maybe", cat:["bars","attractions"], src:["claude"], what:"Social ping-pong club with food & drinks.", why:"Easy, lively and cheap — a good casual evening."},
  {n:"Secret Cinema: Grease", e:"🎬", v:"worth", cat:["shows"], src:["claude"], what:"Dress up and step inside Rydell High for a fully immersive live Grease.", why:"If it's running on your dates it's a one-of-a-kind night — book early."},
  {n:"Center Parcs — Subtropical Swimming Paradise", e:"🌊", v:"maybe", cat:["waterpark","daytrip"], src:["claude"], what:"Rapids, flumes & slides under a glass dome (~1h from London, Woburn).", why:"London's thin on slide parks; only worth it as a day-trip/overnight."},
```

- [ ] **Step 4: Verify counts and render**

Reload the page. In console:

```js
console.assert(HU.length === 53, "HU count = " + HU.length);
console.assert(UK.length === 107, "UK count = " + UK.length);
console.assert(CATS.waterpark, "waterpark cat");
// every entry has a renderable primary category:
[...HU,...UK].forEach(p => console.assert(CATS[(p.cat||[])[0]], "missing cat: " + p.n));
console.log("Task 2 data OK");
```
Expected: `Task 2 data OK`, no warnings. Visually: Hungary tab shows a new "🌊 Water parks & slides" section; UK tab shows the new shows/escape/adrenaline cards with emoji tiles (no broken images).

- [ ] **Step 5: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): merge 42 activity-picker items into HU/UK data (+waterpark cat)"
```

---

### Task 3: Corroboration badge ("🔥 N sources")

**Files:**
- Modify: `saved-places.html` — `card()` (lines 365-389) and CSS (near `.verdict` styles).

**Interfaces:**
- Consumes: `card(p)`, `pid`, `getPick`.
- Produces: updated `card()` that renders `id`, a corroboration badge, and reads `pk` (used by Tasks 6–7 too).

- [ ] **Step 1: Update `card()` to compute id/pk and the badge**

Replace the body of `card()` (lines 365-389) with:

```js
function card(p){
  const id = pid(p);
  const pk = getPick(id);
  const imgs = p.img ? (Array.isArray(p.img) ? p.img : [p.img]) : [];
  const multi = imgs.length > 1;
  let media;
  if(imgs.length){
    const slides = imgs.map((s,i)=>`<div class="slide${i===0?" on":""}"><img src="images/${s}.jpg" alt="${p.n}" loading="lazy"></div>`).join("");
    const ctrls = multi ? `<button class="cbtn prev" aria-label="Previous photo">‹</button><button class="cbtn next" aria-label="Next photo">›</button><span class="ccount">1/${imgs.length}</span><div class="cdots">${imgs.map((_,i)=>`<i class="${i===0?"on":""}"></i>`).join("")}</div>` : "";
    media = slides + ctrls;
  } else {
    media = `<div class="emoji">${p.e||"📍"}</div>`;
  }
  const vlabel = {must:"🟢 Must-see", worth:"👍 Worth it", maybe:"🟡 If you have time", skip:"🔴 Skip"}[p.v];
  const tags = (p.cat||[]).map(c=>`<span class="ptag">${CATS[c].e} ${CATS[c].l}</span>`).join("");
  const srcs = (p.src||[]).map(s=>`<span>${SRCS[s].e} ${SRCS[s].l}</span>`).join("");
  const nsrc = (p.src||[]).length;
  const corro = nsrc >= 2 ? `<span class="corro">🔥 ${nsrc} sources</span>` : "";
  return `<article class="pcard">
    <div class="pimg${multi?" carousel":""}" data-i="0">${media}<span class="verdict v-${p.v}">${vlabel}</span>${corro}</div>
    <div class="pbody">
      <div class="ptop"><h3>${p.n}</h3>${p.r?`<span class="rate">★ ${p.r}</span>`:""}</div>
      <div class="ptags">${tags}</div>
      <p class="what">${p.what}</p>
      <p class="why"><b>Verdict:</b> ${p.why}</p>
      <div class="psrc">${srcs}</div>
      <div class="vote" data-id="${id}">
        <div class="vrow"><span class="vname">Matija</span><span class="stars" data-who="m">${stars(pk.m)}</span></div>
        <div class="vrow"><span class="vname">Tündi</span><span class="stars" data-who="t">${stars(pk.t)}</span></div>
        <button class="notetoggle${pk.note?" has":""}">📝 ${pk.note?"note ●":"note ▾"}</button>
        <div class="notewrap" ${pk.note?"":"hidden"}><textarea class="noteinput" rows="2" placeholder="Write a note…">${escapeHtml(pk.note)}</textarea></div>
      </div>
    </div>
  </article>`;
}
```

(The `.vote` block is wired up in Tasks 6–7; this task is verified by the badge only.)

- [ ] **Step 2: Add CSS for the corroboration badge**

After the `.verdict` rule block (search `.verdict{`, around line 95-100), add:

```css
  .corro{position:absolute; top:10px; left:10px; background:rgba(44,33,24,.86); color:#fff;
    font-size:.7rem; font-weight:700; padding:3px 8px; border-radius:100px; letter-spacing:.02em}
```

- [ ] **Step 3: Verify**

Reload. In console:

```js
// Borough Market has 3 sources (list, google, tripadvisor) → badge of 3
const bm = UK.find(p=>p.n==="Borough Market");
console.assert((bm.src||[]).length>=2, "BM multi-source");
console.log("rendered corro badges:", document.querySelectorAll(".corro").length);
```
Expected: several `.corro` badges visible top-left on multi-source cards (e.g. Borough Market shows "🔥 3 sources"); single-source activity cards show none.

- [ ] **Step 4: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): corroboration badge + card id/pk plumbing for voting"
```

---

### Task 4: Source-primary filter ordering

**Files:**
- Modify: `saved-places.html` — filter bar markup (lines 147-169).

**Interfaces:**
- Consumes: existing chip-build code (`buildChips`, the `.filterbar` click handler) — unchanged.
- Produces: Source filter group rendered first.

- [ ] **Step 1: Move the Source group to the top**

In the `.filterbar` block, reorder the three `.fgroup` divs so **Source** is first, then **Verdict**, then **Category**. Result (lines 147-164 region):

```html
<div class="filterbar"><div class="wrap">
  <div class="fgroup">
    <span class="flabel">Source</span>
    <div class="chips" id="srcchips"></div>
  </div>
  <div class="fgroup">
    <span class="flabel">Verdict</span>
    <div class="chips">
      <button class="chip vchip" data-v="must">🟢 Must-see</button>
      <button class="chip vchip" data-v="worth">👍 Worth it</button>
      <button class="chip vchip" data-v="maybe">🟡 If you have time</button>
      <button class="chip vchip" data-v="skip">🔴 Skip</button>
    </div>
  </div>
  <div class="fgroup">
    <span class="flabel">Category</span>
    <div class="chips" id="catchips"></div>
  </div>
  <div class="fmeta">
    <span class="fcount" id="fcount"></span>
    <button class="clearbtn" id="clearf" hidden onclick="clearFilters()">Clear all filters ✕</button>
  </div>
</div></div>
```

- [ ] **Step 2: Verify**

Reload. Confirm the filter bar now lists **Source** first, then Verdict, then Category. Click a source chip (e.g. 🧠 Claude) → grid narrows to that source. Click again → resets. Carousel and other chips still work.

- [ ] **Step 3: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): make Source the primary (top) filter axis"
```

---

### Task 5: Sort control (By category / Most sources / Top rated)

**Files:**
- Modify: `saved-places.html` — `.fmeta` markup (add control), `state` (line 362), `apply()` (lines 410-429), add `SORTCMP`, add a `change` listener, add CSS.

**Interfaces:**
- Consumes: `state`, `apply`, `matches`, `card`, `CATS`.
- Produces: `state.sort` ("cat"|"src"|"rate"), `SORTCMP` map; `apply()` renders grouped (cat) or flat (src/rate).

- [ ] **Step 1: Add the sort control + "Our picks" toggle to `.fmeta`**

Replace the `.fmeta` div (just edited in Task 4) with:

```html
  <div class="fmeta">
    <div class="fctrls">
      <select id="sortsel" class="sortsel" aria-label="Sort">
        <option value="cat">↕ By category</option>
        <option value="src">↕ Most sources</option>
        <option value="rate">↕ Top rated</option>
      </select>
      <button class="chip" id="ourpicks">⭐ Our picks</button>
    </div>
    <span class="fcount" id="fcount"></span>
    <button class="clearbtn" id="clearf" hidden onclick="clearFilters()">Clear all filters ✕</button>
  </div>
```

(The `#ourpicks` button is wired in Task 8; it is inert until then.)

- [ ] **Step 2: Add CSS for the controls**

After the `.clearbtn` rule (around line 69), add:

```css
  .fctrls{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
  .sortsel{font-family:inherit; font-size:.8rem; font-weight:600; color:var(--ink); cursor:pointer;
    background:var(--paper); border:1.5px solid var(--line); border-radius:100px; padding:6px 12px}
  .sortsel:hover{border-color:var(--ink)}
```

- [ ] **Step 3: Extend `state` and add `SORTCMP`**

Change `state` (line 362) to include `sort` and `ourpicks`:

```js
const state = {country:"Hungary", verdicts:new Set(), cats:new Set(), srcs:new Set(), sort:"cat", ourpicks:false};
const SORTCMP = {
  src:(a,b)=> ((b.src||[]).length-(a.src||[]).length) || ((parseFloat(b.r)||0)-(parseFloat(a.r)||0)) || a.n.localeCompare(b.n),
  rate:(a,b)=> ((parseFloat(b.r)||0)-(parseFloat(a.r)||0)) || a.n.localeCompare(b.n),
};
```

- [ ] **Step 4: Update `apply()` to honour the sort mode**

Replace `apply()` (lines 410-429) with:

```js
function apply(){
  const data = DATA[state.country] || [];
  const visible = data.filter(matches);
  const root = document.getElementById("results");
  if(!visible.length){
    root.innerHTML = `<div class="soon">No places match these filters.<br/><button class="clearbtn" onclick="clearFilters()">Clear filters</button></div>`;
  } else if(state.sort === "cat"){
    root.innerHTML = Object.keys(CATS).map(ck=>{
      const items = visible.filter(p=>(p.cat||[])[0]===ck);
      if(!items.length) return "";
      return `<div class="catname">${CATS[ck].e} ${CATS[ck].l} <span class="ln"></span><span class="count">${items.length}</span></div>
        <div class="grid">${items.map(card).join("")}</div>`;
    }).join("");
  } else {
    const sorted = visible.slice().sort(SORTCMP[state.sort]);
    root.innerHTML = `<div class="grid">${sorted.map(card).join("")}</div>`;
  }
  const t = visible.length;
  document.getElementById("fcount").textContent = `${t} ${t===1?"place":"places"} shown`;
  document.getElementById("clearf").hidden = !(state.verdicts.size||state.cats.size||state.srcs.size||state.ourpicks);
}
```

- [ ] **Step 5: Wire the sort `change` event**

After the `.filterbar` click listener block (ends line 466), add:

```js
document.getElementById("sortsel").addEventListener("change", e=>{ state.sort = e.target.value; apply(); });
```

- [ ] **Step 6: Verify**

Reload. Default = "By category" (section headers present). Switch to "Most sources" → headers disappear, one flat grid, multi-source cards (🔥) first. Switch to "Top rated" → highest `★` first, unrated cards last. Switch back → grouped again.

- [ ] **Step 7: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): sort control — by category / most sources / top rated"
```

---

### Task 6: Dual voting (Matija / Tündi 5★ rows)

**Files:**
- Modify: `saved-places.html` — add a `#results` click handler for stars; add CSS for `.vote`/`.vrow`/`.stars`/`.star`.

**Interfaces:**
- Consumes: `getPick`, `savePick`, `renderStars`, the `.vote` block already rendered by `card()` (Task 3).
- Produces: persisted `picks[id].m` / `.t`.

- [ ] **Step 1: Add CSS for the vote block**

After the `.psrc span` rule (line 122), add:

```css
  .vote{margin-top:10px; padding-top:9px; border-top:1px dashed var(--line); display:flex; flex-direction:column; gap:4px}
  .vrow{display:flex; align-items:center; gap:10px}
  .vname{font-size:.74rem; font-weight:700; color:var(--ink-soft); width:52px; flex:none}
  .stars{display:inline-flex; gap:2px; cursor:pointer}
  .star{font-style:normal; font-size:1.15rem; line-height:1; color:var(--line); transition:.12s; user-select:none}
  .star:hover{transform:scale(1.15)}
  .star.on{color:var(--mustard)}
  .notetoggle{align-self:flex-start; margin-top:4px; font-family:inherit; font-size:.76rem; font-weight:700;
    cursor:pointer; background:none; border:1.5px solid var(--line); border-radius:100px; padding:4px 11px; color:var(--ink-soft)}
  .notetoggle:hover{border-color:var(--ink); color:var(--ink)}
  .notetoggle.has{border-color:var(--paprika); color:var(--paprika-deep)}
  .notewrap{margin-top:6px}
  .noteinput{width:100%; font-family:inherit; font-size:.85rem; color:var(--ink); background:var(--paper);
    border:1.5px solid var(--line); border-radius:10px; padding:8px 10px; resize:vertical}
  .noteinput:focus{outline:none; border-color:var(--ink)}
```

- [ ] **Step 2: Add the star click handler**

After the existing `#results` carousel click listener (ends line 453), add:

```js
/* voting: tap a star to set; tap the current value to clear */
document.getElementById("results").addEventListener("click", e=>{
  const star = e.target.closest(".star"); if(!star) return;
  const voteEl = star.closest(".vote"); const id = voteEl.dataset.id;
  const who = star.closest(".stars").dataset.who;
  const val = +star.dataset.v;
  const next = (getPick(id)[who] === val) ? 0 : val;
  savePick(id, {[who]: next});
  renderStars(star.closest(".stars"), next);
});
```

- [ ] **Step 3: Verify persistence**

Reload. On any card, click Matija's 4th star → 4 stars fill mustard. Click Tündi's 5th → fills. Then:

```js
location.reload();
```
After reload, find the same card — the 4★ / 5★ remain (re-derived from `localStorage`). Click Matija's 4th star again → clears to 0 (tap-current-to-clear). Confirm in console:

```js
console.log(JSON.parse(localStorage.getItem("matidi-picks-v1")));
```
Expected: an object keyed by id with `{m,t,...}` reflecting your taps.

- [ ] **Step 4: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): dual Matija/Tündi 5-star voting, persisted to localStorage"
```

---

### Task 7: Per-card notes

**Files:**
- Modify: `saved-places.html` — add `#results` click handler for the note toggle and an `input` handler for the textarea. (CSS added in Task 6.)

**Interfaces:**
- Consumes: `savePick`, the `.notetoggle`/`.notewrap`/`.noteinput` markup from `card()`.
- Produces: persisted `picks[id].note`.

- [ ] **Step 1: Add the note toggle + input handlers**

After the star click handler (Task 6), add:

```js
/* notes: toggle textarea, persist on input */
document.getElementById("results").addEventListener("click", e=>{
  const tog = e.target.closest(".notetoggle"); if(!tog) return;
  const wrap = tog.nextElementSibling;
  wrap.hidden = !wrap.hidden;
  if(!wrap.hidden) wrap.querySelector("textarea").focus();
});
document.getElementById("results").addEventListener("input", e=>{
  const ta = e.target.closest(".noteinput"); if(!ta) return;
  const voteEl = ta.closest(".vote"); const id = voteEl.dataset.id;
  savePick(id, {note: ta.value});
  const tog = voteEl.querySelector(".notetoggle");
  const has = !!ta.value.trim();
  tog.classList.toggle("has", has);
  tog.textContent = has ? "📝 note ●" : "📝 note ▾";
});
```

- [ ] **Step 2: Verify persistence**

Reload. On a card, click `📝 note ▾` → textarea opens. Type "book ahead". The toggle turns to `📝 note ●` and goes paprika-coloured. Reload the page → the same card shows the filled `●` marker, and opening it shows "book ahead". Clear the text → marker reverts to `▾` and the record is pruned (verify the id disappears from `localStorage` if stars are also 0).

- [ ] **Step 3: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): per-card notes, persisted to localStorage"
```

---

### Task 8: "Our picks" filter

**Files:**
- Modify: `saved-places.html` — `matches()` (lines 392-397), `clearFilters()` (lines 430-434), add `#ourpicks` click listener.

**Interfaces:**
- Consumes: `state.ourpicks`, `getPick`, `pid`, `apply`.
- Produces: filter that keeps only cards with `m>=4 || t>=4`.

- [ ] **Step 1: Add the predicate to `matches()`**

Update `matches()` (lines 392-397) to:

```js
function matches(p){
  if(state.verdicts.size && !state.verdicts.has(p.v)) return false;
  if(state.cats.size && !(p.cat||[]).some(c=>state.cats.has(c))) return false;
  if(state.srcs.size && !(p.src||[]).some(s=>state.srcs.has(s))) return false;
  if(state.ourpicks){ const pk = getPick(pid(p)); if(!(pk.m>=4 || pk.t>=4)) return false; }
  return true;
}
```

- [ ] **Step 2: Reset `ourpicks` in `clearFilters()`**

Update `clearFilters()` (lines 430-434) to also clear the flag (the `.chip.on` sweep already un-highlights the button):

```js
function clearFilters(){
  state.verdicts.clear(); state.cats.clear(); state.srcs.clear(); state.ourpicks=false;
  document.querySelectorAll(".filterbar .chip.on").forEach(c=>c.classList.remove("on"));
  apply();
}
```

- [ ] **Step 3: Wire the `#ourpicks` button**

After the `sortsel` change listener (Task 5, Step 5), add:

```js
document.getElementById("ourpicks").addEventListener("click", e=>{
  state.ourpicks = !state.ourpicks;
  e.currentTarget.classList.toggle("on", state.ourpicks);
  apply();
});
```

- [ ] **Step 4: Verify**

Reload. Give two cards a 4★+ from either person. Click **⭐ Our picks** → only those cards show; the button highlights (`.on`), and "Clear all filters ✕" appears. Click again (or Clear) → all cards return.

- [ ] **Step 5: Commit**

```bash
git add saved-places.html
git commit -m "feat(places): 'Our picks' filter (either of us rated ≥4★)"
```

---

### Task 9: Retitle, nav cleanup, retire activities.html, deploy

**Files:**
- Modify: `saved-places.html` — `<title>`, header `<h1>`/kicker/intro, topnav (lines 131-140).
- Modify: `trip-plan.html` — topnav (lines 200-203) + the two gobtns (lines 443-444).
- Replace: `activities.html` — with a redirect to `saved-places.html`.

**Interfaces:**
- Consumes: nothing new.
- Produces: consistent nav, retired activity-picker page.

- [ ] **Step 1: Retitle `saved-places.html` and fix its nav**

- `<title>` (line 6 region): set to `Places & Activities · Matija & Tündi`.
- Header block (lines 131-140) → replace with:

```html
<header class="top"><div class="wrap">
  <div class="topnav">
    <a href="trip-plan.html">🗺️ Trip plan</a>
    <a class="here" href="saved-places.html">📍 Places & Activities</a>
  </div>
  <p class="kicker">Every place &amp; activity — filter by source, vote, take notes</p>
  <h1>Places &amp; <em>Activities</em></h1>
  <p>Every saved pin plus the activity ideas, in one place — with a photo or icon, what it is, its category, who recommends it, and my honest verdict. <b>Filter by source, verdict or category</b>, sort them, and give each one your own ⭐ stars (Matija &amp; Tündi vote separately) and notes. <i>Saved in this browser.</i></p>
</div></header>
```

- [ ] **Step 2: Fix `trip-plan.html` nav + buttons**

- topnav (lines 200-203) → replace the three links with two:

```html
      <div class="topnav">
        <a class="here" href="trip-plan.html">🗺️ Trip plan</a>
        <a href="saved-places.html">📍 Places & Activities</a>
      </div>
```

- The two gobtns (lines 443-444) → replace with a single button:

```html
  <a class="gobtn" href="saved-places.html">📍 Places &amp; Activities →</a>
```

- [ ] **Step 3: Replace `activities.html` with a redirect**

Overwrite the entire file with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta http-equiv="refresh" content="0; url=saved-places.html" />
<title>Moved · Places &amp; Activities</title>
<script>location.replace("saved-places.html");</script>
</head>
<body style="font-family:sans-serif;padding:2rem">
<p>The activity picker is now part of <a href="saved-places.html">Places &amp; Activities</a>…</p>
</body>
</html>
```

- [ ] **Step 4: Verify the whole flow**

Reload `saved-places.html`: title/heading read "Places & Activities", nav has two items. Open `trip-plan.html`: nav + the single button point to `saved-places.html`. Open `activities.html`: it redirects to `saved-places.html`. No console errors. Spot-check: a multi-source card shows 🔥, stars persist across reload, a note persists, sort modes work, "Our picks" filters.

- [ ] **Step 5: Commit**

```bash
git add saved-places.html trip-plan.html activities.html
git commit -m "chore(site): retitle to Places & Activities, retire activity-picker page"
```

- [ ] **Step 6: Deploy (confirm first)**

Per repo rules, **ask the user before pushing** (push redeploys the live GitHub Pages site). On approval:

```bash
git push
```
Then confirm live: `curl -s -o /dev/null -w "%{http_code}\n" https://pero122.github.io/matija-tundi-trip/saved-places.html` → `200`, and the new title appears after the Pages rebuild (~30-60s).

---

## Self-Review

**Spec coverage:**
- §2.1 merge activities → **Task 2**. §2.2 source-primary + corroboration + sort → **Tasks 3,4,5**. §2.3 dual voting → **Task 6**. §2.4 notes → **Task 7**. §2.5 retire activities + nav → **Task 9**. §3 per-browser persistence → **Task 1** (localStorage). §5.2 stable id → **Task 1** (`pid`). §5.3 dedupe/CATS extension → **Task 2**. §6.5 "Our picks" → **Task 8**. §6 country tabs unchanged → untouched (tab code not modified). §7 retitle/redirect → **Task 9**. All spec sections map to a task.
- v2 non-goals (scraping, cross-device sync, trip-plan reading votes, photos for new cards) → intentionally absent.

**Placeholder scan:** No TBD/TODO; every code step shows full code; data entries are complete literals.

**Type consistency:** `pid`, `getPick`, `savePick`, `stars`, `renderStars`, `escapeHtml`, `state.sort`, `state.ourpicks`, `SORTCMP`, `PICKS_KEY="matidi-picks-v1"`, `.vote[data-id]`, `.stars[data-who]`, `.star[data-v]`, `.notetoggle`/`.notewrap`/`.noteinput` are defined in Tasks 1/3 and used consistently in Tasks 5–8. `card()` is replaced once (Task 3) with the final shape the later tasks rely on.
