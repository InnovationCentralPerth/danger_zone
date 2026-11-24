#!/usr/bin/env bash
# Publish the PUBLIC poster (and nothing else) to the gh-pages branch for GitHub Pages.
#
#   scripts/publish_public_poster.sh            # build, check for leaks, publish
#   scripts/publish_public_poster.sh --dry-run  # build + check only, publish nothing
#
# The branch holds one generated file (index.html) in one commit, force-pushed each time, so the
# poster never enters the main code history and the repo does not grow with every update.
# One-time setup in GitHub: Settings -> Pages -> Deploy from a branch -> gh-pages, / (root).
# The private dashboard (:8001) and MCP bridge are not involved; they are never published.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
[ -f .venv/bin/activate ] && source .venv/bin/activate
python poster/build_poster.py >/dev/null
SRC="poster/dist/safety-sentry-poster-public.html"
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

# Last line of defence: refuse to publish anything that looks private.
bad=0
for pat in 'xenterm' 'ts\.net' '/home/marrabld' '/models/' 'localhost:' '"demo_url"' \
           '100\.[0-9]+\.[0-9]+\.[0-9]+' 'Don.t say it is on-premise' "const BUILD = 'internal'"; do
  if grep -qE "$pat" "$SRC"; then echo "REFUSING: public poster contains /$pat/" >&2; bad=1; fi
done
grep -q "const BUILD = 'public';" "$SRC" || { echo "REFUSING: not a public build" >&2; bad=1; }
[ "$bad" -eq 0 ] || exit 1
echo "leak check passed ($(du -h "$SRC" | cut -f1))"
[ "${1:-}" = "--dry-run" ] && { echo "dry run: nothing published"; exit 0; }

REMOTE="$(git remote get-url origin)"
AUTHOR_NAME="$(git log -1 --format=%an)"
AUTHOR_EMAIL="$(git log -1 --format=%ae)"
FROM="$(git rev-parse --short HEAD)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp "$SRC" "$TMP/index.html"
touch "$TMP/.nojekyll"
cat > "$TMP/README.md" <<'EOF'
Published copy of the Safety Sentry interactive poster (generated; do not edit).
Rebuild and republish with `scripts/publish_public_poster.sh` from the main repository.
EOF
cd "$TMP"
git init -q -b gh-pages
git add -A
git -c user.name="$AUTHOR_NAME" -c user.email="$AUTHOR_EMAIL" commit -q \
  -m "Publish public poster ($(date -u '+%F %H:%MZ'), built from $FROM)"
git push -q --force "$REMOTE" HEAD:gh-pages
echo "published to the gh-pages branch of $REMOTE"
OWNER="$(echo "$REMOTE" | sed -E 's#.*[:/]([^/]+)/[^/]+$#\1#' | tr 'A-Z' 'a-z')"
NAME="$(echo "$REMOTE" | sed -E 's#.*/([^/]+)$#\1#; s#\.git$##')"
echo "address once Pages is enabled: https://${OWNER}.github.io/${NAME}/"
