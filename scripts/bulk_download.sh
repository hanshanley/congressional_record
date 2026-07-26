#!/usr/bin/env bash
# Download whole-day CREC package zips from the www.govinfo.gov content endpoint
# (NO api key, NOT subject to the api.govinfo.gov 36k/hr limit). 12-way parallel.
set -u
cd "$(dirname "$0")/.."
BULK=data/bulk
mkdir -p "$BULK"
LIST="${1:-$BULK/_pkglist.txt}"

dl() {
  local pkg="$1" out="data/bulk/$1.zip"
  # skip if already a valid zip
  if [ -s "$out" ] && python3 -c "import zipfile,sys; sys.exit(0 if zipfile.is_zipfile('$out') else 1)" 2>/dev/null; then
    return 0
  fi
  curl -sL --retry 5 --retry-delay 2 -o "$out" "https://www.govinfo.gov/content/pkg/${pkg}.zip"
  # validate; remove if bad
  if ! python3 -c "import zipfile,sys; sys.exit(0 if zipfile.is_zipfile('$out') else 1)" 2>/dev/null; then
    rm -f "$out"; echo "BAD $pkg" >> data/bulk/_errors.txt
  fi
}
export -f dl

cat "$LIST" | xargs -P 12 -I{} bash -c 'dl "{}"'
echo "BULK_DOWNLOAD_DONE $(date)"
