"""Signed offers: the wholesaler's verifiable statement of price, quantity, delivery and origin.

Every option carries a proof: a JWS (RFC 7515) signed with Ed25519 (EdDSA, RFC 8037) over what
the wholesaler committed to. The public key is published as a JWK Set (RFC 7517) at
/.well-known/jwks.json. The buyer's side can therefore check an offer without calling the
wholesaler back and without trusting the agent or the channel that relayed it.
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm

from aimo_agent.catalog import Catalog
from aimo_agent.models import Option

ISSUER = "valio-aimo-agent-gateway"
ALGORITHM = "EdDSA"
# An offer without an order cut-off stays valid this long.
DEFAULT_VALIDITY = timedelta(hours=24)
# Offers are stamped with the wholesaler's clock; the verifier's clock may differ by this much.
CLOCK_SKEW = timedelta(minutes=5)


class Signer:
    """Holds the gateway's signing key, creating it on first use."""

    def __init__(self, key_file: Path) -> None:
        if key_file.exists():
            loaded = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise ValueError(f"{key_file} is not an Ed25519 private key")
            self._key = loaded
        else:
            self._key = Ed25519PrivateKey.generate()
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(
                self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        self._jwk: dict[str, str] = OKPAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        # Key id: the JWK thumbprint (RFC 7638), so it identifies the key itself.
        canonical = json.dumps(
            {name: self._jwk[name] for name in ("crv", "kty", "x")}, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode()).digest()
        self.key_id = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def jwks(self) -> dict[str, Any]:
        return {"keys": [{**self._jwk, "kid": self.key_id, "use": "sig", "alg": ALGORITHM}]}

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            claims, self._key, algorithm=ALGORITHM, headers={"kid": self.key_id, "typ": "offer+jwt"}
        )


def offer_claims(
    request_id: str, buyer_id: str, option: Option, catalog: Catalog, issued_at: datetime
) -> dict[str, Any]:
    """What the wholesaler states and stands behind for one option."""
    lines = []
    for line in option.lines:
        product = catalog.get(line.product_id)
        lines.append(
            {
                "product_id": line.product_id,
                "gtin": product.gtin if product else None,
                "name": line.name,
                "units": line.units,
                "sales_unit": line.sales_unit,
                "unit_price": line.unit_price,
                "delivery_date": line.delivery_date.isoformat(),
                "country_of_origin": product.country_of_origin if product else None,
            }
        )
    expires = option.order_by or issued_at + DEFAULT_VALIDITY
    return {
        "iss": ISSUER,
        "sub": buyer_id,
        "jti": f"{request_id}/{option.id}",
        "iat": int(issued_at.timestamp()),
        "exp": int(expires.timestamp()),
        "offer": {
            "request_id": request_id,
            "option_id": option.id,
            "currency": option.currency,
            "total": option.total,
            "total_with_tax": option.total_with_tax,
            "delivery_date": option.delivery_date.isoformat(),
            "lines": lines,
        },
    }


def verify_offer(proof: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Check a proof against the wholesaler's published keys and return its claims.

    Raises jwt.InvalidTokenError when the signature does not match, the key is unknown, the
    offer has expired or a required claim is missing.
    """
    key_id = jwt.get_unverified_header(proof).get("kid")
    published = next((key for key in jwks.get("keys", []) if key.get("kid") == key_id), None)
    if published is None:
        raise jwt.InvalidTokenError(f"The offer is signed with an unknown key: {key_id!r}")
    return jwt.decode(
        proof,
        OKPAlgorithm.from_jwk(published),
        algorithms=[ALGORITHM],
        issuer=ISSUER,
        leeway=CLOCK_SKEW,
        options={"require": ["iss", "sub", "iat", "exp"]},
    )
