#!/usr/bin/env bash
# Copy the pybabytracker library into the integration and record the commit it came from.
#
# Usage: scripts/vendor_library.sh <path-to-pybabytracker>
#
# The library is bundled while it is private. Set VENDOR_TARGET to copy somewhere else (tests do).
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <path-to-pybabytracker>" >&2
  exit 2
fi

library="$1"
source_dir="$library/src/pybabytracker"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${VENDOR_TARGET:-$repo_root/custom_components/baby_tracker/pybabytracker}"

if [ ! -d "$source_dir" ]; then
  echo "error: $source_dir not found; expected a checkout with src/pybabytracker" >&2
  exit 1
fi

commit="$(git -C "$library" rev-parse HEAD)"
if [ -n "$(git -C "$library" status --porcelain -- src/pybabytracker)" ]; then
  echo "warning: $source_dir has uncommitted changes; VENDORED_FROM names the last commit only" >&2
fi

rm -rf "$target"
mkdir -p "$target"
cp -R "$source_dir/." "$target/"
find "$target" -name __pycache__ -type d -prune -exec rm -rf {} +
printf '%s\n' "$commit" > "$target/VENDORED_FROM"
echo "Vendored pybabytracker at $commit into $target"
