from datetime import date

import pytest

from aimo_agent.catalog import Catalog
from aimo_agent.mandate import evaluate_policy
from aimo_agent.models import Mandate, Need, Offer
from aimo_agent.offers import build_options, evaluate_offer
from tests.conftest import (
    CLOSED_SINCE,
    MONDAY,
    NOW,
    make_availability,
    make_price,
    make_product,
)

MILK = make_product("100", "Milk drink 1 l lactose free")


def offer_for(price=None, availability=None, quantity=100, unit="L") -> Offer:  # noqa: ANN001
    return evaluate_offer(
        MILK, price, availability, quantity=quantity, unit=unit, deliver_by=MONDAY, now=NOW
    )


def need(**fields: object) -> Need:
    base: dict[str, object] = {"query": "milk", "quantity": 100, "unit": "L", "deliver_by": MONDAY}
    return Need.model_validate(base | fields)


def test_full_quantity_on_time_is_available() -> None:
    offer = offer_for(make_price("100", 1.01, 1.01), make_availability("100"))
    assert (offer.status, offer.orderable, offer.on_time) == ("available", True, True)
    assert (offer.units_needed, offer.units_offered, offer.total) == (100, 100, 101.0)
    assert offer.available_units is None  # filler stock value means "not limited"


def test_case_content_is_derived_from_the_comparison_price() -> None:
    offer = offer_for(make_price("100", 27.39, 1.30), make_availability("100"), quantity=1000)
    assert offer.content_per_unit == 21
    assert offer.units_needed == 48
    assert offer.quantity_offered == 1008


def test_short_stock_gives_a_partial_offer() -> None:
    offer = offer_for(make_price("100", 2.57, 2.57), make_availability("100", 51), quantity=200)
    assert offer.status == "partial"
    assert (offer.units_offered, offer.quantity_offered, offer.available_units) == (51, 51, 51)


@pytest.mark.parametrize(
    ("availability", "status"),
    [
        (None, "unavailable"),
        (make_availability("100", 0), "unavailable"),
        (make_availability("100", blocked=True), "blocked"),
        (make_availability("100", closing=CLOSED_SINCE), "order_window_closed"),
    ],
)
def test_products_that_cannot_be_ordered(availability, status) -> None:  # noqa: ANN001
    offer = offer_for(make_price("100", 1.0, 1.0), availability)
    assert offer.status == status
    assert not offer.orderable


def test_delivery_after_the_requested_date_is_late_but_orderable() -> None:
    late = make_availability("100", delivery=date(2026, 11, 18))
    offer = offer_for(make_price("100", 1.0, 1.0), late)
    assert (offer.status, offer.orderable, offer.on_time) == ("late", True, False)


def test_missing_price_and_wrong_unit_are_explained() -> None:
    assert offer_for(None, make_availability("100")).status == "no_price"
    per_kg = make_price("100", 6.0, 12.0, unit="KG")
    assert offer_for(per_kg, make_availability("100")).status == "unit_mismatch"
    # Sized in sales units, the comparison unit does not matter.
    assert offer_for(per_kg, make_availability("100"), unit="SALES_UNIT").status == "available"


def test_stock_reserved_by_earlier_orders_is_not_offered_again() -> None:
    offer = evaluate_offer(
        MILK,
        make_price("100", 1.0, 1.0),
        make_availability("100", 51),
        quantity=40,
        unit="L",
        deliver_by=MONDAY,
        now=NOW,
        reserved_units=30,
    )
    assert (offer.status, offer.units_offered) == ("partial", 21)


def priced(product_id: str, per_litre: float, **stock: object) -> Offer:
    product = make_product(product_id, f"Milk {product_id}")
    return evaluate_offer(
        product,
        make_price(product_id, per_litre, per_litre),
        make_availability(product_id, **stock),  # type: ignore[arg-type]
        quantity=100,
        unit="L",
        deliver_by=MONDAY,
        now=NOW,
    )


def test_options_are_ranked_cheapest_first_and_capped() -> None:
    offers = [priced("a", 1.5), priced("b", 1.0), priced("c", 2.0), priced("d", 1.2)]
    options = build_options(offers, need())
    assert [o.lines[0].product_id for o in options] == ["b", "d", "a"]
    assert [o.id for o in options] == ["opt-1", "opt-2", "opt-3"]
    assert all(o.tier == "best" for o in options)


def test_partial_stock_is_combined_into_a_split_option() -> None:
    offers = [priced("a", 1.0, amount=40), priced("b", 1.2, amount=30), priced("c", 1.5, amount=50)]
    (option,) = build_options(offers, need())
    assert [(line.product_id, line.units) for line in option.lines] == [
        ("a", 40),
        ("b", 30),
        ("c", 30),
    ]
    assert option.tier == "better"
    assert option.coverage_pct == 100
    assert "Split across 3 products." in option.trade_offs


def test_not_enough_stock_anywhere_gives_a_partial_option() -> None:
    (option,) = build_options([priced("a", 1.0, amount=40)], need())
    assert (option.tier, option.quantity_covered, option.coverage_pct) == ("good", 40, 40.0)


def test_late_delivery_is_offered_last_as_a_compromise() -> None:
    offers = [priced("a", 1.0, delivery=date(2026, 11, 18)), priced("b", 3.0)]
    options = build_options(offers, need())
    assert [(o.lines[0].product_id, o.tier, o.on_time) for o in options] == [
        ("b", "best", True),
        ("a", "good", False),
    ]


def test_preference_changes_the_ranking() -> None:
    cheap = priced("a", 1.0)
    reliable = priced("b", 1.5)
    reliable.delivery_reliability = 0.99
    options = build_options([cheap, reliable], need(prefer="reliability"))
    assert options[0].lines[0].product_id == "b"


# Mandate rules


def decide(catalog: Catalog, mandate: Mandate, offers: list[Offer], **need_fields: object):  # noqa: ANN201
    request = need(**need_fields)
    option = build_options(offers, request)[0]
    return evaluate_policy(mandate, request, option, catalog)


def outcomes(decision) -> dict[str, str]:  # noqa: ANN001
    return {rule.rule: rule.outcome for rule in decision.rules}


def test_order_within_every_limit_is_auto_approved(catalog: Catalog) -> None:
    decision = decide(catalog, Mandate(max_autonomous_spend=1000), [priced("100", 1.0)])
    assert decision.decision == "auto_approve"
    assert set(outcomes(decision).values()) == {"pass"}


def test_spend_above_the_limit_needs_approval_and_above_the_cap_is_denied(catalog: Catalog) -> None:
    offers = [priced("100", 5.0)]  # 500 EUR
    assert decide(catalog, Mandate(max_autonomous_spend=400), offers).decision == "needs_approval"
    capped = Mandate(max_autonomous_spend=400, hard_max_order_total=450)
    assert decide(catalog, capped, offers).decision == "deny"


def test_partial_and_late_need_approval_unless_the_mandate_allows_them(catalog: Catalog) -> None:
    partial = [priced("100", 1.0, amount=40)]
    late = [priced("100", 1.0, delivery=date(2026, 11, 18))]
    strict = Mandate(max_autonomous_spend=1000)
    assert outcomes(decide(catalog, strict, partial))["quantity"] == "needs_approval"
    assert outcomes(decide(catalog, strict, late))["delivery_date"] == "needs_approval"
    relaxed = Mandate(max_autonomous_spend=1000, allow_partial=True, allow_late=True)
    assert decide(catalog, relaxed, partial).decision == "auto_approve"
    assert decide(catalog, relaxed, late).decision == "auto_approve"


def test_pack_size_overshoot_beyond_tolerance_needs_approval(catalog: Catalog) -> None:
    case = evaluate_offer(
        make_product("103", "Milk 10 l", sales_unit="KI"),
        make_price("103", 15.70, 1.57),
        make_availability("103"),
        quantity=12,
        unit="L",
        deliver_by=MONDAY,
        now=NOW,
    )  # 2 cases = 20 l for a 12 l need
    decision = decide(catalog, Mandate(max_autonomous_spend=1000), [case], quantity=12)
    assert outcomes(decision)["quantity"] == "needs_approval"


def test_dietary_violation_is_denied_and_cannot_be_approved(catalog: Catalog) -> None:
    granola = evaluate_offer(
        catalog.get("201"),  # type: ignore[arg-type]
        make_price("201", 6.0, 12.0, unit="KG"),
        make_availability("201"),
        quantity=5,
        unit="KG",
        deliver_by=MONDAY,
        now=NOW,
    )
    nut_free = Mandate(max_autonomous_spend=1000, forbidden_allergens=["nuts"])
    decision = decide(catalog, nut_free, [granola], query="granola", quantity=5, unit="kg")
    assert decision.decision == "deny"
    assert "nuts" in decision.summary


def test_category_scope_limits_what_the_agent_buys_alone(catalog: Catalog) -> None:
    dairy_only = Mandate(max_autonomous_spend=1000, allowed_categories=["10400"])
    assert decide(catalog, dairy_only, [priced("100", 1.0)]).decision == "auto_approve"
    other = priced("105", 1.0)  # category 22540 in the catalog
    assert outcomes(decide(catalog, dairy_only, [other]))["category_scope"] == "needs_approval"
