from contextlib import contextmanager

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def pattern_metadata():
    from ps.db import Base as PatternBase

    meta = PatternBase.metadata
    if not meta.info.get("saas_constraints"):
        for name in ("pattern_workspace", "pattern_artifact"):
            meta.tables[name].append_constraint(UniqueConstraint("tenant_id", "id"))
        for table in meta.tables.values():
            for col, parent in (("workspace_id", "pattern_workspace"), ("artifact_id", "pattern_artifact")):
                if col in table.c:
                    table.append_constraint(
                        ForeignKeyConstraint(["tenant_id", col], [f"{parent}.tenant_id", f"{parent}.id"])
                    )
        meta.info["saas_constraints"] = True
    return meta


def set_scope(session, tenant_id=None, user_id=None, platform_admin=False):
    session.info.update(tenant_id=tenant_id or "", user_id=user_id or "", platform_admin=platform_admin)
    if session.bind.dialect.name == "postgresql" and session.in_transaction():
        for key in ("tenant_id", "user_id"):
            session.execute(
                text("SELECT set_config(:key, :value, true)"),
                {"key": f"app.{key}", "value": session.info[key]},
            )
        session.execute(
            text("SELECT set_config('app.platform_admin', :value, true)"),
            {"value": "true" if session.info.get("platform_admin") else "false"},
        )


class ScopedSession(Session):
    pass


@event.listens_for(ScopedSession, "after_begin")
def begin_scope(session, transaction, connection):
    if connection.dialect.name == "postgresql":
        for key in ("tenant_id", "user_id"):
            connection.execute(
                text("SELECT set_config(:key, :value, true)"),
                {"key": f"app.{key}", "value": session.info.get(key, "")},
            )
        connection.execute(
            text("SELECT set_config('app.platform_admin', :value, true)"),
            {"value": "true" if session.info.get("platform_admin") else "false"},
        )


class Database:
    def __init__(self, url):
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            from pathlib import Path

            path = url.removeprefix("sqlite:///")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, **kwargs)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_pragmas(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        self.factory = sessionmaker(self.engine, class_=ScopedSession, expire_on_commit=False)

    @contextmanager
    def session(self, tenant_id=None, user_id=None, platform_admin=False):
        with self.factory(
            info={"tenant_id": tenant_id or "", "user_id": user_id or "", "platform_admin": platform_admin}
        ) as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def create_dev(self):
        if self.engine.dialect.name != "sqlite":
            raise RuntimeError("Use Alembic for PostgreSQL")
        Base.metadata.create_all(self.engine)
        pattern_metadata().create_all(self.engine)
        with self.engine.begin() as conn:
            from .tenant_names import ensure_unique_tenant_names

            ensure_unique_tenant_names(conn)
            table_columns = {
                name: {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({name})")}
                for name in ("users", "workspaces", "exports")
            }
            for column, ddl in (("platform_admin", "BOOLEAN NOT NULL DEFAULT 0"),):
                if column not in table_columns["users"]:
                    conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
            for column, ddl in (("objective", "TEXT NOT NULL DEFAULT ''"), ("next_action", "VARCHAR(40) NOT NULL DEFAULT 'collect_material'")):
                if column not in table_columns["workspaces"]:
                    conn.exec_driver_sql(f"ALTER TABLE workspaces ADD COLUMN {column} {ddl}")
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(exports)")}
            if "source_refs" not in columns:
                conn.exec_driver_sql("ALTER TABLE exports ADD COLUMN source_refs JSON NOT NULL DEFAULT '[]'")
            for table in ("decisions", "audit_events", "security_events", "refinement_versions"):
                for operation in ("UPDATE", "DELETE"):
                    conn.exec_driver_sql(
                        f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} "
                        f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append only'); END"
                    )
            conn.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS immutable_evidence_body BEFORE UPDATE ON evidence "
                "WHEN NEW.text IS NOT OLD.text OR NEW.ref IS NOT OLD.ref OR NEW.kind IS NOT OLD.kind "
                "OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.workspace_id IS NOT OLD.workspace_id "
                "OR NEW.metadata IS NOT OLD.metadata OR NEW.sensitivity IS NOT OLD.sensitivity "
                "BEGIN SELECT RAISE(ABORT, 'evidence body is immutable'); END"
            )
            conn.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete BEFORE DELETE ON evidence "
                "BEGIN SELECT RAISE(ABORT, 'evidence is append only'); END"
            )

    def assert_runtime_role(self):
        if self.engine.dialect.name != "postgresql":
            raise RuntimeError("Production database must be PostgreSQL")
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one()
            if any(row):
                raise RuntimeError("Runtime database role must not be superuser or BYPASSRLS")
            expected = {
                t.name
                for t in [*Base.metadata.tables.values(), *pattern_metadata().tables.values()]
                if "tenant_id" in t.c and t.name not in {"login_sessions", "queue_tickets"}
            } | {"tenants"}
            protected = set(
                conn.scalars(
                    text(
                        "SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity AND relnamespace='public'::regnamespace"
                    )
                )
            )
            if not expected.issubset(protected):
                raise RuntimeError("Forced RLS is missing for private tables")
            revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "0006":
                raise RuntimeError("Run the current Alembic migrations before starting production")
