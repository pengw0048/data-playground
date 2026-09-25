#!/usr/bin/env bash
# Operator/release drill; creates only disposable, independent SeaweedFS instances.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec uv run --project "$ROOT/kernel" python "$ROOT/scripts/verify_versioned_object_backup.py"
