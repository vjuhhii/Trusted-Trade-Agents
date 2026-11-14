"""Quantity math: turning "1000 litres" into a number of sales units.

The product data does not state litres or kilograms per sales unit in a uniform way, but the
price API does so implicitly: it returns both the price per sales unit and the comparison price
per litre or kilogram. Their ratio is the content of one sales unit.
"""

import math
from typing import Literal

Unit = Literal["L", "KG", "SALES_UNIT"]

# Spelled-out units an agent may send -> (canonical unit, factor).
_ALIASES: dict[str, tuple[Unit, float]] = {
    "l": ("L", 1),
    "litre": ("L", 1),
    "litres": ("L", 1),
    "liter": ("L", 1),
    "liters": ("L", 1),
    "dl": ("L", 0.1),
    "cl": ("L", 0.01),
    "ml": ("L", 0.001),
    "kg": ("KG", 1),
    "kilogram": ("KG", 1),
    "kilograms": ("KG", 1),
    "g": ("KG", 0.001),
    "gram": ("KG", 0.001),
    "grams": ("KG", 0.001),
    "sales_unit": ("SALES_UNIT", 1),
    "sales_units": ("SALES_UNIT", 1),
    "unit": ("SALES_UNIT", 1),
    "units": ("SALES_UNIT", 1),
    "pcs": ("SALES_UNIT", 1),
}

_STEPS = (1000, 500, 250, 100, 50, 25, 10, 5, 2.5, 1, 0.5, 0.25, 0.1, 0.05, 0.025, 0.01, 0.005)

# Both prices are rounded to cents, so each is known to within half a cent.
_HALF_CENT = 0.005


def normalize_quantity(amount: float, unit: str) -> tuple[float, Unit]:
    """Convert e.g. (5, "dl") to (0.5, "L"). Raises ValueError for unknown units."""
    key = unit.strip().lower()
    if key not in _ALIASES:
        raise ValueError(f"Unknown unit {unit!r}. Use one of: {', '.join(sorted(_ALIASES))}")
    canonical, factor = _ALIASES[key]
    return round(amount * factor, 6), canonical


def simplest_in_interval(low: float, high: float) -> float:
    """The roundest number in [low, high]: 10 rather than 9.995, 3.75 rather than 3.7512."""
    middle = (low + high) / 2
    for step in _STEPS:
        candidate = round(round(middle / step) * step, 6)
        if candidate > 0 and low <= candidate <= high:
            return candidate
    return round(middle, 4)


def content_per_sales_unit(unit_price: float, comparison_price: float) -> float | None:
    """Litres or kilograms in one sales unit, from price per unit and price per litre/kg.

    Example: 27.39 EUR per case at 1.30 EUR/L can only be a 21-litre case (12 x 1.75 l),
    because the exact ratio lies between 20.99 and 21.15 once rounding to cents is allowed for.
    """
    if unit_price <= 0 or comparison_price <= _HALF_CENT:
        return None
    low = (unit_price - _HALF_CENT) / (comparison_price + _HALF_CENT)
    high = (unit_price + _HALF_CENT) / (comparison_price - _HALF_CENT)
    return simplest_in_interval(low, high)


def order_step(lot_size: float, sales_unit: str) -> float:
    """Smallest order quantity step. Whole units, unless the product is sold by weight."""
    if lot_size >= 1 or sales_unit == "KG":
        return lot_size
    return 1.0


def units_needed(quantity: float, content: float, step: float) -> float:
    """Sales units covering `quantity`, rounded up to the order step."""
    exact = quantity / content
    return round(math.ceil(exact / step - 1e-9) * step, 4)
