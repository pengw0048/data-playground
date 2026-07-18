#!/usr/bin/env bash
# Local pre-submission gate that mirrors the required CI checks a PR must pass. Run it before
# opening/updating a PR: it catches the mechanical failures (lint, types, OpenAPI drift, migration
# schema drift, cross-file regressions, accidentally committed artifacts) that a green local subset
# hides but CI blocks on. PostgreSQL and web steps run only when their toolchain is present.
#
#   scripts/preflight.sh          full gate (lint + types + openapi + full pytest [+ web] [+ postgres])
#   scripts/preflight.sh --fast   quick gate (lint + types + openapi + core-contract only)
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

if [ -t 1 ]; then RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
else RED=; GRN=; YEL=; DIM=; RST=; fi

RESULTS=()
FAILED=0

step() {  # step "Name" cmd...
  local name="$1"; shift
  printf '%s→ %s%s\n' "$DIM" "$name" "$RST"
  local start; start=$(date +%s)
  if ( "$@" ); then
    RESULTS+=("${GRN}PASS${RST}  $name  ${DIM}($(( $(date +%s) - start ))s)${RST}")
  else
    RESULTS+=("${RED}FAIL${RST}  $name")
    FAILED=1
    printf '%s✗ %s failed — stopping.%s\n' "$RED" "$name" "$RST"
    summary; exit 1
  fi
}

skip() { RESULTS+=("${YEL}SKIP${RST}  $1  ${DIM}${2:-}${RST}"); }

summary() {
  printf '\n%s─ preflight summary ─%s\n' "$DIM" "$RST"
  printf '  %b\n' "${RESULTS[@]}"
}

# 1. No gitignored runtime artifact may live in the index (a bad `git add -A` commits __result_*.lock,
#    outputs/, or a *.db — CI is clean-checkout so it never catches this; a teammate's clone inherits junk).
#    git ls-files is the index: it includes staged additions and excludes staged deletions.
check_artifacts() {
  local junk
  junk=$(git ls-files | grep -E \
    '(^|/)__result_.*\.lock$|(^|/)outputs/|(^|/)canvases/|\.db$|\.db-(journal|wal|shm)$|(^|/)test-results/|\.migrate\.lock$' \
    || true )
  if [ -n "$junk" ]; then
    printf '%sruntime artifacts in the git index (should be gitignored, never committed):%s\n' "$RED" "$RST"
    printf '  %s\n' $junk
    return 1
  fi
}

# 2/3/4. Static gates — identical to the python-quality + kernel-tests CI steps.
run_ruff() { cd kernel && uv run ruff check --config pyproject.toml . ../examples/plugins; }
run_pyright() { cd kernel && uv run basedpyright; }
run_openapi() { cd kernel && uv run python -m hub.contracts.openapi --check; }

# 5. Core-contract subset (fast fail-first): graph structure, SQL policy, and — the recurring miss —
#    migrations (fresh-baseline model↔DB parity + the <=32-char revision-id guard).
run_core_contract() {
  cd kernel && uv run pytest -q \
    hub/tests/test_graph_validation.py hub/tests/test_sqlpolicy.py hub/tests/test_migrations.py
}

# 6. Full kernel suite — the net for cross-file/cross-semantic regressions a targeted subset misses.
#    Fresh data dir + swept lock artifacts so a dev's accumulated outputs can't cause false failures.
run_full_suite() {
  rm -f kernel/__result_*.lock 2>/dev/null || true
  cd kernel && DP_DATA_DIR="$(mktemp -d)" uv run pytest -q -o faulthandler_timeout=60
}

# 7. Web: typecheck + unit tests, only when its toolchain is installed.
run_web() { cd web && npm run typecheck && npm test; }

# 8. PostgreSQL migration/lifecycle contracts — the PG-only class (varchar(32) overflow, real fencing)
#    SQLite hides. Runs only when DP_TEST_DATABASE_URL points at a reachable PostgreSQL.
run_postgres() {
  cd kernel && uv run pytest -q -o faulthandler_timeout=60 \
    hub/tests/test_postgres_migration_smoke.py \
    hub/tests/test_linear_checkpoint_admission.py \
    hub/tests/test_linear_checkpoint_commit.py \
    hub/tests/test_linear_checkpoint_lifecycle.py
}

step "artifacts (no committed runtime junk)" check_artifacts
step "ruff" run_ruff
step "basedpyright" run_pyright
step "openapi --check" run_openapi
step "core-contract (graph + sqlpolicy + migrations)" run_core_contract

if [ "$FAST" = 1 ]; then
  skip "full kernel pytest" "(--fast)"
  skip "web (typecheck + vitest)" "(--fast)"
  skip "postgres contracts" "(--fast)"
  summary
  printf '\n%s--fast: static gates only. Run without --fast before submitting.%s\n' "$YEL" "$RST"
  exit 0
fi

step "full kernel pytest" run_full_suite

if [ -d web/node_modules ]; then
  step "web (typecheck + vitest)" run_web
else
  skip "web (typecheck + vitest)" "(web/node_modules absent — run: cd web && npm install)"
fi

if [ -n "${DP_TEST_DATABASE_URL:-}" ]; then
  step "postgres contracts" run_postgres
else
  skip "postgres contracts" "(set DP_TEST_DATABASE_URL — CI runs these on PostgreSQL 16)"
fi

summary
[ "$FAILED" = 0 ] && printf '\n%sPreflight passed.%s\n' "$GRN" "$RST"
exit "$FAILED"
