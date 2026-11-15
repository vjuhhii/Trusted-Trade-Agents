"""REST interface for external purchasing agents (OpenAPI 3.1), plus the human approval desk.

Run: uv run uvicorn aimo_agent.app:app --reload
Docs: http://127.0.0.1:8000/docs   Approval desk: http://127.0.0.1:8000/
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from aimo_agent.agents import get_agents, get_approvers, update_mandate
from aimo_agent.catalog import Product, SearchFilters, SearchResult
from aimo_agent.config import settings
from aimo_agent.mcp_server import mcp
from aimo_agent.models import (
    AgentIdentity,
    Approver,
    AuditEvent,
    Basket,
    BasketItem,
    LeftOutItem,
    Need,
    Offer,
    Order,
    PurchaseRequest,
)
from aimo_agent.scenarios import SCENARIOS, Scenario, ScenarioResult, run_scenario
from aimo_agent.service import PurchasingService, ServiceError, get_service

WEB_DIR = Path(__file__).parent / "web"
# The pages change with the code; a browser must not keep showing an old one.
_REVALIDATE = {"Cache-Control": "no-cache"}

DESCRIPTION = """
An open interface through which an external purchasing agent works with a food-service
wholesaler: look up products, get fixed-price offers, place an order, and have a human approve
whatever exceeds the agent's mandate.

**The flow**

1. `GET /v1/mandate`: what this agent may decide on its own.
2. `GET /v1/products`: search the static catalog (no rate-limited calls behind it).
3. `POST /v1/requests`: state a need; the answer is a ranked list of orderable options, each
   with the mandate's verdict (`auto_approve`, `needs_approval` or `deny`).
4. `POST /v1/orders`: commit to one option. The order is `confirmed`, or held as
   `pending_approval` until a human decides at the approval desk.
5. `GET /v1/orders/{id}/audit`: the human-readable record of what happened and why.

The same operations are available as MCP tools at `/mcp` (Streamable HTTP).

Errors follow RFC 9457 (`application/problem+json`). Agent endpoints need
`Authorization: Bearer <agent token>`. Deciding an order or changing a mandate needs the personal
key of a person the buyer named as approver (`Authorization: Bearer <approver key>`): the gateway
records who decided from the key, never from a name sent with the request. In production the key
is the buyer's own sign-in.
"""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_service()  # load the catalog before the first request
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Aimo Agent Gateway",
    version="0.1.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Agent", "description": "What an external purchasing agent calls."},
        {"name": "Approval desk", "description": "What the buyer's human approver uses."},
        {"name": "Demo", "description": "Scripted purchases that drive the console."},
    ],
)

bearer = HTTPBearer(auto_error=False, description="Token the wholesaler issued to this agent")
Service = Annotated[PurchasingService, Depends(get_service)]


def current_agent(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AgentIdentity:
    agent = get_agents().get(credentials.credentials) if credentials else None
    if agent is None:
        raise ServiceError("unauthorized", "Missing or unknown agent token.", 401)
    return agent


Agent = Annotated[AgentIdentity, Depends(current_agent)]

approver_key = HTTPBearer(
    auto_error=False,
    scheme_name="ApproverKey",
    description="Personal key of a person the buyer named to approve. In production: the "
    "buyer's own sign-in",
)


def current_approver(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(approver_key)],
) -> Approver:
    approver = get_approvers().get(credentials.credentials) if credentials else None
    if approver is None:
        raise ServiceError("unauthorized", "Missing or unknown approver key.", 401)
    return approver


Person = Annotated[Approver, Depends(current_approver)]


@app.exception_handler(ServiceError)
async def problem_details(_: Request, error: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.http_status,
        media_type="application/problem+json",
        content={
            "type": f"urn:aimo-agent-gateway:error:{error.code}",
            "title": error.code.replace("_", " ").capitalize(),
            "status": error.http_status,
            "detail": error.message,
        },
    )


class OfferQuery(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=60)
    quantity: float = Field(gt=0)
    unit: str = Field("SALES_UNIT", description="L, KG or SALES_UNIT")
    deliver_by: date


class OrderRequest(BaseModel):
    request_id: str
    option_id: str
    note: str | None = Field(
        None, description="Why the agent chose this option. Kept in the audit trail"
    )


class ApprovalDecision(BaseModel):
    """Who decides is not part of the decision: it is the person the approver key belongs to."""

    approved: bool
    comment: str | None = None


class ChainStatus(BaseModel):
    intact: bool
    events: int
    first_bad_seq: int | None


# Agent


@app.get("/v1/mandate", tags=["Agent"], summary="Who the agent acts for and what it may decide")
def read_mandate(agent: Agent) -> AgentIdentity:
    return agent


@app.get("/v1/products", tags=["Agent"], summary="Search the catalog")
def search_products(
    service: Service,
    _: Agent,
    filters: Annotated[SearchFilters, Query()],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchResult:
    return service.search(filters, limit)


@app.get("/v1/products/{product_id}", tags=["Agent"], summary="One product")
def read_product(product_id: str, service: Service, _: Agent) -> Product:
    return service.product(product_id)


@app.post("/v1/offers", tags=["Agent"], summary="Live price and availability for chosen products")
async def check_offers(query: OfferQuery, service: Service, _: Agent) -> list[Offer]:
    return await service.check_offers(
        query.product_ids, quantity=query.quantity, unit=query.unit, deliver_by=query.deliver_by
    )


@app.post(
    "/v1/requests", tags=["Agent"], status_code=201, summary="State a need, get ranked options"
)
async def create_request(need: Need, service: Service, agent: Agent) -> PurchaseRequest:
    return await service.create_request(agent, need)


@app.get("/v1/requests/{request_id}", tags=["Agent"], summary="A request and its options")
def read_request(request_id: str, service: Service, agent: Agent) -> PurchaseRequest:
    return service.get_request(agent, request_id)


@app.get("/v1/requests/{request_id}/audit", tags=["Agent"], summary="Audit trail of a request")
def read_request_audit(request_id: str, service: Service, agent: Agent) -> list[AuditEvent]:
    return service.audit_trail(agent, request_id=request_id)


@app.post("/v1/orders", tags=["Agent"], status_code=201, summary="Commit to one option")
async def place_order(body: OrderRequest, service: Service, agent: Agent) -> Order:
    return await service.place_order(agent, body.request_id, body.option_id, body.note)


@app.get("/v1/orders/{order_id}", tags=["Agent"], summary="Order status")
def read_order(order_id: str, service: Service, agent: Agent) -> Order:
    return service.get_order(agent, order_id)


@app.get("/v1/orders/{order_id}/audit", tags=["Agent"], summary="Audit trail of an order")
def read_order_audit(order_id: str, service: Service, agent: Agent) -> list[AuditEvent]:
    return service.audit_trail(agent, order_id=order_id)


class BasketRequest(BaseModel):
    items: list[BasketItem] = Field(min_length=1, max_length=15)
    title: str = Field(min_length=1, description="What the basket is for, in a few words")
    note: str | None = Field(None, description="Why these options. Kept in the audit trail")
    left_out: list[LeftOutItem] = Field(
        default_factory=list,
        description="Needs that could not be filled, so the buyer sees them. Other requests "
        "without an order since the agent's previous order are closed as replaced",
    )


@app.post(
    "/v1/requests/batch",
    tags=["Agent"],
    status_code=201,
    summary="State several needs at once, e.g. the ingredients of a menu",
)
async def create_requests(
    needs: Annotated[list[Need], Field(min_length=1, max_length=15)], service: Service, agent: Agent
) -> list[PurchaseRequest]:
    return await service.create_requests(agent, needs)


@app.post(
    "/v1/baskets",
    tags=["Agent"],
    status_code=201,
    summary="Order one option from each of several requests as a whole",
)
async def place_basket(body: BasketRequest, service: Service, agent: Agent) -> Basket:
    items = [(item.request_id, item.option_id) for item in body.items]
    return await service.place_basket(agent, items, body.title, body.note, body.left_out)


@app.get("/v1/baskets/{basket_id}", tags=["Agent"], summary="Basket status")
def read_basket(basket_id: str, service: Service, agent: Agent) -> Basket:
    return service.get_basket(agent, basket_id)


# Approval desk


@app.get("/v1/desk/me", tags=["Approval desk"], summary="Who this approver key belongs to")
def desk_me(person: Person) -> Approver:
    return person


@app.get("/v1/desk/orders", tags=["Approval desk"], summary="Orders, newest first")
def desk_orders(
    service: Service,
    status: Literal["confirmed", "pending_approval", "rejected", "denied", "expired"] | None = None,
    buyer_id: str | None = None,
) -> list[Order]:
    return service.store.list_orders(status=status, buyer_id=buyer_id)


@app.post(
    "/v1/desk/orders/{order_id}/decision",
    tags=["Approval desk"],
    summary="Approve or reject, as one of the people the buyer named",
)
async def desk_decide(
    order_id: str, decision: ApprovalDecision, service: Service, person: Person
) -> Order:
    order = service.store.get_order(order_id)
    if order is None:
        raise ServiceError("not_found", f"No order {order_id!r}.", 404)
    if order.buyer_id != person.buyer_id:
        raise ServiceError("forbidden", f"{order_id} belongs to another buyer.", 403)
    if person.name not in order.approvers:
        raise ServiceError(
            "forbidden",
            f"{person.name} is not among those who may approve {order_id}: "
            f"{', '.join(order.approvers) or 'nobody'}.",
            403,
        )
    return await service.decide(
        order_id, approver=person.name, approved=decision.approved, comment=decision.comment
    )


@app.get("/v1/desk/audit", tags=["Approval desk"], summary="The audit log")
def desk_audit(
    service: Service,
    request_id: str | None = None,
    order_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[AuditEvent]:
    return service.store.events(request_id=request_id, order_id=order_id, limit=limit)


@app.get(
    "/v1/desk/audit/verify", tags=["Approval desk"], summary="Check the audit log's hash chain"
)
def desk_audit_verify(service: Service) -> ChainStatus:
    intact, events, first_bad_seq = service.store.verify_chain()
    return ChainStatus(intact=intact, events=events, first_bad_seq=first_bad_seq)


@app.get("/v1/desk/requests", tags=["Approval desk"], summary="Requests, newest first")
def desk_requests(
    service: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    buyer_id: str | None = None,
) -> list[PurchaseRequest]:
    return service.store.list_requests(limit, buyer_id=buyer_id)


class MandateChange(BaseModel):
    max_autonomous_spend: float | None = Field(None, gt=0)
    allow_substitutes: bool | None = None
    allow_outside_preferences: bool | None = None
    allow_partial: bool | None = None
    allow_late: bool | None = None


@app.get("/v1/desk/mandates", tags=["Approval desk"], summary="Agents and their mandates")
def desk_mandates() -> list[AgentIdentity]:
    return list(get_agents().values())


@app.patch(
    "/v1/desk/mandates/{agent_id}",
    tags=["Approval desk"],
    summary="Change an agent's mandate, as one of the people its buyer named",
)
def desk_change_mandate(
    agent_id: str, change: MandateChange, service: Service, person: Person
) -> AgentIdentity:
    if agent_id not in {agent.agent_id for agent in get_agents().values()}:
        raise ServiceError("not_found", f"No agent {agent_id!r}.", 404)
    if person.agent_id != agent_id:
        raise ServiceError("forbidden", f"{person.name} does not decide for {agent_id}.", 403)
    changes = change.model_dump(exclude_none=True)
    agent = update_mandate(agent_id, changes)
    if agent is None:
        raise ServiceError("not_found", f"No agent {agent_id!r}.", 404)
    listed = ", ".join(f"{name} = {value}" for name, value in changes.items()) or "nothing"
    service.store.add_event(
        f"human:{person.name}",
        "mandate.changed",
        f"{person.name} changed the mandate of {agent.agent_name}: {listed}.",
        detail={"agent_id": agent_id, "changes": changes},
    )
    return agent


# Demo


class DemoApprover(Approver):
    key: str
    link: str = Field(description="The kitchen view, signed in as this person")


@app.get(
    "/demo/approvers",
    tags=["Demo"],
    summary="The demo's approvers and their keys. A real gateway has no such list",
)
def demo_approvers() -> list[DemoApprover]:
    return [
        DemoApprover(**person.model_dump(), key=key, link=f"/?key={key}")
        for key, person in get_approvers().items()
    ]


@app.get("/demo/scenarios", tags=["Demo"], summary="Ready-made purchases for the console")
def demo_scenarios() -> list[Scenario]:
    return SCENARIOS


@app.post("/demo/reset", tags=["Demo"], status_code=204, summary="Empty orders and the audit log")
def demo_reset(service: Service) -> None:
    service.store.clear()


@app.post("/demo/scenarios/{scenario_id}", tags=["Demo"], summary="Run a scripted agent")
async def demo_run(
    scenario_id: str,
    service: Service,
    fresh: Annotated[
        bool,
        Query(description="Empty the store first, so earlier orders do not use up the budget"),
    ] = True,
) -> list[ScenarioResult]:
    scenario = next((s for s in SCENARIOS if s.id == scenario_id), None)
    if scenario is None:
        raise ServiceError("not_found", f"No scenario {scenario_id!r}.", 404)
    if fresh:
        service.store.clear()
    registry = get_agents()
    agent = registry.get(settings.aimo_agent_token) or next(iter(registry.values()))
    return await run_scenario(service, agent, scenario, date.today())


@app.get(
    "/.well-known/jwks.json",
    tags=["Agent"],
    summary="Public keys for verifying signed offers (JWK Set, RFC 7517)",
)
def signing_keys(service: Service) -> dict[str, Any]:
    if service.signer is None:
        raise ServiceError("not_found", "This gateway does not sign its offers.", 404)
    return service.signer.jwks()


@app.get("/health", include_in_schema=False)
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "aimo_api_base_url_set": bool(settings.aimo_api_base_url),
        "aimo_api_key_set": bool(settings.aimo_api_key),
    }


@app.get("/", include_in_schema=False)
def kitchen() -> FileResponse:
    """What the buyer's people see: plain words, one decision at a time."""
    return FileResponse(WEB_DIR / "kitchen.html", headers=_REVALIDATE)


@app.get("/console", include_in_schema=False)
def console() -> FileResponse:
    """The same data for those who want to see how it works: all three parties, in detail."""
    return FileResponse(WEB_DIR / "index.html", headers=_REVALIDATE)


# What a phone needs to put the kitchen view on its home screen as an app.
_ASSETS = {
    "manifest.webmanifest": "application/manifest+json",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@app.get("/assets/{name}", include_in_schema=False)
def asset(name: str) -> FileResponse:
    if name not in _ASSETS:
        raise ServiceError("not_found", f"No asset {name!r}.", 404)
    return FileResponse(WEB_DIR / name, media_type=_ASSETS[name], headers=_REVALIDATE)


# MCP over Streamable HTTP at /mcp. Mounted last so the routes above take precedence.
app.mount("/", mcp.streamable_http_app())
