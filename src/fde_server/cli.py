import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path


def initialize(runtime):
    from .db import pattern_metadata
    if runtime.cfg.production:
        raise RuntimeError("Use Alembic with a separate migration role in production")
    runtime.db.create_dev()
    pattern_metadata().create_all(runtime.pattern_db.engine)
    from sqlalchemy import select
    from .models import Tenant, TenantDirectory
    with runtime.db.session() as s:
        for tenant_id in s.scalars(select(Tenant.id)):
            if not s.get(TenantDirectory, tenant_id):
                s.add(TenantDirectory(id=tenant_id))


def seed(runtime):
    from sqlalchemy import select
    from .models import Membership, Tenant, TenantDirectory, User, uid
    from .security import PASSWORDS
    if runtime.cfg.production:
        raise RuntimeError("Seed accounts are prohibited in production")
    with runtime.db.session() as s:
        user = s.scalar(select(User).where(User.email == "admin@example.com"))
        if user:
            return
    password = secrets.token_urlsafe(14)
    tid, user_id = uid(), uid()
    with runtime.db.session(tid, user_id) as s:
        s.add(User(id=user_id, email="admin@example.com", name="平台管理员",
                   password_hash=PASSWORDS.hash(password), platform_admin=True))
        s.add(Tenant(id=tid, name="企业业务组织", allowed_models=["business-reasoner"], router_account="account-"+tid))
        s.flush()
        s.add(TenantDirectory(id=tid))
        s.add(Membership(tenant_id=tid, user_id=user_id, role="tenant_owner"))
    runtime.router.request("POST", "bindings/exchange", {"authorization_code": "demo-local", "tenant_id": tid, "user_id": user_id}, tid)
    Path(".data").mkdir(exist_ok=True)
    path = Path(".data/admin-credentials.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(f"Email: admin@example.com\nPassword: {password}\n")
    print("Administrator credentials saved to .data/admin-credentials.txt", flush=True)


def main():
    parser = argparse.ArgumentParser(prog="fde")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, port in [("serve", 8800), ("pattern", 8801), ("router-simulator", 8802)]:
        p = sub.add_parser(command)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", default=port, type=int)
    server = sub.add_parser("server")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", default=8801, type=int)
    sub.add_parser("init-dev")
    sub.add_parser("seed-demo")
    sub.add_parser("worker")
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("--once", action="store_true")
    sub.add_parser("dev")
    rec = sub.add_parser("reconcile")
    rec.add_argument("--tenant", required=True)
    promote = sub.add_parser("promote-admin")
    promote.add_argument("--email", required=True)
    purge = sub.add_parser("purge-documents")
    purge.add_argument("--tenant", required=True)
    args = parser.parse_args()
    from .config import Settings
    from .runtime import Runtime
    cfg = Settings(environment=(os.environ.get("FDE_ENVIRONMENT") or ("development" if args.command in {"dev", "router-simulator"} else "production")))
    if args.command == "router-simulator":
        import uvicorn
        from .router_simulator import create_simulator
        uvicorn.run(create_simulator(cfg), host=args.host, port=args.port)
        return
    runtime = Runtime(cfg)
    if args.command in {"serve", "pattern"}:
        import uvicorn
        from .api import create_app
        from .pattern import create_pattern_app
        uvicorn.run(create_app(runtime) if args.command == "serve" else create_pattern_app(runtime),
                    host=args.host, port=args.port, proxy_headers=cfg.trust_proxy,
                    forwarded_allow_ips=cfg.forwarded_allow_ips)
    elif args.command == "server":
        # Keep the internal Pattern API, queue consumer and maintenance loop in
        # one process. They retain separate Runtime instances for connection
        # pools and lifecycle ownership, but share a single container process.
        import threading

        import uvicorn

        from .jobs import run_once
        from .maintenance import run_maintenance
        from .pattern import create_pattern_app

        worker_runtime = Runtime(cfg)
        maintenance_runtime = Runtime(cfg)
        stop = threading.Event()
        pattern_server = uvicorn.Server(
            uvicorn.Config(
                create_pattern_app(runtime),
                host=args.host,
                port=args.port,
                proxy_headers=cfg.trust_proxy,
                forwarded_allow_ips=cfg.forwarded_allow_ips,
            )
        )

        def stop_all(*_):
            stop.set()
            pattern_server.should_exit = True

        def run_pattern():
            pattern_server.run()

        def run_worker():
            while not stop.is_set():
                if not run_once(worker_runtime):
                    stop.wait(0.5)

        def run_maintenance_loop():
            while not stop.is_set():
                print(run_maintenance(maintenance_runtime), flush=True)
                stop.wait(60)

        signal.signal(signal.SIGTERM, stop_all)
        signal.signal(signal.SIGINT, stop_all)
        threads = [
            threading.Thread(target=run_pattern, name="pattern", daemon=True),
            threading.Thread(target=run_worker, name="worker", daemon=True),
            threading.Thread(target=run_maintenance_loop, name="maintenance", daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            while not stop.wait(1):
                pass
        finally:
            stop_all()
            for thread in threads:
                thread.join(timeout=10)
            worker_runtime.close()
            maintenance_runtime.close()
    elif args.command == "init-dev":
        initialize(runtime)
    elif args.command == "seed-demo":
        seed(runtime)
    elif args.command == "worker":
        from .jobs import run_once
        running = True
        def stop(*_):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while running:
            if not run_once(runtime):
                time.sleep(0.5)
    elif args.command == "reconcile":
        from .router import reconcile
        print(reconcile(runtime, args.tenant))
    elif args.command == "promote-admin":
        from sqlalchemy import select
        from .models import User
        with runtime.db.session() as s:
            user=s.scalar(select(User).where(User.email==args.email.lower()))
            if not user:
                raise RuntimeError("User not found")
            user.platform_admin=True
            print(f"Platform administrator granted to {user.email}")
    elif args.command == "maintenance":
        from .maintenance import run_maintenance
        while True:
            print(run_maintenance(runtime), flush=True)
            if args.once:
                break
            time.sleep(60)
    elif args.command == "purge-documents":
        from sqlalchemy import delete, or_, select
        from .models import Chunk, Document, DocumentVersion, now
        with runtime.db.session(args.tenant) as s:
            docs = list(s.scalars(select(Document).where(Document.tenant_id == args.tenant,
                                  or_(Document.active.is_(False), Document.expires_at <= now()))))
            for doc in docs:
                doc.active = False
                for version in s.scalars(select(DocumentVersion).where(DocumentVersion.tenant_id == args.tenant, DocumentVersion.document_id == doc.id)):
                    runtime.objects.delete(version.object_key, args.tenant)
                    version.status = "purged"
                s.execute(delete(Chunk).where(Chunk.tenant_id == args.tenant, Chunk.document_id == doc.id))
            print({"purged_documents": len(docs)})
    elif args.command == "dev":
        if cfg.production:
            raise RuntimeError("Development launcher cannot run production")
        initialize(runtime)
        processes = []
        child_env = {**os.environ, "FDE_ENVIRONMENT": "development"}
        try:
            processes.append(subprocess.Popen([sys.executable, "-m", "fde_server.cli", "router-simulator"], env=child_env))
            for _ in range(50):
                try:
                    runtime.router.capabilities()
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Router simulator did not become ready")
            seed(runtime)
            for command in ("pattern", "worker", "maintenance", "serve"):
                processes.append(subprocess.Popen([sys.executable, "-m", "fde_server.cli", command], env=child_env))
            print("FDE SaaS: http://127.0.0.1:8800 — local verification services enabled", flush=True)
            while all(p.poll() is None for p in processes):
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
    runtime.close()


if __name__ == "__main__":
    main()
