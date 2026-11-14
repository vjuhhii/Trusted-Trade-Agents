"""Allergen-safe substitution: what may replace a product, and how the replacement differs."""

from aimo_agent.catalog import DIETARY, Product, readable

_ALLERGEN_NAME = {code: name for name, (_, code) in DIETARY.items()}
# Diets a kitchen relies on: a substitute must keep them.
_DIET_CLAIMS = ("VEGAN", "VEGETARIAN")
_FREE_FROM_CLAIMS = {claim for claim, _ in DIETARY.values() if claim}


def _names(codes: set[str]) -> str:
    return ", ".join(sorted(_ALLERGEN_NAME.get(code, code) for code in codes))


def review_substitute(requested: Product, candidate: Product) -> tuple[list[str], list[str]]:
    """Compare a candidate with the requested product.

    Returns (blocking, differences). A candidate with any blocking issue is never offered:
    it would add an allergen, or lose a dietary property (lactose-free, gluten-free, vegan...)
    that the requested product has. Differences are shown to the buyer as trade-offs.
    """
    blocking: list[str] = []
    differences: list[str] = []

    requested_allergens = set(requested.contains) | set(requested.may_contain)
    added = (set(candidate.contains) | set(candidate.may_contain)) - requested_allergens
    if added:
        blocking.append(f"would add allergens: {_names(added)}")

    undeclared: list[str] = []
    for substance, (claim, code) in DIETARY.items():
        if not requested.is_free_from(substance) or candidate.is_free_from(substance):
            continue
        if code in added:
            continue  # already reported as an added allergen
        if claim is not None:
            blocking.append(f"is not declared free from {substance}")
        else:
            undeclared.append(substance)
    lost_diets = [d for d in _DIET_CLAIMS if d in requested.claims and d not in candidate.claims]
    if lost_diets:
        blocking.append(f"is not {' or '.join(d.lower() for d in lost_diets)}")

    if (candidate.brand or "").lower() != (requested.brand or "").lower():
        differences.append(
            f"brand {candidate.brand or 'unknown'} instead of {requested.brand or 'unknown'}"
        )
    if candidate.category != requested.category:
        differences.append(f"category {candidate.category} instead of {requested.category}")
    if candidate.country_of_origin != requested.country_of_origin:
        differences.append(
            f"origin {candidate.country_of_origin or 'unknown'} instead of "
            f"{requested.country_of_origin or 'unknown'}"
        )
    lost_labels = sorted(set(requested.labels) - set(candidate.labels))
    if lost_labels:
        noun = "label" if len(lost_labels) == 1 else "labels"
        differences.append(f"without the {noun} {', '.join(readable(x) for x in lost_labels)}")
    lost_claims = set(requested.claims) - set(candidate.claims) - _FREE_FROM_CLAIMS
    lost_claims -= set(_DIET_CLAIMS)
    if lost_claims:
        claims = ", ".join(claim.replace("_", " ").lower() for claim in sorted(lost_claims))
        differences.append(f"not marked as {claims}")
    if undeclared:
        differences.append(f"no free-from declaration for {', '.join(undeclared)}")
    return blocking, differences
