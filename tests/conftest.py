from datetime import date, datetime, time, timedelta, timezone

import pytest

from aimo_agent.aimo_client import (
    AvailabilityResponse,
    Price,
    PriceResponse,
    ProductAvailability,
    ProductPrice,
    ScheduleLine,
    Tax,
)
from aimo_agent.catalog import Catalog, Product
from aimo_agent.models import AgentIdentity, Mandate
from aimo_agent.service import PurchasingService
from aimo_agent.store import Store

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 11, 14, 13, 0, tzinfo=TZ)  # a Saturday
MONDAY = date(2026, 11, 16)
OPEN_UNTIL = datetime(2026, 11, 15, 17, 0, tzinfo=TZ)
CLOSED_SINCE = datetime(2026, 11, 12, 15, 0, tzinfo=TZ)
UNLIMITED = 9_999_999_999


def make_product(product_id: str, name: str, **overrides: object) -> Product:
    fields: dict[str, object] = {
        "id": product_id,
        "name": name,
        "names": {"en": name},
        "brand": "Valio",
        "vendor": "VALIO OY",
        "category": "10400",
        "gtin": None,
        "sales_unit": "ST",
        "base_unit": "ST",
        "units": {"ST": 1.0},
        "lot_size": 0.01,
        "country_of_origin": "fi",
        "temperature_class": "chilled",
        "claims": ["FREE_FROM_LACTOSE"],
        "contains": ["AM"],
        "may_contain": [],
        "free_from": [],
        "labels": [],
        "substitutes": [],
        "delivery_reliability": None,
        "carbon_kg_co2e_per_kg": None,
    }
    fields.update(overrides)
    return Product.model_validate(fields)


def make_price(
    product_id: str,
    unit_price: float,
    comparison: float | None = None,
    unit: str = "L",
    tax_rate: float = 13.5,
) -> ProductPrice:
    def money(value: float) -> Price:
        return Price(
            value=value,
            value_with_tax=round(value * (1 + tax_rate / 100), 2),
            valid_from=NOW,
            valid_to=NOW + timedelta(days=365),
            currency="EUR",
        )

    return ProductPrice(
        product_id=product_id,
        quantity=1.0,
        catalog_price=money(unit_price),
        comparison_price=money(comparison) if comparison else None,
        comparison_unit=unit if comparison else None,
        tax=Tax(tax_rate=tax_rate, tax_value=round(unit_price * tax_rate / 100, 2)),
        net_price_found=False,
        error=False,
    )


def make_availability(
    product_id: str,
    amount: float = UNLIMITED,
    *,
    delivery: date = MONDAY,
    closing: datetime = OPEN_UNTIL,
    model: str = "Stock",
    blocked: bool = False,
    sales_unit: str = "ST",
) -> ProductAvailability:
    return ProductAvailability(
        product_id=product_id,
        closing_time=closing,
        sales_block=blocked,
        logistic_model=model,
        schedule_lines=[
            ScheduleLine(
                sales_block=blocked,
                available_amount=amount,
                available_date=delivery,
                sales_unit=sales_unit,
            )
        ],
    )


class FakeAimo:
    """Stands in for the mock API: canned prices and availability, mutable between calls."""

    def __init__(self) -> None:
        self.now = NOW
        self.prices: dict[str, ProductPrice] = {}
        self.stock: dict[str, ProductAvailability] = {}

    def offer(
        self,
        product_id: str,
        unit_price: float,
        comparison: float | None = None,
        unit: str = "L",
        **stock: object,
    ) -> None:
        self.prices[product_id] = make_price(product_id, unit_price, comparison or unit_price, unit)
        self.stock[product_id] = make_availability(product_id, **stock)  # type: ignore[arg-type]

    async def __aenter__(self) -> "FakeAimo":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def availability(self, products: list[str], requested_date: date) -> AvailabilityResponse:
        return AvailabilityResponse(
            time_now=self.now,
            requested_date=datetime.combine(requested_date, time(), TZ),
            products=[
                self.stock.get(p)
                or ProductAvailability(product_id=p, sales_block=False, schedule_lines=[])
                for p in products
            ],
        )

    async def price(self, products: list[str], requested_date: date) -> PriceResponse:
        return PriceResponse(
            customer_id="123",
            currency="EUR",
            pricing_date=datetime.combine(requested_date, time(), TZ),
            products=[
                self.prices.get(p)
                or ProductPrice(product_id=p, quantity=1.0, net_price_found=False, error=True)
                for p in products
            ],
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(
        [
            make_product("100", "Valio semi skimmed milk drink 1 l lactose free"),
            make_product(
                "101", "Valio skimmed milk drink 1 l lactose free", delivery_reliability=0.99
            ),
            make_product("102", "Arla Milkdrink 1L lactose free fat free UHT", brand="Arla"),
            make_product("103", "Valio semi skimmed milk drink 10 l lactose free", sales_unit="KI"),
            make_product("104", "Valio skimmed sour milk 1 l lactose free"),
            make_product("105", "Valio milk chocolate drink 1 l lactose free", category="22540"),
            make_product("106", "Valio skimmed milk 1 l", claims=[]),
            make_product(
                "107",
                "Valio Luomu organic milk drink 1 l lactose free",
                labels=["EU_ORGANIC_FARMING"],
            ),
            make_product("108", "Arla Milkdrink 1L lactose free semi skimmed UHT", brand="Arla"),
            make_product(
                "200",
                "Oat drink 1 l",
                brand="Oatly",
                category="10700",
                claims=["VEGAN"],
                contains=["AW"],
                free_from=["AM", "ML"],
                country_of_origin="se",
                substitutes=["300"],
            ),
            make_product(
                "201",
                "Nut granola 500 g",
                brand="Muesli Co",
                category="30100",
                claims=[],
                contains=["AN", "AW"],
                may_contain=["AP"],
            ),
            make_product(
                "300",
                "Oat drink barista 1 l",
                brand="Oatly",
                category="10700",
                claims=["VEGAN"],
                contains=["AN", "AW"],
                free_from=["ML"],
            ),
        ]
    )


@pytest.fixture
def aimo() -> FakeAimo:
    """Monday's market: two cheap unlimited milks, one short, one closed, one late."""
    fake = FakeAimo()
    fake.offer("100", 1.01)
    fake.offer("101", 1.10)
    fake.offer("102", 2.57, amount=51)
    fake.offer("103", 15.70, 1.57, closing=CLOSED_SINCE, model="Terminal", amount=999_999)
    fake.offer("104", 1.35)
    fake.offer("105", 2.00)
    fake.offer("106", 0.90)
    fake.offer("107", 2.19, delivery=date(2026, 11, 18))
    fake.offer("108", 2.48, amount=384)
    fake.offer("200", 1.80)
    fake.offer("201", 6.00, 12.00, unit="KG")
    fake.offer("300", 2.10)
    return fake


@pytest.fixture
def service(catalog: Catalog, aimo: FakeAimo) -> PurchasingService:
    return PurchasingService(catalog, Store(":memory:"), upstream=lambda: aimo)  # type: ignore[arg-type,return-value]


@pytest.fixture
def hotel() -> AgentIdentity:
    return AgentIdentity(
        agent_id="hotel-agent",
        agent_name="Hotel agent",
        buyer_id="B1",
        buyer_name="Hotel",
        mandate=Mandate(
            max_autonomous_spend=1000, hard_max_order_total=10000, approvers=["Head chef"]
        ),
    )


@pytest.fixture
def school() -> AgentIdentity:
    return AgentIdentity(
        agent_id="school-agent",
        agent_name="School agent",
        buyer_id="B2",
        buyer_name="School",
        mandate=Mandate(
            max_autonomous_spend=400,
            allow_substitutes=False,
            forbidden_allergens=["nuts", "peanuts"],
            approvers=["Kitchen manager"],
        ),
    )
