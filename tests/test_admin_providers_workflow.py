from conftest import additional_tenant, bind, user, workspace
from fde_server.models import User


def promote(platform, user_id):
    with platform['rt'].db.session() as s:
        s.get(User, user_id).platform_admin=True


def test_platform_admin_has_separate_cross_tenant_control_plane(platform):
    owner, owner_body = user(platform, 'owner@example.com', 'A 组织')
    other, _ = user(platform, 'other@example.com', 'B 组织')
    workspace(owner,'A workspace')
    ws_b=workspace(other,'B workspace')
    promote(platform, owner_body['user']['id'])
    # The platform-admin flag is checked from the user row on every request.
    overview=owner.get('/api/admin/overview')
    assert overview.status_code==200
    tenants=owner.get('/api/admin/tenants').json()
    assert len(tenants)>=2
    assert owner.get('/api/workspaces/'+ws_b).status_code==404
    assert owner.get('/admin').status_code==200


def test_platform_admin_can_manage_tenants_and_user_memberships(platform):
    admin_client, admin_body = user(platform, 'control@example.com', '平台组织')
    member_client, member_body = user(platform, 'member@example.com', '成员组织')
    promote(platform, admin_body['user']['id'])

    created = admin_client.post('/api/admin/tenants', json={
        'name': '新客户组织', 'budget_micros': 500000, 'allowed_models': ['business-reasoner'],
    })
    assert created.status_code == 200, created.text
    tenant_id = created.json()['id']
    assert created.json()['active'] is True

    updated = admin_client.patch('/api/admin/tenants/' + tenant_id, json={
        'name': '新客户组织（已配置）', 'budget_micros': 750000,
        'allowed_models': ['business-reasoner', 'deepseek-chat'],
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()['name'] == '新客户组织（已配置）'

    assigned = admin_client.post('/api/admin/users/' + member_body['user']['id'] + '/memberships', json={
        'tenant_id': tenant_id, 'role': 'fde_member',
    })
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()['tenant_id'] == tenant_id

    users = admin_client.get('/api/admin/users').json()
    managed = next(row for row in users if row['id'] == member_body['user']['id'])
    assert any(m['tenant_id'] == tenant_id and m['active'] for m in managed['memberships'])

    changed = admin_client.patch(
        f"/api/admin/users/{member_body['user']['id']}/memberships/{tenant_id}",
        json={'role': 'tenant_admin', 'active': True},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()['role'] == 'tenant_admin'
    assert admin_client.delete(
        f"/api/admin/users/{member_body['user']['id']}/memberships/{tenant_id}"
    ).status_code == 200

    owned = admin_client.post('/api/admin/tenants', json={
        'name': '需要所有者的组织', 'owner_user_id': member_body['user']['id'],
    })
    assert owned.status_code == 200, owned.text
    owned_tenant_id = owned.json()['id']
    assert admin_client.delete(
        f"/api/admin/users/{member_body['user']['id']}/memberships/{owned_tenant_id}"
    ).status_code == 409

    assert admin_client.delete('/api/admin/tenants/' + tenant_id).status_code == 200
    deleted = next(t for t in admin_client.get('/api/admin/tenants').json() if t['id'] == tenant_id)
    assert deleted['active'] is False
    assert admin_client.patch('/api/admin/tenants/' + tenant_id, json={'active': True}).status_code == 200


def test_personal_provider_is_encrypted_and_never_returned(platform):
    client, body=user(platform,'provider@example.com')
    response=client.post('/api/models/providers',json={
        'provider_id':'personal-router','display_name':'我的 Router','kind':'sudorouter',
        'api_base':'https://router.example/v1','protocol':'openai','api_key':'sk-secret-value-123',
        'models':['deepseek-chat','qwen-max'],'default_model':'deepseek-chat'})
    assert response.status_code==200,response.text
    payload=response.json()
    assert payload['api_key_configured'] is True and 'sk-secret' not in response.text
    with platform['rt'].db.session(body['tenant_id']) as s:
        from sqlalchemy import select
        from fde_server.models import ModelProvider
        row=s.scalar(select(ModelProvider).where(ModelProvider.owner_id==body['user']['id']))
        assert row.encrypted_api_key!='sk-secret-value-123'
    session=client.get('/api/session').json()
    assert session['providers'][0]['provider_id']=='personal-router'
    assert client.patch('/api/models/providers/personal-router',json={'api_key':'sk-secret-value-456','models':['deepseek-chat']}).status_code==200
    assert client.delete('/api/models/providers/personal-router').status_code==200
    assert client.get('/api/models/providers').json()==[]


def test_provider_catalog_can_be_fetched_before_saving(platform, monkeypatch):
    client, _ = user(platform, 'catalog@example.com')

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'data': [{'id': 'gpt-6-luna'}, {'id': 'deepseek-chat'}, {'id': 'gpt-6-luna'}]}

    class Client:
        def __init__(self, **kwargs):
            self.headers = kwargs['headers']

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, path):
            assert path == 'models'
            assert self.headers['Authorization'] == 'Bearer sk-catalog-secret'
            return Response()

    monkeypatch.setattr('fde_server.providers.httpx.Client', Client)
    response = client.post('/api/models/providers/catalog', json={
        'api_base': 'https://router.example/v1', 'protocol': 'openai', 'api_key': 'sk-catalog-secret',
    })
    assert response.status_code == 200, response.text
    assert response.json() == {'models': ['gpt-6-luna', 'deepseek-chat']}
    assert 'sk-catalog-secret' not in response.text


def test_first_business_material_can_start_ai_workflow_and_workflow_state_is_explicit(platform):
    client,_=user(platform,'workflow@example.com')
    bind(client)
    ws=workspace(client,'业务流程验收')
    response=client.post('/api/workspaces/'+ws+'/evidence',json={'text':'我们做食品批发，客户是餐厅。','start_ai':True})
    assert response.status_code==200 and response.json()['next_job']
    state=client.get('/api/workspaces/'+ws+'/workflow').json()
    assert state['current']=='intro' and state['active_job']['capability']=='summarize'
    # Worker completion changes the next product action rather than only the visual phase label.
    from conftest import drain
    drain(platform)
    state=client.get('/api/workspaces/'+ws+'/workflow').json()
    assert state['current']=='challenge' and state['next_action']=='answer_challenge'


def test_tenant_and_user_restore_preserve_individual_membership_revocation(platform):
    from test_access import invite_member
    admin_client, body = user(platform, 'lifecycle-admin@example.com')
    promote(platform, body['user']['id'])
    _, member_id = invite_member(admin_client, platform, 'disabled-member@example.com')
    tenant_id = body['tenant_id']
    path = f'/api/admin/users/{member_id}/memberships/{tenant_id}'
    assert admin_client.patch(path, json={'active': False}).status_code == 200
    assert admin_client.post(f'/api/admin/tenants/{tenant_id}/suspend').status_code == 200
    assert admin_client.post(f'/api/admin/tenants/{tenant_id}/restore').status_code == 200
    assert admin_client.patch(f'/api/admin/users/{member_id}/status', json={'role':'fde_member','active':False}).status_code == 200
    assert admin_client.patch(f'/api/admin/users/{member_id}/status', json={'role':'fde_member','active':True}).status_code == 200
    rows = admin_client.get('/api/admin/users').json()
    member = next(m for u in rows if u['id'] == member_id for m in u['memberships'] if m['tenant_id'] == tenant_id)
    assert member['active'] is False
    # Upsert must not be a loophole for demoting the only owner.
    assert admin_client.post(f"/api/admin/users/{body['user']['id']}/memberships", json={'tenant_id':tenant_id,'role':'fde_member'}).status_code == 409


def test_login_skips_suspended_organization(platform):
    from conftest import PASSWORD
    from fastapi.testclient import TestClient
    client, body = user(platform, 'multi-tenant-login@example.com')
    second = additional_tenant(platform, body['user']['id'], 'Active second tenant')
    from fde_server.models import Tenant
    with platform['rt'].db.session(body['tenant_id']) as s:
        s.get(Tenant, body['tenant_id']).active = False
    logged = TestClient(platform['app']).post('/api/auth/login', json={'email':'multi-tenant-login@example.com','password':PASSWORD})
    assert logged.status_code == 200, logged.text
    assert logged.json()['tenant_id'] == second
