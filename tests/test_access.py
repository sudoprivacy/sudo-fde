import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from conftest import PASSWORD, additional_tenant, bind, user, workspace
from fde_server.models import PasswordReset
from fde_server.security import digest


def invite_member(owner, platform, email, role='fde_member'):
    result = owner.post('/api/invitations', json={"email": email, "role": role})
    assert result.status_code == 200, result.text
    link = parse_qs(urlparse(result.json()['development_link']).fragment)
    client = TestClient(platform['app'])
    r = client.post('/api/invitations/accept', json={"token": link['token'][0], "tenant_id": link['tenant'][0],
                     "name": email.split('@')[0], "password": PASSWORD})
    assert r.status_code == 200, r.text
    client.headers['X-CSRF-Token'] = r.json()['csrf']
    return client, r.json()['user']['id']


def test_private_workspace_cross_tenant_and_editor_revocation(platform):
    owner, _ = user(platform, 'owner@example.com')
    stranger, _ = user(platform, 'stranger@example.com')
    member, member_id = invite_member(owner, platform, 'member@example.com')
    ws = workspace(owner)
    assert stranger.get('/api/workspaces/'+ws).status_code == 404
    assert member.get('/api/workspaces').json() == []
    assert member.get('/api/workspaces/'+ws).status_code == 404
    r = owner.post(f'/api/workspaces/{ws}/members', json={"user_id": member_id, "role": "workspace_viewer"})
    assert r.status_code == 200
    assert member.get('/api/workspaces/'+ws).status_code == 200
    assert member.post(f'/api/workspaces/{ws}/evidence', json={"text": "我们经营食品批发"}).status_code == 403
    owner.post(f'/api/workspaces/{ws}/members', json={"user_id": member_id, "role": "workspace_editor"})
    assert member.post(f'/api/workspaces/{ws}/evidence', json={"text": "我们经营食品批发"}).status_code == 200
    assert member.delete('/api/workspaces/'+ws).status_code == 403
    owner.delete(f'/api/workspaces/{ws}/members/{member_id}')
    assert member.get('/api/workspaces/'+ws).status_code == 404


def test_member_disable_immediate_and_billing_no_body(platform):
    owner, _ = user(platform, 'owner@example.com')
    member, mid = invite_member(owner, platform, 'member@example.com')
    viewer, vid = invite_member(owner, platform, 'billing@example.com', 'billing_viewer')
    ws = workspace(member)
    bind(owner)
    assert viewer.get('/api/billing').status_code == 200
    assert viewer.get('/api/workspaces/'+ws).status_code == 404
    assert viewer.post('/api/workspaces', json={"name": "forbidden"}).status_code == 403
    owner.patch('/api/members/'+mid, json={"role": "fde_member", "active": False})
    assert member.get('/api/workspaces/'+ws).status_code == 403


def test_csrf_origin_cookies_and_login_throttling(platform):
    c, _ = user(platform, 'owner@example.com')
    assert c.post('/api/workspaces', json={"name": "blocked"}, headers={'X-CSRF-Token':'wrong'}).status_code == 403
    assert c.post('/api/workspaces', json={"name": "blocked"}, headers={'Origin':'https://evil.invalid'}).status_code == 403
    c.headers.pop('X-CSRF-Token')
    assert c.post('/api/workspaces', json={"name": "blocked"}).status_code == 403
    anon = TestClient(platform['app'])
    # Account setup has already performed one successful login in this rate window.
    for _ in range(9):
        assert anon.post('/api/auth/login', json={"email": "owner@example.com", "password": "wrong"}).status_code == 401
    assert anon.post('/api/auth/login', json={"email": "owner@example.com", "password": PASSWORD}).status_code == 429


def test_tenant_switch_and_cross_scope_exports(platform):
    a, first = user(platform, 'owner@example.com')
    ws = workspace(a)
    exported = a.post(f'/api/workspaces/{ws}/exports').json()
    second = additional_tenant(platform, first['user']['id'], '第二组织')
    assert a.post(f'/api/tenants/{second}/switch').status_code == 200
    assert a.get('/api/workspaces/'+ws).status_code == 404
    assert a.get('/api/exports/'+exported['id']+'/download').status_code == 404
    assert a.post(f'/api/tenants/{first["tenant_id"]}/switch').status_code == 200
    assert a.get('/api/exports/'+exported['id']+'/download').status_code == 200
    a.delete('/api/workspaces/'+ws)
    assert a.get('/api/exports/'+exported['id']+'/download').status_code == 404
    assert a.post(f'/api/workspaces/{ws}/restore').status_code == 200


def test_password_reset_revokes_sessions_and_token_is_single_use(platform):
    from datetime import timedelta
    from fde_server.models import now
    c, body = user(platform, 'owner@example.com')
    token = secrets.token_urlsafe(32)
    with platform['rt'].db.session() as s:
        s.add(PasswordReset(user_id=body['user']['id'], token_hash=digest(token), expires_at=now()+timedelta(minutes=10)))
    anon = TestClient(platform['app'])
    assert anon.post('/api/auth/password-reset/confirm', json={"token":token,"password":"New-test-password-2026"}).status_code == 200
    assert c.get('/api/session').status_code == 401
    assert anon.post('/api/auth/password-reset/confirm', json={"token":token,"password":"New-test-password-2026"}).status_code == 400


def test_owner_and_admin_role_escalation_guard(platform):
    owner, me = user(platform, 'owner@example.com')
    admin, _ = invite_member(owner, platform, 'admin@example.com', 'tenant_admin')
    assert admin.post('/api/invitations', json={"email":"other@example.com","role":"tenant_admin"}).status_code == 403
    assert admin.patch('/api/members/'+me['user']['id'], json={"role":"fde_member","active":False}).status_code == 409


@pytest.mark.parametrize('path', ['/api/workspaces/unknown','/api/knowledge-bases/unknown','/api/jobs/unknown','/api/documents/unknown/download','/api/exports/unknown/download','/api/chunks/unknown'])
def test_no_resource_without_auth(platform, path):
    assert TestClient(platform['app']).get(path).status_code == 401


def test_audit_filter_and_pagination_keep_tenant_isolation(platform):
    from fde_server.models import Audit
    owner, me = user(platform, 'audit-pages@example.com')
    other, _ = user(platform, 'audit-other@example.com')
    with platform['rt'].db.session(me['tenant_id']) as s:
        s.add_all(Audit(tenant_id=me['tenant_id'],user_id=me['user']['id'],action='document.uploaded',resource_id=f'event-{i}') for i in range(205))
        s.add(Audit(tenant_id=me['tenant_id'],user_id=me['user']['id'],action='workspace.viewed',resource_id='background-poll'))
    first=owner.get('/api/audit?exclude_reads=true').json()
    second=owner.get('/api/audit?exclude_reads=true&offset=200').json()
    assert len(first)==200 and second
    assert not ({x['id'] for x in first}&{x['id'] for x in second})
    assert all(x['action']!='workspace.viewed' for x in first+second)
    assert not any(x['resource_id']=='background-poll' for x in first+second)
    assert all(x['tenant_id']!=me['tenant_id'] for x in other.get('/api/audit').json())
