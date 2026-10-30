import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import user


def test_provider_picker_flow(platform, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend regression tests")
    contract = tmp_path / "provider-picker-requests.json"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).parent / "frontend/provider-picker.test.mjs")],
        env={**os.environ, "FDE_PICKER_CONTRACT_OUTPUT": str(contract)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    client, _ = user(platform, "picker-contract@example.com")
    requests = json.loads(contract.read_text())
    assert len(requests) == 3
    for request in requests:
        response = client.request(request["method"], request["path"], json=request["body"])
        assert response.status_code == 200, response.text
        assert response.json()["models"] == request["body"]["models"]
        assert response.json()["default_model"] == request["body"]["default_model"]
        assert response.json()["api_key_configured"] is True
        saved = client.get("/api/models/providers").json()[0]
        assert saved["models"] == request["body"]["models"]
        assert saved["default_model"] == request["body"]["default_model"]
        session = client.get("/api/session").json()
        assert session["providers"][0]["default_chat_model"] == request["body"]["default_model"]


def test_workspace_model_frontend_contract(platform, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend regression tests")
    contract = tmp_path / "workspace-model-requests.json"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).parent / "frontend/workspace-model.test.mjs")],
        env={**os.environ, "FDE_WORKSPACE_MODEL_REQUESTS": str(contract)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    from conftest import workspace
    from test_personal_model_workflow import add_provider
    client, _ = user(platform, "workspace-model-contract@example.com")
    add_provider(client)
    requests = json.loads(contract.read_text())
    assert len(requests) == 2
    for request in requests:
        ws = workspace(client)
        response = client.request(request["method"], request["path"].replace("ws-one", ws), json=request["body"])
        assert response.status_code == 200, response.text
        body = response.json()
        job = body["next_job"] if request["path"].endswith("/evidence") else body
        assert job["model"] == "chat-alternative"
