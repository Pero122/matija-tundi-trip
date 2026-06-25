#!/bin/sh
# Regenerate the PartyKit static bundle (public/) from the canonical site files.
# Run this before `npx partykit deploy`. public/ is gitignored (generated).
set -e
cd "$(dirname "$0")"
rm -rf public
mkdir public
cp ../trip-plan.html ../activities.html ../saved-places.html public/
cp index.html public/index.html
cp -R ../images public/images
echo "built public/ : $(find public -type f | wc -l | tr -d ' ') files"
