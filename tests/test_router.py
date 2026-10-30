from datetime import timedelta

from sqlalchemy import select

from conftest import bind, drain, invoke, user, workspace
from fde_server.models import QueueTicket, now
from fde_server.pattern import create_pattern_app
from fde_server.security import Context, service_token


def ready(platform):
    c, me=user(platform,'owner@example.com')
    bind(c)
    ws=workspace(c)
    c.post(f'/api/workspaces/{ws}/evidence',json={'text':'食品批发，客户是餐厅，库存补货由采购与供应商协调。'})
    return c,me,ws


def retry_now(platform):
    with platform['rt'].db.session() as s:
        for ticket in s.scalars(select(QueueTicket)):
            ticket.available_at=now()-timedelta(seconds=1)
    drain(platform)


def test_quota_exhausted_has_zero_model_requests(platform):
    c,me,ws=ready(platform)
    with platform['sim'].state.database() as db:
        db.execute('UPDATE accounts SET balance=0')
    r=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'insufficient-quota'})
    drain(platform)
    job=c.get('/api/jobs/'+r.json()['id']).json()
    assert job['status']=='failed' and job['error']=='QUOTA_EXHAUSTED'
    assert platform['sim'].state.execution_count==0
    assert platform['sim'].state.settlement_count==0


def test_same_call_replay_and_changed_payload_conflict(platform):
    c,me,ws=ready(platform)
    first=invoke(platform,c,ws,'summarize',call_id='same-operation-123')
    initial=platform['sim'].state.settlement_count
    second=invoke(platform,c,ws,'summarize',call_id='same-operation-123')
    assert second['id']==first['id']
    assert platform['sim'].state.settlement_count==initial==1
    response=c.post(f'/api/workspaces/{ws}/agent/report',json={'call_id':'same-operation-123'})
    assert response.status_code==409


def test_settlement_failure_retries_without_model_reexecution(platform):
    c,me,ws=ready(platform)
    platform['sim'].state.fail_settlement=1
    r=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'settlement-failure'})
    drain(platform)
    assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='retry_wait'
    assert platform['sim'].state.execution_count==1
    retry_now(platform)
    assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='succeeded'
    assert platform['sim'].state.execution_count==1
    assert platform['sim'].state.settlement_count==1
    assert c.post('/api/billing/reconcile').json()=={'checked':1,'matched':1,'mismatched':0}


def test_execution_timeout_after_router_committed_is_safe(platform):
    c,me,ws=ready(platform)
    platform['sim'].state.fail_execute_after_response=1
    r=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'ambiguous-timeout'})
    drain(platform)
    assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='retry_wait'
    retry_now(platform)
    assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='succeeded'
    assert platform['sim'].state.execution_count==platform['sim'].state.settlement_count==1


def test_internal_service_requires_short_lived_bound_identity(platform):
    from fastapi.testclient import TestClient
    c,me,ws=ready(platform)
    response=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'internal-auth-check'})
    jid=response.json()['id']
    internal=TestClient(create_pattern_app(platform['rt']))
    assert internal.post('/internal/jobs/'+jid).status_code==401
    token=service_token(platform['rt'].cfg,Context(me['user']['id'],me['tenant_id'],'tenant_owner'),jid,'wrong-hash')
    assert internal.post('/internal/jobs/'+jid,headers={'Authorization':'Bearer '+token}).status_code==403


def test_production_settings_refuse_development_defaults():
    import pytest
    from fde_server.config import Settings
    with pytest.raises(ValueError):
        Settings(environment='production')


def test_independent_recommendation_samples_have_distinct_operations(platform):
    c,me,ws=ready(platform)
    invoke(platform,c,ws,'recommend')
    with platform['sim'].state.database() as db:
        outputs=db.execute('SELECT output FROM operations').fetchall()
    # Three samples, a merge and a critic are independently accounted for.
    assert len(outputs)>=5


def test_background_reconciliation_recovers_executed_unsettled_calls(platform):
    from fde_server.maintenance import run_maintenance
    c,me,ws=ready(platform)
    platform['sim'].state.fail_settlement=1
    r=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'maintenance-recovery'})
    drain(platform)
    result=run_maintenance(platform['rt'])
    assert result['reconciled']==1
    retry_now(platform)
    assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='succeeded'
    assert platform['sim'].state.execution_count==platform['sim'].state.settlement_count==1


def test_pending_call_survives_runtime_restart(platform):
    from fde_server.runtime import Runtime
    c,me,ws=ready(platform)
    platform['sim'].state.fail_settlement=1
    r=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'restart-recovery'})
    drain(platform)
    old=platform['rt']
    replacement=Runtime(old.cfg)
    replacement.router=old.router
    platform['rt']=replacement
    try:
        retry_now(platform)
        assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='succeeded'
        assert platform['sim'].state.execution_count==platform['sim'].state.settlement_count==1
    finally:
        replacement.db.engine.dispose()
        replacement.pattern_db.engine.dispose()
        platform['rt']=old


def test_concurrent_duplicate_submission_has_one_job_and_charge(platform):
    from concurrent.futures import ThreadPoolExecutor
    c,me,ws=ready(platform)
    def submit(_):
        return c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'concurrent-submission'})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(submit,range(2)))
    assert all(r.status_code in (200,409) for r in results)
    assert any(r.status_code==200 for r in results)
    drain(platform)
    assert platform['sim'].state.execution_count==platform['sim'].state.settlement_count==1


def test_changed_workspace_cannot_publish_stale_generation(platform):
    c,me,ws=ready(platform)
    r=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'stale-workspace'})
    c.post(f'/api/workspaces/{ws}/evidence',json={'text':'补充：客户只包括零售商。'})
    drain(platform)
    job=c.get('/api/jobs/'+r.json()['id']).json()
    assert job['status']=='failed' and job['error']=='WORKSPACE_CHANGED'
    assert platform['sim'].state.execution_count==0


def test_pending_job_pins_prompt_and_pipeline_revision(platform):
    from fde_server.models import Job
    c,me,ws=ready(platform)
    r=c.post(f'/api/workspaces/{ws}/agent/summarize',json={'call_id':'deployment-version-pin'})
    with platform['rt'].db.session(me['tenant_id']) as s:
        job=s.get(Job,r.json()['id'])
        job.payload={**job.payload,'pipeline_revision':'old-deployment'}
    drain(platform)
    job=c.get('/api/jobs/'+r.json()['id']).json()
    assert job['status']=='failed' and job['error']=='PIPELINE_CHANGED'
    assert platform['sim'].state.execution_count==0


def test_multistep_interview_retry_keeps_intermediate_identities(platform):
    c,me,ws=ready(platform)
    platform['sim'].state.fail_settlement_at=2
    r=c.post(f'/api/workspaces/{ws}/agent/interview',json={'call_id':'interview-step-recovery'})
    drain(platform)
    assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='retry_wait'
    assert platform['sim'].state.execution_count==2
    retry_now(platform)
    assert c.get('/api/jobs/'+r.json()['id']).json()['status']=='succeeded'
    assert platform['sim'].state.execution_count==2
    assert platform['sim'].state.settlement_count==2
