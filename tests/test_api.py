from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from aimo_agent import agents
from aimo_agent.app import app
from aimo_agent.models import AgentIdentity
from aimo_agent.service import PurchasingService, get_service

HOTEL = {"Authorization": "Bearer hotel-token"}
SCHOOL = {"Authorization": "Bearer school-token"}
MILK = {
    "text": "I need lactose-free milk by Monday.",
    "query": "milk",
    "free_from": ["lactose"],
    "exclude_terms": ["sour"],
    "quantity": 1000,
    "unit": "litres",
    "deliver_by": "2026-11-16",
}


@pytest.fixture
def client(
    service: PurchasingService,
    hotel: AgentIdentity,
    school: AgentIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    registry = {"hotel-token": hotel, "school-token": school}
    monkeypatch.setattr(agents, "get_agents", lambda: registry)
    monkeypatch.setattr("aimo_agent.app.get_agents", lambda: registry)
    app.dependency_overrides[get_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_agent_endpoints_reject_missing_and_unknown_tokens(client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer nope"}):
        response = client.get("/v1/mandate", headers=headers)
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["type"] == "urn:aimo-agent-gateway:error:unauthorized"


def test_agent_reads_its_own_mandate(client: TestClient) -> None:
    body = client.get("/v1/mandate", headers=HOTEL).json()
    assert body["buyer_name"] == "Hotel"
    assert body["mandate"]["max_autonomous_spend"] == 1000


def test_catalog_search_takes_repeated_query_parameters(client: TestClient) -> None:
    response = client.get(
        "/v1/products",
        params={"query": "milk", "free_from": ["lactose"], "exclude_terms": ["sour"], "limit": 3},
        headers=HOTEL,
    )
    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 7
    assert len(body["products"]) == 3
    assert client.get("/v1/products/100", headers=HOTEL).json()["sales_unit"] == "ST"
    assert client.get("/v1/products/nope", headers=HOTEL).status_code == 404


def test_request_order_approve_flow_over_http(client: TestClient) -> None:
    request = client.post("/v1/requests", json=MILK, headers=HOTEL)
    assert request.status_code == 201
    option = request.json()["options"][0]
    assert option["policy"]["decision"] == "needs_approval"

    order = client.post(
        "/v1/orders",
        json={"request_id": request.json()["id"], "option_id": option["id"], "note": "Cheapest."},
        headers=HOTEL,
    )
    assert order.status_code == 201
    order_id = order.json()["id"]
    assert order.json()["status"] == "pending_approval"
    assert [o["id"] for o in client.get("/v1/desk/orders?status=pending_approval").json()] == [
        order_id
    ]

    decision = client.post(
        f"/v1/desk/orders/{order_id}/decision",
        json={"approver": "Head chef", "approved": True, "comment": "Fine"},
    )
    assert decision.json()["status"] == "confirmed"
    assert client.get(f"/v1/orders/{order_id}", headers=HOTEL).json()["status"] == "confirmed"

    trail = client.get(f"/v1/orders/{order_id}/audit", headers=HOTEL).json()
    assert [event["type"] for event in trail][-2:] == ["approval.granted", "order.confirmed"]
    assert client.get("/v1/desk/audit/verify").json() == {
        "intact": True,
        "events": len(trail),
        "first_bad_seq": None,
    }


def test_errors_are_problem_details(client: TestClient) -> None:
    request = client.post("/v1/requests", json=MILK | {"quantity": 100}, headers=HOTEL).json()

    foreign = client.get(f"/v1/requests/{request['id']}", headers=SCHOOL)
    assert (foreign.status_code, foreign.json()["title"]) == (403, "Forbidden")
    missing = client.post(
        "/v1/orders", json={"request_id": request["id"], "option_id": "opt-99"}, headers=HOTEL
    )
    assert missing.status_code == 404
    invalid = client.post("/v1/requests", json=MILK | {"free_from": ["unicorn"]}, headers=HOTEL)
    assert invalid.status_code == 422


def test_live_offers_endpoint(client: TestClient) -> None:
    response = client.post(
        "/v1/offers",
        json={
            "product_ids": ["100", "103"],
            "quantity": 50,
            "unit": "L",
            "deliver_by": "2026-11-16",
        },
        headers=HOTEL,
    )
    assert [(o["product_id"], o["status"]) for o in response.json()] == [
        ("100", "available"),
        ("103", "order_window_closed"),
    ]


def test_approval_desk_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Approval desk" in response.text
