# Trip-planning project guidance

## Persistent local site

- LaunchAgent label: `com.matija.tundi-trip.local-site`
- Installer and lifecycle command: `./scripts/local-site-launchagent.sh [install|status|uninstall]`
- Health URL: `http://127.0.0.1:8799/budapest-london/tripadvisor/index.html`
- Secondary Trip ideas health URL: `http://127.0.0.1:8799/trip-ideas.html`
- Logs: `~/Library/Logs/tundi-trip-local-site.out.log` and `~/Library/Logs/tundi-trip-local-site.err.log`
- Served root: generated `deploy/public` only; repository internals must remain unreachable.
- The installer runs `./deploy/build.sh` before replacing the active service. Run that build again after local site edits.
- Use this launcher; do not leave a terminal-attached server on port `8799`.

## TripAdvisor research

- Use the repository's headless Camoufox workflow and its local cache. Do not use Apify unless the user explicitly requests it.
- Clear Tripadvisor's locale filter and ground activity briefs in the detail-page description plus up to 10 all-language reviews; preserve names/diacritics and ship the synthesis in English. Keep raw pages and review text gitignored; ship only concise summaries and source links.

## Multi-source activity research

- Persist every Apify dataset item in `activity-research/data/activity-research.sqlite3` before filtering or ranking it. The database is local and gitignored; keep its schema, adapters, tests and compact public exports versioned.
- Keep raw provider JSON losslessly queryable and normalize places, listings, snapshots, categories, media, packages and reviews into separate tables. Do not put reviewer identities in normalized tables or publish raw review text.
- Treat provider ranking as discovery evidence, not truth. Default to Bayesian rating quality, preserve raw rating/review count, and keep a separate memorable/rare wildcard lane.
- Represent Budapest, outside-Budapest Hungary, foreign and unknown locations explicitly. A Budapest-origin tour to an outside destination stays outside-Budapest with a separate origin flag.

## Recommendation fit

- Treat a stop as worth the trip when it is fun, rare, emotionally memorable, exceptionally beautiful, or backed by unusually strong traveler consensus. A quiet experience can be a must-do if it is genuinely distinctive.
- Do not promote interchangeable castles, promenades, ordinary caves, or viewpoint-only hikes without a specific twist, exceptional setting, or unusually strong evidence. Explain the actual payoff and the required effort separately.
