"""Static product catalog: slim product records, dietary filters and text search.

The raw product file is 87 MB, so the first load builds a slim cache next to it
(data/catalog.cache.json) holding only the fields the purchasing flow uses.
"""

import csv
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

from aimo_agent.config import DATA_DIR
from aimo_agent.datasets import CARBON_FOOTPRINT_FILE, DELIVERY_RELIABILITY_FILE, PRODUCTS_FILE

CACHE_FILE = DATA_DIR / "catalog.cache.json"
CACHE_VERSION = 2

# Dietary vocabulary offered to agents: name -> (nutritional claim id, GS1 allergen code).
# A product is "free from X" only when its data says so explicitly, through the claim or
# through a FREE_FROM allergen declaration. Missing data never counts as free-from.
DIETARY: dict[str, tuple[str | None, str]] = {
    "lactose": ("FREE_FROM_LACTOSE", "ML"),
    "gluten": ("FREE_FROM_GLUTEN", "AW"),
    "milk": (None, "AM"),
    "eggs": ("FREE_FROM_EGGS", "AE"),
    "soy": ("FREE_FROM_SOY", "AY"),
    "wheat": ("FREE_FROM_WHEAT", "UW"),
    "nuts": (None, "AN"),
    "peanuts": (None, "AP"),
    "fish": (None, "AF"),
    "crustaceans": (None, "AC"),
    "molluscs": (None, "UM"),
    "sesame": (None, "AS"),
    "celery": (None, "BC"),
    "mustard": (None, "BM"),
    "sulphites": (None, "AU"),
    "lupin": (None, "NL"),
}

_TEMPERATURE_LABELS = {
    "warmTemperature": "ambient",
    "coolTemperature": "chilled",
    "freezeTemperature": "frozen",
}

_WORD = re.compile(r"[\w%]+")
_SMALL_WORDS = {"from", "with", "in", "of", "and", "to", "the", "as"}


def readable(code: str) -> str:
    """A data code as words for people: EU_ORGANIC_FARMING -> EU Organic Farming."""
    words = code.replace("_", " ").lower().split()
    return " ".join(
        word if word in _SMALL_WORDS else word.upper() if len(word) <= 2 else word.capitalize()
        for word in words
    )


class Product(BaseModel):
    id: str = Field(description="Product code, used in every other call")
    name: str = Field(description="English name, or Finnish when no English name exists")
    names: dict[str, str] = Field(description="Name by language code (fi, en, sv)")
    brand: str | None = None
    vendor: str
    category: str = Field(description="Wholesaler category code (names are not published)")
    gtin: str | None = Field(None, description="GS1 GTIN of the sales unit")
    sales_unit: str = Field(description="Unit the product is priced and ordered in")
    base_unit: str
    units: dict[str, float] = Field(description="Packaging unit -> size in base units")
    lot_size: float = Field(description="Order quantity step, in sales units")
    country_of_origin: str | None = None
    temperature_class: str | None = Field(None, description="ambient, chilled or frozen")
    claims: list[str] = Field(description="Nutritional claims, e.g. FREE_FROM_LACTOSE, VEGAN")
    contains: list[str] = Field(description="GS1 allergen codes the product contains")
    may_contain: list[str] = Field(description="GS1 allergen codes the product may contain")
    free_from: list[str] = Field(description="GS1 allergen codes declared absent")
    labels: list[str] = Field(description="Certificates and labels, e.g. EU_ORGANIC_FARMING")
    substitutes: list[str] = Field(description="Substitute product codes listed by the wholesaler")
    delivery_reliability: float | None = Field(
        None, description="Historic share of ordered quantity delivered, 0-1"
    )
    carbon_kg_co2e_per_kg: float | None = Field(
        None, description="Average footprint of the product group, kg CO2e per kg"
    )
    net_content: dict[str, float] = Field(
        default_factory=dict,
        description="Declared net content of one base unit, in KG and/or L (GS1 product data)",
    )

    def declared_content(self, unit: str) -> float | None:
        """KG or L in one sales unit as the product data declares it, None when it does not."""
        size = self.units.get(self.sales_unit) or (
            1.0 if self.sales_unit == self.base_unit else None
        )
        if self.base_unit == "KG":
            # Sold by weight: sizes are in kilograms already. The net weight in the data then
            # describes one piece (a 1.8 kg ham), not one base unit.
            return size if unit == "KG" else None
        per_base = self.net_content.get(unit)
        return round(per_base * size, 6) if per_base and size else None

    def is_free_from(self, substance: str) -> bool:
        """True only when the product data explicitly declares it free from the substance."""
        claim, code = DIETARY[substance]
        return (claim is not None and claim in self.claims) or code in self.free_from

    def declares_allergen(self, substance: str) -> bool:
        code = DIETARY[substance][1]
        return code in self.contains or code in self.may_contain


class SearchFilters(BaseModel):
    query: str = Field("", description="Words that must all appear in the product name or brand")
    exclude_terms: list[str] = Field(
        default_factory=list, description="Words that must not appear in the product name"
    )
    category: str | None = Field(None, description="Restrict to one category code")
    free_from: list[str] = Field(
        default_factory=list,
        description=f"Substances the product must be declared free from: {', '.join(DIETARY)}",
    )
    exclude_allergens: list[str] = Field(
        default_factory=list,
        description="Substances the product must not declare as contained or possibly contained",
    )
    labels: list[str] = Field(
        default_factory=list, description="Labels the product must carry, e.g. EU_ORGANIC_FARMING"
    )
    country_of_origin: str | None = Field(None, description="ISO 3166-1 alpha-2, lowercase")
    brand: str | None = None

    def unknown_substances(self) -> list[str]:
        return [s for s in (*self.free_from, *self.exclude_allergens) if s not in DIETARY]


class CategoryFacet(BaseModel):
    category: str
    count: int
    examples: list[str]


class SearchResult(BaseModel):
    total: int = Field(description="Number of matching products before the limit is applied")
    products: list[Product]
    categories: list[CategoryFacet] = Field(
        description="Categories of the matches, largest first: use one to narrow the search"
    )


def _stem(word: str) -> str:
    """Fold simple English plurals: eggs -> egg, tomatoes -> tomato."""
    if word.endswith("oes") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _stems(text: str) -> list[str]:
    return [_stem(word) for word in _WORD.findall(text.lower())]


def _in_order(wanted: list[str], words: list[str]) -> bool:
    """True when `wanted` appears in `words` in the same order, gaps allowed."""
    remaining = iter(words)
    return all(word in remaining for word in wanted)


class Catalog:
    def __init__(self, products: list[Product]) -> None:
        self.products = products
        self._by_id = {p.id: p for p in products}
        self._text = {p.id: " ".join([*p.names.values(), p.brand or ""]).lower() for p in products}
        self._words = {pid: set(_WORD.findall(text)) for pid, text in self._text.items()}
        self._sizes = Counter(p.category for p in products)
        # Word stems of each language's name, to tell "butter" from "butter beans" and the
        # Swedish "currypasta" (curry paste) from pasta.
        self._stems = {
            p.id: {
                language: _stems(f"{name} {p.brand or ''}") for language, name in p.names.items()
            }
            for p in products
        }

    def get(self, product_id: str) -> Product | None:
        return self._by_id.get(product_id)

    def category_size(self, category: str) -> int:
        return self._sizes[category]

    def closeness(self, product: Product, query: str) -> int:
        """How literally a product's name answers the query words.

        3: every word is a whole word of the English name, in the query's order ("filter
        coffee" is coffee, "coffee filter" is paper). 2: whole words of the English name in
        another order. 1: whole words of the name in one other language. 0: the words only
        appear inside longer words, or across languages.
        """
        wanted = _stems(query)
        names = self._stems[product.id]
        english = names.get("en", [])
        if _in_order(wanted, english):
            return 3
        if set(wanted) <= set(english):
            return 2
        return 1 if any(set(wanted) <= set(words) for words in names.values()) else 0

    def literal_matches(self, filters: SearchFilters) -> list[Product]:
        """The matches whose names answer the query most literally (see `closeness`)."""
        found = self.matches(filters)
        if not found:
            return []
        best = self.closeness(found[0], filters.query)  # `matches` puts the closest first
        return [p for p in found if self.closeness(p, filters.query) == best]

    def name_overlap(self, a: Product, b: Product) -> float:
        """Share of name words two products have in common (Jaccard), 0-1."""
        words_a, words_b = self._words[a.id], self._words[b.id]
        return len(words_a & words_b) / len(words_a | words_b) if words_a | words_b else 0.0

    def matches(self, filters: SearchFilters) -> list[Product]:
        """All products satisfying the filters, most relevant first."""
        tokens = _WORD.findall(filters.query.lower())
        excluded = [t.lower() for t in filters.exclude_terms]
        brand = filters.brand.lower().strip() if filters.brand else None
        scored: list[tuple[int, int, str, Product]] = []
        for p in self.products:
            text = self._text[p.id]
            if any(t not in text for t in tokens) or any(t in text for t in excluded):
                continue
            if filters.category and p.category != filters.category:
                continue
            if brand and brand != (p.brand or "").lower().strip():
                continue
            if filters.country_of_origin and p.country_of_origin != filters.country_of_origin:
                continue
            if any(label not in p.labels for label in filters.labels):
                continue
            if not all(p.is_free_from(s) for s in filters.free_from):
                continue
            if any(p.declares_allergen(s) for s in filters.exclude_allergens):
                continue
            # A whole-word hit ("milk") outranks a hit inside a longer word ("buttermilk").
            scored.append((-self.closeness(p, filters.query), len(p.name), p.id, p))
        scored.sort(key=lambda item: item[:3])
        return [item[3] for item in scored]

    def search(self, filters: SearchFilters, limit: int = 20) -> SearchResult:
        found = self.matches(filters)
        counts = Counter(p.category for p in found)
        facets = [
            CategoryFacet(
                category=category,
                count=count,
                examples=[p.name for p in found if p.category == category][:3],
            )
            for category, count in counts.most_common(8)
        ]
        return SearchResult(total=len(found), products=found[:limit], categories=facets)


def _read_metric(path: Path) -> dict[str, float]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = csv.reader(file)
        next(rows)
        return {row[0]: float(row[1]) for row in rows if row}


def _group(raw: dict, name: str) -> list[dict]:
    for group in raw["synkkaData"].get("classifications", []):
        if group["name"] == name:
            return group["values"]
    return []


# GS1 unit codes of declared weights and volumes -> (our unit, factor).
_MEASURES = {
    "GRM": ("KG", 0.001),
    "KGM": ("KG", 1.0),
    "MLT": ("L", 0.001),
    "CLT": ("L", 0.01),
    "DLT": ("L", 0.1),
    "LTR": ("L", 1.0),
}


def _net_content(synkka: dict) -> dict[str, float]:
    """Net weight and volume of one base unit, from the GS1 packaging data."""
    content: dict[str, float] = {}
    for packaging in synkka.get("unitConversions") or []:
        if packaging.get("synkkaPackagingType") != "BASE_UNIT_OR_EACH":
            continue
        # `netWeight` is a weight; `volume` is the declared net content, in grams or litres.
        for field in ("netWeight", "volume"):
            measure = packaging.get(field) or {}
            unit, factor = _MEASURES.get(measure.get("unit") or "", (None, 0.0))
            value = measure.get("value")
            if unit and isinstance(value, int | float) and value > 0:
                content.setdefault(unit, round(value * factor, 6))
    return content


def _slim(raw: dict, reliability: dict[str, float], carbon: dict[str, float]) -> Product:
    synkka = raw["synkkaData"]
    names = {n["language"]: n["value"].strip() for n in synkka.get("names", []) if n["value"]}
    allergens = _group(raw, "allergen")
    labels = [v["id"] for v in _group(raw, "packagingMarkedLabel")]
    return Product(
        id=raw["id"],
        name=names.get("en") or names.get("fi") or raw["id"],
        names=names,
        brand=(synkka.get("brand") or "").strip() or None,
        vendor=raw["vendorName"],
        category=raw["category"],
        gtin=raw.get("salesUnitGtin"),
        sales_unit=raw["salesUnit"],
        base_unit=raw["baseUnit"],
        units={u["unitId"]: u["sizeInBaseUnits"] for u in raw.get("units", [])},
        lot_size=raw["allowedLotSize"],
        country_of_origin=raw.get("countryOfOrigin"),
        temperature_class=next(
            (_TEMPERATURE_LABELS[x] for x in labels if x in _TEMPERATURE_LABELS), None
        ),
        claims=sorted({v["id"] for v in _group(raw, "nutritionalClaim")}),
        contains=sorted({v["id"] for v in allergens if v.get("unit") == "CONTAINS"}),
        may_contain=sorted({v["id"] for v in allergens if v.get("unit") == "MAY_CONTAIN"}),
        free_from=sorted(
            {v["id"] for v in _group(raw, "nonAllergen") if v.get("unit") == "FREE_FROM"}
        ),
        labels=sorted(x for x in labels if x not in _TEMPERATURE_LABELS),
        substitutes=[s["substituteProduct"] for s in raw.get("substitutions", [])],
        delivery_reliability=reliability.get(raw["id"]),
        carbon_kg_co2e_per_kg=carbon.get(raw["id"]),
        net_content=_net_content(synkka),
    )


def load_products() -> list[Product]:
    """Slim products from the cache, rebuilding it when the source file changed."""
    if not PRODUCTS_FILE.exists():
        raise FileNotFoundError(
            f"{PRODUCTS_FILE} is missing. Run: uv run python -m aimo_agent.datasets"
        )
    stat = PRODUCTS_FILE.stat()
    stamp = {"version": CACHE_VERSION, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text("utf-8"))
        if cached.get("stamp") == stamp:
            return [Product.model_validate(p) for p in cached["products"]]

    reliability = _read_metric(DELIVERY_RELIABILITY_FILE)
    carbon = _read_metric(CARBON_FOOTPRINT_FILE)
    raw_products = json.loads(PRODUCTS_FILE.read_text("utf-8"))
    products = [_slim(raw, reliability, carbon) for raw in raw_products]
    CACHE_FILE.write_text(
        json.dumps({"stamp": stamp, "products": [p.model_dump() for p in products]}),
        encoding="utf-8",
    )
    return products


@lru_cache
def get_catalog() -> Catalog:
    return Catalog(load_products())
