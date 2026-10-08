#!/usr/bin/env bash
# check_versions.sh — Assert every file that carries the version agrees.
#
# Four files carry it. release.yml rewrites nodejs/package.json and
# python/pyproject.toml from the git tag, but neither Zig source, so those two
# only stayed in step if someone remembered to run release.sh. src/mcp.zig
# reported "1.0.1" in its MCP serverInfo handshake for forty-seven releases
# because nothing in the codebase reads that field.
#
# With an argument, also asserts the version matches it, so a release workflow
# can check the tag against the tree:
#   ./scripts/check_versions.sh            # the four files agree
#   ./scripts/check_versions.sh v2.9.0     # ...and equal this tag
#
# Exit code: 0 = consistent, 1 = otherwise.

set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
note() { printf '  %-26s %s\n' "$1" "$2"; }

main_v=$(sed -n 's/^const version = "\(.*\)";$/\1/p' src/main.zig | head -1)
mcp_v=$(grep -o '\\"version\\":\\"[0-9][^\\]*' src/mcp.zig | tail -1 | sed 's/.*\\"//')
pkg_v=$(sed -n 's/^  "version": "\(.*\)",$/\1/p' nodejs/package.json | head -1)
py_v=$(sed -n 's/^version = "\(.*\)"$/\1/p' python/pyproject.toml | head -1)

note "src/main.zig" "${main_v:-NOT FOUND}"
note "src/mcp.zig" "${mcp_v:-NOT FOUND}"
note "nodejs/package.json" "${pkg_v:-NOT FOUND}"
note "python/pyproject.toml" "${py_v:-NOT FOUND}"

for v in "$main_v" "$mcp_v" "$pkg_v" "$py_v"; do
  [[ -n "$v" ]] || { echo "  a version string could not be parsed"; exit 1; }
done

if [[ "$main_v" != "$mcp_v" || "$main_v" != "$pkg_v" || "$main_v" != "$py_v" ]]; then
  echo "  versions disagree"
  fail=1
fi

if [[ $# -ge 1 ]]; then
  want="${1#v}"
  if [[ "$main_v" != "$want" ]]; then
    echo "  tree is $main_v but the tag says $want"
    fail=1
  fi
fi

[[ $fail -eq 0 ]] && echo "  consistent: $main_v"
exit $fail
