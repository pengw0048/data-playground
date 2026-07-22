<!-- Thanks for the PR! Keep it focused — the smallest change that solves the problem. -->

## What & why
<!-- What does this change, and what problem does it solve? -->

## How it was tested

<!-- List make preflight and focused kernel, web, browser, migration, or manual evidence. -->
<!-- Required CI gates and path-owned specialized suites run after push. -->

## Checklist

- [ ] `make preflight` passes
- [ ] Relevant focused evidence is listed above (kernel, web, browser, migration, or manual)
- [ ] New behavior extends the nearest owning test or browser journey
- [ ] Change is focused — no unrelated scope
- [ ] Core stays provider-agnostic + offline-first (vendor-specific code lives behind a plugin seam)
