"""The purchasing flow an external agent drives: need -> options -> order -> approval.

Every step is written to the audit log with the actor and a plain-language reason.
"""

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import NamedTuple

import httpx

from aimo_agent.aimo_client import AimoClient, ProductAvailability, ProductPrice
from aimo_agent.catalog import Catalog, Product, SearchFilters, SearchResult, get_catalog
from aimo_agent.config import settings
from aimo_agent.mandate import evaluate_policy
from aimo_agent.models import (
    AgentIdentity,
    Approval,
    AuditEvent,
    Excluded,
    Need,
    Offer,
    Option,
    Order,
    PolicyDecision,
    PurchaseRequest,
)
from aimo_agent.offers import UNIT_LABEL, build_options, day, evaluate_offer
from aimo_agent.store import Store
from aimo_agent.substitutes import review_substitute
from aimo_agent.trust import Signer, offer_claims
from aimo_agent.units import normalize_quantity

# Live price and availability are fetched only for a shortlist, never for the whole catalog.
MAX_CANDIDATES = 60
MAX_SUBSTITUTE_CANDIDATES = 30
MIN_NAME_OVERLAP = 0.25
# A request without a category is narrowed to the categories its product type lives in:
# the best-fitting category and any other whose fit is within this factor of it.
CATEGORY_FOCUS_LEAD = 2
MAX_EXCLUDED_LISTED = 25

_DECISION_WORDS = {
    "auto_approve": "the agent may order it alone",
    "needs_approval": "needs human approval",
    "deny": "refused by the mandate",
}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class Live(NamedTuple):
    prices: dict[str, ProductPrice]
    availability: dict[str, ProductAvailability]
    now: datetime  # the wholesaler's clock


@dataclass
class Candidates:
    products: list[Product]
    requested: Product | None = None
    listed: set[str] = field(default_factory=set)
    differences: dict[str, list[str]] = field(default_factory=dict)
    unsafe: list[Excluded] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class PurchasingService:
    def __init__(
        self,
        catalog: Catalog,
        store: Store,
        upstream: Callable[[], AimoClient] = AimoClient,
        signer: Signer | None = None,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.signer = signer
        self._upstream = upstream

    # Lookup

    def search(self, filters: SearchFilters, limit: int = 20) -> SearchResult:
        unknown = filters.unknown_substances()
        if unknown:
            raise ServiceError("invalid", f"Unknown substances: {', '.join(unknown)}.")
        return self.catalog.search(filters, limit=max(1, min(limit, 50)))

    def product(self, product_id: str) -> Product:
        found = self.catalog.get(product_id)
        if found is None:
            raise ServiceError("not_found", f"No product with code {product_id!r}.", 404)
        return found

    async def check_offers(
        self, product_ids: list[str], *, quantity: float, unit: str, deliver_by: date
    ) -> list[Offer]:
        """Live price and availability for chosen products, sized to a quantity."""
        products = [self.product(product_id) for product_id in dict.fromkeys(product_ids)]
        if not 1 <= len(products) <= MAX_CANDIDATES:
            raise ServiceError("invalid", f"Give 1-{MAX_CANDIDATES} product codes.")
        try:
            quantity, unit = normalize_quantity(quantity, unit)
        except ValueError as error:
            raise ServiceError("invalid", str(error)) from error
        live = await self._live([p.id for p in products], deliver_by)
        return self._offers(products, live, quantity=quantity, unit=unit, deliver_by=deliver_by)

    async def _live(self, product_ids: list[str], on: date) -> Live:
        try:
            async with self._upstream() as aimo:
                availability, prices = await asyncio.gather(
                    aimo.availability(product_ids, on), aimo.price(product_ids, on)
                )
        except httpx.HTTPError as error:
            raise ServiceError(
                "upstream_unavailable",
                f"The price and availability service did not answer: {error}",
                502,
            ) from error
        return Live(
            prices={p.product_id: p for p in prices.products},
            availability={a.product_id: a for a in availability.products},
            now=availability.time_now,
        )

    def _offers(
        self, products: list[Product], live: Live, *, quantity: float, unit: str, deliver_by: date
    ) -> list[Offer]:
        reserved = self.store.reserved_units()
        return [
            evaluate_offer(
                product,
                live.prices.get(product.id),
                live.availability.get(product.id),
                quantity=quantity,
                unit=unit,
                deliver_by=deliver_by,
                now=live.now,
                reserved_units=reserved.get(product.id, 0.0),
            )
            for product in products
        ]

    # Need -> options

    def _candidates(self, need: Need) -> Candidates:
        filters = SearchFilters(**need.model_dump(include=set(SearchFilters.model_fields)))
        if need.product_id:
            return self._candidates_for_product(self.product(need.product_id), need, filters)

        matches = self.catalog.matches(filters)
        found = Candidates(products=matches)
        if not matches:
            found.notes.append("No product in the catalog matches the request.")
            return found
        if need.category is None and need.query.strip():
            categories, note = self._focus(need)
            narrowed = [p for p in matches if p.category in categories]
            if narrowed and len(narrowed) < len(matches):
                found.products = narrowed
                found.notes.append(note)
        if len(found.products) > MAX_CANDIDATES:
            found.notes.append(
                f"Checked the {MAX_CANDIDATES} most relevant of {len(found.products)} matching "
                "products. Narrow the request to reach the others."
            )
            found.products = found.products[:MAX_CANDIDATES]
        return found

    def _focus(self, need: Need) -> tuple[set[str], str]:
        """The categories the query's product type lives in, and a note explaining the choice.

        Words alone are ambiguous: "butter" also matches butter beans, butter buns and peanut
        butter. A category fits the query by how many of its products match and how large a
        share of the category they are (matches x share): nearly everything in the butter
        category is butter, while butter buns are a sliver of frozen bakery. Only names that
        answer the query literally are counted, and dietary filters are left out, because
        they say nothing about the product type.
        """
        literal = self.catalog.literal_matches(
            SearchFilters(query=need.query, exclude_terms=need.exclude_terms)
        )
        counts = Counter(p.category for p in literal)
        if not counts:
            return set(), ""
        size = self.catalog.category_size
        fit = {category: n * n / size(category) for category, n in counts.items()}
        threshold = max(fit.values()) / CATEGORY_FOCUS_LEAD
        chosen = sorted((c for c in fit if fit[c] >= threshold), key=lambda c: -fit[c])

        def describe(category: str) -> str:
            example = next(p.name for p in literal if p.category == category)
            return (
                f"{category} ({counts[category]} of its {size(category)} products, e.g. {example})"
            )

        label = "category" if len(chosen) == 1 else "categories"
        note = f"Focused on {label} {'; '.join(describe(c) for c in chosen[:4])}."
        others = [category for category, _ in counts.most_common() if category not in chosen][:3]
        if others:
            note += (
                f" Other categories with matches: {'; '.join(describe(c) for c in others)}. "
                "Pass `category` to choose one of them."
            )
        return set(chosen), note

    def _candidates_for_product(
        self, requested: Product, need: Need, filters: SearchFilters
    ) -> Candidates:
        """The requested product plus substitutes that are safe to offer in its place."""
        found = Candidates(products=[requested], requested=requested)
        pool: dict[str, Product] = {}
        for product_id in requested.substitutes:  # the wholesaler's own list comes first
            if listed := self.catalog.get(product_id):
                pool[listed.id] = listed
                found.listed.add(listed.id)
        similar = [
            p
            for p in self.catalog.matches(
                filters.model_copy(update={"category": requested.category})
            )
            if p.id != requested.id and self.catalog.name_overlap(requested, p) >= MIN_NAME_OVERLAP
        ]
        similar.sort(key=lambda p: -self.catalog.name_overlap(requested, p))
        for product in similar[:MAX_SUBSTITUTE_CANDIDATES]:
            pool.setdefault(product.id, product)

        for product in pool.values():
            blocking, differences = review_substitute(requested, product)
            if not all(product.is_free_from(s) for s in need.free_from) or any(
                product.declares_allergen(s) for s in need.exclude_allergens
            ):
                blocking.append("does not meet the dietary constraints of the request")
            if blocking:
                found.unsafe.append(
                    Excluded(
                        product_id=product.id,
                        name=product.name,
                        status="unsafe_substitute",
                        reason=f"Not offered as a substitute: {'; '.join(blocking)}.",
                    )
                )
            else:
                found.products.append(product)
                found.differences[product.id] = differences
        found.notes.append(
            f"{requested.name} was requested specifically. {len(found.products) - 1} safe "
            f"substitute(s) were checked as well ({len(found.listed)} listed by the wholesaler)."
        )
        return found

    def _mark_substitutes(self, found: Candidates, offers: list[Offer]) -> None:
        requested = found.requested
        assert requested is not None
        base = next(o for o in offers if o.product_id == requested.id)
        base_price = (
            base.unit_price / base.content_per_unit
            if base.unit_price and base.content_per_unit
            else None
        )
        for offer in offers:
            if offer is base:
                continue
            offer.is_substitute = True
            offer.listed_substitute = offer.product_id in found.listed
            offer.differences = found.differences.get(offer.product_id, [])
            offer.similarity = round(
                self.catalog.name_overlap(requested, self.product(offer.product_id)), 2
            )
            if base_price and offer.unit_price and offer.content_per_unit:
                price = offer.unit_price / offer.content_per_unit
                offer.price_delta_pct = round((price / base_price - 1) * 100, 1)

    async def create_request(self, agent: AgentIdentity, need: Need) -> PurchaseRequest:
        """Register a need and answer with ranked, orderable options."""
        found = self._candidates(need)
        request_id = self.store.next_id("PR")
        unit = UNIT_LABEL[need.unit]
        if found.requested:
            subject = found.requested.name
        else:
            subject = f'"{need.query}"' + (
                f" free from {', '.join(need.free_from)}" if need.free_from else ""
            )
        quoted = f' Buyer\'s words: "{need.text}"' if need.text else ""
        self.store.add_event(
            f"agent:{agent.agent_id}",
            "request.received",
            f"{agent.agent_name} asked for {need.quantity:g} {unit} of {subject}, "
            f"delivery by {day(need.deliver_by)}.{quoted}",
            request_id=request_id,
            detail={"buyer": agent.buyer_name, "need": need.model_dump(mode="json")},
        )

        offers: list[Offer] = []
        now = datetime.now(UTC)
        if found.products:
            live = await self._live([p.id for p in found.products], need.deliver_by)
            now = live.now
            offers = self._offers(
                found.products,
                live,
                quantity=need.quantity,
                unit=need.unit,
                deliver_by=need.deliver_by,
            )
            if found.requested:
                self._mark_substitutes(found, offers)

        options = build_options(offers, need)
        for option in options:
            option.policy = self._policy(agent, need, option)
            if self.signer:
                claims = offer_claims(request_id, agent.buyer_id, option, self.catalog, now)
                option.proof = self.signer.sign(claims)
        excluded = found.unsafe + [
            Excluded(product_id=o.product_id, name=o.name, status=o.status, reason=o.reason)
            for o in offers
            if not o.orderable
        ]

        on_time = sum(o.orderable and o.on_time for o in offers)
        late = sum(o.orderable and not o.on_time for o in offers)
        self.store.add_event(
            "system",
            "lookup.completed",
            f"Checked live price and availability for {len(offers)} product(s): {on_time} "
            f"orderable on time, {late} orderable late, {len(offers) - on_time - late} not "
            "orderable.",
            request_id=request_id,
            detail={
                "notes": found.notes,
                "not_orderable": dict(Counter(e.status for e in excluded)),
                "wholesaler_time": now.isoformat(),
            },
        )
        if options:
            top = options[0]
            verdict = _DECISION_WORDS[top.policy.decision] if top.policy else ""
            summary = (
                f"Proposed {len(options)} option(s). Top: {top.title} for {top.total:.2f} EUR "
                f"({verdict})."
            )
            if self.signer:
                summary += f" Each option is signed by the wholesaler (key {self.signer.key_id})."
        else:
            summary = "No orderable option could be proposed."
        self.store.add_event(
            "system",
            "options.proposed",
            summary,
            request_id=request_id,
            detail={
                "options": [
                    {
                        "id": o.id,
                        "tier": o.tier,
                        "title": o.title,
                        "total": o.total,
                        "decision": o.policy.decision if o.policy else None,
                        "trade_offs": o.trade_offs,
                    }
                    for o in options
                ]
            },
        )

        if len(excluded) > MAX_EXCLUDED_LISTED:
            found.notes.append(
                f"{len(excluded)} matching products cannot be ordered; the first "
                f"{MAX_EXCLUDED_LISTED} are listed."
            )
        request = PurchaseRequest(
            id=request_id,
            created_at=datetime.now(UTC),
            agent_id=agent.agent_id,
            buyer_id=agent.buyer_id,
            need=need,
            server_time=now,
            candidates_considered=len(offers),
            notes=found.notes,
            options=options,
            excluded=excluded[:MAX_EXCLUDED_LISTED],
        )
        self.store.save_request(request)
        return request

    def _policy(self, agent: AgentIdentity, need: Need, option: Option) -> PolicyDecision:
        """The mandate's verdict on an option.

        Orders the agent placed alone for the same delivery day count against its spend limit.
        Orders a human approved do not: that spend was a human's decision.
        """
        earlier = [
            order
            for order in self.store.list_orders(status="confirmed", buyer_id=agent.buyer_id)
            if order.agent_id == agent.agent_id
            and order.approval is None
            and order.option.delivery_date == option.delivery_date
        ]
        return evaluate_policy(
            agent.mandate,
            need,
            option,
            self.catalog,
            already_spent=round(sum(order.option.total for order in earlier), 2),
            earlier_orders=sorted(order.id for order in earlier),
        )

    def get_request(self, agent: AgentIdentity, request_id: str) -> PurchaseRequest:
        request = self.store.get_request(request_id)
        if request is None:
            raise ServiceError("not_found", f"No request {request_id!r}.", 404)
        if request.buyer_id != agent.buyer_id:
            raise ServiceError("forbidden", f"{request_id} belongs to another buyer.", 403)
        return request

    # Order

    async def _revalidate(self, need: Need, option: Option, *, holds_stock: bool) -> list[str]:
        """Re-check an option against live data. Returns what changed, empty when it still holds.

        `holds_stock` is true for an order waiting for approval: its own reservation must not
        count against it.
        """
        live = await self._live([line.product_id for line in option.lines], need.deliver_by)
        reserved = self.store.reserved_units()
        problems: list[str] = []
        for line in option.lines:
            held = reserved.get(line.product_id, 0.0) - (line.units if holds_stock else 0.0)
            offer = evaluate_offer(
                self.product(line.product_id),
                live.prices.get(line.product_id),
                live.availability.get(line.product_id),
                quantity=line.quantity,
                unit=need.unit,
                deliver_by=need.deliver_by,
                now=live.now,
                reserved_units=max(held, 0.0),
            )
            if not offer.orderable:
                problems.append(f"{line.name}: {offer.reason}")
            elif offer.unit_price != line.unit_price:
                problems.append(
                    f"{line.name}: the price changed from {line.unit_price:.2f} to "
                    f"{offer.unit_price:.2f} EUR"
                )
            elif (offer.units_offered or 0.0) < line.units:
                problems.append(
                    f"{line.name}: only {offer.available_units or 0:g} {line.sales_unit} left"
                )
            elif offer.delivery_date and offer.delivery_date > line.delivery_date:
                problems.append(f"{line.name}: delivery moved to {day(offer.delivery_date)}")
        return problems

    async def place_order(
        self, agent: AgentIdentity, request_id: str, option_id: str, note: str | None = None
    ) -> Order:
        """Commit to one option. The mandate decides: confirmed, held for approval, or refused."""
        request = self.get_request(agent, request_id)
        option = next((o for o in request.options if o.id == option_id), None)
        if option is None:
            known = ", ".join(o.id for o in request.options) or "none"
            raise ServiceError(
                "not_found", f"{request_id} has no option {option_id!r}. Options: {known}.", 404
            )
        active = [
            o
            for o in self.store.list_orders(request_id=request_id)
            if o.status in ("confirmed", "pending_approval")
        ]
        if active:
            raise ServiceError(
                "already_ordered",
                f"{request_id} already has order {active[0].id} ({active[0].status}). "
                "Create a new request to order again.",
                409,
            )

        reason = f' Reason given: "{note}"' if note else ""
        self.store.add_event(
            f"agent:{agent.agent_id}",
            "order.requested",
            f"{agent.agent_name} chose {option.id}: {option.title}, "
            f"{option.total:.2f} EUR.{reason}",
            request_id=request_id,
            detail={"option_id": option.id, "note": note},
        )
        problems = await self._revalidate(request.need, option, holds_stock=False)
        if problems:
            changed = "; ".join(problems)
            self.store.add_event(
                "system",
                "order.refused",
                f"The offer changed since the options were proposed: {changed}. No order was made.",
                request_id=request_id,
            )
            raise ServiceError(
                "offer_changed",
                f"The offer no longer holds: {changed}. Create a new request for current options.",
                409,
            )

        policy = self._policy(agent, request.need, option)
        status, status_reason = {
            "auto_approve": ("confirmed", "Confirmed automatically: within the agent's mandate."),
            "needs_approval": ("pending_approval", policy.summary),
            "deny": ("denied", policy.summary),
        }[policy.decision]
        order = Order(
            id=self.store.next_id("PO"),
            request_id=request_id,
            created_at=datetime.now(UTC),
            status=status,
            status_reason=status_reason,
            agent_id=agent.agent_id,
            buyer_id=agent.buyer_id,
            buyer_name=agent.buyer_name,
            need_text=request.need.text,
            option=option.model_copy(update={"policy": policy}),
            policy=policy,
            agent_note=note,
            approvers=agent.mandate.approvers,
        )
        self.store.save_order(order)

        def log(type_: str, summary: str, detail: dict | None = None) -> None:
            self.store.add_event(
                "system", type_, summary, request_id=request_id, order_id=order.id, detail=detail
            )

        log("policy.evaluated", policy.summary, {"rules": [r.model_dump() for r in policy.rules]})
        if status == "confirmed":
            log(
                "order.confirmed",
                f"Order {order.id} confirmed without human involvement: {option.total:.2f} EUR "
                f"({option.total_with_tax:.2f} with VAT), delivery {day(option.delivery_date)}.",
            )
        elif status == "pending_approval":
            approvers = " or ".join(agent.mandate.approvers) or "the buyer"
            log("approval.requested", f"Order {order.id} is held until {approvers} decides.")
        else:
            log(
                "order.denied",
                f"Order {order.id} was refused. "
                "A mandate refusal cannot be overridden by approval.",
            )
        return order

    def get_order(self, agent: AgentIdentity, order_id: str) -> Order:
        order = self.store.get_order(order_id)
        if order is None:
            raise ServiceError("not_found", f"No order {order_id!r}.", 404)
        if order.buyer_id != agent.buyer_id:
            raise ServiceError("forbidden", f"{order_id} belongs to another buyer.", 403)
        return order

    # Human approval

    async def decide(
        self, order_id: str, *, approver: str, approved: bool, comment: str | None = None
    ) -> Order:
        order = self.store.get_order(order_id)
        if order is None:
            raise ServiceError("not_found", f"No order {order_id!r}.", 404)
        if order.status != "pending_approval":
            raise ServiceError(
                "not_pending", f"{order_id} is {order.status}, not waiting for approval.", 409
            )
        request = self.store.get_request(order.request_id)
        assert request is not None
        order.approval = Approval(
            approver=approver, approved=approved, comment=comment, decided_at=datetime.now(UTC)
        )
        remark = f' Comment: "{comment}"' if comment else ""

        def log(actor: str, type_: str, summary: str) -> None:
            self.store.add_event(
                actor, type_, summary, request_id=order.request_id, order_id=order.id
            )

        if not approved:
            order.status = "rejected"
            order.status_reason = f"Rejected by {approver}."
            log(
                f"human:{approver}", "approval.rejected", f"{approver} rejected {order.id}.{remark}"
            )
            self.store.save_order(order)
            return order

        log(f"human:{approver}", "approval.granted", f"{approver} approved {order.id}.{remark}")
        problems = await self._revalidate(request.need, order.option, holds_stock=True)
        if problems:
            order.status = "expired"
            order.status_reason = f"Approved too late: {'; '.join(problems)}."
            log("system", "order.expired", f"Order {order.id} expired. {order.status_reason}")
        else:
            order.status = "confirmed"
            order.status_reason = f"Confirmed after approval by {approver}."
            log(
                "system",
                "order.confirmed",
                f"Order {order.id} confirmed after human approval: {order.option.total:.2f} EUR "
                f"({order.option.total_with_tax:.2f} with VAT), delivery "
                f"{day(order.option.delivery_date)}.",
            )
        self.store.save_order(order)
        return order

    # Audit

    def audit_trail(
        self,
        agent: AgentIdentity | None,
        *,
        request_id: str | None = None,
        order_id: str | None = None,
    ) -> list[AuditEvent]:
        """Events of a request or an order. `agent` None is the wholesaler's own view."""
        if agent is not None:
            if order_id:
                self.get_order(agent, order_id)
            elif request_id:
                self.get_request(agent, request_id)
            else:
                raise ServiceError("invalid", "Give `request_id` or `order_id`.")
        return self.store.events(request_id=request_id, order_id=order_id)


@lru_cache
def get_service() -> PurchasingService:
    return PurchasingService(
        get_catalog(),
        Store(settings.aimo_db_path),
        signer=Signer(settings.aimo_signing_key_file),
    )
