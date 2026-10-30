"""Content-free maintenance directory, refinement history, and immutable evidence bodies."""
from alembic import op
from sqlalchemy import text
from fde_server.models import RefinementVersion, TenantDirectory

revision = "0002"
down_revision = "0001"


def upgrade():
    conn = op.get_bind()
    TenantDirectory.__table__.create(conn, checkfirst=True)
    RefinementVersion.__table__.create(conn, checkfirst=True)
    if conn.dialect.name != "postgresql":
        return
    op.execute("INSERT INTO tenant_directory (id) SELECT id FROM tenants ON CONFLICT DO NOTHING")
    op.execute("ALTER TABLE refinement_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE refinement_versions FORCE ROW LEVEL SECURITY")
    exists = conn.scalar(text("SELECT count(*) FROM pg_policies WHERE tablename='refinement_versions' AND policyname='tenant_boundary'"))
    if not exists:
        op.execute("CREATE POLICY tenant_boundary ON refinement_versions USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')) WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),''))")
    op.execute("CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON refinement_versions FOR EACH ROW EXECUTE FUNCTION fde_append_only()")
    op.execute("""CREATE FUNCTION fde_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'evidence is append only'; END IF;
        IF NEW.text IS DISTINCT FROM OLD.text OR NEW.ref IS DISTINCT FROM OLD.ref
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.sensitivity IS DISTINCT FROM OLD.sensitivity
           OR NEW.metadata::text IS DISTINCT FROM OLD.metadata::text THEN
          RAISE EXCEPTION 'evidence body is immutable';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON evidence FOR EACH ROW EXECUTE FUNCTION fde_evidence_immutable()")


def downgrade():
    raise RuntimeError("Restore a verified backup or apply a forward migration")
