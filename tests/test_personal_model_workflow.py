import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from conftest import drain, user, workspace
from fde_server import providers
from fde_server.models import Job, QueueTicket
from test_access import invite_member


def add_provider(client, protocol="openai", default="chat-primary"):
    response = client.post("/api/models/providers", json={
        "provider_id": "personal", "display_name": "Personal provider", "api_base": "https://models.example/v1",
        "protocol": protocol, "api_key": "sk-test-personal-only",
        "models": ["gpt-image-2", "chat-primary", "chat-alternative", "text-embedding-ada-002"],
        "default_model": default,
    })
    assert response.status_code == 200, response.text
    return response.json()


def mock_personal_http(monkeypatch, protocol="openai"):
    calls = []

    def transport(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.method == "POST"
        assert request.url.host == "models.example"
        assert request.url.path == ("/v1/messages" if protocol == "anthropic" else "/v1/chat/completions")
        if protocol == "anthropic":
            assert request.headers["x-api-key"] == "sk-test-personal-only"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer sk-test-personal-only"
        assert "食品批发" in body["messages"][-1]["content"]
        summary = {
            "one_liner": "个人提供商返回的业务概要", "industry": "食品批发",
            "offerings": {"what": "食品", "evidence": []},
            "customers": {"who": "餐厅", "evidence": []},
            "revenue_model": {"how": "批发", "evidence": []},
            "scale": {"note": "待确认", "evidence": []},
            "business_lines": [], "value_chain": [], "systems_mentioned": [],
            "open_points": ["库存规模"], "challenge_questions": [],
        }
        text = json.dumps(summary, ensure_ascii=False)
        payload = {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 100, "output_tokens": 50}} if protocol == "anthropic" else {
            "choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(providers, "httpx", SimpleNamespace(
        Client=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(transport), **kwargs),
        HTTPError=httpx.HTTPError, Timeout=httpx.Timeout, TimeoutException=httpx.TimeoutException, HTTPStatusError=httpx.HTTPStatusError,
    ))
    return calls


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("entry", ["automatic", "manual", "legacy"])
def test_personal_model_reaches_generation_without_organization_binding(platform, monkeypatch, protocol, entry):
    client, me = user(platform, "personal@example.com")
    add_provider(client, protocol)
    assert client.get("/api/session").json()["router_bound"] is False
    ws = workspace(client)
    selection = {"provider_id": "personal", "model": "chat-alternative"}
    payload = {"text": "我们做食品批发，服务餐厅。", "start_ai": entry != "manual"}
    if entry == "automatic":
        payload.update(selection)
    saved = client.post(f"/api/workspaces/{ws}/evidence", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["ai_error"] is None
    if entry == "manual":
        queued = client.post(f"/api/workspaces/{ws}/agent/summarize", json={"call_id": "personal-manual-call", **selection})
        assert queued.status_code == 200, queued.text
        job = queued.json()
    else:
        job = saved.json()["next_job"]
    expected_model = "chat-primary" if entry == "legacy" else "chat-alternative"
    assert job["model"] == expected_model
    with platform["rt"].db.session(me["tenant_id"]) as s:
        assert s.get(Job, job["id"]).payload["provider_id"] == "personal"
    calls = mock_personal_http(monkeypatch, protocol)
    drain(platform)
    completed = client.get("/api/jobs/" + job["id"]).json()
    assert completed["status"] == "succeeded", completed
    assert len(calls) == 1 and calls[0]["model"] == expected_model
    data = client.get("/api/workspaces/" + ws).json()
    assert len(data["evidence"]) == 1
    assert data["artifacts"][0]["body"]["summary"]["one_liner"] == "个人提供商返回的业务概要"
    assert data["workflow"]["current"] == "challenge"
    assert platform["sim"].state.execution_count == 0
    assert platform["sim"].state.settlement_count == 0
    usage = client.get('/api/billing').json()['personal_calls']
    assert usage[0]['model'] == expected_model and usage[0]['calls'] == 1
    assert usage[0]['tokens_in'] == 100 and usage[0]['tokens_out'] == 50
    metrics = client.get('/api/metrics').json()
    assert metrics['tokens_in'] == 100 and metrics['tokens_out'] == 50
    assert metrics['counts']['personal_model_calls'] == 1


def test_image_default_does_not_become_business_model(platform):
    client, _ = user(platform, "image-default@example.com")
    provider = add_provider(client, default="gpt-image-2")
    assert provider["default_model"] == "gpt-image-2"
    assert provider["default_chat_model"] == "chat-primary"
    assert provider["chat_models"] == ["chat-primary", "chat-alternative"]
    ws = workspace(client)
    response = client.post(f"/api/workspaces/{ws}/evidence", json={"text": "我们做食品批发", "start_ai": True})
    assert response.json()["next_job"]["model"] == "chat-primary"


@pytest.mark.parametrize("selection, code", [
    ({}, "MODEL_NOT_CONFIGURED"),
    ({"provider_id": "missing", "model": "chat-primary"}, "PROVIDER_NOT_FOUND"),
    ({"provider_id": "personal", "model": "gpt-image-2"}, "MODEL_CAPABILITY_UNSUPPORTED"),
    ({"provider_id": "personal", "model": "not-authorized"}, "MODEL_NOT_ALLOWED"),
])
def test_rejected_automatic_ai_does_not_lose_material(platform, selection, code):
    client, me = user(platform, "preserve@example.com")
    if selection:
        add_provider(client)
    ws = workspace(client)
    response = client.post(f"/api/workspaces/{ws}/evidence", json={"text": "我们做食品批发", "start_ai": True, **selection})
    assert response.status_code == 200, response.text
    assert response.json()["next_job"] is None
    assert response.json()["ai_error"]["code"] == code
    data = client.get("/api/workspaces/" + ws).json()
    assert data["evidence"][0]["text"] == "我们做食品批发"
    assert data["workspace"]["version"] == 1
    assert data["workspace"]["next_action"] == "collect_material"
    assert data["jobs"] == []
    with platform["rt"].db.session(me["tenant_id"]) as s:
        assert list(s.scalars(select(QueueTicket))) == []


def test_personal_keys_are_not_shared_with_other_members_or_tenants(platform):
    owner, _ = user(platform, "private-provider@example.com")
    member, _ = invite_member(owner, platform, "member-provider@example.com")
    stranger, _ = user(platform, "other-provider@example.com")
    add_provider(owner)
    for client in (member, stranger):
        ws = workspace(client)
        response = client.post(f"/api/workspaces/{ws}/agent/summarize", json={
            "call_id": "private-provider-call", "provider_id": "personal", "model": "chat-primary",
        })
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PROVIDER_NOT_FOUND"
        automatic = client.post(f"/api/workspaces/{ws}/evidence", json={"text": "我们做食品批发", "start_ai": True})
        assert automatic.json()["next_job"] is None
        assert automatic.json()["ai_error"]["code"] == "MODEL_NOT_CONFIGURED"


def test_revoking_provider_blocks_queued_generation(platform, monkeypatch):
    client, _ = user(platform, "revoke-provider@example.com")
    add_provider(client)
    ws = workspace(client)
    response = client.post(f"/api/workspaces/{ws}/evidence", json={"text": "我们做食品批发", "start_ai": True})
    job = response.json()["next_job"]
    assert client.delete("/api/models/providers/personal").status_code == 200
    calls = mock_personal_http(monkeypatch)
    drain(platform)
    completed = client.get("/api/jobs/" + job["id"]).json()
    assert completed["status"] == "failed" and completed["error"] == "PROVIDER_NOT_FOUND"
    assert calls == []


def test_personal_timeout_is_explicit_and_does_not_automatically_replay(platform, monkeypatch):
    client, _ = user(platform, 'timeout-provider@example.com')
    add_provider(client)
    ws = workspace(client)
    queued = client.post(f'/api/workspaces/{ws}/evidence', json={'text':'我们做食品批发','start_ai':True}).json()['next_job']
    attempts = []
    def make_client(**kwargs):
        assert kwargs['timeout'].read == 180
        def transport(request):
            attempts.append(request)
            raise httpx.ReadTimeout('timeout')
        return httpx.Client(transport=httpx.MockTransport(transport), **kwargs)
    monkeypatch.setattr(providers, 'httpx', SimpleNamespace(Client=make_client, HTTPError=httpx.HTTPError,
        Timeout=httpx.Timeout, TimeoutException=httpx.TimeoutException, HTTPStatusError=httpx.HTTPStatusError))
    drain(platform)
    job = client.get('/api/jobs/'+queued['id']).json()
    assert job['status']=='failed' and job['error']=='PROVIDER_TIMEOUT'
    drain(platform)
    assert len(attempts)==1


@pytest.mark.parametrize('content,finish,expected', [
    ('{"unexpected":"shape"}','stop','PROVIDER_RESPONSE_INVALID'),
    ('{"one_liner":"partial"','length','PROVIDER_OUTPUT_LIMIT'),
])
def test_invalid_or_truncated_personal_output_is_not_published(platform,monkeypatch,content,finish,expected):
    client,_=user(platform,'invalid-output@example.com')
    add_provider(client)
    ws=workspace(client)
    queued=client.post(f'/api/workspaces/{ws}/evidence',json={'text':'我们做食品批发','start_ai':True}).json()['next_job']
    def transport(request):
        assert json.loads(request.content)['max_tokens']==platform['rt'].cfg.provider_max_output_tokens
        return httpx.Response(200,json={'choices':[{'finish_reason':finish,'message':{'content':content}}]})
    monkeypatch.setattr(providers,'httpx',SimpleNamespace(
        Client=lambda **kw:httpx.Client(transport=httpx.MockTransport(transport),**kw),Timeout=httpx.Timeout,
        HTTPError=httpx.HTTPError,TimeoutException=httpx.TimeoutException,HTTPStatusError=httpx.HTTPStatusError))
    drain(platform)
    job=client.get('/api/jobs/'+queued['id']).json()
    assert job['status']=='failed' and job['error']==expected
    assert client.get('/api/workspaces/'+ws).json()['artifacts']==[]
