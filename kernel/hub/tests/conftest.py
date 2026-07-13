# Point the metadata DB at a throwaway temp file so tests never touch the dev DB.
# Runs at import (before hub.settings is imported), so settings picks it up.
import os
import tempfile

# FORCE a throwaway metadata DB (override, not setdefault) so pytest NEVER writes a real/exported
# DP_DATABASE_URL — a dev running the suite with their prod/dev DB exported would otherwise have the
# tests destructively mutate it. Opt into a specific test DB (e.g. Postgres, F54) via DP_TEST_DATABASE_URL.
os.environ["DP_DATABASE_URL"] = os.environ.get("DP_TEST_DATABASE_URL") or (
    "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="dp-test-"), "test.db"))

# The product default execution is now the per-canvas kernel (a spawned process). Force the suite to
# the in-process runner so tests don't spawn a kernel per run (fast + deterministic); the kernel path
# keeps its own dedicated tests (which set execution back to "kernel"). Set before hub.settings imports.
os.environ.setdefault("DP_EXECUTION", "local-out-of-core")

# Ensure the sample datasets (events/images/movies) exist before the catalog is built. They're
# gitignored (regenerated via `make seed`), so a fresh checkout / CI has an empty data dir and the
# many tests that read tbl_events/tbl_images would fail with KeyError. seed_if_empty is a no-op
# when the dir already has data (the normal local case), so this only fires on a clean tree.
from hub.seed import seed_if_empty  # noqa: E402
from hub.settings import settings  # noqa: E402

seed_if_empty(settings.data_dir)

# A deliberately supplied Postgres test database follows the production contract too: the test
# harness is the one-shot migrator, while importing hub.main later only performs the strict head check.
if os.environ.get("DP_TEST_DATABASE_URL") and not settings.database_url.startswith("sqlite"):
    from hub import metadb  # noqa: E402
    metadb.migrate_db()
