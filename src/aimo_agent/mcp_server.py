"""MCP interface for external purchasing agents: the same flow as the REST API, as tools.

stdio (the agent host launches it):  uv run python -m aimo_agent.mcp_server
Streamable HTTP:                     served by the REST app at /mcp
"""

from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from functools import wraps
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from aimo_agent.agents import get_agents
from aimo_agent.catalog import DIETARY, Product, SearchFilters, SearchResult
from aimo_agent.config import settings
from aimo_agent.models import (
    AgentIdentity,
    AuditEvent,
    Basket,
    BasketItem,
    LeftOut,
    LeftOutItem,
    Need,
    Offer,
    Option,
    Order,
    PurchaseRequest,
)
from aimo_agent.service import ServiceError, get_service

INSTRUCTIONS = """\
Purchasing gateway of the food-service wholesaler Valio Aimo, for agents that buy on behalf of a
professional kitchen.

How to buy:
1. get_mandate: learn who you act for and what you may decide alone.
2. search_products: explore the catalog to understand what the buyer's words map to. Categories
   have codes but no names, so read the example names in the `categories` facet.
3. request_options: state the need in structured form. You get ranked options that can be
   ordered right now, each with the mandate's verdict, plus the products that were excluded and
   why (cut-off passed, sales block, out of stock, unsafe substitute).
4. place_order: submit the option that fits best and say why you chose it. A request to find
   or get a product by a date is a request to buy it, so go on to place_order unless the buyer
   said they only want to compare. Submit even when the verdict is `needs_approval`: that is
   how the request reaches the approver, and nothing is committed until a human approves. Do
   not ask the buyer for permission first. Report who has to approve and by when (`order_by`).
5. get_order and get_audit_trail: follow up and show what happened.

More than one product (a menu, an event, a weekly list): work out the products and quantities,
then call request_options_for_list and place_basket once. The basket is decided as a whole:
confirmed together, or held together for one approval. Do not order such items one by one.
Retrying a need with better words is fine. A need you could not fill goes into place_basket's
`left_out`, so the buyer sees it; requests you replaced are closed for you.

Rules of the road:
- Prices are fixed and in EUR without VAT unless a field says otherwise. There is no negotiation.
- Before ordering, read the product name of the option against the buyer's words. If it is not
  the product meant (beef-pork mince for "minced beef", liquid sugar for "sugar"), call
  request_options again with `exclude_terms` or `category`; `notes` lists the categories found.
- Never weaken a dietary constraint (free_from, exclude_allergens) to get more options.
- Never split or reshape an order to stay under a limit of the mandate.
- Options expire: if place_order answers `offer_changed`, call request_options again.
- Each option carries `proof`, the wholesaler's signature over its terms. Say that an offer is
  signed, but do not print the token unless asked; when asked, give it unchanged. You cannot
  check the signature yourself: get_signing_keys returns the keys the buyer's systems need.
- Do not poll price and availability for products you are not about to order.
"""

mcp = MCPServer(
    "valio-aimo-purchasing",
    title="Valio Aimo purchasing gateway",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

_SUBSTANCES = ", ".join(DIETARY)

# Hints for agent hosts: which tools only read, and which one creates an obligation.
READS = ToolAnnotations(readOnlyHint=True)
RECORDS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)


def _agent(ctx: Context) -> AgentIdentity:
    """The calling agent: bearer token over HTTP, the configured identity over stdio."""
    headers = ctx.headers
    if headers is None:
        token = settings.aimo_agent_token
    else:
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
    agent = get_agents().get(token)
    if agent is None:
        raise ToolError("unauthorized: missing or unknown agent token.")
    return agent


def _reported(tool: Callable[..., Any]) -> Callable[..., Any]:
    """Turn expected failures into tool errors the model can read and act on."""

    @wraps(tool)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = tool(*args, **kwargs)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except ServiceError as error:
            raise ToolError(f"{error.code}: {error.message}") from error
        except ValidationError as error:
            raise ToolError(f"invalid: {error}") from error

    return wrapper


@mcp.tool(title="Read my mandate", annotations=READS)
@_reported
def get_mandate(ctx: Context) -> AgentIdentity:
    """Who you act for and what you may decide alone: spend limit per delivery day, whether you may
    accept substitutes, partial quantities or late delivery, forbidden allergens, and who
    approves what exceeds these limits."""
    return _agent(ctx)


@mcp.tool(title="Search the catalog", annotations=READS)
@_reported
def search_products(
    ctx: Context,
    query: Annotated[
        str, Field(description="Words that must all appear in the product name or brand")
    ] = "",
    free_from: Annotated[
        list[str],
        Field(description=f"Must be explicitly declared free from these. Known: {_SUBSTANCES}"),
    ] = [],  # noqa: B006
    exclude_allergens: Annotated[
        list[str], Field(description="Must not contain or possibly contain these substances")
    ] = [],  # noqa: B006
    exclude_terms: Annotated[
        list[str], Field(description="Words that must not appear in the name, e.g. sour, coffee")
    ] = [],  # noqa: B006
    category: Annotated[str | None, Field(description="Category code to restrict to")] = None,
    labels: Annotated[
        list[str], Field(description="Labels required, e.g. EU_ORGANIC_FARMING")
    ] = [],  # noqa: B006
    country_of_origin: Annotated[
        str | None, Field(description="ISO 3166-1 alpha-2, lowercase, e.g. fi")
    ] = None,
    brand: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 15,
) -> SearchResult:
    """Search the static product catalog (17,500 products; names in English, Finnish and
    Swedish). Free of charge and not rate-limited: use it to understand the assortment before
    asking for options. Returns matching products and the categories they fall into."""
    _agent(ctx)
    filters = SearchFilters(
        query=query,
        free_from=free_from,
        exclude_allergens=exclude_allergens,
        exclude_terms=exclude_terms,
        category=category,
        labels=labels,
        country_of_origin=country_of_origin,
        brand=brand,
    )
    return get_service().search(filters, limit)


@mcp.tool(title="Read one product", annotations=READS)
@_reported
def get_product(ctx: Context, product_id: str) -> Product:
    """Full catalog record of one product: packaging units, claims, allergens, labels, origin,
    delivery reliability, carbon footprint and wholesaler-listed substitutes."""
    _agent(ctx)
    return get_service().product(product_id)


@mcp.tool(title="Check price and availability", annotations=READS)
@_reported
async def check_price_and_availability(
    ctx: Context,
    product_ids: Annotated[list[str], Field(min_length=1, max_length=60)],
    quantity: Annotated[float, Field(gt=0)],
    deliver_by: Annotated[date, Field(description="Latest acceptable delivery date, YYYY-MM-DD")],
    unit: Annotated[str, Field(description="L, KG or SALES_UNIT")] = "SALES_UNIT",
) -> list[Offer]:
    """Live fixed price and availability for specific products, sized to a quantity. Use this
    to inspect chosen products. To buy, call request_options instead: only its options can be
    ordered."""
    _agent(ctx)
    return await get_service().check_offers(
        product_ids, quantity=quantity, unit=unit, deliver_by=deliver_by
    )


@mcp.tool(title="State a need and get orderable options", annotations=RECORDS)
@_reported
async def request_options(
    ctx: Context,
    quantity: Annotated[float, Field(gt=0, description="Amount wanted, in `unit`")],
    deliver_by: Annotated[date, Field(description="Latest acceptable delivery date, YYYY-MM-DD")],
    unit: Annotated[
        str, Field(description="L, KG or SALES_UNIT (litres, dl, ml, g are accepted too)")
    ] = "L",
    query: Annotated[str, Field(description="Words describing the product type, e.g. 'milk'")] = "",
    product_id: Annotated[
        str | None,
        Field(
            description="Set when the buyer wants one specific product. Other products are "
            "then offered only as allergen-safe substitutes"
        ),
    ] = None,
    free_from: Annotated[
        list[str], Field(description=f"Hard dietary constraints. Known: {_SUBSTANCES}")
    ] = [],  # noqa: B006
    exclude_allergens: Annotated[
        list[str], Field(description="Substances no product may contain or possibly contain")
    ] = [],  # noqa: B006
    exclude_terms: Annotated[
        list[str],
        Field(description="Words that rule a product out, e.g. sour, coffee for plain milk"),
    ] = [],  # noqa: B006
    category: Annotated[str | None, Field(description="Category code to restrict to")] = None,
    labels: Annotated[list[str], Field(description="Labels the product must carry")] = [],  # noqa: B006
    country_of_origin: Annotated[str | None, Field(description="e.g. fi")] = None,
    brand: str | None = None,
    prefer: Annotated[
        Literal["price", "reliability", "carbon"],
        Field(description="What to rank by when several options fit"),
    ] = "price",
    buyer_words: Annotated[
        str | None,
        Field(description="The buyer's request verbatim. Stored in the audit trail"),
    ] = None,
) -> PurchaseRequest:
    """Register what the buyer needs and get ranked options that can be ordered right now.

    Tiers: `best` is exactly as asked; `better` is the full quantity on time with a compromise
    (a substitute, or a split across products); `good` is a partial quantity or late delivery.
    Each option carries `policy`: what the mandate will do if you order it. `excluded` lists
    matching products that cannot be ordered and why. Options are not a reservation.

    `labels`, `brand` and `country_of_origin` are preferences. When nothing that meets them can
    cover the need in time, products without them are proposed too, marked with
    `unmet_preferences`; ordering one is held for a human unless the mandate says otherwise.
    Dietary constraints (`free_from`, `exclude_allergens`) are never relaxed."""
    need = Need(
        text=buyer_words,
        query=query,
        product_id=product_id,
        free_from=free_from,
        exclude_allergens=exclude_allergens,
        exclude_terms=exclude_terms,
        category=category,
        labels=labels,
        country_of_origin=country_of_origin,
        brand=brand,
        quantity=quantity,
        unit=unit,
        deliver_by=deliver_by,
        prefer=prefer,
    )
    return await get_service().create_request(_agent(ctx), need)


@mcp.tool(title="Place an order", annotations=RECORDS)
@_reported
async def place_order(
    ctx: Context,
    request_id: Annotated[str, Field(description="From request_options, e.g. PR-0001")],
    option_id: Annotated[str, Field(description="The option to order, e.g. opt-1")],
    reason: Annotated[
        str, Field(description="Why this option, in one sentence. Stored in the audit trail")
    ],
) -> Order:
    """Submit one option of a request as an order.

    The gateway re-checks price and stock and applies the buyer's mandate. Within your
    authority the order is `confirmed` at once and is binding. Beyond it nothing is committed:
    the order is held as `pending_approval` until a human named in `approvers` decides, so
    submitting is the way to ask for approval. `denied` means the mandate forbids the order
    and approval cannot override it."""
    return await get_service().place_order(_agent(ctx), request_id, option_id, reason)


class OptionsDigest(BaseModel):
    """One need of a list with its orderable options, shortened to what a choice needs."""

    request_id: str
    asked: str
    notes: list[str]
    options: list[Option]
    not_orderable: dict[str, int] = Field(description="Matching products left out, by reason")


class OrderDigest(BaseModel):
    order_id: str
    request_id: str
    status: str
    status_reason: str
    title: str
    total: float
    delivery_date: date


class BasketDigest(BaseModel):
    basket_id: str
    title: str
    status: str = Field(description="confirmed, pending_approval or closed")
    status_reason: str
    total: float
    total_with_tax: float
    approvers: list[str]
    order_by: datetime | None
    orders: list[OrderDigest]
    left_out: list[LeftOut]


def _basket_digest(basket: Basket) -> BasketDigest:
    return BasketDigest(
        basket_id=basket.id,
        title=basket.title,
        status=basket.status,
        status_reason=basket.status_reason,
        total=basket.total,
        total_with_tax=basket.total_with_tax,
        approvers=basket.approvers,
        order_by=basket.order_by,
        orders=[
            OrderDigest(
                order_id=order.id,
                request_id=order.request_id,
                status=order.status,
                status_reason=order.status_reason,
                title=order.option.title,
                total=order.option.total,
                delivery_date=order.option.delivery_date,
            )
            for order in basket.orders
        ],
        left_out=basket.left_out,
    )


@mcp.tool(title="State several needs at once", annotations=RECORDS)
@_reported
async def request_options_for_list(
    ctx: Context,
    needs: Annotated[
        list[Need],
        Field(
            min_length=1,
            max_length=15,
            description="One entry per product needed. The fields are those of "
            "request_options; `text` holds the buyer's words for that product",
        ),
    ],
) -> list[OptionsDigest]:
    """Use this whenever more than one product is needed: a menu, an event, a weekly list.

    One call answers every need. Each entry has a `request_id` and its ranked options, in a
    short form: only the mandate rules that are not passed are listed, and the signature is
    left out (it comes with the order). Then call place_basket with one option per request."""
    requests = await get_service().create_requests(_agent(ctx), needs)
    digests = []
    for request in requests:
        options = [
            option.model_copy(
                update={
                    "proof": None,
                    "policy": option.policy.model_copy(
                        update={"rules": [r for r in option.policy.rules if r.outcome != "pass"]}
                    )
                    if option.policy
                    else None,
                }
            )
            for option in request.options
        ]
        need = request.need
        digests.append(
            OptionsDigest(
                request_id=request.id,
                asked=need.text or need.query or need.product_id or "",
                notes=request.notes,
                options=options,
                not_orderable=dict(Counter(e.status for e in request.excluded)),
            )
        )
    return digests


@mcp.tool(title="Order several options as one basket", annotations=RECORDS)
@_reported
async def place_basket(
    ctx: Context,
    items: Annotated[list[BasketItem], Field(min_length=1, max_length=15)],
    title: Annotated[
        str,
        Field(
            description="What the basket is for, in a few words the approver will recognise, "
            "e.g. 'Dinner for 80 guests on Monday'"
        ),
    ],
    reason: Annotated[
        str, Field(description="Why these options, in a sentence. Stored in the audit trail")
    ],
    left_out: Annotated[
        list[LeftOutItem],
        Field(
            description="Needs of the list you could not fill, by request, so the buyer sees "
            "them, e.g. nothing arrives in time. Leave out requests you merely replaced"
        ),
    ] = [],  # noqa: B006
) -> BasketDigest:
    """Order one option from each of several requests as a whole.

    The mandate is applied to every item and the spend limit to their sum. A basket is all or
    nothing: if everything is within your authority, everything is confirmed; if any item needs
    approval, the whole basket is held for one human decision. Nothing is ordered if any offer
    changed in the meantime. Use this instead of placing the items one by one.

    Placing the basket closes your other requests made since your previous order that have no
    order: those in `left_out` are reported to the buyer, the rest are closed as replaced."""
    basket = await get_service().place_basket(
        _agent(ctx),
        [(item.request_id, item.option_id) for item in items],
        title,
        reason,
        left_out,
    )
    return _basket_digest(basket)


@mcp.tool(title="Read a basket", annotations=READS)
@_reported
def get_basket(ctx: Context, basket_id: str) -> BasketDigest:
    """Current status of a basket and of each order in it."""
    return _basket_digest(get_service().get_basket(_agent(ctx), basket_id))


@mcp.tool(title="Read an order", annotations=READS)
@_reported
def get_order(ctx: Context, order_id: str) -> Order:
    """Current status of an order, including the human approval decision once it is made."""
    return get_service().get_order(_agent(ctx), order_id)


@mcp.tool(title="Read the audit trail", annotations=READS)
@_reported
def get_audit_trail(
    ctx: Context,
    order_id: Annotated[str | None, Field(description="e.g. PO-0001")] = None,
    request_id: Annotated[str | None, Field(description="e.g. PR-0001")] = None,
) -> list[AuditEvent]:
    """The human-readable, tamper-evident record of a request or an order: who did what, when
    and why, including every mandate rule that was applied."""
    return get_service().audit_trail(_agent(ctx), request_id=request_id, order_id=order_id)


@mcp.tool(title="Read the wholesaler's signing keys", annotations=READS)
@_reported
def get_signing_keys(ctx: Context) -> dict[str, Any]:
    """The public keys (JWK Set, RFC 7517) that verify the `proof` on every option, and the
    path they are published at. Hand them to the buyer's systems together with a proof: any
    JWT library can then confirm what the wholesaler stated, without asking the wholesaler."""
    _agent(ctx)
    signer = get_service().signer
    if signer is None:
        raise ToolError("not_found: this gateway does not sign its offers.")
    return {"published_at": "/.well-known/jwks.json", **signer.jwks()}


if __name__ == "__main__":
    mcp.run("stdio")
