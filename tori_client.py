"""
Tori.fi scraper using the embedded React Query state from the search page.
Location filtering uses a bbox derived from geocoding a free-text address + radius.
Hope it's useful! 
"""

import base64
import json
import math
import re
from dataclasses import dataclass
from typing import Optional

import httpx

TORI_SEARCH_URL = "https://www.tori.fi/recommerce/forsale/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_TORI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "fi-FI,fi;q=0.9",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    id: str
    title: str
    price: Optional[int]
    location: str
    url: str

    @property
    def price_display(self) -> str:
        if self.price is not None:
            return f"€{self.price:,}".replace(",", "\u202f")
        return "Hinta sopimuksen mukaan"


@dataclass
class Category:
    code: str
    name: str
    count: int


@dataclass
class SearchResult:
    listings: list[Listing]
    categories: list[Category]
    total: int
    location_name: str
    current_page: int
    last_page: int


# ---------------------------------------------------------------------------
# HTML / state parsing
# ---------------------------------------------------------------------------

async def _fetch_html(params: dict) -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(TORI_SEARCH_URL, params=params, headers=_TORI_HEADERS)
        resp.raise_for_status()
        return resp.text


def _extract_search_data(html: str) -> dict:
    """Pull the dehydrated React Query state for the search query out of the page."""
    scripts = re.findall(
        r'<script[^>]*type="[^"]*json[^"]*"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    for script in scripts:
        try:
            decoded = base64.b64decode(script.strip()).decode("utf-8")
            d = json.loads(decoded)
            if "queries" not in d:
                continue
            for q in d["queries"]:
                qk = q.get("queryKey", [])
                if any(isinstance(k, dict) and k.get("scope") == "search" for k in qk):
                    return q["state"]["data"]
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
    return {}


def _parse_listings(data: dict) -> list[Listing]:
    listings = []
    for doc in data.get("docs", []):
        price_data = doc.get("price")
        price = price_data.get("amount") if price_data else None
        listings.append(
            Listing(
                id=str(doc.get("ad_id", "")),
                title=doc.get("heading", ""),
                price=price,
                location=doc.get("location", ""),
                url=doc.get("canonical_url", ""),
            )
        )
    return listings


def _parse_categories(data: dict) -> list[Category]:
    categories = []
    for f in data.get("filters", []):
        if f["name"] == "category":
            for item in f.get("filter_items", []):
                categories.append(
                    Category(
                        code=item["value"],
                        name=item["display_name"],
                        count=item["hits"],
                    )
                )
    return sorted(categories, key=lambda c: c.count, reverse=True)


# ---------------------------------------------------------------------------
# Address → bbox resolution
# ---------------------------------------------------------------------------

async def geocode_address(address: str) -> tuple[float, float, str]:
    """
    Geocodes a free-text address (biased to Finland).
    Returns (lat, lon, display_name).
    """
    params = {
        "q": address,
        "countrycodes": "fi",
        "format": "json",
        "addressdetails": "1",
        "limit": "1",
    }
    headers = {"User-Agent": "ToriBot/1.0 (address geocoding)"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(NOMINATIM_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if not data:
        raise ValueError(f"Address not found: {address}")

    result = data[0]
    lat = float(result["lat"])
    lon = float(result["lon"])

    # Build a readable display name from address components
    addr = result.get("address", {})
    parts = []
    for key in ("road", "suburb", "neighbourhood", "city", "town", "municipality"):
        val = addr.get(key)
        if val and val not in parts:
            parts.append(val)
        if len(parts) == 2:
            break
    display = ", ".join(parts) if parts else result.get("display_name", address).split(",")[0]

    return lat, lon, display


def make_bbox(lat: float, lon: float, radius_km: float) -> str:
    """
    Build a bounding box for tori.fi from a center point and radius.
    Format: lon_min,lat_min,lon_max,lat_max
    """
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    return (
        f"{round(lon - lon_delta, 6)},"
        f"{round(lat - lat_delta, 6)},"
        f"{round(lon + lon_delta, 6)},"
        f"{round(lat + lat_delta, 6)}"
    )


async def resolve_address(address: str, radius_km: float) -> tuple[str, str]:
    """
    Returns (bbox_string, display_name) for a free-text address + radius.
    """
    lat, lon, display = await geocode_address(address)
    bbox = make_bbox(lat, lon, radius_km)
    return bbox, display


# ---------------------------------------------------------------------------
# Main search entry point
# ---------------------------------------------------------------------------

async def search(
    bbox: str,
    location_name: str,
    category: str = None,
    price_from: int = None,
    price_to: int = None,
    page: int = 1,
) -> SearchResult:
    params: dict = {"bbox": bbox}
    if page > 1:
        params["page"] = page
    if category:
        params["category"] = category
    if price_from is not None:
        params["price_from"] = price_from
    if price_to is not None:
        params["price_to"] = price_to

    html = await _fetch_html(params)
    data = _extract_search_data(html)

    meta = data.get("metadata", {})
    paging = meta.get("paging", {})

    return SearchResult(
        listings=_parse_listings(data),
        categories=_parse_categories(data),
        total=meta.get("result_size", {}).get("match_count", 0),
        location_name=location_name,
        current_page=paging.get("current", 1),
        last_page=paging.get("last", 1),
    )
