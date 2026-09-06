#!/usr/bin/env bash
#
# Fill the local R2 that `wrangler pages dev` reads: it starts empty, so
# without this the viewer opens on "The bucket has no published regions yet."
#
# A `--dest` directory is a bucket root — FileStore writes each object at the
# key it would use in R2 — so the key is just the path under it. Cache-Control
# is set to what a real publish sets, because the functions pass it through
# rather than inventing it.
#
#   uv run shachen-ops run --region swus --dest ./localbucket   # get some data
#   web/seed-local.sh && cd web && wrangler pages dev
#
# State lives in web/.wrangler/; delete it to empty the bucket again.

set -euo pipefail
cd "$(dirname "$0")"

BUCKET=$(sed -n 's/.*"bucket_name"[^"]*"\([^"]*\)".*/\1/p' wrangler.jsonc | head -1)
SOURCE=../localbucket/shachen-ops

find "$SOURCE" -type f ! -name .DS_Store | sort | while read -r file; do
  case "$file" in
    */latest.json) cache="public, max-age=60" ;;
    */index/*.json) cache="public, max-age=300" ;;
    */meta.json) cache="public, max-age=3600" ;;
    *) cache="public, max-age=31536000, immutable" ;;
  esac
  wrangler r2 object put "$BUCKET/shachen-ops/${file#"$SOURCE"/}" \
    --file "$file" --cache-control "$cache" --local >/dev/null
  echo "  $BUCKET/shachen-ops/${file#"$SOURCE"/}"
done
