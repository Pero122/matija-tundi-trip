#!/bin/sh
# Build an immutable static release, then atomically point public at it.
# Run this before `npx partykit deploy`. Generated releases are gitignored.
set -eu
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
script_path="$script_dir/$(basename -- "$0")"
cd "$script_dir"

# Serialize validation, activation, and pruning. Without the lock, an older
# build can delete a newer build's active immutable release during cleanup.
if [ "${TUNDI_TRIP_BUILD_LOCKED:-0}" != "1" ]; then
  exec /usr/bin/lockf -k -t 30 "$PWD/.build.lock" \
    /usr/bin/env TUNDI_TRIP_BUILD_LOCKED=1 "$script_path" "$@"
fi

release_root=".site-releases"
release_rel="$release_root/release.$(date +%Y%m%d%H%M%S).$$"
release="$PWD/$release_rel"
link_tmp="$PWD/.public-link.$$"
legacy=""
activated=0

cleanup() {
  rm -f "$link_tmp"
  if [ "$activated" -eq 0 ]; then rm -rf "$release"; fi
  if [ -n "$legacy" ] && [ -e "$legacy" ] && [ ! -e public ]; then mv "$legacy" public; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

mkdir -p "$release/budapest-london/tripadvisor"
cp ../trip-plan.html ../trip-map.html ../trip-ideas.html ../activities.html ../saved-places.html "$release/"
cp index.html "$release/index.html"
cp -R ../images "$release/images"
cp ../budapest-london/tripadvisor/index.html "$release/budapest-london/tripadvisor/index.html"
cp ../budapest-london/tripadvisor/activity-briefs.js "$release/budapest-london/tripadvisor/activity-briefs.js"
cp ../budapest-london/tripadvisor/activity-pricing.js "$release/budapest-london/tripadvisor/activity-pricing.js"
cp ../budapest-london/tripadvisor/discover-collaboration.js "$release/budapest-london/tripadvisor/discover-collaboration.js"
cp ../budapest-london/tripadvisor/discover-pricing.js "$release/budapest-london/tripadvisor/discover-pricing.js"

# Bind every local browser script URL to the validated research generation.
# This prevents a browser cache from mixing helpers or one bundle from an older
# immutable release with the new HTML.
briefs_revision="$(/usr/bin/sed -n 's/^window.ACTIVITY_BRIEFS_REVISION="\([0-9a-f]\{64\}\)";$/\1/p' "$release/budapest-london/tripadvisor/activity-briefs.js")"
pricing_revision="$(/usr/bin/sed -n 's/^window.ACTIVITY_PRICING_REVISION="\([0-9a-f]\{64\}\)";$/\1/p' "$release/budapest-london/tripadvisor/activity-pricing.js")"
if [ -z "$briefs_revision" ] || [ "$briefs_revision" != "$pricing_revision" ]; then
  echo "research bundles do not share one valid revision" >&2
  exit 1
fi
BUNDLE_REVISION="$briefs_revision" /usr/bin/perl -0pi -e '
  $revision = $ENV{"BUNDLE_REVISION"};
  s{<script src="\./(activity-briefs|activity-pricing|discover-collaboration|discover-pricing)\.js(?:\?v=[0-9a-f]{64})?"></script>}{<script src="./$1.js?v=$revision"></script>}g;
' "$release/budapest-london/tripadvisor/index.html"
versioned_scripts="$(/usr/bin/grep -o "?v=$briefs_revision" "$release/budapest-london/tripadvisor/index.html" | /usr/bin/wc -l | /usr/bin/tr -d ' ')"
if [ "$versioned_scripts" != "4" ]; then
  echo "failed to version all four local Discover scripts" >&2
  exit 1
fi

# Keep the guide data and its renderer indivisible across atomic release swaps.
# One HTML response now carries both modules, so no second request can cross the
# public-symlink activation boundary and mix two generations.
node --check ../trip-location-data.js
node --check ../trip-location-cards.js
if /usr/bin/grep -qi '</script' ../trip-location-data.js ../trip-location-cards.js; then
  echo "trip-location modules cannot be safely inlined because they contain </script" >&2
  exit 1
fi
trip_plan_inline="$release/trip-plan.inline.$$"
/usr/bin/awk \
  -v data="$script_dir/../trip-location-data.js" \
  -v cards="$script_dir/../trip-location-cards.js" '
  function dump(path, line) {
    while ((getline line < path) > 0) print line
    close(path)
  }
  /<script src="trip-location-data\.js([^\"]*)"><\/script>/ { print "<script>"; dump(data); next }
  /<script src="trip-location-cards\.js([^\"]*)"><\/script>/ { dump(cards); print "</script>"; next }
  { print }
' "$release/trip-plan.html" > "$trip_plan_inline"
/bin/mv -f "$trip_plan_inline" "$release/trip-plan.html"
if /usr/bin/grep -q 'src="trip-location-' "$release/trip-plan.html"; then
  echo "failed to inline both trip-location modules" >&2
  exit 1
fi
/usr/bin/grep -q 'TRIP_LOCATION_DATA' "$release/trip-plan.html"
/usr/bin/grep -q 'renderTripLocationCards' "$release/trip-plan.html"

# Keep the route map's shared data and renderer in the same HTML response too.
node --check ../trip-map-photos.js
node --check ../trip-map.js
if /usr/bin/grep -qi '</script' ../trip-map-photos.js ../trip-map.js; then
  echo "trip-map modules cannot be safely inlined because they contain </script" >&2
  exit 1
fi
trip_map_inline="$release/trip-map.inline.$$"
/usr/bin/awk \
  -v data="$script_dir/../trip-location-data.js" \
  -v photos="$script_dir/../trip-map-photos.js" \
  -v map="$script_dir/../trip-map.js" '
  function dump(path, line) {
    while ((getline line < path) > 0) print line
    close(path)
  }
  /<script src="trip-location-data\.js([^\"]*)"><\/script>/ { print "<script>"; dump(data); next }
  /<script src="trip-map-photos\.js([^\"]*)"><\/script>/ { dump(photos); next }
  /<script src="trip-map\.js([^\"]*)"><\/script>/ { dump(map); print "</script>"; next }
  { print }
' "$release/trip-map.html" > "$trip_map_inline"
/bin/mv -f "$trip_map_inline" "$release/trip-map.html"
if /usr/bin/grep -q 'src="trip-\(location-data\|map-photos\|map\)\.js' "$release/trip-map.html"; then
  echo "failed to inline all three route-map modules" >&2
  exit 1
fi
/usr/bin/grep -q 'TRIP_LOCATION_DATA' "$release/trip-map.html"
/usr/bin/grep -q 'TRIP_MAP_PHOTOS' "$release/trip-map.html"
/usr/bin/grep -q 'buildTripMap' "$release/trip-map.html"

# Publish every Trip ideas dependency as one validated response. The route
# choices, full guides and five-photo galleries must never cross release
# generations during an atomic symlink swap.
node --check ../trip-ideas-data.js
node --check ../trip-ideas.js
if /usr/bin/grep -qi '</script' ../trip-ideas-data.js ../trip-ideas.js; then
  echo "trip-ideas modules cannot be safely inlined because they contain </script" >&2
  exit 1
fi
trip_ideas_inline="$release/trip-ideas.inline.$$"
/usr/bin/awk \
  -v data="$script_dir/../trip-location-data.js" \
  -v photos="$script_dir/../trip-map-photos.js" \
  -v ideas_data="$script_dir/../trip-ideas-data.js" \
  -v ideas="$script_dir/../trip-ideas.js" '
  function dump(path, line) {
    while ((getline line < path) > 0) print line
    close(path)
  }
  /<script src="trip-location-data\.js([^"]*)"><\/script>/ { print "<script>"; dump(data); next }
  /<script src="trip-map-photos\.js([^"]*)"><\/script>/ { dump(photos); next }
  /<script src="trip-ideas-data\.js([^"]*)"><\/script>/ { dump(ideas_data); next }
  /<script src="trip-ideas\.js([^"]*)"><\/script>/ { dump(ideas); print "</script>"; next }
  { print }
' "$release/trip-ideas.html" > "$trip_ideas_inline"
/bin/mv -f "$trip_ideas_inline" "$release/trip-ideas.html"
if /usr/bin/grep -q 'src="trip-\(location-data\|map-photos\|ideas-data\|ideas\)\.js' "$release/trip-ideas.html"; then
  echo "failed to inline all four Trip ideas modules" >&2
  exit 1
fi
/usr/bin/grep -q 'TRIP_LOCATION_DATA' "$release/trip-ideas.html"
/usr/bin/grep -q 'TRIP_MAP_PHOTOS' "$release/trip-ideas.html"
/usr/bin/grep -q 'TRIP_IDEAS_DATA' "$release/trip-ideas.html"
/usr/bin/grep -q 'buildTripIdeas' "$release/trip-ideas.html"

test -f "$release/budapest-london/tripadvisor/index.html"
test -f "$release/budapest-london/tripadvisor/activity-briefs.js"
test -f "$release/budapest-london/tripadvisor/activity-pricing.js"
test -f "$release/budapest-london/tripadvisor/discover-collaboration.js"
test -f "$release/budapest-london/tripadvisor/discover-pricing.js"
test -f "$release/trip-map.html"
test -f "$release/trip-ideas.html"
grep -q 'budapest-london/tripadvisor/index.html' "$release/trip-plan.html"
grep -q 'trip-map.html' "$release/trip-plan.html"
grep -q 'trip-ideas.html' "$release/trip-plan.html"

# Validate the immutable snapshot that will be activated. The generator replaces
# briefs and pricing separately, so validating before these copies would leave a
# race where the release could contain an unvalidated mixed-revision pair.
node ../budapest-london/tripadvisor/validate_discover_groups.mjs \
  --site-root "$release/budapest-london/tripadvisor"

audit_python="${TUNDI_TRIP_PYTHON:-$HOME/workspace/scripts/stealth/.venv/bin/python}"
if [ ! -x "$audit_python" ]; then
  audit_python="$(command -v python3 || true)"
fi
if [ -z "$audit_python" ]; then
  echo "python3 is required for the strict Tripadvisor evidence audit" >&2
  exit 1
fi
"$audit_python" ../budapest-london/tripadvisor/audit_detail_context.py \
  --site-root "$release/budapest-london/tripadvisor"
"$audit_python" ../budapest-london/tripadvisor/generate_activity_research.py \
  --verify-published \
  --site-root "$release/budapest-london/tripadvisor" \
  --briefs "$release/budapest-london/tripadvisor/activity-briefs.js" \
  --pricing "$release/budapest-london/tripadvisor/activity-pricing.js"
node --test \
  ../test_trip_location_cards.mjs \
  ../test_trip_map.mjs \
  ../test_trip_ideas.mjs \
  ../budapest-london/tripadvisor/test_curated_activity_briefs.mjs \
  ../budapest-london/tripadvisor/test_discover_collaboration.mjs \
  ../budapest-london/tripadvisor/test_discover_pricing.mjs \
  ../budapest-london/tripadvisor/test_validate_discover_groups.mjs

previous=""
if [ -L public ]; then previous="$(readlink public || true)"; fi
ln -s "$release_rel" "$link_tmp"

if [ -e public ] && [ ! -L public ]; then
  legacy="$PWD/$release_root/legacy.$$"
  mv public "$legacy"
fi
/bin/mv -fh "$link_tmp" public
activated=1

# Keep the active and immediately previous immutable releases. Any request
# already resolving the old symlink can therefore finish safely during a build.
for candidate in "$PWD/$release_root"/release.*; do
  [ -e "$candidate" ] || continue
  candidate_rel="$release_root/${candidate##*/}"
  if [ "$candidate_rel" != "$release_rel" ] && [ "$candidate_rel" != "$previous" ]; then
    rm -rf "$candidate"
  fi
done
if [ -n "$legacy" ]; then rm -rf "$legacy"; legacy=""; fi

count="$(find "$release" -type f | wc -l | tr -d ' ')"
echo "built public -> $release_rel : $count files"
