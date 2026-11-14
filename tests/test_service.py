from datetime import date

import pytest

from aimo_agent.catalog import Catalog
from aimo_agent.models import AgentIdentity, Need
from aimo_agent.service import PurchasingService, ServiceError
from aimo_agent.store import Store
from tests.conftest import MONDAY, FakeAimo, make_product

pytestmark = pytest.mark.anyio


def milk(quantity: float = 1000, **fields: object) -> Need:
    base: dict[str, object] = {
        "text": "I need lactose-free milk by Monday.",
        "query": "milk",
        "free_from": ["lactose"],
        "exclude_terms": ["sour"],
        "quantity": quantity,
        "unit": "litres",
        "deliver_by": MONDAY,
    }
    return Need.model_validate(base | fields)


def event_types(service: PurchasingService, **scope: str) -> list[str]:
    return [event.type for event in service.store.events(**scope)]


async def test_request_returns_ranked_options_for_what_was_asked(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    request = await service.create_request(hotel, milk())

    best = request.options[0]
    assert (best.tier, best.lines[0].product_id, best.lines[0].units) == ("best", "100", 1000)
    assert best.total == 1010.0
    # 102 and 108 are too short on stock to cover 1000 l alone; 107 arrives after Monday.
    assert [o.lines[0].product_id for o in request.options] == ["100", "101", "107"]
    assert request.options[-1].tier == "good"  # 107 arrives after Monday

    considered = {line.product_id for o in request.options for line in o.lines}
    excluded = {e.product_id: e.status for e in request.excluded}
    assert "105" not in considered | set(excluded)  # other category: left out by the focus
    assert "106" not in considered | set(excluded)  # not lactose-free
    assert "104" not in considered | set(excluded)  # "sour" excluded by the agent
    assert excluded == {"103": "order_window_closed"}
    assert any("Focused on category 10400" in note for note in request.notes)


async def test_a_bare_word_is_read_as_the_category_it_is_typical_of(
    hotel: AgentIdentity, aimo: FakeAimo
) -> None:
    # "butter" also names beans and buns. Nearly all of category B is butter; the others
    # are a sliver of theirs. The beans are cheaper and must still not win.
    canned = [
        make_product(f"c{n}", f"Canned chick peas {n} kg", category="C", claims=[])
        for n in range(4)
    ]
    catalog = Catalog(
        [
            make_product("b1", "Valio butter 500 g salted", category="B", claims=[]),
            make_product("b2", "Valio butter 500 g unsalted", category="B", claims=[]),
            make_product("c9", "Butter beans 2,5 kg", category="C", claims=[]),
            *canned,
        ]
    )
    aimo.offer("b1", 3.50, 7.00, unit="KG")
    aimo.offer("b2", 3.30, 6.60, unit="KG")
    aimo.offer("c9", 5.00, 2.00, unit="KG")
    service = PurchasingService(catalog, Store(":memory:"), upstream=lambda: aimo)  # type: ignore[arg-type,return-value]

    request = await service.create_request(
        hotel, Need(query="butter", quantity=25, unit="kg", deliver_by=MONDAY)
    )

    assert [o.lines[0].product_id for o in request.options] == ["b2", "b1"]
    assert request.notes[0].startswith("Focused on category B (2 of its 2 products")
    assert (
        "Other categories with matches: C (1 of its 5 products, e.g. Butter beans"
        in (request.notes[0])
    )


async def test_order_within_the_mandate_is_confirmed_without_a_human(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    request = await service.create_request(hotel, milk(300))
    assert request.options[0].policy.decision == "auto_approve"

    order = await service.place_order(hotel, request.id, "opt-1", note="Cheapest on time.")

    assert (order.status, order.id, order.option.total) == ("confirmed", "PO-0001", 303.0)
    assert event_types(service, order_id=order.id) == [
        "request.received",
        "lookup.completed",
        "options.proposed",
        "order.requested",
        "policy.evaluated",
        "order.confirmed",
    ]
    assert service.store.verify_chain() == (True, 6, None)


async def test_order_above_the_spend_limit_waits_for_a_human(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    request = await service.create_request(hotel, milk(1000))
    order = await service.place_order(hotel, request.id, "opt-1")
    assert order.status == "pending_approval"
    assert "exceeds the agent's limit of 1000.00 EUR" in order.status_reason

    approved = await service.decide(order.id, approver="Head chef", approved=True, comment="OK")

    assert approved.status == "confirmed"
    assert approved.approval is not None and approved.approval.approver == "Head chef"
    assert event_types(service, order_id=order.id)[-3:] == [
        "approval.requested",
        "approval.granted",
        "order.confirmed",
    ]
    with pytest.raises(ServiceError) as error:
        await service.decide(order.id, approver="Head chef", approved=True)
    assert error.value.code == "not_pending"


async def test_splitting_an_order_does_not_get_around_the_spend_limit(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    first = await service.create_request(hotel, milk(500))
    assert (await service.place_order(hotel, first.id, "opt-1")).status == "confirmed"

    second = await service.create_request(hotel, milk(500))
    verdict = second.options[0].policy  # the agent is told before it orders
    order = await service.place_order(hotel, second.id, "opt-1")

    assert verdict.decision == "needs_approval"
    assert order.status == "pending_approval"
    assert (
        "Order total 505.00 EUR plus 505.00 EUR the agent already ordered on its own for "
        "Mon 16 Nov (PO-0001) comes to 1010.00 EUR, which exceeds the agent's limit of "
        "1000.00 EUR per delivery day." in order.status_reason
    )


async def test_spend_a_human_approved_does_not_use_up_the_agents_own_budget(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    big = await service.create_request(hotel, milk(1000))
    held = await service.place_order(hotel, big.id, "opt-1")
    await service.decide(held.id, approver="Head chef", approved=True)

    small = await service.create_request(hotel, milk(300))

    assert small.options[0].policy.decision == "auto_approve"


async def test_another_delivery_day_and_another_agent_have_their_own_budgets(
    service: PurchasingService, hotel: AgentIdentity, school: AgentIdentity, aimo: FakeAimo
) -> None:
    first = await service.create_request(hotel, milk(900))
    await service.place_order(hotel, first.id, "opt-1")

    other_agent = await service.create_request(school, milk(300))
    assert other_agent.options[0].policy.decision == "auto_approve"

    aimo.offer("100", 1.01, delivery=date(2026, 11, 17))  # the same milk, a day later
    other_day = await service.create_request(hotel, milk(900, deliver_by=date(2026, 11, 17)))
    assert other_day.options[0].lines[0].product_id == "100"
    assert other_day.options[0].policy.decision == "auto_approve"


def organic(**fields: object) -> Need:
    base: dict[str, object] = {
        "text": "200 litres of organic lactose-free milk by Monday.",
        "query": "milk",
        "free_from": ["lactose"],
        "labels": ["EU_ORGANIC_FARMING"],
        "quantity": 200,
        "unit": "L",
        "deliver_by": MONDAY,
    }
    return Need.model_validate(base | fields)


async def test_when_nothing_preferred_fits_alternatives_are_proposed_for_a_human(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    # The only organic milk (107) arrives after Monday.
    request = await service.create_request(hotel, organic())

    top = request.options[0]
    assert (top.lines[0].product_id, top.tier, top.outside_preferences) == ("100", "better", True)
    assert top.lines[0].unmet_preferences == ["not labelled EU Organic Farming"]
    assert {r.rule: r.outcome for r in top.policy.rules}["stated_preferences"] == "needs_approval"
    assert top.policy.decision == "needs_approval"

    offered = {line.product_id for option in request.options for line in option.lines}
    assert "107" in offered  # the organic one is still there, as a late option
    assert "106" not in offered  # plain milk: the lactose-free constraint was not relaxed
    assert request.funnel[-3].label == "Alternatives outside the stated preferences"


async def test_a_preferred_product_that_fits_needs_no_alternatives(
    service: PurchasingService, hotel: AgentIdentity, aimo: FakeAimo
) -> None:
    aimo.offer("107", 2.19)  # the organic milk can make Monday after all

    request = await service.create_request(hotel, organic())

    (option,) = request.options
    assert (option.lines[0].product_id, option.tier, option.outside_preferences) == (
        "107",
        "best",
        False,
    )
    assert {r.rule: r.outcome for r in option.policy.rules}["stated_preferences"] == "pass"


async def test_the_mandate_can_let_the_agent_accept_alternatives_alone(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    hotel.mandate.allow_outside_preferences = True
    request = await service.create_request(hotel, organic())
    assert request.options[0].policy.decision == "auto_approve"


async def test_rejected_order_stays_rejected(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    request = await service.create_request(hotel, milk(1000))
    order = await service.place_order(hotel, request.id, "opt-1")
    rejected = await service.decide(order.id, approver="Head chef", approved=False)
    assert rejected.status == "rejected"
    assert service.store.reserved_units() == {}


async def test_approval_after_the_cut_off_expires_the_order(
    service: PurchasingService, hotel: AgentIdentity, aimo: FakeAimo
) -> None:
    request = await service.create_request(hotel, milk(1000))
    order = await service.place_order(hotel, request.id, "opt-1")
    aimo.now = aimo.now.replace(day=15, hour=18)  # Sunday evening: the window has closed

    late = await service.decide(order.id, approver="Head chef", approved=True)

    assert late.status == "expired"
    assert "cut-off" in late.status_reason


async def test_short_product_is_topped_up_with_a_safe_substitute(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    request = await service.create_request(
        hotel, Need(product_id="102", quantity=200, unit="L", deliver_by=MONDAY)
    )

    substitute, split = request.options
    assert [(line.product_id, line.is_substitute) for line in substitute.lines] == [("108", True)]
    assert [(line.product_id, line.units) for line in split.lines] == [("102", 51), ("108", 149)]
    assert substitute.tier == split.tier == "better"
    assert substitute.lines[0].price_delta_pct == -3.5
    assert substitute.policy.decision == "auto_approve"  # same profile, cheaper than requested


async def test_substitute_needs_approval_when_the_mandate_forbids_substitutes(
    service: PurchasingService, school: AgentIdentity
) -> None:
    request = await service.create_request(
        school, Need(product_id="102", quantity=100, unit="L", deliver_by=MONDAY)
    )
    rules = {rule.rule: rule.outcome for rule in request.options[0].policy.rules}
    assert rules["substitution"] == "needs_approval"


async def test_substitute_that_adds_an_allergen_is_never_offered(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    request = await service.create_request(
        hotel, Need(product_id="200", quantity=10, unit="L", deliver_by=MONDAY)
    )
    assert {line.product_id for o in request.options for line in o.lines} == {"200"}
    (unsafe,) = request.excluded
    assert (unsafe.product_id, unsafe.status) == ("300", "unsafe_substitute")
    assert "nuts" in unsafe.reason


async def test_order_is_refused_when_the_offer_changed_since_lookup(
    service: PurchasingService, hotel: AgentIdentity, aimo: FakeAimo
) -> None:
    request = await service.create_request(hotel, milk(300))
    aimo.offer("100", 1.25)  # the price moved

    with pytest.raises(ServiceError) as error:
        await service.place_order(hotel, request.id, "opt-1")

    assert error.value.code == "offer_changed"
    assert "price changed from 1.01 to 1.25" in error.value.message
    assert service.store.list_orders() == []
    assert event_types(service, request_id=request.id)[-1] == "order.refused"


async def test_confirmed_orders_reduce_the_stock_offered_next(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    first = await service.create_request(
        hotel, Need(query="fat free", brand="arla", quantity=51, unit="L", deliver_by=MONDAY)
    )
    await service.place_order(hotel, first.id, "opt-1")

    second = await service.create_request(
        hotel, Need(query="fat free", brand="arla", quantity=10, unit="L", deliver_by=MONDAY)
    )

    assert second.options == []
    assert [(e.product_id, e.reason) for e in second.excluded] == [("102", "Out of stock.")]


async def test_forbidden_allergen_is_refused_outright(
    service: PurchasingService, school: AgentIdentity
) -> None:
    request = await service.create_request(
        school, Need(query="granola", quantity=5, unit="kg", deliver_by=MONDAY)
    )
    assert request.options[0].policy.decision == "deny"

    order = await service.place_order(school, request.id, "opt-1")

    assert order.status == "denied"
    with pytest.raises(ServiceError):
        await service.decide(order.id, approver="Kitchen manager", approved=True)


async def test_a_request_cannot_be_ordered_twice_or_read_by_another_buyer(
    service: PurchasingService, hotel: AgentIdentity, school: AgentIdentity
) -> None:
    request = await service.create_request(hotel, milk(100))
    await service.place_order(hotel, request.id, "opt-1")

    with pytest.raises(ServiceError) as twice:
        await service.place_order(hotel, request.id, "opt-2")
    assert twice.value.code == "already_ordered"
    with pytest.raises(ServiceError) as foreign:
        service.get_request(school, request.id)
    assert foreign.value.code == "forbidden"


async def test_no_match_gives_no_options_and_says_why(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    request = await service.create_request(
        hotel, Need(query="unicorn steak", quantity=1, unit="kg", deliver_by=MONDAY)
    )
    assert request.options == []
    assert request.notes == ["No product in the catalog matches the request."]


async def test_tampering_with_the_audit_log_is_detected(
    service: PurchasingService, hotel: AgentIdentity
) -> None:
    await service.create_request(hotel, milk(100))
    service.store._db.execute("UPDATE events SET summary = 'nothing happened' WHERE seq = 2")
    assert service.store.verify_chain() == (False, 1, 2)
