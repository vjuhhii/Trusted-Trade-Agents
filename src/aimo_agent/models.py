"""Shared data models of the purchasing flow: need -> offers -> options -> order -> audit."""

from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from aimo_agent.catalog import DIETARY, SearchFilters
from aimo_agent.units import normalize_quantity

OfferStatus = Literal[
    "available",  # full quantity, on time
    "partial",  # on time, but stock covers only part of the quantity
    "late",  # orderable, but delivered after the requested date
    "order_window_closed",  # the cut-off for the offered delivery date has passed
    "blocked",  # sales block at the wholesaler
    "unavailable",  # no delivery date or no stock
    "no_price",  # the price API has no price
    "unit_mismatch",  # cannot be sized in the requested unit
    "unsafe_substitute",  # would add an allergen or drop a free-from claim
]
Outcome = Literal["pass", "needs_approval", "deny"]
Decision = Literal["auto_approve", "needs_approval", "deny"]
OrderStatus = Literal["confirmed", "pending_approval", "rejected", "denied", "expired"]


class Need(SearchFilters):
    """What the buyer wants, in structured form. The agent translates the buyer's words into it."""

    text: str | None = Field(
        None, description="The buyer's original wording, kept verbatim for the audit trail"
    )
    product_id: str | None = Field(
        None,
        description="A specific product that is wanted. Other products are then substitutes.",
    )
    quantity: float = Field(gt=0, description="Amount wanted, in `unit`")
    unit: str = Field("L", description="L, KG or SALES_UNIT. Also accepts litres, dl, ml, g, units")
    deliver_by: date = Field(description="Latest acceptable delivery date")
    prefer: Literal["price", "reliability", "carbon"] = Field(
        "price", description="What to rank options by when several fit"
    )

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        self.quantity, self.unit = normalize_quantity(self.quantity, self.unit)
        unknown = self.unknown_substances()
        if unknown:
            raise ValueError(f"Unknown substances {unknown}. Known: {', '.join(DIETARY)}")
        if not self.query.strip() and not self.product_id:
            raise ValueError("Give `query`, `product_id`, or both")
        return self


class Offer(BaseModel):
    """One product priced and checked for availability against a need. Prices are fixed."""

    product_id: str
    name: str
    brand: str | None = None
    category: str
    sales_unit: str
    labels: list[str] = []
    country_of_origin: str | None = None
    delivery_reliability: float | None = None
    carbon_kg_co2e_per_kg: float | None = None

    status: OfferStatus
    reason: str = Field(description="Why the offer has this status, in plain words")
    orderable: bool = Field(False, description="Can be ordered right now")
    on_time: bool = Field(False, description="Delivered on or before the requested date")

    unit_price: float | None = Field(None, description="EUR per sales unit, without VAT")
    unit_price_with_tax: float | None = None
    tax_rate: float | None = Field(None, description="VAT percent")
    comparison_price: float | None = Field(None, description="EUR per comparison unit, without VAT")
    comparison_unit: str | None = Field(None, description="L or KG")
    content_per_unit: float | None = Field(
        None, description="Requested units (L or KG) contained in one sales unit"
    )
    content_source: Literal["price_list", "product_data"] | None = Field(
        None,
        description="Where content_per_unit comes from: the price list's comparison price, or "
        "the net content declared in the product data when the price list has none",
    )
    price_valid_to: datetime | None = None

    logistic_model: str | None = Field(
        None, description="Stock: quantity is known. Terminal: supplier warehouse, not known"
    )
    available_units: float | None = Field(
        None, description="Sales units in stock. Null when not limited or not known"
    )
    delivery_date: date | None = None
    order_by: datetime | None = Field(None, description="Order cut-off for the delivery date")

    order_step: float = Field(1.0, description="Order quantity step, in sales units")
    units_needed: float | None = Field(None, description="Sales units covering the full need")
    units_offered: float | None = Field(None, description="Sales units that can be supplied")
    quantity_offered: float | None = Field(None, description="Supplied amount in the need's unit")
    total: float | None = Field(None, description="EUR without VAT for units_offered")
    total_with_tax: float | None = None

    is_substitute: bool = Field(
        False, description="Differs from the specifically requested product"
    )
    listed_substitute: bool = Field(False, description="Named as a substitute by the wholesaler")
    similarity: float | None = Field(
        None, description="How close a substitute's name is to the requested product, 0-1"
    )
    unmet_preferences: list[str] = Field(
        default_factory=list,
        description="Stated preferences (label, brand, origin) this product does not meet",
    )
    differences: list[str] = Field(
        default_factory=list, description="How a substitute differs from the requested product"
    )
    price_delta_pct: float | None = Field(
        None, description="Price per litre/kg compared with the requested product, percent"
    )


class OptionLine(BaseModel):
    product_id: str
    name: str
    sales_unit: str
    units: float = Field(description="Sales units to order")
    quantity: float = Field(description="Amount covered, in the need's unit")
    unit_price: float
    total: float
    total_with_tax: float
    delivery_date: date
    order_by: datetime | None = None
    is_substitute: bool = False
    price_delta_pct: float | None = None
    unmet_preferences: list[str] = Field(
        default_factory=list,
        description="Stated preferences (label, brand, origin) this product does not meet",
    )
    country_of_origin: str | None = None
    labels: list[str] = Field(default_factory=list, description="Certificates and labels")
    delivery_reliability: float | None = Field(
        None, description="Historic share of ordered quantity delivered, 0-1"
    )
    carbon_kg_co2e_per_kg: float | None = Field(
        None, description="Average footprint of the product group, kg CO2e per kg"
    )


class RuleResult(BaseModel):
    rule: str
    outcome: Outcome
    message: str


class PolicyDecision(BaseModel):
    """The mandate applied to one option: what happens if the agent orders it."""

    decision: Decision
    summary: str
    rules: list[RuleResult]


class Option(BaseModel):
    """One concrete way to fulfil the need. The agent orders by option id."""

    id: str
    tier: Literal["best", "better", "good"] = Field(
        description="best: exactly as asked. better: full and on time with a compromise "
        "(substitute or split). good: partial quantity or late delivery"
    )
    title: str
    lines: list[OptionLine]
    quantity_requested: float
    quantity_covered: float
    unit: str
    coverage_pct: float
    total: float = Field(description="EUR without VAT")
    total_with_tax: float
    currency: str = "EUR"
    delivery_date: date
    on_time: bool
    order_by: datetime | None = Field(None, description="Earliest cut-off among the lines")
    has_substitute: bool = False
    outside_preferences: bool = Field(
        False, description="Contains a product that does not meet a stated preference"
    )
    trade_offs: list[str] = Field(default_factory=list)
    policy: PolicyDecision | None = None
    proof: str | None = Field(
        None,
        description="The wholesaler's signature over this option's products, quantities, "
        "prices, delivery date and origin: a JWS (EdDSA). Anyone can verify it with the keys "
        "at /.well-known/jwks.json, without trusting the party that relayed the offer",
    )


class Excluded(BaseModel):
    product_id: str
    name: str
    status: OfferStatus
    reason: str


class FunnelStep(BaseModel):
    label: str
    count: int


class PurchaseRequest(BaseModel):
    id: str
    created_at: datetime
    agent_id: str
    buyer_id: str
    need: Need
    server_time: datetime = Field(description="Wholesaler clock used for cut-off checks")
    candidates_considered: int
    funnel: list[FunnelStep] = Field(
        default_factory=list,
        description="How the catalog was narrowed down to the options, step by step",
    )
    notes: list[str] = Field(default_factory=list)
    options: list[Option]
    excluded: list[Excluded] = Field(
        default_factory=list, description="Matching products that cannot be ordered, with reasons"
    )
    closed_as: Literal["left_out", "superseded"] | None = Field(
        None,
        description="Set when a basket was placed without this request. left_out: the agent "
        "could not fill the need and reported it. superseded: a working step the agent replaced",
    )
    basket_id: str | None = Field(None, description="The basket that closed this request")
    closed_reason: str | None = None


class Approval(BaseModel):
    approver: str
    approved: bool
    comment: str | None = None
    decided_at: datetime


class Order(BaseModel):
    id: str
    request_id: str
    created_at: datetime
    status: OrderStatus
    status_reason: str
    agent_id: str
    buyer_id: str
    buyer_name: str
    need_text: str | None = None
    option: Option
    policy: PolicyDecision
    agent_note: str | None = Field(None, description="The agent's stated reason for this choice")
    approvers: list[str] = Field(default_factory=list, description="Who may approve this order")
    basket_id: str | None = Field(
        None, description="Set when the order was placed together with others, as one decision"
    )
    basket_title: str | None = Field(None, description="What the basket is for, in a few words")
    approval: Approval | None = None


class BasketItem(BaseModel):
    request_id: str = Field(description="A request from request_options, e.g. PR-0001")
    option_id: str = Field("opt-1", description="The option of that request to order")


class LeftOutItem(BaseModel):
    request_id: str = Field(description="A request of a need that is not in the basket")
    reason: str | None = Field(
        None, description="Why, in the buyer's terms. Defaults to what the gateway found"
    )


class LeftOut(BaseModel):
    request_id: str
    asked: str = Field(description="The need, in the buyer's words where given")
    reason: str


class Basket(BaseModel):
    """Several orders decided as one: confirmed together, or held together for a human."""

    id: str
    title: str
    status: Literal["confirmed", "pending_approval", "closed"]
    status_reason: str
    total: float = Field(description="EUR without VAT, over the orders that are still active")
    total_with_tax: float
    approvers: list[str]
    order_by: datetime | None = Field(None, description="Earliest order cut-off in the basket")
    orders: list[Order]
    left_out: list[LeftOut] = Field(
        default_factory=list, description="Needs of the list that could not be filled"
    )


class AuditEvent(BaseModel):
    seq: int
    at: datetime
    request_id: str | None = None
    order_id: str | None = None
    actor: str = Field(description="agent:<id>, human:<name> or system")
    type: str
    summary: str = Field(description="One human-readable sentence")
    detail: dict[str, Any] = Field(default_factory=dict)
    hash: str = Field(description="SHA-256 over this event and the previous event's hash")


class Mandate(BaseModel):
    """What a buyer lets its agent decide alone. Enforced by the wholesaler side on every order."""

    max_autonomous_spend: float = Field(
        description="EUR without VAT the agent may commit on its own per delivery day. Orders "
        "it placed alone for the same day count together, so splitting an order gains nothing. "
        "Spend a human approved does not count. Above the limit a human must approve"
    )
    hard_max_order_total: float | None = Field(
        None, description="EUR without VAT. Above this the order is refused outright"
    )
    allow_substitutes: bool = Field(True, description="May the agent accept a substitute alone")
    substitute_max_price_increase_pct: float = Field(
        10, description="Largest price increase per litre/kg a substitute may carry"
    )
    allow_outside_preferences: bool = Field(
        False,
        description="May the agent accept, alone, a product outside the buyer's stated "
        "preferences (label, brand, origin) when nothing that meets them can be delivered",
    )
    allow_partial: bool = Field(False, description="May the agent accept a partial quantity alone")
    allow_late: bool = Field(False, description="May the agent accept late delivery alone")
    overage_tolerance_pct: float = Field(
        5, description="Largest overshoot of the quantity caused by pack sizes"
    )
    forbidden_allergens: list[str] = Field(
        default_factory=list,
        description="Substances no ordered product may contain or possibly contain",
    )
    allowed_categories: list[str] | None = Field(
        None, description="Category codes the agent may buy from alone. Null: all"
    )
    approvers: list[str] = Field(default_factory=list)


class Approver(BaseModel):
    """A person the buyer named to decide what its agent may not decide alone."""

    name: str
    agent_id: str = Field(description="The agent whose orders and rules this person decides")
    buyer_id: str
    buyer_name: str


class AgentIdentity(BaseModel):
    agent_id: str
    agent_name: str
    buyer_id: str
    buyer_name: str
    mandate: Mandate
