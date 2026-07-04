# Point the metadata DB at a throwaway temp file so tests never touch the dev DB.
# Runs at import (before kernel.settings is imported), so settings picks it up.
import os
import tempfile

os.environ.setdefault("DP_DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="dp-test-"), "test.db"))

# Ensure the sample datasets (events/images/movies) exist before the catalog is built. They're
# gitignored (regenerated via `make seed`), so a fresh checkout / CI has an empty data dir and the
# many tests that read tbl_events/tbl_images would fail with KeyError. seed_if_empty is a no-op
# when the dir already has data (the normal local case), so this only fires on a clean tree.
from kernel.seed import seed_if_empty  # noqa: E402
from kernel.settings import settings  # noqa: E402

seed_if_empty(settings.data_dir)
