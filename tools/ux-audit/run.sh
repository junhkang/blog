#!/usr/bin/env bash
# Usage: tools/ux-audit/run.sh <git-ref|WORKTREE> <out-name> [port] [--lighthouse]
#   예) run.sh origin/main before 1314 --lighthouse && run.sh WORKTREE after 1315 --lighthouse
#       node tools/ux-audit/compare.mjs tools/ux-audit/out/before tools/ux-audit/out/after
set -euo pipefail
REF=$1; NAME=$2; PORT=${3:-1314}; LH=${4:-}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
SRC=$ROOT
if [ "$REF" != "WORKTREE" ]; then
  SRC=$(mktemp -d)
  git -C "$ROOT" worktree add --detach "$SRC" "$REF" >/dev/null
  trap 'git -C "$ROOT" worktree remove --force "$SRC"' EXIT
fi
SITE="$HERE/out/site-$NAME"
rm -rf "$SITE" "$SITE-prod"
# 운영과 같은 옵션으로 빌드(baseURL 만 로컬). SEO 비교용으로 운영 baseURL 빌드도 따로 만든다.
hugo --source "$SRC" --minify --quiet --baseURL "http://localhost:$PORT/" -d "$SITE"
hugo --source "$SRC" --minify --quiet --baseURL "https://junhkang.com/" -d "$SITE-prod"
mkdir -p "$HERE/out/$NAME"
node "$HERE/seo-snapshot.mjs" "$SITE-prod" "$HERE/out/$NAME/seo.json"
python3 -m http.server "$PORT" -d "$SITE" >/dev/null 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null; [ "$REF" != "WORKTREE" ] && git -C "$ROOT" worktree remove --force "$SRC"' EXIT
sleep 1
node "$HERE/audit.mjs" --base "http://localhost:$PORT" --out "$HERE/out/$NAME" $LH
