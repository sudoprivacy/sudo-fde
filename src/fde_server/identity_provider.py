"""Reserved enterprise identity integration boundary; password login is the v1 implementation.

An OIDC adapter must verify issuer, audience, nonce, state and PKCE, then resolve a
pre-authorized local user/membership. An arbitrary IdP claim never creates tenant access.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExternalIdentity:
    issuer: str
    subject: str
    verified_email: str


class EnterpriseIdentityProvider(Protocol):
    def begin(self, *, state: str, nonce: str, code_challenge: str) -> str: ...

    def exchange(self, *, code: str, code_verifier: str, expected_nonce: str) -> ExternalIdentity: ...
