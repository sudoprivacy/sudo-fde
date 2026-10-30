
from conftest import bind, drain, invoke, user, workspace
from test_workflow import DDL


def test_revision_apply_undo_and_stale_proposal(platform):
    c,_=user(platform,'owner@example.com')
    bind(c)
    ws=workspace(c)
    c.post(f'/api/workspaces/{ws}/evidence',json={'text':'我们做食品批发，客户是餐厅。订单库存由 ERP 管理。'})
    invoke(platform,c,ws,'summarize')
    ev=c.post(f'/api/workspaces/{ws}/evidence',json={'text':'客户是零售商'}).json()
    invoke(platform,c,ws,'revise',{'instruction_evidence_id':ev['id']})
    arts=c.get('/api/workspaces/'+ws).json()['artifacts']
    revision=next(a for a in arts if a['kind']=='revise')
    summary=next(a for a in arts if a['kind']=='summarize')
    before=summary['body']
    assert revision['body']['changes']
    chosen=[revision['body']['changes'][0]['artifact_id']]
    r=c.post(f'/api/artifacts/{revision["id"]}/revision/apply',json={'change_ids':chosen})
    assert r.status_code==200,r.text
    new=next(a for a in c.get('/api/workspaces/'+ws).json()['artifacts'] if a['id']==summary['id'])
    assert new['body']!=before and new['version']==summary['version']+1
    assert c.post(f'/api/artifacts/{revision["id"]}/revision/apply',json={'change_ids':chosen}).status_code==409
    assert c.post('/api/revisions/'+r.json()['id']+'/undo').status_code==200
    restored=next(a for a in c.get('/api/workspaces/'+ws).json()['artifacts'] if a['id']==summary['id'])
    assert restored['body']==before and restored['version']==new['version']+1


def test_field_bindings_require_catalog_and_optimistic_versions(platform):
    c,_=user(platform,'owner@example.com')
    bind(c)
    ws=workspace(c)
    c.post(f'/api/workspaces/{ws}/evidence',json={'text':'食品批发，仓库根据库存安排采购补货，订单由第三方物流发货。'})
    c.post(f'/api/workspaces/{ws}/catalogs',files={'file':('demo.sql',DDL.encode())})
    invoke(platform,c,ws,'recommend')
    art=next(a for a in c.get('/api/workspaces/'+ws).json()['artifacts'] if a['kind']=='process')
    c.post('/api/artifacts/'+art['id']+'/decisions',json={'action':'accept','version':art['version']})
    drain(platform)
    slot=c.get(f'/api/workspaces/{ws}/refinement').json()['slots'][0]
    body={'slot_id':slot['id'],'action':'bind','fields':['Products.InventedColumn'],'version':0}
    assert c.post(f'/api/workspaces/{ws}/refinement',json=body).status_code==400
    body['fields']=['Products.UnitsInStock']
    r=c.post(f'/api/workspaces/{ws}/refinement',json=body)
    assert r.status_code==200,r.text
    assert c.post(f'/api/workspaces/{ws}/refinement',json=body).status_code==409
    body.update(action='offline',fields=[],version=1)
    assert c.post(f'/api/workspaces/{ws}/refinement',json=body).status_code==200


def test_viewer_can_download_existing_export_but_cannot_create_one(platform):
    from test_access import invite_member
    owner,_=user(platform,'owner@example.com')
    viewer,mid=invite_member(owner,platform,'viewer@example.com')
    ws=workspace(owner)
    export=owner.post(f'/api/workspaces/{ws}/exports').json()
    owner.post(f'/api/workspaces/{ws}/members',json={'user_id':mid,'role':'workspace_viewer'})
    assert viewer.get('/api/exports/'+export['id']+'/download').status_code==200
    assert viewer.post(f'/api/workspaces/{ws}/exports').status_code==403


def test_export_uses_authoritative_edited_statement_and_identity(platform):
    import io
    import zipfile
    import yaml
    from fde_server.models import Artifact
    client, me = user(platform, 'export-edit@example.com')
    ws = workspace(client)
    with platform['rt'].db.session(me['tenant_id']) as s:
        art=Artifact(tenant_id=me['tenant_id'],workspace_id=ws,job_id='manual',kind='process',status='confirmed',
                     body={'statement':'人工确认后的流程','object':{'id':'untrusted-object-id','statement':'旧流程文字','name':'补货','steps':['审核','下单'],'roles':['采购']}})
        s.add(art)
        s.flush()
        aid=art.id
    exported=client.post(f'/api/workspaces/{ws}/exports').json()
    downloaded=client.get('/api/exports/'+exported['id']+'/download')
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as bundle:
        process=yaml.safe_load(bundle.read('business.yaml'))['processes'][0]
        assert process['statement']=='人工确认后的流程' and process['id']==aid
