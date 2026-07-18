#!/usr/bin/env bash
# Fast local pre-submission gate (~15s): the mechanical CI checks cheap enough to run before every
# PR. It deliberately does NOT run the full kernel suite, web tests, or PostgreSQL — CI owns those,
# and running them locally just slows you down. Run this, push, and let CI do the heavy suites.
#
#   make preflight        (or)   scripts/preflight.sh
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

if [ -t 1 ]; then RED=$'\e[31m'; GRN=$'\e[32m'; DIM=$'\e[2m'; RST=$'\e[0m'
else RED=; GRN=; DIM=; RST=; fi

RESULTS=()

step() {  # step "Name" cmd...
  local name="$1"; shift
  printf '%s→ %s%s\n' "$DIM" "$name" "$RST"
  local start; start=$(date +%s)
  if ( "$@" ); then
    RESULTS+=("${GRN}PASS${RST}  $name  ${DIM}($(( $(date +%s) - start ))s)${RST}")
  else
    RESULTS+=("${RED}FAIL${RST}  $name")
    printf '\n%s─ preflight summary ─%s\n' "$DIM" "$RST"
    printf '  %b\n' "${RESULTS[@]}"
    printf '%s✗ %s failed — fix it before opening/updating a PR.%s\n' "$RED" "$name" "$RST"
    exit 1
  fi
}

# No gitignored runtime artifact may live in the git index (a bad `git add -A` commits __result_*.lock,
# outputs/, or a *.db — CI is a clean checkout so it never catches this; a teammate's clone inherits it).
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

run_ruff() { cd kernel && uv run ruff check --config pyproject.toml . ../examples/plugins; }
run_pyright() { cd kernel && uv run basedpyright; }
run_openapi() { cd kernel && uv run python -m hub.contracts.openapi --check; }
# Graph structure, SQL policy, and — the recurring miss — migrations (fresh-baseline model<->DB parity
# plus the <=32-char revision-id guard). Fast (~10s) and catches the PostgreSQL-only class on SQLite.
run_core_contract() {
  cd kernel && uv run pytest -q \
    hub/tests/test_graph_validation.py hub/tests/test_sqlpolicy.py hub/tests/test_migrations.py
}

step "artifacts (no committed runtime junk)" check_artifacts
step "ruff" run_ruff
step "basedpyright" run_pyright
step "openapi --check" run_openapi
step "core-contract (graph + sqlpolicy + migrations)" run_core_contract

printf '\n%s─ preflight summary ─%s\n' "$DIM" "$RST"
printf '  %b\n' "${RESULTS[@]}"
printf '\n%sPreflight passed.%s %sCI runs the full kernel suite, web, and PostgreSQL.%s\n' \
  "$GRN" "$RST" "$DIM" "$RST"
