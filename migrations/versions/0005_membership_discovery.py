"""Restore self-service membership discovery under platform-admin RLS."""
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # Additional SELECT-only policies allow login and organization switching.
    # Mutation policies remain bound to the tenant or platform-admin context.
    op.execute("""CREATE POLICY membership_self_discovery ON memberships FOR SELECT
        USING (user_id = nullif(current_setting('app.user_id', true), ''))""")
    op.execute("""CREATE POLICY tenant_self_discovery ON tenants FOR SELECT
        USING (id IN (SELECT tenant_id FROM memberships
            WHERE user_id = nullif(current_setting('app.user_id', true), '') AND active))""")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY tenant_self_discovery ON tenants")
        op.execute("DROP POLICY membership_self_discovery ON memberships")
