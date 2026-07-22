# Trip-planning project guidance

## Persistent local site

- LaunchAgent label: `com.matija.tundi-trip.local-site`
- Installer and lifecycle command: `./scripts/local-site-launchagent.sh [install|status|uninstall]`
- Health URL: `http://127.0.0.1:8799/budapest-london/tripadvisor/index.html`
- Logs: `~/Library/Logs/tundi-trip-local-site.out.log` and `~/Library/Logs/tundi-trip-local-site.err.log`
- Served root: generated `deploy/public` only; repository internals must remain unreachable.
- The installer runs `./deploy/build.sh` before replacing the active service. Run that build again after local site edits.
- Use this launcher; do not leave a terminal-attached server on port `8799`.

## TripAdvisor research

- Use the repository's headless Camoufox workflow and its local cache. Do not use Apify unless the user explicitly requests it.
- Clear Tripadvisor's locale filter and ground activity briefs in the detail-page description plus up to 10 all-language reviews; preserve names/diacritics and ship the synthesis in English. Keep raw pages and review text gitignored; ship only concise summaries and source links.
