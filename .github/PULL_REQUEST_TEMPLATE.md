<!-- Thanks for the PR! Keep it focused — the smallest change that solves the problem. -->

## What & why
<!-- What does this change, and what problem does it solve? -->

## How it was tested
<!-- kernel `make test`; if you touched web/, tsc + vitest + build. New behavior should have a test. -->

## Checklist
- [ ] `make test` passes (and `tsc` + `vitest` + `build` if `web/` changed)
- [ ] New behavior has a test (mirrors the nearest one in `kernel/hub/tests/test_kernel.py`)
- [ ] Change is focused — no unrelated scope
- [ ] Core stays provider-agnostic + offline-first (vendor-specific code lives behind a plugin seam)
