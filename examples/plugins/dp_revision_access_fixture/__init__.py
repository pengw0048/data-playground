"""Reference adapter using the public revision-access failure seam."""

from hub.sdk import raise_revision_access_error_from_os


class RevisionAccessFixtureAdapter:
    name = "revision-access-fixture"

    def matches(self, uri: str) -> bool:
        return str(uri).startswith("revision-access-fixture://")

    def _open_exact(self, _uri: str, _revision_id: str):
        raise RuntimeError("the revision-access fixture requires an injected provider failure")

    def revision_detail(self, uri: str, revision_id: str, *, preview_limit: int):
        del preview_limit
        try:
            return self._open_exact(uri, revision_id)
        except Exception as exc:
            raise_revision_access_error_from_os(exc)


def register(reg) -> None:
    reg.add_adapter(RevisionAccessFixtureAdapter())
