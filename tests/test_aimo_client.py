import json
from datetime import date, datetime

import httpx
import pytest

from aimo_agent.aimo_client import MAX_PRODUCTS_PER_REQUEST, UNKNOWN_STOCK_AMOUNT, AimoClient

pytestmark = pytest.mark.anyio

# Trimmed copies of real responses from the mock API (2026-11-14).
AVAILABILITY = {
    "timeNow": "2026-11-14T11:17:41+0200",
    "requestedDate": "2026-11-16T00:00:00+0200",
    "products": [
        {"productId": "460013", "salesBlock": False, "scheduleLines": []},
        {
            "productId": "460063",
            "closingTime": "2026-11-12T15:00:00+0200",
            "salesBlock": False,
            "logisticModel": "Terminal",
            "scheduleLines": [
                {
                    "salesBlock": False,
                    "availableAmount": 999999.0,
                    "availableDate": "2026-11-16",
                    "salesUnit": "KI",
                }
            ],
        },
        {
            "productId": "460056",
            "closingTime": "2026-11-15T17:00:00+0200",
            "salesBlock": False,
            "logisticModel": "Stock",
            "scheduleLines": [
                {
                    "salesBlock": False,
                    "availableAmount": 63.0,
                    "availableDate": "2026-11-16",
                    "salesUnit": "PAK",
                }
            ],
        },
    ],
}

PRICE = {
    "customerId": "123",
    "currency": "EUR",
    "pricingDate": "2026-11-16T00:00:00+0200",
    "products": [
        {
            "productId": "460063",
            "quantity": 1.0,
            "catalogPrice": {
                "value": 64.17,
                "valueWithTax": 80.53,
                "validFrom": "2026-06-01T00:00:00+0300",
                "validTo": "2026-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "priceWithTax": {
                "value": 80.53,
                "validFrom": "2026-06-01T00:00:00+0300",
                "validTo": "2026-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "comparisonPrice": {
                "value": 6.42,
                "valueWithTax": 8.06,
                "validFrom": "2026-06-01T00:00:00+0300",
                "validTo": "2026-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "comparisonUnit": "L",
            "tax": {"taxRate": 25.5, "taxValue": 16.36},
            "listPrice": {
                "value": 80.22,
                "valueWithTax": 100.68,
                "validFrom": "2026-05-01T00:00:00+0300",
                "validTo": "9999-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "netPriceFound": True,
            "error": False,
        },
        {
            "productId": "460056",
            "quantity": 1.0,
            "catalogPrice": {
                "value": 12.23,
                "valueWithTax": 15.35,
                "validFrom": "2026-05-01T00:00:00+0300",
                "validTo": "9999-12-31T00:00:00+0200",
                "currency": "EUR",
            },
            "tax": {"taxRate": 25.5, "taxValue": 3.12},
            "netPriceFound": False,
            "error": False,
        },
    ],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_client(payload: dict, seen: list[httpx.Request]) -> AimoClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return AimoClient(
        base_url="https://aimo.test", api_key="test-key", transport=httpx.MockTransport(handler)
    )


async def test_availability_sends_documented_request_and_parses_response() -> None:
    seen: list[httpx.Request] = []
    async with make_client(AVAILABILITY, seen) as aimo:
        result = await aimo.availability(["460013", "460063", "460056"], date(2026, 11, 16))

    request = seen[0]
    assert request.url.path == "/api/availability"
    assert request.headers["X-API-Key"] == "test-key"
    assert json.loads(request.content) == {
        "customer": "123",
        "requestedDate": "2026-11-16",
        "products": ["460013", "460063", "460056"],
    }

    undeliverable, terminal, stock = result.products
    assert undeliverable.schedule_lines == []
    assert undeliverable.closing_time is None
    assert terminal.logistic_model == "Terminal"
    assert terminal.schedule_lines[0].available_amount == UNKNOWN_STOCK_AMOUNT
    assert stock.schedule_lines[0].available_amount == 63.0
    assert stock.schedule_lines[0].available_date == date(2026, 11, 16)
    assert stock.closing_time == datetime.fromisoformat("2026-11-15T17:00:00+02:00")


async def test_price_parses_optional_fields() -> None:
    seen: list[httpx.Request] = []
    async with make_client(PRICE, seen) as aimo:
        result = await aimo.price(["460063", "460056"], date(2026, 11, 16))

    assert seen[0].url.path == "/api/price"
    assert result.currency == "EUR"

    full, sparse = result.products
    assert full.catalog_price is not None and full.catalog_price.value == 64.17
    assert full.comparison_price is not None and full.comparison_price.value == 6.42
    assert full.comparison_unit == "L"
    assert full.net_price_found is True
    assert full.list_price is not None and full.list_price.valid_to.year == 9999
    assert sparse.comparison_price is None
    assert sparse.comparison_unit is None


@pytest.mark.parametrize("count", [0, MAX_PRODUCTS_PER_REQUEST + 1])
async def test_product_count_outside_api_limit_is_rejected_locally(count: int) -> None:
    seen: list[httpx.Request] = []
    async with make_client(PRICE, seen) as aimo:
        with pytest.raises(ValueError):
            await aimo.price(["460063"] * count, date(2026, 11, 16))
    assert seen == []


async def test_http_errors_are_raised() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"detail": "nope"}))
    async with AimoClient(base_url="https://aimo.test", api_key="bad", transport=transport) as aimo:
        with pytest.raises(httpx.HTTPStatusError):
            await aimo.ping()
