"""Download the static challenge datasets into data/.

Run: uv run python -m aimo_agent.datasets [--force]

The files do not change during the challenge unless the organisers announce it, so existing
files are skipped unless --force is given.
"""

import sys
from pathlib import Path

import httpx

from aimo_agent.config import DATA_DIR, settings

PRODUCTS_FILE = DATA_DIR / "valio_aimo_junction26_product_data.json"
PRODUCTS_SCHEMA_FILE = DATA_DIR / "valio_aimo_junction26_product_data_schema.json"
DELIVERY_RELIABILITY_FILE = DATA_DIR / "valio_aimo_junction26_delivery_reliability.csv"
CARBON_FOOTPRINT_FILE = DATA_DIR / "valio_aimo_junction26_carbon_footprint.csv"

# The /api/.../download paths redirect to the current file. The schema has no such endpoint,
# so it is fetched by the direct link from the resource portal.
SOURCES: dict[Path, str] = {
    PRODUCTS_FILE: "/api/products/download",
    PRODUCTS_SCHEMA_FILE: (
        "/files/008f9a50ba9a6538d2e5bdf29d592af8/valio_aimo_junction26_product_data_schema.json"
    ),
    DELIVERY_RELIABILITY_FILE: "/api/delivery-reliability/download",
    CARBON_FOOTPRINT_FILE: "/api/carbon-footprint/download",
}


def download_all(*, force: bool = False) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with httpx.Client(
        base_url=settings.aimo_api_base_url,
        headers={"X-API-Key": settings.aimo_api_key},
        follow_redirects=True,
        timeout=120,
    ) as client:
        for target, path in SOURCES.items():
            if target.exists() and not force:
                print(f"skip  {target.name} (already downloaded)")
                continue
            partial = target.with_name(target.name + ".part")
            with client.stream("GET", path) as response:
                response.raise_for_status()
                with partial.open("wb") as file:
                    for chunk in response.iter_bytes():
                        file.write(chunk)
            partial.replace(target)
            print(f"saved {target.name} ({target.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    download_all(force="--force" in sys.argv[1:])
