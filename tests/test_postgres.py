import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from conftest import additional_tenant, bind, invoke, user, workspace
from fde_server.api import create_app
from fde_server.config import Settings
from fde_server.models import Evidence, User, Workspace
from fde_server.router_simulator import create_simulator
from fde_server.runtime import Runtime

pytestmark = pytest.mark.postgres


@pytest.fixture
def postgres(tmp_path):
    url=os.environ.get('FDE_TEST_DATABASE_URL')
    if not url:
        pytest.skip('FDE_TEST_DATABASE_URL points to an isolated migrated PostgreSQL runtime-role database')
    cfg=Settings(environment='test', database_url=url, object_root=tmp_path/'objects',public_url='http://testserver')
    simulator=create_simulator(cfg,tmp_path/'router.db')
    remote=TestClient(simulator)
    runtime=Runtime(cfg,httpx.MockTransport(lambda req:remote.request(req.method,req.url.path,headers=dict(req.headers),content=req.content)))
    runtime.db.assert_runtime_role()
    yield {'rt':runtime,'app':create_app(runtime),'sim':simulator}
    runtime.close()


def test_actual_postgres_rls_composite_constraints_and_pipeline(postgres):
    unique=uuid.uuid4().hex[:8]
    a,aa=user(postgres,f'a-{unique}@example.com')
    b,bb=user(postgres,f'b-{unique}@example.com')
    wa,wb=workspace(a),workspace(b)
    db=postgres['rt'].db
    with db.session(aa['tenant_id'],aa['user']['id']) as s:
        # Deliberately omit the tenant predicate; database enforcement must still hold.
        ids=set(s.scalars(select(Workspace.id)))
        assert wa in ids and wb not in ids
        assert s.scalar(select(Workspace).where(Workspace.id==wb)) is None
        role=s.execute(text('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user')).one()
        assert not any(role)
    with db.session() as s:
        assert s.scalars(select(Workspace)).all()==[]
    with pytest.raises((DBAPIError,IntegrityError)):
        with db.session(aa['tenant_id'],aa['user']['id']) as s:
            s.add(Evidence(tenant_id=bb['tenant_id'],workspace_id=wb,text='cross-tenant write'))
    with pytest.raises((DBAPIError,IntegrityError)):
        with db.session(aa['tenant_id'],aa['user']['id']) as s:
            s.add(Evidence(tenant_id=aa['tenant_id'],workspace_id=wb,text='cross-tenant foreign key'))
    with pytest.raises(DBAPIError):
        with db.session(aa['tenant_id'],aa['user']['id']) as s:
            s.execute(text("DELETE FROM audit_events WHERE tenant_id=:t"),{'t':aa['tenant_id']})
    # Pool reuse must not carry the previous SET LOCAL context to another request.
    with db.session(bb['tenant_id'],bb['user']['id']) as s:
        assert set(s.scalars(select(Workspace.id)))=={wb}
    with db.session(aa['tenant_id'],aa['user']['id']) as s:
        s.get(User,aa['user']['id']).platform_admin=True
    with db.session(platform_admin=True) as s:
        assert {row.tenant_id for row in s.scalars(select(Workspace))} >= {aa['tenant_id'], bb['tenant_id']}
    # Global uniqueness must hold even when RLS hides the conflicting organization.
    name_a = a.get('/api/session').json()['tenants'][0]['name']
    conflict = b.patch('/api/tenant', json={'name': name_a.upper()})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()['error']['code'] == 'TENANT_NAME_EXISTS'
    assert b.get('/api/admin/audit').status_code == 403
    visible_audit = b.get('/api/audit')
    assert visible_audit.status_code == 200, visible_audit.text
    assert all(row['tenant_id'] == bb['tenant_id'] for row in visible_audit.json())
    assert all(row['display']['tenant'] for row in visible_audit.json())
    bind(a)
    assert a.post(f'/api/workspaces/{wa}/evidence',json={'text':'我们做食品批发，库存补货由采购根据供应商交期安排。'}).status_code==200
    invoke(postgres,a,wa,'summarize')
    with db.session(bb['tenant_id'],bb['user']['id']) as s:
        assert s.execute(text('SELECT id FROM pattern_artifact')).all()==[]


def test_postgres_password_login_and_cross_membership_discovery(postgres):
    from conftest import PASSWORD
    unique = uuid.uuid4().hex[:8]
    email = f'login-{unique}@example.com'
    first, registered = user(postgres, email)
    second = additional_tenant(postgres, registered['user']['id'], 'Second organization ' + unique)
    login = TestClient(postgres['app'])
    result = login.post('/api/auth/login', json={'email': email, 'password': PASSWORD})
    assert result.status_code == 200, result.text
    login.headers['X-CSRF-Token'] = result.json()['csrf']
    session = login.get('/api/session')
    assert session.status_code == 200, session.text
    assert {t['id'] for t in session.json()['tenants']} == {registered['tenant_id'], second}
    assert login.post(f'/api/tenants/{second}/switch').status_code == 200
    assert login.get('/api/session').json()['tenant_id'] == second
    assert login.post(f"/api/tenants/{registered['tenant_id']}/switch").status_code == 200
