#!/usr/bin/env bash
# Cut a release from a laptop: bump deploy/jsonnet/VERSION, commit, tag vX.Y.Z
# and push both. The tag starts .github/workflows/release.yml, which tests,
# publishes ghcr.io/derfred/remarkling:X.Y.Z and the GitHub release. From a
# phone, run the Release workflow instead.
#
#   scripts/release.sh [patch|minor|major]   # default: patch
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
VERSION_FILE=deploy/jsonnet/VERSION
bump="${1:-patch}"

[[ "$(git rev-parse --abbrev-ref HEAD)" == main ]] || { echo "Release from main." >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Working tree not clean." >&2; exit 1; }
git pull --ff-only -q

IFS=. read -r major minor patch < "$VERSION_FILE"
case "$bump" in
  major) version="$((major + 1)).0.0" ;;
  minor) version="$major.$((minor + 1)).0" ;;
  patch) version="$major.$minor.$((patch + 1))" ;;
  *) echo "usage: $0 [patch|minor|major]" >&2; exit 1 ;;
esac

echo "$version" > "$VERSION_FILE"
git commit -q -m "Release v$version" -- "$VERSION_FILE"
git tag -a "v$version" -m "v$version"
git push -q origin main "v$version"
echo "Pushed v$version; follow it with: gh run watch \$(gh run list -w release.yml -L1 --json databaseId -q '.[0].databaseId')"
