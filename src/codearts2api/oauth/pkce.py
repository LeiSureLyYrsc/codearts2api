"""PKCE helpers matching the official CodeArts CLI (26.11.11) implementation.

Observed in the official `agentkernel` binary shipped at
https://codearts-agent-obs-cdn.huaweicloud.com/codearts/cli_tui/latest/ :

- code_verifier: 64 random bytes rendered as 128 hex characters
- code_challenge: unpadded base64url(SHA-256(verifier))
- code_challenge_method: the literal string "SHA-256"
- ticket_id: 32 random bytes rendered as 64 hex characters
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


def b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sha256_b64url(value: str) -> str:
    return b64url_nopad(hashlib.sha256(value.encode("ascii")).digest())


@dataclass(frozen=True, slots=True)
class PkcePair:
    code_verifier: str
    code_challenge: str
    code_challenge_method: str = "SHA-256"


def generate_code_verifier(byte_length: int = 64) -> str:
    return secrets.token_bytes(byte_length).hex()


def generate_pkce_pair() -> PkcePair:
    verifier = generate_code_verifier(64)
    return PkcePair(
        code_verifier=verifier,
        code_challenge=sha256_b64url(verifier),
        code_challenge_method="SHA-256",
    )


def generate_ticket_id() -> str:
    return secrets.token_bytes(32).hex()
