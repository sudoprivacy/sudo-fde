from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FDE_", env_file=".env", extra="ignore")
    environment: str = "production"
    database_url: str = "sqlite:///.data/fde.db"
    migration_database_url: str | None = None
    public_url: str = "http://127.0.0.1:8800"
    session_secret: str = "development-only-change-session-secret"
    service_secret: str = "development-only-change-service-secret"
    credential_encryption_key: str = ""
    session_hours: int = 12
    idle_minutes: int = 60
    pattern_url: str = "http://127.0.0.1:8801"
    router_url: str = "http://127.0.0.1:8802"
    router_token: str = "development-router-token"
    router_contract: str = "fde-router/1"
    router_timeout: float = 60
    provider_timeout: float = Field(default=180, ge=10, le=300)
    provider_max_output_tokens: int = Field(default=16384, ge=256, le=32768)
    object_backend: str = "local"
    object_root: Path = Path(".data/objects")
    s3_endpoint: str | None = None
    s3_bucket: str = "fde-private"
    s3_region: str = "us-east-1"
    embedding_backend: str = "development"
    embedding_url: str | None = None
    embedding_model: str = "multilingual-e5-small"
    virus_backend: str = "development"
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    max_upload_bytes: int = 10 * 1024 * 1024
    max_context_chars: int = 60000
    max_output_tokens: int = 4096
    max_calls_per_job: int = 16
    max_budget_micros: int = 200000
    max_concurrent_jobs: int = 3
    max_chunks_per_document: int = 500
    retrieval_limit: int = 6
    job_lease_seconds: int = 600
    allow_registration: bool = True
    trust_proxy: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "FDE <noreply@example.test>"
    smtp_starttls: bool = True
    cors_origins: list[str] = Field(default_factory=list)
    platform_admin_emails: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def production_guards(self):
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("Unknown environment")
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL")
            for value in (self.session_secret, self.service_secret, self.router_token):
                if len(value) < 32 or any(
                    marker in value.lower()
                    for marker in ("development", "change-me", "replace", "placeholder")
                ):
                    raise ValueError("Production secrets must be independently provisioned")
            if self.session_secret == self.service_secret:
                raise ValueError("Session and service signing keys must differ")
            if urlparse(self.public_url).scheme != "https":
                raise ValueError("Production requires HTTPS public URL")
            if urlparse(self.router_url).scheme != "https":
                raise ValueError("Production Router transport requires TLS")
            if self.object_backend != "s3" or self.embedding_backend not in {"internal", "router"}:
                raise ValueError("Production requires object storage and governed embeddings")
            if self.virus_backend != "clamav" or not self.smtp_host:
                raise ValueError("Production requires ClamAV and email delivery")
            if not self.credential_encryption_key:
                raise ValueError("Production requires FDE_CREDENTIAL_ENCRYPTION_KEY")
        for url in (self.router_url, self.pattern_url):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise ValueError("Invalid service URL")
        if self.embedding_backend == "internal" and not self.embedding_url:
            raise ValueError("Internal embedding URL is required")
        return self

    @property
    def production(self):
        return self.environment == "production"
