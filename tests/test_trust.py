import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from aimo_agent.catalog import Catalog
from aimo_agent.models import AgentIdentity, Need
from aimo_agent.service import PurchasingService
from aimo_agent.store import Store
from aimo_agent.trust import ISSUER, Signer, verify_offer
from tests.conftest import MONDAY, OPEN_UNTIL, FakeAimo

pytestmark = pytest.mark.anyio


@pytest.fixture
def signer(tmp_path: Path) -> Signer:
    return Signer(tmp_path / "signing_key.pem")


@pytest.fixture
def signing_service(catalog: Catalog, aimo: FakeAimo, signer: Signer) -> PurchasingService:
    return PurchasingService(catalog, Store(":memory:"), upstream=lambda: aimo, signer=signer)  # type: ignore[arg-type,return-value]


async def top_option(service: PurchasingService, agent: AgentIdentity):  # noqa: ANN201
    request = await service.create_request(
        agent, Need(query="milk", free_from=["lactose"], quantity=300, unit="L", deliver_by=MONDAY)
    )
    return request, request.options[0]


async def test_every_option_carries_a_proof_of_what_the_wholesaler_stated(
    signing_service: PurchasingService, hotel: AgentIdentity, signer: Signer
) -> None:
    request, option = await top_option(signing_service, hotel)

    claims = verify_offer(option.proof, signer.jwks())

    assert all(o.proof for o in request.options)
    assert (claims["iss"], claims["sub"], claims["jti"]) == (ISSUER, "B1", f"{request.id}/opt-1")
    assert claims["exp"] == int(OPEN_UNTIL.timestamp())  # valid until the order cut-off
    assert claims["offer"]["total"] == option.total == 303.0
    assert claims["offer"]["lines"] == [
        {
            "product_id": "100",
            "gtin": None,
            "name": "Valio semi skimmed milk drink 1 l lactose free",
            "units": 300,
            "sales_unit": "ST",
            "unit_price": 1.01,
            "delivery_date": "2026-11-16",
            "country_of_origin": "fi",
        }
    ]


async def test_altered_offer_is_rejected(
    signing_service: PurchasingService, hotel: AgentIdentity, signer: Signer
) -> None:
    _, option = await top_option(signing_service, hotel)
    header, payload, signature = option.proof.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["offer"]["total"] = 150.0  # a middleman halves the price
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

    with pytest.raises(jwt.InvalidSignatureError):
        verify_offer(f"{header}.{forged_payload}.{signature}", signer.jwks())


async def test_offer_signed_by_another_key_is_rejected(
    signing_service: PurchasingService, hotel: AgentIdentity, tmp_path: Path
) -> None:
    _, option = await top_option(signing_service, hotel)
    impostor = Signer(tmp_path / "other_key.pem")

    with pytest.raises(jwt.InvalidTokenError, match="unknown key"):
        verify_offer(option.proof, impostor.jwks())


async def test_offer_expires_at_the_order_cut_off(
    signing_service: PurchasingService, hotel: AgentIdentity, signer: Signer, aimo: FakeAimo
) -> None:
    # The cut-off is moved into the real past, so the proof is already expired when checked.
    aimo.now = aimo.now.replace(year=2020)
    aimo.offer("100", 1.01, closing=aimo.now.replace(day=15))
    aimo.offer("101", 1.10, closing=aimo.now.replace(day=15))
    _, option = await top_option(signing_service, hotel)

    with pytest.raises(jwt.ExpiredSignatureError):
        verify_offer(option.proof, signer.jwks())


def test_small_clock_difference_between_wholesaler_and_buyer_is_tolerated(signer: Signer) -> None:
    now = datetime.now(UTC)
    claims = {"iss": ISSUER, "sub": "B1", "exp": int((now + timedelta(hours=1)).timestamp())}
    ahead = signer.sign(claims | {"iat": int((now + timedelta(seconds=90)).timestamp())})
    far_ahead = signer.sign(claims | {"iat": int((now + timedelta(hours=1)).timestamp())})

    assert verify_offer(ahead, signer.jwks())["sub"] == "B1"
    with pytest.raises(jwt.ImmatureSignatureError):
        verify_offer(far_ahead, signer.jwks())


def test_signing_key_is_created_once_and_reused(tmp_path: Path) -> None:
    first = Signer(tmp_path / "key.pem")
    second = Signer(tmp_path / "key.pem")

    assert first.key_id == second.key_id
    (key,) = first.jwks()["keys"]
    assert (key["kty"], key["crv"], key["alg"], key["use"]) == ("OKP", "Ed25519", "EdDSA", "sig")
    assert "d" not in key  # the private part is never published
