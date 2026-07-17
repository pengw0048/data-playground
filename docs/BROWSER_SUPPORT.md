# Browser and viewport support

Data Playground's workbench is a **desktop-first** graph editor: a collapsible navigation rail, a
collapsible inspector, an open canvas, and floating data/run panels. It is not designed for phones
or tablets. The shell compacts for browse and inspect at 1024px, but full canvas authoring there is
exploratory rather than a release guarantee.

## Minimum viewport

| | |
| --- | --- |
| **Minimum supported viewport** | **1280×720** CSS pixels |

That size is owned by the shared constant in [`web/support/min-viewport.ts`](../web/support/min-viewport.ts)
(`MIN_VIEWPORT`). CI's Playwright min-viewport project imports the same constant and fails if the
docs and the tested size drift.

The rail and Inspector remember their expanded/collapsed state across ordinary refreshes. Fit View
uses the remaining canvas after those panels change. Mobile canvas authoring remains out of scope.

## Browsers

| Browser | Status |
| --- | --- |
| **Chromium / Google Chrome** | **Tested** — GitHub Actions installs Chromium and runs the Playwright e2e suite, including a project pinned to the minimum viewport above |
| Firefox | Expected to work; not tested in CI |
| Safari / WebKit | Expected to work; not tested in CI |
| Mobile browsers | Not supported |

Only Chromium may be called "tested", because [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
installs and runs Chromium only.

## Input model

| Input | Status |
| --- | --- |
| **Keyboard** | Supported — focusable controls, menus, and editable fields |
| **Mouse / trackpad** | Supported — primary interaction for the canvas, rail, inspector, and panels |
| Touch / stylus | Not supported |

Graph editing assumes pointer hover, precise drag-to-connect, and multi-panel mouse workflows. A
tablet or phone layout would need a separate interaction design, not a scaled-down desktop shell.
