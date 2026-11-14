"""The guardrails: applying a buyer's mandate to an option the agent wants to order.

Every rule yields pass, needs_approval or deny with a sentence a human can read. The worst
outcome decides. The rules run on the wholesaler side, so they hold whatever the agent does.
"""

from collections.abc import Sequence

from aimo_agent.catalog import Catalog
from aimo_agent.models import Mandate, Need, Option, Outcome, PolicyDecision, RuleResult
from aimo_agent.offers import UNIT_LABEL, day


def evaluate_policy(
    mandate: Mandate,
    need: Need,
    option: Option,
    catalog: Catalog,
    *,
    already_spent: float = 0.0,
    earlier_orders: Sequence[str] = (),
) -> PolicyDecision:
    """Apply the mandate to one option.

    `already_spent` is what the agent has committed on its own for the option's delivery day
    (`earlier_orders` names those orders). It counts against the spend limit, so an order split
    into smaller ones meets the same limit as the whole.
    """
    rules: list[RuleResult] = []
    unit = UNIT_LABEL[need.unit]

    def add(rule: str, outcome: Outcome, message: str) -> None:
        rules.append(RuleResult(rule=rule, outcome=outcome, message=message))

    # Dietary constraints are never negotiable: a violation cannot be approved by anyone.
    violations: list[str] = []
    for line in option.lines:
        product = catalog.get(line.product_id)
        if product is None:
            violations.append(f"{line.product_id} is not in the catalog")
            continue
        violations += [
            f"{product.name} is not declared free from {substance}"
            for substance in need.free_from
            if not product.is_free_from(substance)
        ]
        violations += [
            f"{product.name} contains or may contain {substance}"
            for substance in sorted({*need.exclude_allergens, *mandate.forbidden_allergens})
            if product.declares_allergen(substance)
        ]
    add(
        "dietary_constraints",
        "deny" if violations else "pass",
        "; ".join(violations) + "."
        if violations
        else "Every product meets the dietary constraints of the request and the mandate.",
    )

    if mandate.hard_max_order_total is not None and option.total > mandate.hard_max_order_total:
        add(
            "spend_limit",
            "deny",
            f"Order total {option.total:.2f} EUR exceeds the absolute limit of "
            f"{mandate.hard_max_order_total:.2f} EUR.",
        )
    else:
        # The limit is a budget per delivery day, not per order: what the agent already
        # ordered alone for that day counts too, so splitting an order gains nothing.
        limit = f"{mandate.max_autonomous_spend:.2f} EUR per delivery day"
        day_total = round(option.total + already_spent, 2)
        subject = f"Order total {option.total:.2f} EUR"
        if already_spent:
            subject += (
                f" plus {already_spent:.2f} EUR the agent already ordered on its own for "
                f"{day(option.delivery_date)} ({', '.join(earlier_orders)}) comes to "
                f"{day_total:.2f} EUR, which"
            )
        if day_total > mandate.max_autonomous_spend:
            add("spend_limit", "needs_approval", f"{subject} exceeds the agent's limit of {limit}.")
        else:
            add("spend_limit", "pass", f"{subject} is within the agent's limit of {limit}.")

    substitutes = [line for line in option.lines if line.is_substitute]
    tolerance = mandate.substitute_max_price_increase_pct
    if not substitutes:
        add("substitution", "pass", "No substitution: the products are what was asked for.")
    elif not mandate.allow_substitutes:
        add(
            "substitution",
            "needs_approval",
            "The mandate does not let the agent accept substitutes on its own.",
        )
    else:
        unknown = [line.name for line in substitutes if line.price_delta_pct is None]
        dearer = [
            f"{line.name} ({line.price_delta_pct:+.1f}%)"
            for line in substitutes
            if line.price_delta_pct is not None and line.price_delta_pct > tolerance
        ]
        if dearer:
            add(
                "substitution",
                "needs_approval",
                f"Substitute price per {unit} is above the {tolerance:g}% tolerance: "
                f"{', '.join(dearer)}.",
            )
        elif unknown:
            add(
                "substitution",
                "needs_approval",
                "The requested product has no price, so the price change of the substitute "
                f"cannot be checked: {', '.join(unknown)}.",
            )
        else:
            add(
                "substitution",
                "pass",
                f"Substitutes keep the dietary profile and stay within the {tolerance:g}% "
                "price tolerance.",
            )

    overshoot_pct = (option.quantity_covered / need.quantity - 1) * 100
    if option.quantity_covered < need.quantity - 1e-6:
        add(
            "quantity",
            "pass" if mandate.allow_partial else "needs_approval",
            f"Only {option.quantity_covered:g} of {need.quantity:g} {unit} can be supplied "
            f"({option.coverage_pct:g}%).",
        )
    elif overshoot_pct > mandate.overage_tolerance_pct:
        add(
            "quantity",
            "needs_approval",
            f"Pack sizes overshoot the quantity by {overshoot_pct:.1f}%, above the "
            f"{mandate.overage_tolerance_pct:g}% tolerance.",
        )
    else:
        add("quantity", "pass", f"The full {need.quantity:g} {unit} is covered.")

    if option.on_time:
        add(
            "delivery_date",
            "pass",
            f"Delivery on {day(option.delivery_date)}, on or before the requested "
            f"{day(need.deliver_by)}.",
        )
    else:
        add(
            "delivery_date",
            "pass" if mandate.allow_late else "needs_approval",
            f"Delivery on {day(option.delivery_date)} is after the requested "
            f"{day(need.deliver_by)}.",
        )

    outside = [
        f"{line.name} ({', '.join(line.unmet_preferences)})"
        for line in option.lines
        if line.unmet_preferences
    ]
    if outside:
        add(
            "stated_preferences",
            "pass" if mandate.allow_outside_preferences else "needs_approval",
            f"Outside the buyer's stated preferences: {'; '.join(outside)}.",
        )
    elif need.labels or need.brand or need.country_of_origin:
        add("stated_preferences", "pass", "Every product meets the stated preferences.")

    if mandate.allowed_categories is not None:
        outside = sorted(
            {
                product.category
                for line in option.lines
                if (product := catalog.get(line.product_id))
                and product.category not in mandate.allowed_categories
            }
        )
        add(
            "category_scope",
            "needs_approval" if outside else "pass",
            f"Categories outside the agent's scope: {', '.join(outside)}."
            if outside
            else "All products are in categories the agent may buy from.",
        )

    denied = [r.message for r in rules if r.outcome == "deny"]
    escalated = [r.message for r in rules if r.outcome == "needs_approval"]
    if denied:
        return PolicyDecision(
            decision="deny", summary="Refused by the mandate: " + " ".join(denied), rules=rules
        )
    if escalated:
        return PolicyDecision(
            decision="needs_approval",
            summary="Needs human approval: " + " ".join(escalated),
            rules=rules,
        )
    return PolicyDecision(
        decision="auto_approve",
        summary="Within the mandate: the agent may place this order on its own.",
        rules=rules,
    )
