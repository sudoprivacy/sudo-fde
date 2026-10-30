from conftest import bind, drain, invoke, user, workspace
from test_access import invite_member


def kb(client, ws=None):
    r=client.post('/api/knowledge-bases',json={'name':'供应链知识','workspace_id':ws})
    assert r.status_code==200,r.text
    return r.json()['id']


def upload(client, kb_id, text, document_id=None):
    data={'source':'采购制度','classification':'internal'}
    if document_id:
        data['document_id']=document_id
    r=client.post('/api/knowledge-bases/'+kb_id+'/documents',data=data,files={'file':('policy.md',text.encode(),'text/markdown')})
    assert r.status_code==200,r.text
    return r.json()['document']['id']


def test_rag_grants_versions_revocation_and_citations(platform):
    owner,_=user(platform,'owner@example.com')
    member,mid=invite_member(owner,platform,'member@example.com')
    stranger,_=user(platform,'other@example.com')
    ws=workspace(member)
    knowledge=kb(owner)
    doc=upload(owner,knowledge,'库存补货采购规则：根据安全库存与供应商交期安排采购，优先解决食品缺货。')
    drain(platform)
    search={'workspace_id':ws,'query':'库存补货采购'}
    assert member.post('/api/knowledge/search',json=search).json()['hits']==[]
    granted=owner.post(f'/api/knowledge-bases/{knowledge}/grants',json={'subject_type':'workspace','subject_id':ws})
    assert granted.status_code==200,granted.text
    hits=member.post('/api/knowledge/search',json=search).json()['hits']
    assert hits and hits[0]['page']==1 and hits[0]['version']==1
    chunk=hits[0]['chunk_id']
    assert stranger.get(f'/api/chunks/{chunk}?workspace_id={ws}').status_code==404
    assert stranger.get(f'/api/documents/{doc}/download').status_code==404
    assert member.get(f'/api/chunks/{chunk}?workspace_id={ws}').status_code==200
    upload(owner,knowledge,'库存补货新版规则：优先关注需求变化与供应商的配送延迟。',doc)
    # The old index becomes ineligible as soon as the version changes.
    assert member.post('/api/knowledge/search',json=search).json()['hits']==[]
    assert member.get(f'/api/chunks/{chunk}?workspace_id={ws}').status_code==410
    drain(platform)
    hits=member.post('/api/knowledge/search',json=search).json()['hits']
    assert hits[0]['version']==2
    owner.delete('/api/documents/'+doc)
    assert member.post('/api/knowledge/search',json=search).json()['hits']==[]
    assert member.get(f'/api/documents/{doc}/download?workspace_id={ws}').status_code==404


def test_cross_workspace_isolation_and_knowledge_injection(platform):
    owner,_=user(platform,'owner@example.com')
    bind(owner)
    a,b=workspace(owner,'A'),workspace(owner,'B')
    knowledge=kb(owner,a)
    upload(owner,knowledge,'我们做食品批发，客户是零售商。库存补货需要结合采购周期与安全库存。')
    drain(platform)
    assert owner.post('/api/knowledge/search',json={'workspace_id':b,'query':'库存补货'}).json()['hits']==[]
    owner.post(f'/api/workspaces/{a}/evidence',json={'text':'食品批发业务经常遇到库存补货问题'})
    invoke(platform,owner,a,'summarize')
    arts=owner.get('/api/workspaces/'+a).json()['artifacts']
    assert arts[0]['citations'][0]['title']=='policy.md'


def test_restricted_sensitive_and_malicious_files_do_not_embed(platform):
    owner,_=user(platform,'owner@example.com')
    knowledge=kb(owner)
    r=owner.post(f'/api/knowledge-bases/{knowledge}/documents',data={'classification':'restricted'},files={'file':('secret.txt',b'hello')})
    assert r.status_code==422
    r=owner.post(f'/api/knowledge-bases/{knowledge}/documents',files={'file':('virus.txt',b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE')})
    assert r.status_code==400
    upload(owner,knowledge,'联系人手机号 13812345678，必须保密。')
    drain(platform)
    documents=owner.get('/api/knowledge-bases/'+knowledge).json()['documents']
    assert documents[0]['versions'][0]['status']=='failed'
    assert documents[0]['versions'][0]['error']=='INPUT_SENSITIVE'
    assert platform['sim'].state.execution_count==0


def test_revoked_grant_immediately_blocks_chunk_read(platform):
    owner,_=user(platform,'owner@example.com')
    member,mid=invite_member(owner,platform,'member@example.com')
    ws=workspace(member)
    knowledge=kb(owner)
    upload(owner,knowledge,'采购制度：供应商选择和库存补货。')
    drain(platform)
    grant=owner.post(f'/api/knowledge-bases/{knowledge}/grants',json={'subject_type':'user','subject_id':mid}).json()
    hit=member.post('/api/knowledge/search',json={'workspace_id':ws,'query':'采购库存'}).json()['hits'][0]
    owner.delete(f'/api/knowledge-bases/{knowledge}/grants/{grant["id"]}')
    assert member.get('/api/chunks/'+hit['chunk_id']).status_code==404


def test_derived_results_and_export_cannot_bypass_source_grants(platform):
    owner,_=user(platform,'owner@example.com')
    reader,mid=invite_member(owner,platform,'reader@example.com')
    bind(owner)
    ws=workspace(owner)
    knowledge=kb(owner)
    doc=upload(owner,knowledge,'私有采购策略：食品库存补货需要参考供应商交期与内部采购预算。')
    drain(platform)
    owner.post(f'/api/workspaces/{ws}/evidence',json={'text':'我们做食品批发，希望解决库存补货问题。'})
    job=invoke(platform,owner,ws,'summarize')
    export=owner.post(f'/api/workspaces/{ws}/exports')
    assert export.status_code==200,export.text
    owner.post(f'/api/workspaces/{ws}/members',json={'user_id':mid,'role':'workspace_viewer'})
    response=reader.get('/api/workspaces/'+ws)
    assert response.status_code==200
    assert response.json()['artifacts'][0]['source_unavailable']
    assert '私有采购策略' not in response.text
    assert reader.get('/api/jobs/'+job['id']).status_code==404
    assert reader.get('/api/exports/'+export.json()['id']+'/download').status_code==404
    owner.post(f'/api/knowledge-bases/{knowledge}/grants',json={'subject_type':'user','subject_id':mid})
    assert not reader.get('/api/workspaces/'+ws).json()['artifacts'][0].get('source_unavailable')
    assert reader.get('/api/exports/'+export.json()['id']+'/download').status_code==200
    owner.delete('/api/documents/'+doc)
    assert owner.get('/api/exports/'+export.json()['id']+'/download').status_code==410


def test_numeric_document_locator_does_not_bypass_content_scanning():
    from ps.core.guard import scan
    evidence={'id':'kb_0123456789abcdef0123456789abcdef','kind':'doc_snippet',
              'ref':'document:0123456789abcdef0123456789abcdef@1#p1#c1','text':'采购补货制度'}
    assert scan({'context':{'evidence':[evidence]}})==[]
    evidence['text']='联系人手机号 13812345678'
    assert scan({'context':{'evidence':[evidence]}})


def test_router_embeddings_are_metered_with_actual_model_receipts(platform):
    from sqlalchemy import select
    from fde_server.models import Chunk, RouterCall
    platform['rt'].cfg.embedding_backend='router'
    owner,me=user(platform,'owner@example.com')
    bind(owner)
    ws=workspace(owner)
    knowledge=kb(owner,ws)
    upload(owner,knowledge,'食品库存采购补货需要安全库存和供应商交期记录。')
    drain(platform)
    owner.post(f'/api/workspaces/{ws}/evidence',json={'text':'食品批发希望改进库存采购补货。'})
    invoke(platform,owner,ws,'summarize')
    with platform['rt'].db.session(me['tenant_id']) as s:
        calls=list(s.scalars(select(RouterCall).where(RouterCall.tenant_id==me['tenant_id'])))
        chunk=s.scalar(select(Chunk).where(Chunk.tenant_id==me['tenant_id']))
        assert chunk.embedding_model==platform['rt'].cfg.embedding_model
        assert len(calls)==3  # Document embedding, query embedding, and grounded generation.
        assert all(row.status=='settled' and row.receipt['model']==row.model for row in calls)
        assert sum(row.model==platform['rt'].cfg.embedding_model for row in calls)==2
