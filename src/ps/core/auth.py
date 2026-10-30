"""Pipeline identity supplied by the SaaS short-lived service-token boundary."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    actor: str
