import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from conftest import PASSWORD, user, workspace
from fde_server.models import Audit, Tenant, User
from fde_server.tenant_names import ensure_unique_tenant_names
from test_access import invite_member
from test_admin_providers_workflow import promote


def test_bootstrap_admin_has_explicit_platform_privileges(platform):
    from pathlib import Path
    from fde_server.cli import seed

    seed(platform['rt'])
    lines = Path('.data/admin-credentials.txt').read_text().splitlines()
    password = next(line.removeprefix('Password: ') for line in lines if line.startswith('Password: '))
    client = TestClient(platform['app'])
    response = client.post('/api/auth/login', json={'email': 'admin@example.com', 'password': password})
    assert response.status_code == 200
    assert client.get('/api/session').json()['platform_admin'] is True
    assert client.get('/api/admin/tenants').status_code == 200


def test_unique_organization_names_on_every_creation_and_edit_path(platform):
    control, body = user(platform, 'control@example.com', 'Acme Team')
    promote(platform, body['user']['id'])
    for name in ('Acme Team', '  acme team  ', 'ＡＣＭＥ　Ｔｅａｍ', 'Acme   Team'):
        response = control.post('/api/admin/tenants', json={'name': name})
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'TENANT_NAME_EXISTS'
    response = control.post('/api/tenants', json={'name': 'Acme Team'})
    assert response.status_code == 409
    anonymous = TestClient(platform['app'])
    response = anonymous.post('/api/auth/register', json={
        'email': 'new@example.com', 'name': 'New user', 'password': PASSWORD, 'organization': 'acme team',
    })
    assert response.status_code == 409
    with platform['rt'].db.session() as s:
        assert s.scalar(select(User).where(User.email == 'new@example.com')) is None
    created = control.post('/api/admin/tenants', json={'name': 'Beta Company'})
    assert created.status_code == 200
    tid = created.json()['id']
    assert control.patch('/api/admin/tenants/' + tid, json={'name': 'ACME TEAM'}).status_code == 409
    assert control.patch('/api/tenant', json={'name': 'Beta Company'}).status_code == 409
    same = control.patch('/api/tenant', json={'name': '  Acme Team  '})
    assert same.status_code == 200 and same.json()['name'] == 'Acme Team'
    assert control.get('/api/admin/tenants/' + tid).json()['tenant']['name'] == 'Beta Company'
    # The database, not a race-prone precheck, is the final duplicate-write guard.
    with pytest.raises(IntegrityError):
        with platform['rt'].db.session(platform_admin=True) as s:
            s.add(Tenant(name='acme team'))


@pytest.mark.parametrize('role', ['tenant_owner', 'tenant_admin', 'fde_member', 'billing_viewer'])
def test_non_platform_roles_are_confined_to_their_organizations(platform, role):
    owner, own = user(platform, 'owner@example.com', '当前组织')
    stranger, foreign = user(platform, 'stranger@example.com', '其他组织')
    wid = workspace(stranger, '其他组织工作区')
    client = owner if role == 'tenant_owner' else invite_member(owner, platform, role + '@example.com', role)[0]
    session = client.get('/api/session').json()
    assert session['platform_admin'] is False
    assert [row['id'] for row in session['tenants']] == [own['tenant_id']]
    for path in ('/api/admin/overview', '/api/admin/tenants', '/api/admin/users', '/api/admin/jobs',
                 '/api/admin/audit', '/api/admin/tenants/' + foreign['tenant_id']):
        assert client.get(path).status_code == 403, path
    assert client.post('/api/tenants', json={'name': '越权创建组织'}).status_code == 403
    assert client.patch('/api/admin/tenants/' + foreign['tenant_id'], json={'name': '越权名称'}).status_code == 403
    assert client.post('/api/tenants/' + foreign['tenant_id'] + '/switch').status_code == 404
    assert client.get('/api/workspaces/' + wid).status_code == 404
    assert wid not in [row['id'] for row in client.get('/api/workspaces').json()]
    assert client.get('/api/session').json()['tenant_id'] == own['tenant_id']
    manageable = role in {'tenant_owner', 'tenant_admin'}
    assert client.get('/api/tenant').status_code == (200 if manageable else 403)
    response = client.patch('/api/tenant', json={'name': '已更新的当前组织'})
    assert response.status_code == (200 if manageable else 403)
    if manageable:
        assert response.json()['id'] == own['tenant_id']
        assert stranger.get('/api/tenant').json()['name'] == '其他组织'
        assert client.patch('/api/tenant', json={'name': '越权名称', 'tenant_id': foreign['tenant_id']}).status_code == 422
        assert client.patch('/api/tenant', json={'name': '越权名称', 'active': False}).status_code == 422
        assert client.patch('/api/members/' + foreign['user']['id'], json={'role': 'fde_member', 'active': False}).status_code == 404


def test_platform_admin_retains_global_management_without_name_based_privilege(platform):
    control, body = user(platform, 'admin@example.com', '平台组织')
    customer, target = user(platform, 'customer@example.com', '客户组织')
    assert control.get('/api/admin/tenants').status_code == 403
    promote(platform, body['user']['id'])
    tenants = control.get('/api/admin/tenants').json()
    assert {t['id'] for t in tenants} == {body['tenant_id'], target['tenant_id']}
    assert control.patch('/api/admin/tenants/' + target['tenant_id'], json={'name': '客户组织新名称'}).status_code == 200
    assert customer.get('/api/tenant').json()['name'] == '客户组织新名称'
    assert control.post('/api/tenants', json={'name': '平台新建组织'}).status_code == 200


def test_existing_duplicate_names_are_migrated_without_merging_tenant_data(platform):
    first, a = user(platform, 'first@example.com', '数牍测试')
    second, b = user(platform, 'second@example.com', '数牍测试临时名称')
    second_ws = workspace(second, '主要业务工作区')
    with platform['rt'].db.engine.begin() as connection:
        connection.exec_driver_sql('DROP INDEX uq_tenants_name_normalized')
        connection.execute(text('UPDATE tenants SET name=:name WHERE id=:id'), {'name': '数牍测试', 'id': b['tenant_id']})
        changes = ensure_unique_tenant_names(connection)
        assert changes == [{'id': a['tenant_id'], 'before': '数牍测试', 'name': '数牍测试 · 2'}]
        assert ensure_unique_tenant_names(connection) == []
    assert first.get('/api/tenant').json()['name'] == '数牍测试 · 2'
    assert second.get('/api/tenant').json()['name'] == '数牍测试'
    assert second.get('/api/workspaces/' + second_ws).status_code == 200
    assert first.get('/api/workspaces/' + second_ws).status_code == 404
    with platform['rt'].db.session(a['tenant_id']) as s:
        record = s.scalar(select(Audit).where(Audit.action == 'organization.name_normalized'))
        assert record.details == {'previous_name': '数牍测试', 'name': '数牍测试 · 2'}
