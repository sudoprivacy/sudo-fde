from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from .tenant_names import normalize_tenant_name

OrganizationName = Annotated[str, BeforeValidator(normalize_tenant_name), Field(min_length=1, max_length=150)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Strict):
    email: EmailStr
    password: str = Field(max_length=128)


class Name(Strict):
    name: str = Field(min_length=1, max_length=150)


class TenantCreate(Strict):
    name: OrganizationName
    owner_user_id: str | None = Field(default=None, max_length=64)
    budget_micros: int = Field(default=200000, ge=100, le=1000000000)
    allowed_models: list[str] = Field(default_factory=list, max_length=50)


class TenantPatch(Strict):
    name: OrganizationName | None = None
    active: bool | None = None
    budget_micros: int | None = Field(default=None, ge=100, le=1000000000)
    allowed_models: list[str] | None = Field(default=None, max_length=50)


class TenantName(Strict):
    name: OrganizationName


class TenantMembershipCreate(Strict):
    tenant_id: str = Field(max_length=64)
    role: Literal["tenant_owner", "tenant_admin", "fde_member", "billing_viewer"] = "fde_member"


class TenantMembershipPatch(Strict):
    role: Literal["tenant_owner", "tenant_admin", "fde_member", "billing_viewer"] | None = None
    active: bool | None = None


class WorkspaceCreate(Strict):
    name: str = Field(min_length=1, max_length=150)
    objective: str = Field(default="", max_length=1000)


class Invite(Strict):
    email: EmailStr
    role: Literal["tenant_admin", "fde_member", "billing_viewer"] = "fde_member"


class AcceptInvite(Strict):
    token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=128)
    tenant_id: str = Field(max_length=64)


class MemberEdit(Strict):
    role: Literal["tenant_admin", "fde_member", "billing_viewer"]
    active: bool


class WorkspaceInvite(Strict):
    user_id: str = Field(max_length=64)
    role: Literal["workspace_editor", "workspace_viewer"]


class EvidenceInput(Strict):
    text: str = Field(min_length=1, max_length=12000)
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    start_ai: bool = False
    model: str = Field(default="", max_length=128)
    provider_id: str | None = Field(default=None, max_length=80)


class AgentInput(Strict):
    call_id: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    params: dict[str, Any] = Field(default_factory=dict)
    model: str = Field(default="", max_length=128)
    provider_id: str | None = Field(default=None, max_length=80)


ProviderModelID = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class ProviderModels(Strict):
    # Provider catalogs can contain hundreds of models. Limit each ID (as on
    # AgentInput), not the number of entries returned by a compatible gateway.
    @field_validator("models", check_fields=False)
    @classmethod
    def unique_models(cls, value):
        return list(dict.fromkeys(value)) if value is not None else None


class ModelProviderInput(ProviderModels):
    provider_id: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    kind: Literal["sudorouter", "openai_compatible"] = "sudorouter"
    api_base: str = Field(min_length=8, max_length=300)
    protocol: Literal["openai", "anthropic"] = "openai"
    api_key: str = Field(min_length=8, max_length=500)
    models: list[ProviderModelID] = Field(min_length=1)
    default_model: str | None = Field(default=None, max_length=128)


class ModelCatalogInput(Strict):
    api_base: str = Field(min_length=8, max_length=300)
    protocol: Literal["openai", "anthropic"] = "openai"
    api_key: str = Field(min_length=8, max_length=500)


class ModelCatalogSavedInput(Strict):
    api_base: str | None = Field(default=None, min_length=8, max_length=300)
    protocol: Literal["openai", "anthropic"] | None = None


class ModelProviderPatch(ProviderModels):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    api_base: str | None = Field(default=None, max_length=300)
    protocol: Literal["openai", "anthropic"] | None = None
    api_key: str | None = Field(default=None, min_length=8, max_length=500)
    models: list[ProviderModelID] | None = Field(default=None, min_length=1)
    default_model: str | None = Field(default=None, max_length=128)
    active: bool | None = None


class DecisionInput(Strict):
    action: Literal["accept", "edit", "reject"]
    version: int = Field(ge=1)
    body: dict[str, Any] | None = None
    reason: str = Field(default="", max_length=500)


class KBInput(Strict):
    name: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=2000)
    workspace_id: str | None = None


class GrantInput(Strict):
    subject_type: Literal["user", "role", "workspace"]
    subject_id: str = Field(max_length=64)


class SearchInput(Strict):
    workspace_id: str = Field(max_length=64)
    query: str = Field(min_length=1, max_length=2000)


class BindRouter(Strict):
    authorization_code: str = Field(min_length=8, max_length=256)


class Policy(Strict):
    models: list[str] = Field(min_length=1, max_length=20)
    budget_micros: int = Field(ge=100, le=10000000)


class RefinementInput(Strict):
    slot_id: str = Field(max_length=80)
    action: Literal["bind", "gap", "offline"]
    fields: list[str] = Field(default_factory=list, max_length=20)
    grain: list[str] = Field(default_factory=list, max_length=20)
    scope: str = Field(default="", max_length=1000)
    note: str = Field(default="", max_length=2000)
    version: int = Field(default=0, ge=0)


class ApplyRevision(Strict):
    change_ids: list[str] = Field(min_length=1, max_length=40)


class ResetRequest(Strict):
    email: EmailStr


class ResetConfirm(Strict):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=128)
