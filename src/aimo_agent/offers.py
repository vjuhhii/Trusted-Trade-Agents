"""Turning live price and availability into offers, and offers into ranked options."""

import math
from collections.abc import Callable
from datetime import date, datetime

from aimo_agent.aimo_client import ProductAvailability, ProductPrice
from aimo_agent.catalog import Product
from aimo_agent.models import Need, Offer, OfferStatus, Option, OptionLine
from aimo_agent.units import content_per_sales_unit, order_step, units_needed

# The API reports 999999 (or larger filler values) when stock is not limited or not known.
UNLIMITED_STOCK = 999_999
MAX_SINGLE_OPTIONS = 3
MAX_LATE_OPTIONS = 2
MAX_SPLIT_LINES = 4

UNIT_LABEL = {"L": "L", "KG": "kg", "SALES_UNIT": "units"}
_TIER_ORDER = {"best": 0, "better": 1, "good": 2}


def day(value: date) -> str:
    return value.strftime("%a %d %b")


def moment(value: datetime) -> str:
    return value.strftime("%a %d %b %H:%M")


def evaluate_offer(
    product: Product,
    price: ProductPrice | None,
    availability: ProductAvailability | None,
    *,
    quantity: float,
    unit: str,
    deliver_by: date,
    now: datetime,
    reserved_units: float = 0.0,
) -> Offer:
    """Price one product and size it to `quantity` of `unit` (L, KG or SALES_UNIT).

    `now` is the wholesaler's clock, used for the order cut-off. `reserved_units` is stock
    already committed to earlier orders made through this interface.
    """
    offer = Offer(
        product_id=product.id,
        name=product.name,
        brand=product.brand,
        category=product.category,
        sales_unit=product.sales_unit,
        labels=product.labels,
        country_of_origin=product.country_of_origin,
        delivery_reliability=product.delivery_reliability,
        carbon_kg_co2e_per_kg=product.carbon_kg_co2e_per_kg,
        status="unavailable",
        reason="",
    )

    def stop(status: OfferStatus, reason: str) -> Offer:
        offer.status, offer.reason = status, reason
        return offer

    catalog_price = price.catalog_price if price and not price.error else None
    if price is None or catalog_price is None:
        return stop("no_price", "The price API has no price for this product.")
    offer.unit_price = catalog_price.value
    offer.unit_price_with_tax = catalog_price.value_with_tax
    offer.price_valid_to = catalog_price.valid_to
    offer.tax_rate = price.tax.tax_rate if price.tax else None
    offer.comparison_unit = price.comparison_unit
    offer.comparison_price = price.comparison_price.value if price.comparison_price else None

    label = UNIT_LABEL[unit]
    if unit == "SALES_UNIT":
        content = 1.0
    else:
        # The price list's own ratio first; failing that, the net content the product data
        # declares (a 1 kg bag of sugar is priced per bag only, but its data says 1000 g).
        measured = None
        if price.comparison_unit == unit and offer.comparison_price is not None:
            measured = content_per_sales_unit(offer.unit_price, offer.comparison_price)
            offer.content_source = "price_list" if measured else None
        if measured is None:
            measured = product.declared_content(unit)
            offer.content_source = "product_data" if measured else None
        if measured is None:
            basis = (
                f"per {price.comparison_unit}" if price.comparison_unit else "per sales unit only"
            )
            return stop(
                "unit_mismatch",
                f"Priced {basis} and no net content in {label} is declared, so it cannot be "
                f"sized in {label}.",
            )
        content = measured
    offer.content_per_unit = content

    line = availability.schedule_lines[0] if availability and availability.schedule_lines else None
    if availability is None or line is None or line.available_date is None:
        return stop("unavailable", "Not deliverable on any date at the moment.")
    offer.logistic_model = availability.logistic_model
    offer.delivery_date = line.available_date
    offer.order_by = availability.closing_time
    if availability.sales_block or line.sales_block:
        return stop("blocked", "Sales block: the product cannot be ordered currently.")
    if availability.closing_time and now > availability.closing_time:
        return stop(
            "order_window_closed",
            f"The order cut-off for delivery on {day(line.available_date)} passed on "
            f"{moment(availability.closing_time)}.",
        )

    limited = line.available_amount < UNLIMITED_STOCK
    in_stock = max(line.available_amount - reserved_units, 0.0) if limited else None
    offer.available_units = in_stock
    offer.order_step = order_step(product.lot_size, product.sales_unit)
    needed = units_needed(quantity, content, offer.order_step)
    offered = needed
    if in_stock is not None:
        whole_steps = math.floor(in_stock / offer.order_step + 1e-9) * offer.order_step
        offered = min(needed, round(whole_steps, 4))
    if offered <= 0:
        return stop("unavailable", "Out of stock.")

    offer.units_needed = needed
    offer.units_offered = offered
    offer.quantity_offered = round(offered * content, 3)
    offer.total = round(offered * offer.unit_price, 2)
    offer.total_with_tax = round(offered * offer.unit_price_with_tax, 2)
    offer.orderable = True
    offer.on_time = line.available_date <= deliver_by

    deadline = f"; order by {moment(offer.order_by)}" if offer.order_by else ""
    if not offer.on_time:
        return stop(
            "late",
            f"Earliest delivery is {day(line.available_date)}, after the requested "
            f"{day(deliver_by)}{deadline}.",
        )
    if offered < needed:
        return stop(
            "partial",
            f"Only {in_stock:g} {product.sales_unit} in stock: covers {offer.quantity_offered:g} "
            f"of {quantity:g} {label}{deadline}.",
        )
    return stop("available", f"Full quantity deliverable on {day(line.available_date)}{deadline}.")


def _ranking(need: Need) -> Callable[[Offer], tuple[float, ...]]:
    def cost(offer: Offer) -> float:
        # What the buyer pays for the quantity asked, in whole packs: a cheaper price per kg in
        # a pack that overshoots the need is not cheaper for the buyer.
        units = offer.units_needed if offer.units_needed is not None else math.inf
        return units * (offer.unit_price or 0.0)

    if need.prefer == "reliability":
        return lambda o: (-(o.delivery_reliability or 0.0), cost(o))
    if need.prefer == "carbon":
        return lambda o: (
            o.carbon_kg_co2e_per_kg if o.carbon_kg_co2e_per_kg is not None else math.inf,
            cost(o),
        )
    return lambda o: (cost(o),)


def _fill(offers: list[Offer], need: Need) -> list[tuple[Offer, float]]:
    """Cover the quantity from several offers, taking them in the given order."""
    parts: list[tuple[Offer, float]] = []
    remaining = need.quantity
    for offer in offers:
        if remaining <= 1e-9 or len(parts) == MAX_SPLIT_LINES:
            break
        content = offer.content_per_unit or 1.0
        units = min(offer.units_offered or 0.0, units_needed(remaining, content, offer.order_step))
        if units > 0:
            parts.append((offer, units))
            remaining -= units * content
    return parts


def _option(parts: list[tuple[Offer, float]], need: Need) -> Option:
    unit = UNIT_LABEL[need.unit]
    lines = [
        OptionLine(
            product_id=offer.product_id,
            name=offer.name,
            sales_unit=offer.sales_unit,
            units=units,
            quantity=round(units * (offer.content_per_unit or 1.0), 3),
            unit_price=offer.unit_price or 0.0,
            total=round(units * (offer.unit_price or 0.0), 2),
            total_with_tax=round(units * (offer.unit_price_with_tax or 0.0), 2),
            delivery_date=offer.delivery_date or need.deliver_by,
            order_by=offer.order_by,
            is_substitute=offer.is_substitute,
            price_delta_pct=offer.price_delta_pct,
            unmet_preferences=offer.unmet_preferences,
            country_of_origin=offer.country_of_origin,
            labels=offer.labels,
            delivery_reliability=offer.delivery_reliability,
            carbon_kg_co2e_per_kg=offer.carbon_kg_co2e_per_kg,
        )
        for offer, units in parts
    ]
    covered = round(sum(line.quantity for line in lines), 3)
    full = covered >= need.quantity - 1e-6
    delivery = max(line.delivery_date for line in lines)
    on_time = delivery <= need.deliver_by
    has_substitute = any(line.is_substitute for line in lines)
    outside_preferences = any(line.unmet_preferences for line in lines)

    trade_offs: list[str] = []
    if len(lines) > 1:
        trade_offs.append(f"Split across {len(lines)} products.")
    for offer, _ in parts:
        if offer.content_source == "product_data":
            trade_offs.append(
                f"{offer.name}: {offer.content_per_unit:g} {unit} per {offer.sales_unit} as the "
                f"product data declares it; the price list gives no price per {unit}."
            )
        if offer.unmet_preferences:
            trade_offs.append(
                f"{offer.name} is outside the stated preferences: "
                f"{', '.join(offer.unmet_preferences)}."
            )
        if offer.is_substitute:
            delta = (
                f" ({offer.price_delta_pct:+.1f}% per {unit})"
                if offer.price_delta_pct is not None
                else ""
            )
            source = "wholesaler-listed substitute" if offer.listed_substitute else "substitute"
            detail = f": {'; '.join(offer.differences)}" if offer.differences else ""
            trade_offs.append(f"{offer.name} is a {source}{delta}{detail}.")
    if not full:
        share = covered / need.quantity * 100
        trade_offs.append(f"Covers {covered:g} of {need.quantity:g} {unit} ({share:.0f}%).")
    elif covered > need.quantity * 1.0001:
        trade_offs.append(f"Pack sizes round the quantity up to {covered:g} {unit}.")
    if not on_time:
        days_late = (delivery - need.deliver_by).days
        trade_offs.append(
            f"Delivery on {day(delivery)}, {days_late} day(s) after the requested "
            f"{day(need.deliver_by)}."
        )
    for offer, _ in parts:
        if offer.logistic_model == "Terminal":
            trade_offs.append(
                f"{offer.name} ships from the supplier's warehouse: "
                "the quantity is not confirmed at order time."
            )

    cut_offs = [line.order_by for line in lines if line.order_by]
    if len(lines) == 1:
        title = f"{lines[0].units:g} {lines[0].sales_unit} x {lines[0].name}"
    else:
        title = " + ".join(f"{line.units:g} {line.sales_unit} x {line.name}" for line in lines)
    return Option(
        id="",
        tier="good"
        if not (full and on_time)
        else "better"
        if has_substitute or outside_preferences or len(lines) > 1
        else "best",
        title=title,
        lines=lines,
        quantity_requested=need.quantity,
        quantity_covered=covered,
        unit=need.unit,
        coverage_pct=round(min(covered / need.quantity, 1.0) * 100, 1),
        total=round(sum(line.total for line in lines), 2),
        total_with_tax=round(sum(line.total_with_tax for line in lines), 2),
        delivery_date=delivery,
        on_time=on_time,
        order_by=min(cut_offs) if cut_offs else None,
        has_substitute=has_substitute,
        outside_preferences=outside_preferences,
        trade_offs=trade_offs,
    )


def build_options(offers: list[Offer], need: Need) -> list[Option]:
    """Rank concrete ways to fulfil the need, from exactly-as-asked down to compromises.

    The requested product always comes before substitutes; within each group offers follow the
    buyer's preference (price by default). Price is what the buyer pays for the quantity asked,
    in whole packs. A cheaper option that overshoots the quantity still ranks first: whether
    that needs a human is the mandate's call, not the ranking's.
    """
    rank = _ranking(need)

    def order(offer: Offer) -> tuple[float, ...]:
        # Substitutes: the wholesaler's own list first, then the closest product, then the
        # buyer's preference. Similarity is compared in steps of 0.1 so price breaks near-ties.
        unlisted = offer.is_substitute and not offer.listed_substitute
        closeness = -round(offer.similarity or 0.0, 1)
        # What meets the stated preferences always comes before what does not.
        outside = float(bool(offer.unmet_preferences))
        return (outside, float(offer.is_substitute), float(unlisted), closeness, *rank(offer))

    orderable = [o for o in offers if o.orderable]
    on_time = sorted((o for o in orderable if o.on_time), key=order)
    late = sorted((o for o in orderable if not o.on_time), key=order)

    def covers_fully(offer: Offer) -> bool:
        return (offer.units_offered or 0.0) >= (offer.units_needed or math.inf)

    candidates: list[list[tuple[Offer, float]]] = []
    full_on_time = [o for o in on_time if covers_fully(o)]
    candidates += [[(o, o.units_needed or 0.0)] for o in full_on_time[:MAX_SINGLE_OPTIONS]]

    # A split is worth offering when single products are scarce, or when the specifically
    # requested product is short and a substitute can top it up.
    requested_is_short = any(not o.is_substitute and not covers_fully(o) for o in on_time)
    if len(full_on_time) < MAX_SINGLE_OPTIONS or (need.product_id and requested_is_short):
        split = _fill(on_time, need)
        if split and (len(split) > 1 or not full_on_time):
            candidates.append(split)

    full_late = [o for o in late if covers_fully(o)]
    candidates += [[(o, o.units_needed or 0.0)] for o in full_late[:MAX_LATE_OPTIONS]]
    if not candidates and late:
        candidates.append(_fill(late, need))

    options: list[Option] = []
    seen: set[tuple[tuple[str, float], ...]] = set()
    for parts in candidates:
        signature = tuple(sorted((offer.product_id, units) for offer, units in parts))
        if parts and signature not in seen:
            seen.add(signature)
            options.append(_option(parts, need))
    options.sort(key=lambda option: _TIER_ORDER[option.tier])
    for number, option in enumerate(options, start=1):
        option.id = f"opt-{number}"
    return options
