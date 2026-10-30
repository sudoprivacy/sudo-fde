"""Platform administrators, user providers and workflow metadata."""
from alembic import op
import sqlalchemy as sa
from fde_server.models import ModelProvider, WorkflowRun

revision = "0004"
down_revision = "0003"


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if "platform_admin" not in user_cols:
        op.add_column("users", sa.Column("platform_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
    ws_cols = {c["name"] for c in inspector.get_columns("workspaces")}
    if "objective" not in ws_cols:
        op.add_column("workspaces", sa.Column("objective", sa.Text(), nullable=False, server_default=""))
    if "next_action" not in ws_cols:
        op.add_column("workspaces", sa.Column("next_action", sa.String(40), nullable=False, server_default="collect_material"))
    ModelProvider.__table__.create(conn, checkfirst=True)
    WorkflowRun.__table__.create(conn, checkfirst=True)
    if conn.dialect.name == "postgresql":
        for name in ("model_providers", "workflow_runs"):
            op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_boundary_{name} ON {name} USING (tenant_id=nullif(current_setting('app.tenant_id',true),'') OR current_setting('app.platform_admin',true)='true') WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'') OR current_setting('app.platform_admin',true)='true')")
        tables = [t[0] for t in conn.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))]
        for name in tables:
            policies = list(conn.execute(sa.text("SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename=:t"), {"t": name}))
            for (policy,) in policies:
                if policy.startswith("tenant_"):
                    op.execute(f"DROP POLICY IF EXISTS {policy} ON {name}")
            if name == "tenants":
                op.execute("CREATE POLICY tenant_visibility ON tenants USING (id=nullif(current_setting('app.tenant_id',true),'') OR current_setting('app.platform_admin',true)='true') WITH CHECK (id=nullif(current_setting('app.tenant_id',true),'') OR current_setting('app.platform_admin',true)='true')")
            elif name not in {"login_sessions", "queue_tickets", "rate_buckets"} and "tenant_id" in {c["name"] for c in inspector.get_columns(name)}:
                op.execute(f"CREATE POLICY tenant_boundary ON {name} USING (tenant_id=nullif(current_setting('app.tenant_id',true),'') OR current_setting('app.platform_admin',true)='true') WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'') OR current_setting('app.platform_admin',true)='true')")


def downgrade():
    raise RuntimeError("Administrative and provider history cannot be safely downgraded")
