#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
action="${1:-build}"
if [ "$#" -gt 0 ]; then shift; fi
mkdir -p .build/clang-module-cache .build/swift-module-cache
export CLANG_MODULE_CACHE_PATH="$project_root/.build/clang-module-cache"
export SWIFT_MODULECACHE_PATH="$project_root/.build/swift-module-cache"
exec swift "$action" --disable-sandbox --cache-path "$project_root/.build/cache" --config-path "$project_root/.build/config" --security-path "$project_root/.build/security" "$@"
