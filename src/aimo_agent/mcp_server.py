"""MCP interface for external purchasing agents: the same flow as the REST API, as tools.

stdio (the agent host launches it):  uv run python -m aimo_agent.mcp_server
Streamable HTTP:                     served by the REST app at /mcp
"""

from collections.abc import Callable
from datetime import date
from functools import wraps
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError

from aimo_agent.agents import get_agents
from aimo_agent.catalog import DIETARY, Product, SearchFilters, SearchResult
from aimo_agent.config import settings
from aimo_agent.models import AgentIdentity, AuditEvent, Need, Offer, Order, PurchaseRequest
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

Rules of the road:
- Prices are fixed and in EUR without VAT unless a field says otherwise. There is no negotiation.
- Never weaken a dietary constraint (free_from, exclude_allergens) to get more options.
- Never split or reshape an order to stay under a limit of the mandate.
- Options expire: if place_order answers `offer_changed`, call request_options again.
- Each option carries `proof`, the wholesaler's signature over its terms. Pass it on unchanged
  when you report an option: the buyer's systems verify it. Do not summarise or re-type it.
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
    matching products that cannot be ordered and why. Options are not a reservation."""
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


if __name__ == "__main__":
    mcp.run("stdio")
