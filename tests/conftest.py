import httpx
import pytest
from fastapi.testclient import TestClient

from fde_server.api import create_app
from fde_server.cli import initialize
from fde_server.config import Settings
from fde_server.jobs import run_once
from fde_server.pattern import execute
from fde_server.router_simulator import create_simulator
from fde_server.runtime import Runtime

PASSWORD = "Testing-password-2026!"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Settings(environment="test", database_url=f"sqlite:///{tmp_path/'app.db'}", object_root=tmp_path/'objects',
                   public_url="http://testserver", max_concurrent_jobs=20)
    simulator = create_simulator(cfg, tmp_path/'router.db')
    router_web = TestClient(simulator)
    def transport(request):
        return router_web.request(request.method, request.url.path, content=request.content,
                                  headers=dict(request.headers))
    runtime = Runtime(cfg, httpx.MockTransport(transport))
    initialize(runtime)
    app = create_app(runtime)
    result = {"rt": runtime, "sim": simulator, "app": app, "tmp": tmp_path}
    yield result
    runtime.close()
    router_web.close()


def user(platform, email, organization=None):
    """Provision isolated test accounts, then exercise the real password-login flow."""
    from fde_server.models import Membership, Tenant, TenantDirectory, User, uid
    from fde_server.security import PASSWORDS

    tenant_id, user_id = uid(), uid()
    with platform['rt'].db.session(tenant_id, user_id) as s:
        s.add(User(id=user_id, email=email, name=email.split('@')[0], password_hash=PASSWORDS.hash(PASSWORD)))
        s.add(Tenant(id=tenant_id, name=organization or ('测试组织 · ' + email)))
        s.flush()
        s.add(TenantDirectory(id=tenant_id))
        s.add(Membership(tenant_id=tenant_id, user_id=user_id, role='tenant_owner'))
    client = TestClient(platform["app"])
    response = client.post('/api/auth/login', json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    return client, response.json()


def additional_tenant(platform, user_id, name):
    """Provision another explicit membership without granting global rights to the test user."""
    from fde_server.models import Membership, Tenant, TenantDirectory, uid
    tenant_id = uid()
    with platform['rt'].db.session(tenant_id, user_id) as s:
        s.add(Tenant(id=tenant_id, name=name))
        s.flush()
        s.add(TenantDirectory(id=tenant_id))
        s.add(Membership(tenant_id=tenant_id, user_id=user_id, role='tenant_owner'))
    return tenant_id


def workspace(client, name="食品批发业务"):
    response = client.post('/api/workspaces', json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()['id']


def bind(client):
    r = client.post('/api/billing/binding', json={"authorization_code": "demo-local"})
    assert r.status_code == 200, r.text


def drain(platform, limit=20):
    for _ in range(limit):
        if not run_once(platform['rt'], dispatch=execute):
            break


def invoke(platform, client, ws, capability, params=None, call_id=None):
    import uuid
    r = client.post(f'/api/workspaces/{ws}/agent/{capability}', json={"call_id": call_id or uuid.uuid4().hex,
                     "params": params or {}})
    assert r.status_code == 200, r.text
    job_id = r.json()['id']
    drain(platform)
    job = client.get('/api/jobs/'+job_id).json()
    assert job['status'] == 'succeeded', job
    return job
