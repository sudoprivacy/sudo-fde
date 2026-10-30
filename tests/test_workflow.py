import io
import json
import zipfile
from xml.etree import ElementTree

import yaml
from rdflib import Graph

from conftest import bind, drain, invoke, user, workspace

DDL = '''CREATE TABLE Products(ProductID INTEGER PRIMARY KEY, ProductName TEXT, UnitsInStock INTEGER, ReorderLevel INTEGER);
CREATE TABLE Orders(OrderID INTEGER PRIMARY KEY, OrderDate DATE, ShippedDate DATE, RequiredDate DATE);
CREATE TABLE OrderDetails(OrderID INTEGER REFERENCES Orders(OrderID), ProductID INTEGER REFERENCES Products(ProductID), Quantity INTEGER, UnitPrice DECIMAL, PRIMARY KEY(OrderID,ProductID));'''


def test_end_to_end_business_understanding_and_exports(platform):
    c, _ = user(platform, 'owner@example.com')
    bind(c)
    ws = workspace(c)
    r = c.post(f'/api/workspaces/{ws}/evidence', json={"text":"我们做食品批发，客户是餐厅，靠进销差价赚钱。供应商采购，仓库盘点库存补货，第三方物流发货。订单库存记在 ERP，回款和采购用 Excel 表格，最头疼断货、压货与发货延迟。"})
    assert r.status_code == 200, r.text
    invoke(platform,c,ws,'summarize')
    invoke(platform,c,ws,'interview')
    uploaded = c.post(f'/api/workspaces/{ws}/catalogs',files={'file':('northwind.sql',DDL.encode(),'text/plain')})
    assert uploaded.status_code == 200, uploaded.text
    invoke(platform,c,ws,'recommend')
    invoke(platform,c,ws,'match')
    invoke(platform,c,ws,'report')
    result = c.get('/api/workspaces/'+ws).json()
    cards = [a for a in result['artifacts'] if a['kind'] in ('process','ontology','question')]
    assert cards
    for card in cards:
        response = c.post('/api/artifacts/'+card['id']+'/decisions',json={'action':'accept','version':card['version']})
        assert response.status_code == 200, response.text
        drain(platform)
    invoke(platform,c,ws,'refine')
    exported = c.post(f'/api/workspaces/{ws}/exports')
    assert exported.status_code == 200, exported.text
    data = c.get('/api/exports/'+exported.json()['id']+'/download').content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert {'business.yaml','ontology.ttl','ontology.owl','report.md','summarize.md','refinement.yaml','manifest.json'}.issubset(names)
        business = yaml.safe_load(archive.read('business.yaml'))
        assert business['processes'] and business['ontology']
        Graph().parse(data=archive.read('ontology.ttl'),format='turtle')
        Graph().parse(data=archive.read('ontology.owl'),format='xml')
        for name in names:
            if name.endswith('.bpmn'):
                root=ElementTree.fromstring(archive.read(name))
                assert root.tag.endswith('definitions')
        assert json.loads(archive.read('manifest.json'))


def test_sensitive_inputs_and_direct_connections_are_refused(platform):
    c, _ = user(platform, 'owner@example.com')
    ws=workspace(c)
    assert c.post(f'/api/workspaces/{ws}/evidence',json={'text':'手机 13812345678'}).status_code == 422
    assert c.post(f'/api/workspaces/{ws}/evidence',json={'text':'内部资料','sensitivity':'restricted'}).status_code == 422
    assert c.post(f'/api/workspaces/{ws}/connect/db',json={'url':'postgres://evil'}).status_code == 404
    assert c.post(f'/api/workspaces/{ws}/agent/report',json={'call_id':'safe-call-123','provider_url':'http://evil'}).status_code == 422


def test_job_cannot_execute_after_membership_revocation(platform):
    from test_access import invite_member
    owner,_=user(platform,'owner@example.com')
    member,mid=invite_member(owner,platform,'member@example.com')
    bind(owner)
    ws=workspace(member)
    member.post(f'/api/workspaces/{ws}/evidence',json={'text':'我们做食品批发'})
    r=member.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'revoked-job-123'})
    assert r.status_code==200
    owner.patch('/api/members/'+mid,json={'role':'fde_member','active':False})
    drain(platform)
    assert platform['sim'].state.execution_count == 0


def test_report_does_not_include_unused_candidate_payloads(platform):
    from fde_server.models import Artifact
    client, me = user(platform, 'compact-report@example.com')
    bind(client)
    ws = workspace(client)
    client.post(f'/api/workspaces/{ws}/evidence', json={'text':'我们做食品批发，客户是餐厅，希望减少库存缺货。'})
    with platform['rt'].db.session(me['tenant_id']) as s:
        s.add(Artifact(tenant_id=me['tenant_id'],workspace_id=ws,job_id='earlier-match',kind='binding',
                      body={'statement':'商品来自库存表','object':{'candidates':['unused-ranking-data'*5000]}}))
    platform['rt'].cfg.max_context_chars = 8000
    result = invoke(platform,client,ws,'report')
    assert result['status'] == 'succeeded'
