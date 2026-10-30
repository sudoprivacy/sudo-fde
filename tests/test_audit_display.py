import json

from sqlalchemy import select

from conftest import user, workspace
from fde_server.models import Audit, Job, ModelProvider, User
from test_access import invite_member
from test_admin_providers_workflow import promote


def find_event(client, action, path='/api/audit'):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return next(row for row in response.json() if row['action'] == action)


def test_platform_audit_names_and_nested_change_descriptions(platform):
    control, admin = user(platform, 'control@example.com', '平台运营')
    customer, target = user(platform, 'customer@example.com', '客户原组织')
    promote(platform, admin['user']['id'])
    created = control.post('/api/admin/tenants', json={'name': '华谷食配'})
    assert created.status_code == 200
    tenant_id = created.json()['id']
    with platform['rt'].db.session() as s:
        s.get(User, admin['user']['id']).name = '平台管理员'
        s.get(User, target['user']['id']).name = '张经理'
    response = control.post(f'/api/admin/users/{target["user"]["id"]}/memberships', json={
        'tenant_id': tenant_id, 'role': 'fde_member',
    })
    assert response.status_code == 200, response.text
    row = find_event(control, 'platform.membership_assigned', '/api/admin/audit')
    display = row['display']
    assert display['tenant'] == '华谷食配'
    assert display['actor'] == {'name': '平台管理员', 'email': 'control@example.com'}
    assert display['action'] == '关联用户到租户'
    assert display['resource']['name'] == '张经理'
    assert display['details'] == [
        {'label': '关联租户', 'value': '华谷食配'}, {'label': '角色', 'value': 'FDE 成员'},
    ]
    assert row['details']['role'] == 'fde_member'
    assert control.patch('/api/admin/tenants/' + tenant_id, json={
        'name': '华谷食配新名称', 'active': False, 'allowed_models': ['chat-primary'],
    }).status_code == 200
    display = find_event(control, 'platform.tenant_updated', '/api/admin/audit')['display']
    assert display['resource']['name'] == '华谷食配新名称'
    assert {'label': '状态', 'value': '停用'} in display['details']
    assert {'label': '允许使用的模型', 'value': 'chat-primary'} in display['details']
    assert 'changes' not in json.dumps(display, ensure_ascii=False)
    assert customer.get('/api/admin/audit').status_code == 403


def test_audit_enrichment_keeps_tenant_and_role_boundaries(platform):
    owner, own = user(platform, 'owner@example.com', '本组织')
    other, foreign = user(platform, 'foreign@example.com', '其他私密组织')
    member, _ = invite_member(owner, platform, 'member@example.com')
    foreign_ws = workspace(other, '其他组织的私密工作区')
    with platform['rt'].db.session(own['tenant_id']) as s:
        s.add(Audit(tenant_id=own['tenant_id'], user_id=own['user']['id'], action='workspace.renamed',
                    resource_id=foreign_ws, workspace_id=foreign_ws))
        s.add(Audit(tenant_id=own['tenant_id'], user_id=own['user']['id'], action='member.changed',
                    resource_id=foreign['user']['id'], details={'tenant_id': foreign['tenant_id'], 'role': 'fde_member'}))
    response = owner.get('/api/audit')
    assert response.status_code == 200
    visible = json.dumps([row['display'] for row in response.json()], ensure_ascii=False)
    assert '其他私密组织' not in visible and '其他组织的私密工作区' not in visible
    assert 'foreign@example.com' not in visible and '名称不可用' in visible
    assert all(row['tenant_id'] == own['tenant_id'] for row in response.json())
    assert member.get('/api/audit').status_code == 403
    assert owner.get('/api/admin/audit').status_code == 403


def test_archived_resources_tasks_and_provider_credentials(platform):
    owner, own = user(platform, 'owner@example.com', '食品业务')
    wid = workspace(owner, '补货分析')
    exported = owner.post(f'/api/workspaces/{wid}/exports')
    assert exported.status_code == 200, exported.text
    assert find_event(owner, 'export.created')['display']['resource'] == {
        'type': '交付包', 'name': exported.json()['name'], 'workspace': '补货分析',
    }
    assert owner.delete('/api/workspaces/' + wid).status_code == 200
    assert find_event(owner, 'workspace.deleted')['display']['resource']['name'] == '补货分析'
    with platform['rt'].db.session(own['tenant_id']) as s:
        job = Job(tenant_id=own['tenant_id'], workspace_id=wid, user_id=own['user']['id'],
                  call_id='audit-test', kind='pattern', capability='report', payload={}, input_hash='hash')
        provider = ModelProvider(tenant_id=own['tenant_id'], owner_id=own['user']['id'], provider_id='personal',
                                 display_name='个人模型网关', api_base='https://provider.example/v1',
                                 protocol='openai', encrypted_api_key='secret-never-returned', active=False)
        s.add_all([job, provider])
        s.flush()
        s.add_all([
            Audit(tenant_id=own['tenant_id'], user_id=own['user']['id'], action='job.failed', resource_id=job.id,
                  workspace_id=wid, details={'code': 'PROVIDER_TIMEOUT'}),
            Audit(tenant_id=own['tenant_id'], user_id=own['user']['id'], action='model_provider.deleted',
                  resource_id=provider.id, details={'provider_id': 'personal'}),
        ])
    row = find_event(owner, 'job.failed')
    assert row['display']['resource'] == {'name': '业务理解任务', 'type': '任务', 'workspace': '补货分析'}
    assert row['display']['details'] == [{'label': '原因', 'value': '模型生成超时'}]
    provider_row = find_event(owner, 'model_provider.deleted')
    assert provider_row['display']['resource']['name'] == '个人模型网关'
    assert 'secret-never-returned' not in json.dumps(provider_row)


def test_missing_resources_unknown_events_and_original_audit_preserved(platform):
    owner, own = user(platform, 'owner@example.com')
    with platform['rt'].db.session(own['tenant_id']) as s:
        events = [
            Audit(tenant_id=own['tenant_id'], action='knowledge.grant_revoked', resource_id='removed-grant'),
            Audit(tenant_id=own['tenant_id'], action='future.event', resource_id='old-resource',
                  details={'unrecognized': {'future_flag': True}}),
        ]
        s.add_all(events)
        s.flush()
        originals = {row.id: (row.action, row.details.copy()) for row in events}
    missing = find_event(owner, 'knowledge.grant_revoked')
    assert missing['display']['actor']['name'] == '系统自动执行'
    assert missing['display']['resource']['name'] == '知识库授权（名称不可用）'
    future = find_event(owner, 'future.event')
    assert future['display']['action'] == '其他操作（详见技术详情）'
    assert 'future_flag' not in json.dumps(future['display'])
    assert future['details'] == {'unrecognized': {'future_flag': True}}
    with platform['rt'].db.session(own['tenant_id']) as s:
        for row in s.scalars(select(Audit).where(Audit.id.in_(originals))):
            assert (row.action, row.details) == originals[row.id]
