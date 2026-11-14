import pytest

from aimo_agent.units import (
    content_per_sales_unit,
    normalize_quantity,
    order_step,
    units_needed,
)


# (price per sales unit, comparison price) pairs taken from live mock API responses.
@pytest.mark.parametrize(
    ("unit_price", "comparison_price", "expected"),
    [
        (64.17, 6.42, 10),  # 10 l case
        (31.31, 1.57, 20),  # 2 x 10 l
        (27.39, 1.30, 21),  # 12 x 1.75 l
        (10.97, 2.19, 5),  # 5 x 1 l
        (7.65, 2.04, 3.75),  # 15 x 2.5 dl
        (10.44, 3.73, 2.8),  # 14 x 2 dl
        (15.09, 7.55, 2),  # 100 x 2 cl
        (1.01, 1.01, 1),  # 1 l carton
        (0.88, 1.76, 0.5),  # 0.5 l carton
        (3.32, 15.51, 0.214),  # no round number fits: keep the measured ratio
    ],
)
def test_content_is_recovered_from_rounded_prices(
    unit_price: float, comparison_price: float, expected: float
) -> None:
    assert content_per_sales_unit(unit_price, comparison_price) == pytest.approx(expected, abs=1e-3)


def test_content_is_unknown_without_a_usable_comparison_price() -> None:
    assert content_per_sales_unit(5.0, 0.0) is None


def test_units_needed_rounds_up_to_whole_sales_units() -> None:
    assert units_needed(1000, 10, 1) == 100
    assert units_needed(1000, 21, 1) == 48  # 47.6 cases -> 48
    assert units_needed(1000, 3.75, 1) == 267


def test_units_needed_respects_the_lot_size() -> None:
    assert units_needed(10, 1, 3) == 12
    assert units_needed(12.345, 1, 0.01) == 12.35


def test_order_step_is_whole_units_unless_sold_by_weight() -> None:
    assert order_step(0.01, "KI") == 1
    assert order_step(0.01, "KG") == 0.01
    assert order_step(5.0, "ST") == 5


def test_normalize_quantity_converts_to_canonical_units() -> None:
    assert normalize_quantity(1000, "litres") == (1000, "L")
    assert normalize_quantity(5, "dl") == (0.5, "L")
    assert normalize_quantity(750, "g") == (0.75, "KG")
    assert normalize_quantity(3, "units") == (3, "SALES_UNIT")
    with pytest.raises(ValueError):
        normalize_quantity(1, "barrels")
