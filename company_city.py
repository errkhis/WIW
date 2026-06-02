import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx


DIRECTINFO_BASE = "https://www.directinfo.ma"
DIRECTINFO_SEARCH = f"{DIRECTINFO_BASE}/directinfo-backend/api/queryDsl/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{DIRECTINFO_BASE}/",
    "Origin": DIRECTINFO_BASE,
    "X-Requested-By": "directinfo-app",
}


@dataclass
class CompanyCity:
    name: str
    city: Optional[str]
    matched_name: Optional[str] = None


def lookup_company_cities(company_names: list[str]) -> list[CompanyCity]:
    unique_names = list(dict.fromkeys(name.strip() for name in company_names if name.strip()))
    results: list[CompanyCity] = []
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        client.get(DIRECTINFO_BASE)
        for name in unique_names:
            results.append(_lookup_company_city(client, name))
    return results


def _lookup_company_city(client: httpx.Client, name: str) -> CompanyCity:
    url = f"{DIRECTINFO_SEARCH}/{urllib.parse.quote(name, safe='')}"
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return CompanyCity(name=name, city=None)

    rows = payload[0] if isinstance(payload, list) and payload else []
    if not isinstance(rows, list) or not rows:
        return CompanyCity(name=name, city=None)

    row = _best_match(name, rows)
    city = _clean_value(row.get("libelle"))
    matched_name = _clean_value(row.get("denomination") or row.get("nom"))
    return CompanyCity(name=name, city=city, matched_name=matched_name)


def _best_match(name: str, rows: list[dict]) -> dict:
    target = _norm(name)
    for row in rows:
        denomination = _norm(str(row.get("denomination") or ""))
        if denomination == target:
            return row
    for row in rows:
        denomination = _norm(str(row.get("denomination") or ""))
        if target in denomination or denomination in target:
            return row
    return rows[0]


def _clean_value(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\b(ste|sarl|au|societe|société)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
