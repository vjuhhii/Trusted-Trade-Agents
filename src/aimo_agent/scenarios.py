"""Ready-made purchases for the console: a scripted agent run inside the gateway.

The script does what an external agent does (state the need, take the top option the mandate
does not forbid, say why), through the same service the MCP tools and the REST API use.
"""

from datetime import date
from typing import Any

from pydantic import BaseModel

from aimo_agent.demo_agent import next_monday
from aimo_agent.models import AgentIdentity, LeftOutItem, Need, Option, PurchaseRequest
from aimo_agent.offers import day
from aimo_agent.service import PurchasingService

_PLAIN_MILK = ["sour", "coffee", "latte", "portion", "cafè"]


class Scenario(BaseModel):
    id: str
    title: str
    shows: str
    needs: list[dict[str, Any]]
    basket: str | None = None  # set: the needs are asked for at once and ordered as one basket


class ScenarioResult(BaseModel):
    request_id: str
    order_id: str | None = None
    basket_id: str | None = None
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
    Scenario(
        id="event",
        title="Wedding dinner for 120, as one basket",
        shows="Seven needs in one call, six orderable products in one order. Each is within "
        "the limit, together they are not: the whole basket waits for one approval. The salmon "
        "cannot arrive in time and is flagged, not swapped.",
        basket="Wedding dinner for 120 on Monday",
        needs=[
            {
                "text": "50 kg of beef for the braise",
                "query": "beef",
                "exclude_terms": ["pork", "broth", "stock", "bouillon", "cooked", "meatball"],
                "quantity": 50,
                "unit": "kg",
            },
            {
                "text": "60 kg of potatoes",
                "query": "potato",
                "category": "17421",
                "quantity": 60,
                "unit": "kg",
            },
            {
                "text": "30 litres of lactose-free cream for the sauce",
                "query": "cream",
                "free_from": ["lactose"],
                "exclude_terms": ["sour", "ice", "cheese", "alternative", "coffee", "spray"],
                "quantity": 30,
                "unit": "L",
            },
            {
                "text": "10 kg of butter",
                "query": "butter",
                "category": "10500",
                "quantity": 10,
                "unit": "kg",
            },
            {
                "text": "20 kg of onions",
                "query": "onion",
                "category": "17405",
                "quantity": 20,
                "unit": "kg",
            },
            {
                "text": "10 kg of lingonberries",
                "query": "lingonberry",
                "exclude_terms": ["juice", "drink", "jam"],
                "quantity": 10,
                "unit": "kg",
            },
            {
                "text": "15 kg of salmon fillet for the starter",
                "query": "salmon fillet",
                "quantity": 15,
                "unit": "kg",
            },
        ],
    ),
]


def _choice(request: PurchaseRequest) -> Option | None:
    """The top-ranked option the mandate does not forbid."""
    return next((o for o in request.options if o.policy and o.policy.decision != "deny"), None)


def _why(option: Option) -> str:
    """The scripted agent's reason, in the words a cook reads on the kitchen view."""
    arrives = f"arriving {day(option.delivery_date)}"
    if option.tier == "best":
        return f"Exactly what you asked for, all of it, {arrives}: the cheapest way to get it."
    if option.tier == "better":
        if option.has_substitute:
            return (
                f"What you asked for is short, so I took the closest safe replacement: all of "
                f"it, {arrives}. The differences are listed with the product."
            )
        return f"All of it, {arrives}, though not from a single product."
    return f"The most that can be had: {option.coverage_pct:g}% of it, {arrives}."


async def run_scenario(
    service: PurchasingService, agent: AgentIdentity, scenario: Scenario, today: date
) -> list[ScenarioResult]:
    needs = [Need(**fields, deliver_by=next_monday(today)) for fields in scenario.needs]
    if scenario.basket:
        return await _run_basket(service, agent, scenario.basket, needs)
    results = []
    for need in needs:
        request = await service.create_request(agent, need)
        choice = _choice(request)
        if choice is None:
            results.append(ScenarioResult(request_id=request.id, status="nothing_orderable"))
            continue
        order = await service.place_order(agent, request.id, choice.id, _why(choice))
        results.append(
            ScenarioResult(request_id=request.id, order_id=order.id, status=order.status)
        )
    return results


async def _run_basket(
    service: PurchasingService, agent: AgentIdentity, title: str, needs: list[Need]
) -> list[ScenarioResult]:
    """Two calls, as an agent with a list makes them: all needs at once, then one basket."""
    requests = await service.create_requests(agent, needs)
    chosen = [(request, _choice(request)) for request in requests]
    items = [(request.id, option.id) for request, option in chosen if option]
    results = [
        ScenarioResult(request_id=request.id, status="nothing_orderable")
        for request, option in chosen
        if option is None
    ]
    if not items:
        return results
    by = day(needs[0].deliver_by)
    # What could not be found goes to left_out: the kitchen view shows it, the note need not.
    note = f"For each of the {len(items)} products, the cheapest that arrives by {by}."
    gaps = [LeftOutItem(request_id=request.id) for request, option in chosen if option is None]
    basket = await service.place_basket(agent, items, title, note, gaps)
    return [
        ScenarioResult(
            request_id=order.request_id, order_id=order.id, basket_id=basket.id, status=order.status
        )
        for order in basket.orders
    ] + results
