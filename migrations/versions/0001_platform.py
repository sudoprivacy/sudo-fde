"""Initial tenant-isolated platform and upstream Pattern tables."""
from alembic import op
from fde_server.db import pattern_metadata
from fde_server.models import Base

revision = "0001"
down_revision = None


def upgrade():
    conn = op.get_bind()
    Base.metadata.create_all(conn)
    meta = pattern_metadata()
    meta.create_all(conn)
    if conn.dialect.name != "postgresql":
        return
    tid = "nullif(current_setting('app.tenant_id', true), '')"
    user = "nullif(current_setting('app.user_id', true), '')"
    for table in [*Base.metadata.tables.values(), *meta.tables.values()]:
        if "tenant_id" not in table.c or table.name in {"login_sessions", "queue_tickets"}:
            continue
        name = table.name
        op.execute(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{name}" FORCE ROW LEVEL SECURITY')
        if name == "memberships":
            op.execute(f'CREATE POLICY tenant_read ON "{name}" FOR SELECT USING (tenant_id={tid} OR user_id={user})')
            for cmd in ("INSERT", "UPDATE", "DELETE"):
                predicate = f"WITH CHECK (tenant_id={tid})" if cmd == "INSERT" else f"USING (tenant_id={tid})"
                if cmd == "UPDATE":
                    predicate += f" WITH CHECK (tenant_id={tid})"
                op.execute(f'CREATE POLICY tenant_{cmd.lower()} ON "{name}" FOR {cmd} {predicate}')
        else:
            op.execute(f'CREATE POLICY tenant_boundary ON "{name}" USING (tenant_id={tid}) WITH CHECK (tenant_id={tid})')
    op.execute('ALTER TABLE tenants ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE tenants FORCE ROW LEVEL SECURITY')
    op.execute(f"CREATE POLICY tenant_visibility ON tenants FOR SELECT USING (id={tid} OR id IN (SELECT tenant_id FROM memberships WHERE user_id={user} AND active))")
    for cmd in ("INSERT", "UPDATE", "DELETE"):
        predicate = f"WITH CHECK (id={tid})" if cmd == "INSERT" else f"USING (id={tid})"
        if cmd == "UPDATE":
            predicate += f" WITH CHECK (id={tid})"
        op.execute(f'CREATE POLICY tenant_{cmd.lower()} ON tenants FOR {cmd} {predicate}')
    op.execute("CREATE FUNCTION fde_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append only'; END $$")
    for name in ("decisions", "audit_events", "security_events"):
        op.execute(f"CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION fde_append_only()")
    op.execute("CREATE INDEX ix_chunk_search ON knowledge_chunks USING gin(to_tsvector('simple', text))")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup or apply a forward migration")
