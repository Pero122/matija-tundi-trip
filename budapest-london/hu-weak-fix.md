# Hungary "weak photo" fix — Google Maps replacements

Fetched proper representative photos for 10 Budapest/Hungary places whose
existing card images were weak (menu boards, water towers, generic/wrong shots).
Heroes overwrite the existing weak `images/<slug>.jpg`. A second distinct shot,
where available, is saved as `images/<slug>-2.jpg` (these slugs can become
multi-pic carousels).

| slug | hero (`<slug>.jpg`) | 2nd photo (`<slug>-2.jpg`)? | notes / problems |
|------|---------------------|----------------------------|------------------|
| varosliget | Széchenyi Bath facade behind the formal City Park garden with manicured hedges & flowerbeds (landscape) | ✅ Vajdahunyad Castle turret rising above trees by the lake | Direct hit on "City Park". Both green-parkland shots as requested. |
| margaret-island | Musical Fountain at dusk — tall water jets lit pink/green, reflections in the pool (landscape) | ✅ Daytime fountain with white water jets & big plane trees | Search hit a Results list; picked "Musical Fountain" (4.7, 1,146) marked **Saved in Matija&Tündi**. Both are the actual fountain. |
| marina-setany | Manicured riverside lawn on the District XIII Marina sétány — Danube, a paved path, deck chairs, young trees (landscape) | ❌ none | Saved pin name unclear; took the best "modern riverfront walkway" frame from "Marina sétány játszótér" (the place ON the promenade). Other nearby results were a rocky bank / excavator / playground — weaker, so no 2nd shot. |
| jazz-club | Budapest Jazz Club stage: grand piano, drum kit, double bass under blue/red lighting, audience tables (landscape) | ✅ Live band performing on stage (4-piece, gowns) | Direct hit. Replaces the BJC Bistro menu board. |
| nandori | Pastry display case packed with cakes/slices, price tags (landscape) | ✅ Charming wooden storefront "Nándori Cukrászda" sign + outdoor table | Direct hit. Replaces the koala-cake chalkboard. (A great close-up cream-cake slice was also available.) |
| esetleg | Riverside terrace — wooden bistro tables, string lights, bar, Danube + green Liberty Bridge behind (landscape) | ✅ Plated bruschetta (tomato/basil/parmesan on toast) | Direct hit. Replaces the cocktails menu page. |
| the-spot | Riverside terrace with lit igloo/bubble domes & the golden Parliament glowing across the Danube at night (landscape) | ✅ Plated cheesecake dessert on slate board | Direct hit (4.6, 579). Replaces the food menu page. Hero is the iconic igloo+Parliament shot. |
| negy-musketas | Chicken paprikash with nokedli (egg dumplings), sour cream & paprika — hearty Hungarian plate (landscape) | ✅ Rustic wooden storefront with "Négy Muskétás" sign + terrace | Direct hit. Replaces the menu. (A breaded-fillet+potatoes plate was also available.) |
| bujdoso | Rustic Székely interior — painted ceiling, red-white embroidered tablecloths, folk decor, mountain mural, book/ceramic shelves (landscape) | ✅ Whole fried trout with chips & sauerkraut on a carved fish-shaped wooden board | Direct hit. Replaces the logo'd menu. One candidate downloaded as a PNG menu — avoided, so no normalization needed. |
| saloon-pizzeria | Real thin-crust pizza from the place (pickles, peppers, onion) on a wooden table (landscape) | ✅ Cozy wooden-beam interior with Christmas tree & rustic tables | Search hit a Results list; the user's pin is **Saloon Pizzéria, Akadémia krt. 69** (4.2, 1,290, marked **Saved in Matija&Tündi**) — NOT the Croatian "Pizzeria Novi Saloon". Replaces the generic stock pizza. |

## Multi-pic candidates (have a `-2.jpg`)
varosliget, margaret-island, jazz-club, nandori, esetleg, the-spot,
negy-musketas, bujdoso, saloon-pizzeria  — **9 of 10**.
Only `marina-setany` is hero-only.

## Method notes
- No menu boards, price lists, bare logos, or stock images were used as final picks.
- Every grab was sanity-checked against the page `h1`/title to avoid the Maps SPA
  serving the previous place's images during transitions.
- Disambiguation via the **"Saved in Matija&Tündi"** marker confirmed the exact
  user pins for Margaret Island (Musical Fountain) and Saloon Pizzéria.
