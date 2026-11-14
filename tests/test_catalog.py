from aimo_agent.catalog import Catalog, SearchFilters
from tests.conftest import make_product


def ids(catalog: Catalog, **filters: object) -> list[str]:
    return [p.id for p in catalog.matches(SearchFilters.model_validate(filters))]


def test_query_words_must_all_match(catalog: Catalog) -> None:
    assert set(ids(catalog, query="skimmed milk")) == {"100", "101", "103", "104", "106", "108"}
    assert ids(catalog, query="granola nut") == ["201"]


def test_free_from_requires_an_explicit_declaration(catalog: Catalog) -> None:
    lactose_free = ids(catalog, query="milk", free_from=["lactose"])
    assert "106" not in lactose_free  # plain milk: no claim
    assert "100" in lactose_free  # declared through the nutritional claim
    assert ids(catalog, query="oat", free_from=["lactose", "milk"]) == ["200"]  # allergen codes


def test_exclude_allergens_rejects_contains_and_may_contain(catalog: Catalog) -> None:
    assert ids(catalog, query="granola", exclude_allergens=["peanuts"]) == []
    assert ids(catalog, query="oat", exclude_allergens=["nuts"]) == ["200"]


def test_exclude_terms_category_label_and_origin_narrow_the_result(catalog: Catalog) -> None:
    assert "104" not in ids(catalog, query="milk", exclude_terms=["sour"])
    assert "105" not in ids(catalog, query="milk", category="10400")
    assert ids(catalog, query="milk", labels=["EU_ORGANIC_FARMING"]) == ["107"]
    assert ids(catalog, query="drink", country_of_origin="se") == ["200"]
    assert set(ids(catalog, query="milk", brand="arla")) == {"102", "108"}


def test_whole_word_hits_rank_before_partial_hits(catalog: Catalog) -> None:
    found = ids(catalog, query="milk")
    assert found.index("106") < found.index("102")  # "milk" before "Milkdrink"


def test_closeness_tells_the_product_from_its_namesakes() -> None:
    coffee = make_product("1", "Traditional filter coffee 500g")
    paper = make_product("2", "Coffee filter 1x4 100 pcs")
    curry = make_product(
        "3", "Curry paste 1kg", names={"en": "Curry paste 1kg", "sv": "Currypasta"}
    )
    tom_yam = make_product(
        "4", "Tom yam paste", names={"en": "Tom yam paste", "sv": "Tom yam pasta"}
    )
    penne = make_product("5", "Penne pasta 5 kg")
    tomatoes = make_product("6", "Crushed tomatoes 10kg")
    catalog = Catalog([coffee, paper, curry, tom_yam, penne, tomatoes])

    assert catalog.closeness(coffee, "filter coffee") == 3  # the words in the buyer's order
    assert catalog.closeness(paper, "filter coffee") == 2  # same words, another product
    assert catalog.closeness(penne, "pasta") == 3
    assert catalog.closeness(tom_yam, "pasta") == 1  # "pasta" is Swedish for paste
    assert catalog.closeness(curry, "pasta") == 0  # only inside a longer word
    assert catalog.closeness(tomatoes, "crushed tomato") == 3  # plural folded

    literal = catalog.literal_matches(SearchFilters(query="pasta"))
    assert [p.id for p in literal] == ["5"]


def test_search_reports_total_and_category_facets(catalog: Catalog) -> None:
    result = catalog.search(SearchFilters(query="milk", free_from=["lactose"]), limit=2)
    assert result.total == 8
    assert len(result.products) == 2
    assert result.categories[0].category == "10400"
    assert result.categories[0].count == 7


def test_unknown_substances_are_reported() -> None:
    filters = SearchFilters(free_from=["lactose", "unicorn"])
    assert filters.unknown_substances() == ["unicorn"]
