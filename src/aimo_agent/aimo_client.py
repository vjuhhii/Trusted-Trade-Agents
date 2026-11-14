"""Typed async client for the Valio Aimo Junction 2026 mock API (availability and price).

API reference: https://junction26.valioaimo.fi/docs (portal sign-in required).

Prices and availability are dynamic and rate-limited (limits undisclosed): query them only for
products already shortlisted from the static product data, never for the whole catalog.
"""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from aimo_agent.config import settings

MAX_PRODUCTS_PER_REQUEST = 1000

# availableAmount reported for "Terminal" products: they ship from the supplier's warehouse,
# so the real quantity is unknown.
UNKNOWN_STOCK_AMOUNT = 999999


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ScheduleLine(ApiModel):
    sales_block: bool
    available_amount: float  # in sales_unit; 0 means not available
    available_date: date | None = None  # earliest delivery date, may be later than requested
    sales_unit: str


class ProductAvailability(ApiModel):
    product_id: str
    closing_time: datetime | None = None  # order cut-off to receive goods on available_date
    sales_block: bool  # True: cannot be ordered currently
    logistic_model: str | None = None  # "Stock" (real quantities) or "Terminal" (unknown)
    schedule_lines: list[ScheduleLine]  # empty: cannot be delivered on any date


class AvailabilityResponse(ApiModel):
    time_now: datetime
    requested_date: datetime
    products: list[ProductAvailability]


class Price(ApiModel):
    value: float  # without tax
    value_with_tax: float
    valid_from: datetime
    valid_to: datetime
    currency: str


class PriceWithTax(ApiModel):
    value: float
    valid_from: datetime
    valid_to: datetime
    currency: str


class Tax(ApiModel):
    tax_rate: float  # percent
    tax_value: float


class ProductPrice(ApiModel):
    product_id: str
    quantity: float  # always 1.0: prices are per sales unit
    catalog_price: Price | None = None
    price_with_tax: PriceWithTax | None = None
    comparison_price: Price | None = None  # per comparison_unit, to compare pack sizes
    comparison_unit: str | None = None  # "KG" or "L"
    tax: Tax | None = None
    list_price: Price | None = None  # before the customer-specific price or discount
    net_price_found: bool  # True: a customer-specific net price was applied
    error: bool  # True: no price could be found


class PriceResponse(ApiModel):
    customer_id: str
    currency: str
    pricing_date: datetime
    products: list[ProductPrice]


class AimoClient:
    """Use as ``async with AimoClient() as aimo: ...``. Settings default to the values in .env."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        customer: str = "123",  # required by the API but has no effect in the mock
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.customer = customer
        self._http = httpx.AsyncClient(
            base_url=base_url or settings.aimo_api_base_url,
            headers={"X-API-Key": api_key or settings.aimo_api_key},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ping(self) -> dict[str, Any]:
        response = await self._http.get("/api/ping")
        response.raise_for_status()
        return response.json()

    async def availability(
        self, products: Sequence[str], requested_date: date
    ) -> AvailabilityResponse:
        """Whether each product can be delivered on ``requested_date`` and how much is available."""
        data = await self._query("/api/availability", products, requested_date)
        return AvailabilityResponse.model_validate(data)

    async def price(self, products: Sequence[str], requested_date: date) -> PriceResponse:
        """Price of each product for delivery on ``requested_date``."""
        data = await self._query("/api/price", products, requested_date)
        return PriceResponse.model_validate(data)

    async def _query(self, path: str, products: Sequence[str], requested_date: date) -> Any:
        if not 1 <= len(products) <= MAX_PRODUCTS_PER_REQUEST:
            raise ValueError(
                f"products must contain 1-{MAX_PRODUCTS_PER_REQUEST} codes, got {len(products)}"
            )
        response = await self._http.post(
            path,
            json={
                "customer": self.customer,
                "requestedDate": requested_date.isoformat(),
                "products": list(products),
            },
        )
        response.raise_for_status()
        return response.json()
