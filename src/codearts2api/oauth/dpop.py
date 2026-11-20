"""DPoP proof generation as used by the official CodeArts CLI.

The official CLI (26.11.11) builds a DPoP JWT with:

- typ: "dpop+jwt"
- alg: "ES256" (ECDSA P-256 / SHA-256)
- header.jwk: the public JWK
- claims: htm, htu, iat, jti (jti is 16 random bytes as hex)
- signature: raw IEEE P1363 r||s, unpadded base64url

The same P-256 key pair is reused for later refresh_token requests.
This follows RFC 9449 field names as implemented in the official client.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from codearts2api.oauth.pkce import b64url_nopad


def _b64url_uint(value: int, size: int = 32) -> str:
    return b64url_nopad(value.to_bytes(size, "big"))


def _b64url_to_int(text: str) -> int:
    padded = text + "=" * ((4 - len(text) % 4) % 4)
    import base64

    return int.from_bytes(base64.urlsafe_b64decode(padded.encode("ascii")), "big")


@dataclass(slots=True)
class DPoPKeyPair:
    private_jwk: dict[str, str]
    public_jwk: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"private_jwk": self.private_jwk, "public_jwk": self.public_jwk}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DPoPKeyPair":
        return cls(
            private_jwk=dict(payload["private_jwk"]),
            public_jwk=dict(payload["public_jwk"]),
        )


def generate_p256_keypair() -> DPoPKeyPair:
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.private_numbers()
    public = numbers.public_numbers
    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url_uint(public.x),
        "y": _b64url_uint(public.y),
    }
    private_jwk = {
        **public_jwk,
        "d": _b64url_uint(numbers.private_value),
    }
    return DPoPKeyPair(private_jwk=private_jwk, public_jwk=public_jwk)


def _private_key_from_jwk(jwk: dict[str, str]) -> ec.EllipticCurvePrivateKey:
    d = _b64url_to_int(jwk["d"])
    x = _b64url_to_int(jwk["x"])
    y = _b64url_to_int(jwk["y"])
    public_numbers = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1())
    private_numbers = ec.EllipticCurvePrivateNumbers(d, public_numbers)
    return private_numbers.private_key()


def build_dpop_proof(keypair: DPoPKeyPair, method: str, htu: str, *, now: int | None = None) -> str:
    """Return a compact JWS DPoP proof for the given HTTP method and token URL."""
    header = {
        "alg": "ES256",
        "typ": "dpop+jwt",
        "jwk": {
            "kty": keypair.public_jwk["kty"],
            "crv": keypair.public_jwk["crv"],
            "x": keypair.public_jwk["x"],
            "y": keypair.public_jwk["y"],
        },
    }
    payload = {
        "htm": method.upper(),
        "htu": htu,
        "iat": int(time.time() if now is None else now),
        "jti": secrets.token_bytes(16).hex(),
    }
    signing_input = (
        f"{b64url_nopad(json.dumps(header, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))}."
        f"{b64url_nopad(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))}"
    )
    private_key = _private_key_from_jwk(keypair.private_jwk)
    der = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{b64url_nopad(raw)}"
