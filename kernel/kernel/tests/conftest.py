# Point the metadata DB at a throwaway temp file so tests never touch the dev DB.
# Runs at import (before kernel.settings is imported), so settings picks it up.
import os
import tempfile

os.environ.setdefault("DP_DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="dp-test-"), "test.db"))
