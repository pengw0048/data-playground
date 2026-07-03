# Testing — how we stop shipping bugs the user has to find

This project had a recurring failure mode: **interaction and visual bugs reached the user because
nothing tested for them.** A dropdown that jumped, nodes that stacked on top of each other, a Run
button that errored instead of being disabled, a popup that shouldn't appear, a minimap that
zoomed instead of panned and overlapped the zoom controls — none of these are catchable by the
kernel's unit tests, and all of them were found by hand.

The fix is a layered test strategy where **every class of bug has a layer that catches it**, and a
rule: *when a bug is found by hand, it becomes an assertion before it's closed.*

## The three layers

| Layer | Runs | Catches | Misses |
|---|---|---|---|
| **Kernel tests** (`make test`) — `kernel/kernel/tests/`, pytest over the real engine on real files | every push (CI), locally in ~1s | Engine correctness, lowering, out-of-core spill, plugin SPI, concurrency, the agent tool-loop, API contracts. Includes regression tests for every adversarial + code-review finding. | Anything in the browser. It never renders a pixel. |
| **E2E tests** (`make e2e`) — `web/e2e/`, Playwright driving the real UI on the real kernel | every push (CI), locally in ~4s | Interaction & visual invariants: menu positioning, node placement, disabled affordances, absence of forced popups, minimap/controls layout, autosave, the agent building real nodes. Console errors on load. | Deep engine logic (covered by kernel tests); fine-grained visual polish (color, spacing). |
| **Adversarial multi-agent review** — on demand, before a release | many independent reviewers hunt for correctness/concurrency/security defects, then adversarially verify each finding | Structural bugs behavioral tests don't reach (the shared-connection concurrency corruption, the sandbox escape, view-name collisions). | It's a review, not a gate — findings must be turned into layer-1/2 tests to stay fixed. |

## Why the interaction bugs slipped, precisely

The kernel tests asserted on **API responses** (does `/run/preview` return the right rows?). They
were green while the UI was broken, because the bugs lived entirely in the browser:

- **DOM geometry** — "the menu jumped left", "two nodes overlap", "the minimap overlaps the +
  button" are statements about bounding boxes. No test measured a bounding box.
- **Affordance state** — "Run should be disabled on an unconnected node" is about a button's
  enabled state, not about what happens when you click it.
- **Absence** — "no popup after a run", "no Save button" are assertions that something is *not*
  there. Behavioral tests check that things *are* there.

The E2E layer exists to make exactly these assertions. See `web/e2e/canvas.spec.ts`.

## The invariant catalog (what E2E asserts today)

Each maps to a bug that was previously found by hand:

1. **App loads with zero console errors** — guards the white-screen / React-#185 class.
2. **Toolbar category menu opens above the toolbar and does not jump** — records its box, waits a
   tick, asserts it didn't move; asserts it sits entirely above the toolbar.
3. **Added nodes never overlap** — adds two nodes, asserts their bounding boxes are disjoint.
4. **A node with no upstream source has Run disabled** — asserts `aria-disabled`, not an error.
5. **There is no Save button; an autosave indicator is present** — asserts absence + the indicator.
6. **Minimap and zoom controls are both visible and do not overlap** — asserts disjoint boxes.
7. **The agent builds real nodes** — opens the dock, confirms the mode indicator, runs a build,
   asserts real nodes appear (offline planner in CI, LLM when `ANTHROPIC_API_KEY` is set).

## The rule that keeps it working

**A UI bug is not closed until an E2E assertion would have caught it.** When you (or a user) find a
new interaction/visual bug:

1. Add a `test(...)` to `web/e2e/canvas.spec.ts` that fails on the current build.
2. Fix the bug.
3. Confirm the test passes.

This converts one-time manual QA into permanent coverage. The suite is cheap (~4s) so it can grow
without becoming a burden.

## Running

```bash
make test          # kernel tests (pytest)
make e2e-install   # one-time: install the Playwright browser
make e2e           # build the SPA, boot the kernel, run the browser suite
```

CI (`.github/workflows/ci.yml`) runs the kernel tests and the E2E suite on every push and pull
request, so a regression fails the build instead of reaching the user. On E2E failure, the
Playwright HTML report is uploaded as a CI artifact.

## Adding stable selectors

E2E prefers role/text selectors and a few `data-testid` hooks (`toolbar`, `autosave`,
`agent-submit`) plus React Flow's stable classes (`.react-flow__node`, `.react-flow__minimap`,
`.react-flow__controls`). When a new element needs to be targeted, add a `data-testid` rather than
depending on brittle structure or styling.
