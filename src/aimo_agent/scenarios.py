"""Ready-made purchases for the console: a scripted agent run inside the gateway.

The script does what an external agent does (state the need, take the top option the mandate
does not forbid, say why), through the same service the MCP tools and the REST API use.
"""

from datetime import date
from typing import Any

from pydantic import BaseModel

from aimo_agent.demo_agent import next_monday
from aimo_agent.models import AgentIdentity, Need
from aimo_agent.service import PurchasingService

_PLAIN_MILK = ["sour", "coffee", "latte", "portion", "cafè"]


class Scenario(BaseModel):
    id: str
    title: str
    shows: str
    needs: list[dict[str, Any]]


class ScenarioResult(BaseModel):
    request_id: str
    order_id: str | None = None
    status: str


def _milk(litres: int, words: str) -> dict[str, Any]:
    return {
        "text": words,
        "query": "milk",
        "free_from": ["lactose"],
        "exclude_terms": _PLAIN_MILK,
        "quantity": litres,
        "unit": "L",
    }


SCENARIOS = [
    Scenario(
        id="example",
        title="1,000 l lactose-free milk",
        shows="The challenge's example. The order exceeds the agent's limit and waits for a human.",
        needs=[
            _milk(
                1000,
                "I need 1,000 litres of lactose-free milk by Monday. Find an option that meets "
                "our requirements based on price and availability.",
            )
        ],
    ),
    Scenario(
        id="within",
        title="300 l, within the mandate",
        shows="Small enough for the agent to decide alone: confirmed without a human.",
        needs=[_milk(300, "I need 300 litres of lactose-free milk by Monday.")],
    ),
    Scenario(
        id="split",
        title="Split into 2 x 500 l",
        shows="Splitting does not get around the limit: the second half is held for approval.",
        needs=[
            _milk(500, "I need 500 litres of lactose-free milk by Monday."),
            _milk(500, "And another 500 litres of the same milk for Monday."),
        ],
    ),
    Scenario(
        id="shortage",
        title="The usual product is short",
        shows="51 units in stock for a 200 l order: the closest allergen-safe substitute.",
        needs=[
            {
                "text": "Order 200 litres of our usual Arla fat-free lactose-free UHT milk drink "
                "for Monday.",
                "product_id": "462775",
                "quantity": 200,
                "unit": "L",
            }
        ],
    ),
    Scenario(
        id="butter",
        title="25 kg of butter",
        shows='"Butter" also names beans, buns and peanut butter: the category decides.',
        needs=[
            {
                "text": "We need 25 kg of butter for Monday.",
                "query": "butter",
                "quantity": 25,
                "unit": "kg",
            }
        ],
    ),
    Scenario(
        id="organic",
        title="Organic milk, too late",
        shows="Every organic milk is past its order cut-off: regular milk is proposed, "
        "marked as outside the preference, and a human decides.",
        needs=[
            {
                "text": "200 litres of organic milk for Monday.",
                "query": "milk",
                "labels": ["EU_ORGANIC_FARMING"],
                "exclude_terms": [*_PLAIN_MILK, "coconut", "oat"],
                "quantity": 200,
                "unit": "L",
            }
        ],
    ),
]


async def run_scenario(
    service: PurchasingService, agent: AgentIdentity, scenario: Scenario, today: date
) -> list[ScenarioResult]:
    results = []
    for fields in scenario.needs:
        request = await service.create_request(agent, Need(**fields, deliver_by=next_monday(today)))
        choice = next(
            (o for o in request.options if o.policy and o.policy.decision != "deny"), None
        )
        if choice is None:
            results.append(ScenarioResult(request_id=request.id, status="nothing_orderable"))
            continue
        order = await service.place_order(
            agent,
            request.id,
            choice.id,
            f"Top-ranked option ({choice.tier}): covers {choice.coverage_pct:g}% of the need, "
            f"delivery {choice.delivery_date}, and the mandate does not forbid it.",
        )
        results.append(
            ScenarioResult(request_id=request.id, order_id=order.id, status=order.status)
        )
    return results
