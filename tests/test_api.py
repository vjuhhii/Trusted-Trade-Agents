import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo_agent import agents
from aimo_agent.app import app
from aimo_agent.config import settings
from aimo_agent.models import AgentIdentity, Approver
from aimo_agent.service import PurchasingService, get_service

HOTEL = {"Authorization": "Bearer hotel-token"}
CHEF = {"Authorization": "Bearer chef-key"}
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
    people = {
        "chef-key": Approver(
            name="Head chef", agent_id="hotel-agent", buyer_id="B1", buyer_name="Hotel"
        ),
        "porter-key": Approver(
            name="Porter", agent_id="hotel-agent", buyer_id="B1", buyer_name="Hotel"
        ),
        "manager-key": Approver(
            name="Kitchen manager", agent_id="school-agent", buyer_id="B2", buyer_name="School"
        ),
    }
    monkeypatch.setattr("aimo_agent.app.get_approvers", lambda: people)
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
        json={"approved": True, "comment": "Fine"},
        headers=CHEF,
    )
    assert decision.json()["status"] == "confirmed"
    assert decision.json()["approval"]["approver"] == "Head chef"  # from the key, not a claim
    assert client.get(f"/v1/orders/{order_id}", headers=HOTEL).json()["status"] == "confirmed"

    trail = client.get(f"/v1/orders/{order_id}/audit", headers=HOTEL).json()
    assert [event["type"] for event in trail][-2:] == ["approval.granted", "order.confirmed"]
    assert client.get("/v1/desk/audit/verify").json() == {
        "intact": True,
        "events": len(trail),
        "first_bad_seq": None,
    }


def test_basket_flow_over_http_takes_three_calls(client: TestClient) -> None:
    oat = {"text": "Oat drink", "query": "oat drink", "quantity": 400, "deliver_by": "2026-11-16"}
    requests = client.post(
        "/v1/requests/batch", json=[MILK | {"quantity": 400}, oat], headers=HOTEL
    )
    assert requests.status_code == 201
    items = [{"request_id": r["id"], "option_id": "opt-1"} for r in requests.json()]

    basket = client.post(
        "/v1/baskets", json={"items": items, "title": "Banquet", "note": "Menu"}, headers=HOTEL
    )
    assert basket.status_code == 201
    body = basket.json()
    assert (body["id"], body["status"], body["total"]) == ("BK-0001", "pending_approval", 1124.0)

    # One decision on any order of the basket decides all of it.
    first = body["orders"][0]["id"]
    client.post(f"/v1/desk/orders/{first}/decision", json={"approved": True}, headers=CHEF)
    after = client.get("/v1/baskets/BK-0001", headers=HOTEL).json()
    assert [o["status"] for o in after["orders"]] == ["confirmed", "confirmed"]
    assert client.get("/v1/baskets/BK-0001", headers=SCHOOL).status_code == 404
    empty = client.post("/v1/baskets", json={"items": [], "title": "x"}, headers=HOTEL)
    assert empty.status_code == 422


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


def test_kitchen_view_and_console_are_served(client: TestClient) -> None:
    kitchen = client.get("/")
    assert kitchen.status_code == 200
    assert "Your purchasing assistant" in kitchen.text
    assert "mandate" not in kitchen.text.split("<script>")[0].lower()  # plain words on the page
    console = client.get("/console")
    assert console.status_code == 200
    assert "Trade console" in console.text


def test_scripted_scenario_runs_a_purchase_and_the_console_can_read_it(client: TestClient) -> None:
    assert "split" in {s["id"] for s in client.get("/demo/scenarios").json()}

    results = client.post("/demo/scenarios/split").json()

    # Two orders of 500 l: the first within the mandate, the second held as a split.
    assert [r["status"] for r in results] == ["confirmed", "pending_approval"]
    newest, oldest = client.get("/v1/desk/requests").json()
    assert (newest["id"], oldest["id"]) == (results[1]["request_id"], results[0]["request_id"])
    assert [step["label"] for step in newest["funnel"]] == [
        "Match the buyer's words and dietary rules",
        "Are the product type that was asked for",
        "Can be ordered now and delivered in time",
        "Options proposed",
    ]
    # 7 lactose-free milks by name, 6 of them in the milk category, 4 orderable for Monday.
    assert [step["count"] for step in newest["funnel"]] == [7, 6, 4, 3]
    line = newest["options"][0]["lines"][0]
    assert (line["country_of_origin"], line["labels"]) == ("fi", [])
    assert client.post("/demo/scenarios/nope").status_code == 404


def test_reset_empties_requests_orders_and_the_audit_log(client: TestClient) -> None:
    client.post("/demo/scenarios/within")
    assert client.post("/demo/reset").status_code == 204
    assert client.get("/v1/desk/requests").json() == []
    assert client.get("/v1/desk/orders").json() == []
    assert client.get("/v1/desk/audit/verify").json()["events"] == 0


def test_mandate_change_is_stored_logged_and_seen_at_once(
    service: PurchasingService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "mandates.json"
    shutil.copy(settings.aimo_agents_defaults, registry)
    monkeypatch.setattr(settings, "aimo_agents_file", registry)
    app.dependency_overrides[get_service] = lambda: service
    try:
        desk = TestClient(app)
        agent = desk.get("/v1/desk/mandates").json()[0]
        assert agent["mandate"]["max_autonomous_spend"] == 1000

        url = f"/v1/desk/mandates/{agent['agent_id']}"
        change = {"max_autonomous_spend": 1200, "allow_late": True}
        assert desk.patch(url, json=change).status_code == 401
        school = {"Authorization": "Bearer demo-kitchen-manager"}
        assert desk.patch(url, json=change, headers=school).status_code == 403

        changed = desk.patch(url, json=change, headers={"Authorization": "Bearer demo-head-chef"})

        assert changed.json()["mandate"]["max_autonomous_spend"] == 1200
        assert desk.get("/v1/desk/mandates").json()[0]["mandate"]["allow_late"] is True
        assert "demo-nordic-hotels-agent" in registry.read_text("utf-8")  # tokens are kept
        (event,) = desk.get("/v1/desk/audit").json()
        assert event["type"] == "mandate.changed"
        assert event["actor"] == "human:Head chef"
        assert "max_autonomous_spend = 1200" in event["summary"]
        assert "demo-head-chef" not in str(
            desk.get("/v1/desk/mandates").json()
        )  # keys stay private
        chef = {"Authorization": "Bearer demo-head-chef"}
        assert desk.patch("/v1/desk/mandates/nobody", json={}, headers=chef).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_pages_are_revalidated_so_a_demo_never_shows_an_old_one(client: TestClient) -> None:
    for page in ("/", "/console"):
        assert client.get(page).headers["cache-control"] == "no-cache"


def test_the_kitchen_view_can_be_added_to_a_home_screen(client: TestClient) -> None:
    manifest = client.get("/assets/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    icons = manifest.json()["icons"]
    for icon in icons:
        assert client.get(icon["src"]).content.startswith(b"\x89PNG")
    assert '<link rel="manifest" href="/assets/manifest.webmanifest">' in client.get("/").text
    assert client.get("/assets/..%2Fapp.py").status_code == 404
    assert client.get("/assets/kitchen.html").status_code == 404


def test_only_a_person_the_buyer_named_can_approve(client: TestClient) -> None:
    request = client.post("/v1/requests", json=MILK, headers=HOTEL).json()
    order = client.post(
        "/v1/orders", json={"request_id": request["id"], "option_id": "opt-1"}, headers=HOTEL
    ).json()
    url = f"/v1/desk/orders/{order['id']}/decision"

    no_key = client.post(url, json={"approved": True})
    assert (no_key.status_code, no_key.json()["title"]) == (401, "Unauthorized")
    assert (
        client.post(url, json={"approved": True}, headers={"Authorization": "Bearer x"}).status_code
        == 401
    )
    other_buyer = client.post(
        url, json={"approved": True}, headers={"Authorization": "Bearer manager-key"}
    )
    assert (other_buyer.status_code, other_buyer.json()["detail"]) == (
        403,
        f"{order['id']} belongs to another buyer.",
    )
    not_named = client.post(
        url, json={"approved": True}, headers={"Authorization": "Bearer porter-key"}
    )
    assert not_named.status_code == 403
    assert "Porter is not among those who may approve" in not_named.json()["detail"]
    assert (
        client.get(f"/v1/orders/{order['id']}", headers=HOTEL).json()["status"]
        == "pending_approval"
    )

    assert client.get("/v1/desk/me", headers=CHEF).json()["name"] == "Head chef"
    assert client.get("/v1/desk/me").status_code == 401
    assert [o["buyer_id"] for o in client.get("/v1/desk/orders?buyer_id=B2").json()] == []
    assert len(client.get("/v1/desk/requests?buyer_id=B1").json()) == 1
    assert {p["key"] for p in client.get("/demo/approvers").json()} == {
        "chef-key",
        "porter-key",
        "manager-key",
    }
