import pytest

from conftest import user


def provider_body(models=None):
    return {
        "provider_id": "sudorouter", "display_name": "SudoRouter",
        "api_base": "https://router.example/v1", "protocol": "openai",
        "api_key": "sk-validation-test-only", "models": models or ["chat-model"],
        "default_model": "chat-model",
    }


@pytest.mark.parametrize("count", [51, 137, 1200])
def test_complete_catalog_can_be_created_updated_and_read(platform, count):
    client, _ = user(platform, "large-catalog@example.com")
    models = ["chat-model", *[f"model-{i}" for i in range(count - 1)]]
    created = client.post("/api/models/providers", json=provider_body(models))
    assert created.status_code == 200, created.text
    assert created.json()["models"] == models
    updated_models = [*models, "new-model"]
    updated = client.patch("/api/models/providers/sudorouter", json={"models": updated_models})
    assert updated.status_code == 200, updated.text
    assert updated.json()["models"] == updated_models
    assert client.get("/api/models/providers").json()[0]["models"] == updated_models
    assert client.get("/api/session").json()["providers"][0]["models"] == updated_models


@pytest.mark.parametrize("invalid, field, message", [
    ({"models": []}, "body.models", "模型目录至少需要 1 项"),
    ({"models": [""]}, "body.models.0", "模型目录第 1 个模型 ID至少需要 1 个字符"),
    ({"models": ["m" * 129]}, "body.models.0", "模型目录第 1 个模型 ID不能超过 128 个字符"),
    ({"provider_id": "Invalid Provider"}, "body.provider_id", "Provider ID须以小写字母开头"),
    ({"display_name": ""}, "body.display_name", "显示名称至少需要 1 个字符"),
    ({"api_key": "sk-secret-" * 60}, "body.api_key", "API 密钥不能超过 500 个字符"),
])
def test_validation_explains_the_field_without_exposing_credentials(platform, invalid, field, message):
    client, _ = user(platform, "validation@example.com")
    body = {**provider_body(), **invalid}
    response = client.post("/api/models/providers", json=body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert field in error["fields"]
    assert message in error["message"]
    assert error["field_errors"][0]["field"] == field.removeprefix("body.")
    assert body["api_key"] not in response.text
    assert "sk-secret-" not in response.text
    assert "input" not in response.text


def test_provider_normalizes_models_and_validates_default_against_final_catalog(platform):
    client, _ = user(platform, "default-model@example.com")
    created = client.post("/api/models/providers", json=provider_body([" chat-model ", "chat-model"]))
    assert created.status_code == 200, created.text
    assert created.json()["models"] == ["chat-model"]
    updated = client.patch("/api/models/providers/sudorouter", json={
        "models": ["new-model"], "default_model": "new-model",
    })
    assert updated.status_code == 200, updated.text
    invalid = client.patch("/api/models/providers/sudorouter", json={"default_model": "missing-model"})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "MODEL_INVALID"
    assert client.get("/api/models/providers").json()[0]["default_model"] == "new-model"
