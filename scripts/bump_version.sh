#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 0.2.0"
    exit 1
fi

VERSION="$1"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Update ui/src-tauri/tauri.conf.json
sed -i '' "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "$REPO_ROOT/ui/src-tauri/tauri.conf.json"
echo "Updated ui/src-tauri/tauri.conf.json -> ${VERSION}"

# Update ui/src-tauri/Cargo.toml (only the package version, not dependency
# versions). No brace group: BSD sed rejects `{s/…/…/}` on one line.
sed -i '' "/^\[package\]/,/^\[/ s/^version = \"[^\"]*\"/version = \"${VERSION}\"/" "$REPO_ROOT/ui/src-tauri/Cargo.toml"
echo "Updated ui/src-tauri/Cargo.toml -> ${VERSION}"

# Update ui/package.json
sed -i '' "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "$REPO_ROOT/ui/package.json"
echo "Updated ui/package.json -> ${VERSION}"

# Update pyproject.toml (was previously missed — drifted 0.1.0 vs app 0.2.0).
sed -i '' "s/^version = \"[^\"]*\"/version = \"${VERSION}\"/" "$REPO_ROOT/pyproject.toml"
echo "Updated pyproject.toml -> ${VERSION}"

# Keep Cargo.lock's own package entry in sync so post-bump builds don't
# leave a dirty lockfile.
sed -i '' "/^name = \"context-recall\"/,/^version/ s/^version = \"[^\"]*\"/version = \"${VERSION}\"/" "$REPO_ROOT/ui/src-tauri/Cargo.lock"
echo "Updated ui/src-tauri/Cargo.lock (context-recall) -> ${VERSION}"

echo ""
echo "Version bumped to ${VERSION}"
echo "Don't forget to commit and tag: git tag v${VERSION}"
