from alembic import context
from sqlalchemy import create_engine
import os
from fde_server.db import pattern_metadata
from fde_server.models import Base

url = os.environ.get("FDE_MIGRATION_DATABASE_URL") or os.environ.get("FDE_DATABASE_URL") or "sqlite:///.data/fde.db"
target_metadata = [Base.metadata, pattern_metadata()]
if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
