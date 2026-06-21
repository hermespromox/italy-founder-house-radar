#!/usr/bin/env python3
"""Scrape/refresh Italy founder-house real-estate opportunities.

Strategy:
1) Use Scrapingdog Google Search API to discover fresh public listing URLs on
   Idealista, Immobiliare, Casa.it and Gate-away for targeted Italian regions.
2) Fetch listing pages through Scrapingdog Web Scraping API when possible.
3) Parse/normalize title, price, sqm, rooms, city/region and score the deal for
   coliving/founder-house suitability.

This deliberately stores only normalized metadata + source links, not copied
photos or full listing content.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "opportunities.json"
ENV = Path.home() / ".hermes" / ".env"

REGIONS = ["Sicilia", "Sardegna", "Calabria", "Abruzzo", "Molise"]
SOURCES = {
    "Idealista": {"domain": "idealista.it", "site": "idealista.it/immobile", "pattern": r"/immobile/\d+"},
    "Immobiliare": {"domain": "immobiliare.it", "site": "immobiliare.it/annunci", "pattern": r"/annunci/\d+"},
    "Casa": {"domain": "casa.it", "site": "casa.it/immobili", "pattern": r"/immobili/[^/]+-\d+/?"},
    "Gate-away": {"domain": "gate-away.com", "site": "gate-away.com/properties", "pattern": r"/(properties|immobili)/.+\d"},
}
SEARCH_TERMS = [
    "casa vendita {region} 30000 100000 euro 200 mq",
    "palazzo vendita {region} 50000 100000 euro",
    "immobile intero vendita {region} 100000 euro",
    "casa indipendente vendita {region} 80000 euro 200 mq",
]
HEADERS = {"User-Agent": "ItalyFounderHouseRadar/1.0 (+https://vercel.app)"}


def load_key(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    if ENV.exists():
        for line in ENV.read_text(errors="ignore").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def first_int(patterns: list[str], text: str) -> int | None:
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            raw = re.sub(r"[^0-9]", "", m.group(1))
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    pass
    return None


def infer_source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for name, cfg in SOURCES.items():
        if cfg["domain"] in host:
            return name
    return host.replace("www.", "")


def infer_region(text: str) -> str | None:
    low = text.lower()
    aliases = {"abruzzo": "Abruzzo", "abruzzi": "Abruzzo", "sicilia": "Sicilia", "sardegna": "Sardegna", "calabria": "Calabria", "molise": "Molise"}
    for k, v in aliases.items():
        if k in low:
            return v
    return None


def parse_city(text: str, region: str | None) -> str | None:
    # Common snippets: "Casa indipendente in vendita a Modica, Ragusa".
    pats = [r"(?:a|in)\s+([A-ZÀ-Ý][A-Za-zÀ-ÿ' -]{2,28})(?:,|\s+-)", r"\b([A-ZÀ-Ý][A-Za-zÀ-ÿ' -]{2,28}),\s*(?:%s)" % (region or "Italia")]
    for pat in pats:
        m = re.search(pat, text)
        if m:
            city = clean_text(m.group(1)).strip(" -,")
            if city.lower() not in {"vendita", "casa", "italia"}:
                return city[:40]
    return None


def parse_listing(url: str, title: str = "", snippet: str = "", html_text: str = "") -> dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "lxml") if html_text else None
    page_text = " ".join([title, snippet, clean_text(soup.get_text(" ") if soup else "")])
    if soup:
        h1 = soup.find("h1")
        if h1 and len(clean_text(h1.get_text())) > len(title):
            title = clean_text(h1.get_text())
        mt = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
        if mt and mt.get("content") and len(mt["content"]) > len(title):
            title = clean_text(mt["content"])
    title = clean_text(title)[:150] or "Opportunité immobilière Italie"

    price = first_int([
        r"€\s*([0-9\.]{2,9})",
        r"([0-9\.]{2,9})\s*€",
        r"EUR\s*([0-9\.]{2,9})",
    ], page_text)
    if price and price < 1000:
        price = None
    sqm = first_int([
        r"([0-9]{2,4})\s*(?:m²|mq|sqm|m2)",
        r"superficie\s*(?:di)?\s*([0-9]{2,4})",
    ], page_text)
    rooms = first_int([r"([0-9]{1,2})\s*(?:camere|locali|rooms|bedrooms)", r"camere\s*([0-9]{1,2})"], page_text)
    region = infer_region(page_text)
    city = parse_city(page_text, region)
    source = infer_source(url)
    ppm = round(price / sqm, 1) if price and sqm else None
    score = score_deal(price, sqm, rooms, page_text, region)
    reason = build_reason(price, sqm, rooms, page_text, region, ppm)
    return {
        "id": hashlib.sha1(url.encode()).hexdigest()[:12],
        "title": title,
        "url": url,
        "source": source,
        "region": region or "Italie",
        "city": city,
        "price_eur": price,
        "sqm": sqm,
        "rooms": rooms,
        "price_per_sqm": ppm,
        "score": score,
        "reason": reason,
        "snippet": clean_text(snippet)[:280],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def score_deal(price: int | None, sqm: int | None, rooms: int | None, text: str, region: str | None) -> int:
    score = 35
    low = text.lower()
    if price:
        if price <= 50000: score += 20
        elif price <= 80000: score += 15
        elif price <= 100000: score += 9
        else: score -= 30
    else:
        score -= 8
    if sqm:
        if 200 <= sqm <= 450: score += 22
        elif 150 <= sqm < 200: score += 14
        elif sqm > 450: score += 10
        else: score -= 6
    if rooms:
        if rooms >= 8: score += 12
        elif rooms >= 5: score += 8
    if any(w in low for w in ["palazzo", "intero", "indipendente", "villa", "casale", "stabile", "terratetto"]):
        score += 12
    if any(w in low for w in ["ristrutturare", "da restaurare", "to restore", "renovation"]):
        score -= 4
    if region in REGIONS:
        score += 5
    return max(0, min(100, score))


def build_reason(price, sqm, rooms, text, region, ppm) -> str:
    bits = []
    if price and price <= 100000: bits.append(f"budget compatible ({price:,}€)".replace(',', ' '))
    if sqm and sqm >= 180: bits.append(f"grande surface ({sqm} m²)")
    if rooms and rooms >= 5: bits.append(f"potentiel chambres/locali ({rooms})")
    if ppm and ppm < 500: bits.append(f"prix/m² bas (~{int(ppm)} €/m²)")
    low = text.lower()
    if any(w in low for w in ["palazzo", "stabile", "intero"]): bits.append("format immeuble/palazzo")
    if region: bits.append(region)
    return " · ".join(bits[:4]) or "Annonce à vérifier : potentiel détecté via titre/snippet et source publique."


def google_discover(key: str, limit_per_query: int = 8) -> list[dict[str, str]]:
    rows, seen = [], set()
    max_queries = int(os.environ.get("MAX_DISCOVERY_QUERIES", "30"))
    query_count = 0
    for region in REGIONS:
        for source_name, cfg in SOURCES.items():
            for term in SEARCH_TERMS[:3]:
                site = cfg.get("site", cfg["domain"])
                query = f"site:{site} {term.format(region=region)}"
                params = {
                    "api_key": key,
                    "query": query,
                    "country": "it",
                    "domain": "google.it",
                    "language": "lang_it",
                    "page": "0",
                    "num": str(limit_per_query),
                    "advance_search": "false",
                }
                if query_count >= max_queries:
                    return rows
                query_count += 1
                try:
                    r = requests.get("https://api.scrapingdog.com/google", params=params, timeout=60)
                    if r.status_code != 200:
                        print(f"WARN google {r.status_code} {query[:80]}", file=sys.stderr)
                        continue
                    data = r.json()
                except Exception as e:
                    print(f"WARN google exception {e}", file=sys.stderr)
                    continue
                organic = data.get("organic_data") or data.get("organic_results") or []
                for item in organic:
                    url = item.get("link") or item.get("url")
                    if not url or url in seen:
                        continue
                    if not any(c["domain"] in urlparse(url).netloc.lower() for c in SOURCES.values()):
                        continue
                    # Prefer individual listing URLs; Google sometimes returns category/search pages.
                    pat = cfg.get("pattern")
                    if pat and not re.search(pat, url):
                        # Keep high-signal result pages only as fallback when snippet contains concrete price+sqm.
                        if not (re.search(r"€|euro", item.get("snippet", ""), re.I) and re.search(r"m²|mq", item.get("snippet", ""), re.I)):
                            continue
                    seen.add(url)
                    rows.append({"url": url, "title": clean_text(item.get("title")), "snippet": clean_text(item.get("snippet") or item.get("description")), "source": source_name})
                time.sleep(0.25)
    return rows


def fetch_page(key: str, url: str) -> str:
    params = {"api_key": key, "url": url, "dynamic": "false", "country_code": "it"}
    try:
        r = requests.get("https://api.scrapingdog.com/scrape", params=params, timeout=70)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
        # Try JS render only when static failed.
        params["dynamic"] = "true"
        params["wait"] = "2500"
        r = requests.get("https://api.scrapingdog.com/scrape", params=params, timeout=90)
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        print(f"WARN fetch exception {url} {e}", file=sys.stderr)
        return ""


def main() -> int:
    key = load_key("SCRAPINGDOG_API_KEY")
    if not key:
        raise SystemExit("SCRAPINGDOG_API_KEY is required")
    discovered = google_discover(key)
    print(f"discovered={len(discovered)}")
    opportunities: list[dict[str, Any]] = []
    seen = set()
    # Limit detail fetches to keep credits predictable; snippets still produce usable rows.
    max_detail = int(os.environ.get("MAX_DETAIL_FETCHES", "0"))
    for i, item in enumerate(discovered[:90]):
        url = item["url"].split("#", 1)[0]
        if url in seen:
            continue
        seen.add(url)
        page = fetch_page(key, url) if i < max_detail else ""
        row = parse_listing(url, item.get("title", ""), item.get("snippet", ""), page)
        # For this product, a usable opportunity needs a visible low-budget price.
        if not row["price_eur"] or row["price_eur"] < 15000 or row["price_eur"] > 120000:
            continue
        opportunities.append(row)
        time.sleep(0.35)
    opportunities.sort(key=lambda x: (x.get("score") or 0, -(x.get("price_eur") or 999999)), reverse=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(opportunities),
        "sources": list(SOURCES.keys()),
        "regions": REGIONS,
        "opportunities": opportunities[:120],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written={OUT} count={len(opportunities[:120])}")
    return 0 if opportunities else 2


if __name__ == "__main__":
    raise SystemExit(main())
