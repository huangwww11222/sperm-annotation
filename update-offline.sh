#!/usr/bin/env bash
# Run from the delivered package; paths are never evaluated as shell code.
set -euo pipefail
command -v python3 >/dev/null || { echo '需要 Python 3（Ubuntu 24.04 已提供）。' >&2; exit 1; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$root/scripts/offline_update.py" "$@"
