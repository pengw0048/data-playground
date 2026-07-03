.PHONY: setup run dev-kernel dev-web build test e2e e2e-install seed clean

# One-time setup: kernel deps + sample data + web deps.
setup:
	cd kernel && uv sync --extra dev && uv run python -m kernel.seed
	cd web && npm install

# The product: build the SPA, then `dataplay` serves SPA + API + engine on :8471 and opens the browser.
run: build
	cd kernel && uv run dataplay --workspace $(CURDIR)/kernel --port 8471

# Dev: kernel with autoreload (:8471) + Vite hot-reload (:5173, proxies /api).
dev-kernel:
	cd kernel && uv run uvicorn kernel.main:app --reload --port 8471

dev-web:
	cd web && npm run dev

build:
	cd web && npm run build

test:
	cd kernel && uv run pytest -q

# One-time: download the Playwright browser used by the E2E suite.
e2e-install:
	cd web && npm install && npx playwright install --with-deps chromium

# End-to-end: build the SPA, boot the kernel on a test port, drive the real UI with Playwright.
# Encodes the interaction/visual invariants (menu positioning, no node overlap, disabled Run on
# unconnected nodes, autosave, minimap layout, agent build) so regressions fail CI, not the user.
e2e: build
	cd web && npm run e2e

seed:
	cd kernel && uv run python -m kernel.seed

clean:
	rm -rf web/dist web/node_modules kernel/.venv kernel/outputs kernel/canvases kernel/data/outputs
