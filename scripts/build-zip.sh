#!/usr/bin/env bash
# Reproducibly package pinned Python 3.12 Cookidoo MCP for a ZIP-based Lambda.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
commit=6f7c33b1d23f8257814fd790341bc84cd88785e0
target="${root}/dist"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
command -v uv >/dev/null
command -v git >/dev/null
command -v zip >/dev/null
mkdir -p "$target"
git clone --quiet https://github.com/alexandrepa/mcp-cookidoo.git "$scratch/upstream"
git -C "$scratch/upstream" checkout --quiet --detach "$commit"
test "$(git -C "$scratch/upstream" rev-parse HEAD)" = "$commit"
(
  cd "$scratch/upstream"
  uv export --locked --no-dev --no-emit-project --format requirements-txt     --output-file "$scratch/requirements-upstream.txt"
)
mkdir -p "$scratch/package"
uv pip install --target "$scratch/package" --python-version 3.12   -r "$scratch/requirements-upstream.txt"   -r "$root/requirements-runtime.txt"
# Use upstream modules as a pinned, unmodified vendored copy within build artifacts only.
for file in server.py cookidoo_service.py guided.py schemas.py prompt.md prompt_FR.md pyproject.toml; do
  cp "$scratch/upstream/$file" "$scratch/package/$file"
done
cp -R "$root/src" "$scratch/package/src"
find "$scratch/package" -type d \( -name __pycache__ -o -name tests \) -prune -exec rm -rf {} +
find "$scratch/package" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
(
  cd "$scratch/package"
  zip -q -r -9 "$target/cookidoo-mcp.zip" .
)
uncompressed="$(du -sm "$scratch/package" | cut -f1)"
compressed="$(du -sm "$target/cookidoo-mcp.zip" | cut -f1)"
echo "ZIP compressed: ${compressed} MiB; uncompressed: ${uncompressed} MiB"
if [ "$uncompressed" -ge 250 ]; then
  echo "Lambda uncompressed code size limit exceeded (250 MB)." >&2
  exit 1
fi
