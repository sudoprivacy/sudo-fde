import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from conftest import PASSWORD, user
from fde_server.config import Settings
from fde_server.models import Tenant, User
from test_access import invite_member
from test_admin_providers_workflow import promote


@pytest.mark.parametrize('caller', ['anonymous', 'tenant_owner', 'platform_admin'])
def test_legacy_registration_cannot_create_accounts_or_organizations(platform, monkeypatch, caller):
    owner, body = user(platform, 'owner@example.com', '已有组织')
    if caller == 'platform_admin':
        promote(platform, body['user']['id'])
    client = TestClient(platform['app']) if caller == 'anonymous' else owner
    # Existing deployment settings must not reopen the retired public endpoint.
    monkeypatch.setenv('FDE_ALLOW_REGISTRATION', 'true')
    monkeypatch.setenv('FDE_PLATFORM_ADMIN_EMAILS', '["new@example.com"]')
    platform['rt'].cfg = Settings(**platform['rt'].cfg.model_dump())
    with platform['rt'].db.session() as s:
        before = [s.scalar(select(func.count()).select_from(model)) for model in (Tenant, User)]
    response = client.post('/api/auth/register', json={
        'email': 'new@example.com', 'name': 'New user', 'password': PASSWORD, 'organization': '未经授权的新组织',
    })
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'REGISTRATION_DISABLED'
    assert not response.cookies
    with platform['rt'].db.session() as s:
        assert [s.scalar(select(func.count()).select_from(model)) for model in (Tenant, User)] == before
    if caller != 'anonymous':
        assert client.get('/api/session').json()['user']['id'] == body['user']['id']
    if caller == 'platform_admin':
        created = client.post('/api/admin/tenants', json={'name': '管理员创建的组织'})
        assert created.status_code == 200, created.text
    else:
        assert client.post('/api/admin/tenants', json={'name': '无权创建'}).status_code in (401, 403)


def test_invited_users_can_join_and_sign_in_without_creating_a_tenant(platform):
    owner, body = user(platform, 'owner@example.com', '受邀加入的组织')
    member, member_id = invite_member(owner, platform, 'invited@example.com')
    assert member.get('/api/session').json()['tenant_id'] == body['tenant_id']
    with platform['rt'].db.session() as s:
        assert s.scalar(select(func.count()).select_from(Tenant)) == 1
    login = TestClient(platform['app'])
    response = login.post('/api/auth/login', json={'email': 'invited@example.com', 'password': PASSWORD})
    assert response.status_code == 200
    assert response.json()['user']['id'] == member_id
    assert login.get('/api/session').json()['platform_admin'] is False
