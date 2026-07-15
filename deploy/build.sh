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

node ../budapest-london/tripadvisor/validate_discover_groups.mjs

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
cp ../trip-plan.html ../activities.html ../saved-places.html "$release/"
cp index.html "$release/index.html"
cp -R ../images "$release/images"
cp ../budapest-london/tripadvisor/index.html "$release/budapest-london/tripadvisor/index.html"

test -f "$release/budapest-london/tripadvisor/index.html"
grep -q 'budapest-london/tripadvisor/index.html' "$release/trip-plan.html"

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
