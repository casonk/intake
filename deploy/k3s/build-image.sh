#!/usr/bin/env bash
# build-image.sh — build the extractor sandbox image and push it to the in-cluster registry (no sudo).
# Needs crew-chief's deploy/k3s/install-registry.sh once, and enable-registry-mirror.sh once as root.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CREW_CHIEF="${CREW_CHIEF_DIR:-$REPO/../crew-chief}"
IMAGE="localhost/intake-extractor:k3s"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
# The pod runs what is committed; the daemon runs whatever is in the checkout. If they differ, the parser in the
# sandbox is not the one the daemon was tested with. Build only from committed code, or say so explicitly.
if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(git -C "$REPO" status --porcelain -- intake pyproject.toml)" ]; then
  echo "intake/ or pyproject.toml has uncommitted changes; the image would not match what is committed. Commit them, or set ALLOW_DIRTY=1." >&2
  exit 1
fi
cp "$REPO/pyproject.toml" "$REPO/README.md" "$REPO/LICENSE" "$WORK/"
cp -r "$REPO/intake" "$WORK/intake"
find "$WORK" \( -name '__pycache__' -o -name '*.pyc' -o -name '*.egg-info' \) -prune -exec rm -rf {} +
cp "$HERE/Containerfile" "$WORK/Containerfile"
podman build -t "$IMAGE" -f "$WORK/Containerfile" "$WORK"
"$CREW_CHIEF/deploy/k3s/push-image.sh" "$IMAGE" intake-extractor:k3s
echo "Next: deploy/k3s/apply.sh"
