"""Persist export source authorization dependencies."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"


def upgrade():
    conn = op.get_bind()
    columns = {col["name"] for col in sa.inspect(conn).get_columns("exports")}
    if "source_refs" not in columns:
        op.add_column("exports", sa.Column("source_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade():
    raise RuntimeError("Export authorization metadata cannot be safely dropped")
