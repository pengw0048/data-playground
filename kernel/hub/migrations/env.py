"""Alembic environment — invoked programmatically from metadb.init_db (command.upgrade/stamp).

The DB url always comes from hub settings (DP_DATABASE_URL), so migrations target the same DB
the app uses. SQLite gets batch mode so ALTER TABLE (add column, etc.) works.
"""

from alembic import context
from sqlalchemy import create_engine

from hub.metadb import Base
from hub.settings import settings

target_metadata = Base.metadata


def _url() -> str:
    return settings.database_url


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    url = _url()
    kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    connectable = create_engine(url, **kw)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=url.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()
