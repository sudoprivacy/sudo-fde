"""Require globally unique normalized organization names."""

from alembic import op
from sqlalchemy import text

from fde_server.tenant_names import ensure_unique_tenant_names

revision = "0006"
down_revision = "0005"


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == 'postgresql':
        connection.execute(text("SELECT set_config('app.platform_admin', 'true', true)"))
    ensure_unique_tenant_names(connection)


def downgrade():
    raise RuntimeError('Organization name history must be preserved; use a forward migration')
