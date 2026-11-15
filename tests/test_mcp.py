import pytest
from mcp import Client

from aimo_agent.config import settings
from aimo_agent.mcp_server import mcp
from aimo_agent.models import AgentIdentity
from aimo_agent.service import PurchasingService

pytestmark = pytest.mark.anyio


@pytest.fixture
def wired(
    service: PurchasingService, hotel: AgentIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aimo_agent.mcp_server.get_service", lambda: service)
    monkeypatch.setattr(
        "aimo_agent.mcp_server.get_agents", lambda: {settings.aimo_agent_token: hotel}
    )


async def test_agent_buys_through_mcp_tools(wired: None) -> None:
    async with Client(mcp) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert {"get_mandate", "search_products", "request_options", "place_order"} <= tools

        request = await client.call_tool(
            "request_options",
            {
                "query": "milk",
                "free_from": ["lactose"],
                "exclude_terms": ["sour"],
                "quantity": 300,
                "unit": "litres",
                "deliver_by": "2026-11-16",
                "buyer_words": "I need 300 litres of lactose-free milk by Monday.",
            },
        )
        body = request.structured_content
        assert body["options"][0]["policy"]["decision"] == "auto_approve"

        order = await client.call_tool(
            "place_order",
            {"request_id": body["id"], "option_id": "opt-1", "reason": "Cheapest on time."},
        )
        assert order.structured_content["status"] == "confirmed"


async def test_agent_orders_a_list_in_two_calls(wired: None) -> None:
    async with Client(mcp) as client:
        listed = await client.call_tool(
            "request_options_for_list",
            {
                "needs": [
                    {
                        "text": "Lactose-free milk for the soup",
                        "query": "milk",
                        "free_from": ["lactose"],
                        "exclude_terms": ["sour"],
                        "quantity": 100,
                        "unit": "litres",
                        "deliver_by": "2026-11-16",
                    },
                    {
                        "text": "Oat drink for the dessert",
                        "query": "oat drink",
                        "quantity": 100,
                        "deliver_by": "2026-11-16",
                    },
                    {
                        "text": "Salmon for the soup",
                        "query": "salmon",
                        "quantity": 6,
                        "unit": "kg",
                        "deliver_by": "2026-11-16",
                    },
                ]
            },
        )
        *found, salmon = listed.structured_content["result"]
        assert [d["asked"] for d in found] == [
            "Lactose-free milk for the soup",
            "Oat drink for the dessert",
        ]
        assert salmon["options"] == []
        option = found[0]["options"][0]
        assert option["proof"] is None  # the short form leaves the signature to the order
        assert option["policy"]["rules"] == []  # only rules that are not passed

        basket = await client.call_tool(
            "place_basket",
            {
                "items": [{"request_id": d["request_id"], "option_id": "opt-1"} for d in found],
                "title": "Dinner for 80",
                "reason": "Cheapest on time for each.",
                "left_out": [{"request_id": salmon["request_id"]}],
            },
        )
        body = basket.structured_content
        assert (body["status"], len(body["orders"])) == ("confirmed", 2)
        assert [g["asked"] for g in body["left_out"]] == ["Salmon for the soup"]

        read = await client.call_tool("get_basket", {"basket_id": body["basket_id"]})
        assert read.structured_content["total"] == body["total"]


async def test_expected_failures_reach_the_model_as_readable_errors(wired: None) -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("get_order", {"order_id": "PO-9999"})
        assert result.is_error
        assert "not_found" in result.content[0].text
