#!/usr/bin/env python3
"""Scrape/refresh Italy founder-house real-estate opportunities.

Product goal: NOT a cheap-apartment feed. This radar should surface whole houses,
villas, palazzi, casali and entire village buildings that could realistically become
a small coliving/founder-house: volume, nature/outdoor space, and airport access.
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

# Strongly biased toward houses/buildings + nature/outdoor words. Avoid broad
# "cheap property" queries that bring apartments, garages and land.
SEARCH_TERMS = [
    "casa indipendente {region} vendita 150 mq 100000 euro giardino",
    "villa {region} vendita 150 mq 100000 euro giardino",
    "palazzo {region} vendita 100000 euro intero stabile",
    "intero stabile {region} vendita 100000 euro",
    "casale {region} vendita 100000 euro terreno",
    "rustico abitabile {region} vendita 100000 euro giardino",
    "casa con giardino {region} vendita 80000 euro 150 mq",
    "casa di paese {region} vendita 200 mq 80000 euro",
    "townhouse {region} Italy 100000 euro 5 bedrooms",
    "country house {region} Italy 100000 euro garden",
]
HEADERS = {"User-Agent": "ItalyFounderHouseRadar/2.0 (+https://italy-founder-house-radar.vercel.app)"}

APARTMENT_WORDS = {
    "appartamento", "bilocale", "trilocale", "quadrilocale", "monolocale", "attico",
    "condominio", "piano senza ascensore", "flat", "apartment", "studio apartment",
}
JUNK_WORDS = {
    "terreno", "non edificabile", "garage", "box auto", "magazzino", "locale commerciale",
    "asta", "aste", "nuda proprietà", "multiproprietà", "timeshare", "deposito",
}
HOUSE_WORDS = {
    "casa indipendente", "indipendente", "villa", "palazzo", "intero stabile", "stabile",
    "casale", "cascina", "rustico", "terratetto", "casa singola", "casa di paese",
    "townhouse", "country house", "house", "farmhouse", "manor", "borgo", "dimora",
}
NATURE_WORDS = {
    "giardino", "terreno", "campagna", "vista", "panoramica", "mare", "collina", "uliveto",
    "oliveto", "agrumeto", "vigneto", "cortile", "terrazza", "balcone", "veranda",
    "patio", "rural", "garden", "land", "countryside", "sea view", "terrace", "orchard",
}
RENOVATION_WORDS = {"ristrutturare", "da restaurare", "to restore", "renovation", "rudere", "ruin", "collabente", "da completare"}

AIRPORT_HINTS = {
    "Sicilia": ["Catania", "Palermo", "Trapani", "Comiso", "Ragusa", "Modica", "Noto", "Siracusa", "Caltagirone", "Enna", "Castiglione di Sicilia", "Taormina", "Acireale"],
    "Sardegna": ["Cagliari", "Olbia", "Alghero", "Sassari", "Nuoro", "Bosa", "Iglesias", "Oristano"],
    "Calabria": ["Lamezia", "Catanzaro", "Cosenza", "Reggio Calabria", "Tropea", "Vibo Valentia", "Crotone", "Scalea"],
    "Abruzzo": ["Pescara", "Chieti", "Teramo", "Lanciano", "Vasto", "Sulmona", "L'Aquila"],
    "Molise": ["Campobasso", "Termoli", "Isernia", "Larino", "Agnone"],
}


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
        for m in re.finditer(pat, text, flags=re.I):
            raw = re.sub(r"[^0-9]", "", m.group(1))
            if not raw:
                continue
            try:
                val = int(raw)
            except ValueError:
                continue
            return val
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
    pats = [
        r"(?:a|in|near|vicino a)\s+([A-ZÀ-Ý][A-Za-zÀ-ÿ' -]{2,32})(?:,|\s+-|\s+\()",
        r"\b([A-ZÀ-Ý][A-Za-zÀ-ÿ' -]{2,32}),\s*(?:%s)" % (region or "Italia"),
    ]
    bad = {"vendita", "casa", "italia", "via", "viale", "corso", "contrada", "strada", "prezzo"}
    for pat in pats:
        m = re.search(pat, text)
        if m:
            city = clean_text(m.group(1)).strip(" -,.")
            if city and city.split()[0].lower() not in bad and city.lower() not in bad:
                return city[:40]
    return None


def count_keywords(low: str, words: set[str]) -> int:
    return sum(1 for w in words if w in low)


def infer_property_type(text: str) -> str:
    low = text.lower()
    if any(w in low for w in ["intero stabile", "palazzo", "stabile", "borgo"]):
        return "Immeuble / palazzo"
    if any(w in low for w in ["villa", "manor"]):
        return "Villa"
    if any(w in low for w in ["casale", "cascina", "farmhouse", "country house"]):
        return "Casale / maison de campagne"
    if any(w in low for w in ["rustico", "terratetto", "townhouse", "casa di paese"]):
        return "Maison de village"
    if any(w in low for w in ["casa indipendente", "indipendente", "casa singola", "house"]):
        return "Maison entière"
    if any(w in low for w in APARTMENT_WORDS):
        return "Appartement"
    if any(w in low for w in ["terreno", "land"]):
        return "Terrain"
    return "À vérifier"


def airport_access(region: str | None, city: str | None, text: str) -> dict[str, Any]:
    hay = " ".join([city or "", text]).lower()
    if region in AIRPORT_HINTS:
        for place in AIRPORT_HINTS[region]:
            if place.lower() in hay:
                return {"label": "probablement <1h30 aéroport", "score": 12, "nearest_hint": place}
    if any(w in hay for w in ["aeroporto", "airport", "catania", "palermo", "cagliari", "olbia", "lamezia", "pescara", "comiso"]):
        return {"label": "aéroport mentionné", "score": 10, "nearest_hint": None}
    return {"label": "aéroport à vérifier", "score": 0, "nearest_hint": None}


def classify_candidate(price: int | None, sqm: int | None, rooms: int | None, text: str, property_type: str) -> tuple[bool, list[str]]:
    low = text.lower()
    reasons: list[str] = []
    if price is None or price < 15000 or price > 120000:
        reasons.append("prix hors budget ou absent")
    if property_type in {"Appartement", "Terrain"} or any(w in low for w in APARTMENT_WORDS):
        reasons.append("appartement/terrain plutôt qu'une maison entière")
    if any(w in low for w in JUNK_WORDS):
        reasons.append("bien non adapté au coliving")
    if sqm is not None and sqm < 110:
        reasons.append("surface trop petite pour coliving")
    if not any(w in low for w in HOUSE_WORDS) and property_type == "À vérifier":
        reasons.append("pas assez d'indices maison entière")
    return (len(reasons) == 0, reasons)


def score_deal(price: int | None, sqm: int | None, rooms: int | None, text: str, region: str | None, city: str | None, property_type: str) -> tuple[int, dict[str, int], dict[str, Any]]:
    low = text.lower()
    airport = airport_access(region, city, text)
    parts = {"budget": 0, "volume": 0, "type": 0, "nature": 0, "airport": airport["score"], "risk": 0}

    if price:
        if 35000 <= price <= 90000:
            parts["budget"] += 18
        elif price < 35000:
            parts["budget"] += 13  # cheap can mean ruin; still useful but not max
        elif price <= 105000:
            parts["budget"] += 10
        else:
            parts["budget"] -= 25
    else:
        parts["budget"] -= 10

    if sqm:
        if 180 <= sqm <= 420:
            parts["volume"] += 24
        elif 140 <= sqm < 180:
            parts["volume"] += 14
        elif sqm > 420:
            parts["volume"] += 12
        elif sqm < 110:
            parts["volume"] -= 20
    else:
        parts["volume"] -= 2

    if rooms:
        if rooms >= 8:
            parts["volume"] += 10
        elif rooms >= 5:
            parts["volume"] += 7
        elif rooms <= 3:
            parts["volume"] -= 6

    if property_type == "Immeuble / palazzo":
        parts["type"] += 22
    elif property_type in {"Villa", "Casale / maison de campagne", "Maison entière", "Maison de village"}:
        parts["type"] += 16
    elif property_type in {"Appartement", "Terrain"}:
        parts["type"] -= 40

    nature_hits = count_keywords(low, NATURE_WORDS)
    if nature_hits >= 3:
        parts["nature"] += 16
    elif nature_hits == 2:
        parts["nature"] += 11
    elif nature_hits == 1:
        parts["nature"] += 6

    if region in REGIONS:
        parts["type"] += 4

    reno_hits = count_keywords(low, RENOVATION_WORDS)
    if reno_hits:
        parts["risk"] -= 8
    if any(w in low for w in ["rudere", "ruin", "collabente", "non edificabile", "asta"]):
        parts["risk"] -= 18

    base = 20
    total = base + sum(parts.values())
    return max(0, min(100, total)), parts, airport


def build_reason(price, sqm, rooms, text, region, ppm, property_type, airport, parts) -> str:
    bits = []
    if property_type and property_type != "À vérifier":
        bits.append(property_type)
    if sqm and sqm >= 140:
        bits.append(f"volume exploitable ({sqm} m²)")
    if rooms and rooms >= 5:
        bits.append(f"potentiel 5+ chambres/pièces ({rooms})")
    low = text.lower()
    if count_keywords(low, NATURE_WORDS):
        labels = []
        for w, label in [("giardino", "jardin"), ("terreno", "terrain"), ("campagna", "campagne"), ("vista", "vue"), ("terrazza", "terrasse"), ("mare", "mer")]:
            if w in low and label not in labels:
                labels.append(label)
        bits.append("nature/espace" + (f" ({', '.join(labels[:3])})" if labels else ""))
    if airport.get("score"):
        bits.append(airport["label"])
    if price and price <= 100000:
        bits.append(f"budget compatible ({price:,}€)".replace(',', ' '))
    if ppm and ppm < 650:
        bits.append(f"prix/m² intéressant (~{int(ppm)} €/m²)")
    if region:
        bits.append(region)
    return " · ".join(bits[:5]) or "À vérifier manuellement : indices partiels de maison exploitable."


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
    title = clean_text(title)[:150] or "Opportunité maison Italie"

    # Avoid mistakenly taking land area like "600.000 € 100.000 m²" as listing price/sqm.
    price = first_int([r"€\s*([0-9\.]{2,9})", r"([0-9\.]{2,9})\s*€", r"EUR\s*([0-9\.]{2,9})"], page_text)
    if price and price < 1000:
        price = None
    sqm = first_int([r"([0-9]{2,4})\s*(?:m²|mq|sqm|m2)\b", r"superficie\s*(?:di)?\s*([0-9]{2,4})"], page_text)
    if sqm and sqm > 900:
        sqm = None
    rooms = first_int([r"([0-9]{1,2})\s*(?:camere|locali|rooms|bedrooms)", r"camere\s*([0-9]{1,2})"], page_text)
    region = infer_region(page_text)
    city = parse_city(page_text, region)
    source = infer_source(url)
    ppm = round(price / sqm, 1) if price and sqm else None
    property_type = infer_property_type(page_text)
    score, score_parts, airport = score_deal(price, sqm, rooms, page_text, region, city, property_type)
    is_candidate, reject_reasons = classify_candidate(price, sqm, rooms, page_text, property_type)
    if reject_reasons:
        score = min(score, 44)
    quality_tier = "Top coliving pick" if score >= 78 and is_candidate else "Maison à étudier" if score >= 60 and is_candidate else "À vérifier"
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
        "property_type": property_type,
        "airport_access": airport["label"],
        "nearest_airport_hint": airport.get("nearest_hint"),
        "score": score,
        "score_parts": score_parts,
        "quality_tier": quality_tier,
        "is_coliving_candidate": is_candidate,
        "reject_reasons": reject_reasons,
        "reason": build_reason(price, sqm, rooms, page_text, region, ppm, property_type, airport, score_parts),
        "snippet": clean_text(snippet)[:320],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def google_discover(key: str, limit_per_query: int = 6) -> list[dict[str, str]]:
    rows, seen = [], set()
    max_queries = int(os.environ.get("MAX_DISCOVERY_QUERIES", "45"))
    query_count = 0
    for region in REGIONS:
        for source_name, cfg in SOURCES.items():
            for term in SEARCH_TERMS:
                site = cfg.get("site", cfg["domain"])
                query = f"site:{site} {term.format(region=region)} -appartamento -bilocale -trilocale -quadrilocale -garage -terreno"
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
                        print(f"WARN google {r.status_code} {query[:100]}", file=sys.stderr)
                        continue
                    data = r.json()
                except Exception as e:
                    print(f"WARN google exception {e}", file=sys.stderr)
                    continue
                organic = data.get("organic_data") or data.get("organic_results") or []
                for item in organic:
                    url = (item.get("link") or item.get("url") or "").split("#", 1)[0]
                    if not url or url in seen:
                        continue
                    if not any(c["domain"] in urlparse(url).netloc.lower() for c in SOURCES.values()):
                        continue
                    pat = cfg.get("pattern")
                    snippet = clean_text(item.get("snippet") or item.get("description"))
                    title = clean_text(item.get("title"))
                    text = f"{title} {snippet}".lower()
                    if pat and not re.search(pat, url):
                        if not (re.search(r"€|euro", text, re.I) and re.search(r"m²|mq|sqm", text, re.I)):
                            continue
                    # Strong pre-filter to save detail fetches/credits.
                    if any(w in text for w in APARTMENT_WORDS | JUNK_WORDS):
                        continue
                    if not any(w in text for w in HOUSE_WORDS):
                        continue
                    seen.add(url)
                    rows.append({"url": url, "title": title, "snippet": snippet, "source": source_name})
                time.sleep(0.2)
    return rows


def fetch_page(key: str, url: str) -> str:
    params = {"api_key": key, "url": url, "dynamic": "false", "country_code": "it"}
    try:
        r = requests.get("https://api.scrapingdog.com/scrape", params=params, timeout=70)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
        params["dynamic"] = "true"
        params["wait"] = "2500"
        r = requests.get("https://api.scrapingdog.com/scrape", params=params, timeout=90)
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        print(f"WARN fetch exception {url} {e}", file=sys.stderr)
        return ""


def load_existing() -> list[dict[str, Any]]:
    if not OUT.exists():
        return []
    try:
        data = json.loads(OUT.read_text(encoding="utf-8"))
        return data.get("opportunities") or []
    except Exception:
        return []


def main() -> int:
    key = load_key("SCRAPINGDOG_API_KEY")
    if not key:
        raise SystemExit("SCRAPINGDOG_API_KEY is required")
    discovered = google_discover(key)
    print(f"discovered={len(discovered)}")

    existing_by_url = {r.get("url"): r for r in load_existing() if r.get("url")}
    fresh: list[dict[str, Any]] = []
    seen = set()
    max_detail = int(os.environ.get("MAX_DETAIL_FETCHES", "0"))
    for i, item in enumerate(discovered[:120]):
        url = item["url"].split("#", 1)[0]
        if url in seen:
            continue
        seen.add(url)
        page = fetch_page(key, url) if i < max_detail else ""
        row = parse_listing(url, item.get("title", ""), item.get("snippet", ""), page)
        if row["price_eur"] and 15000 <= row["price_eur"] <= 120000:
            fresh.append(row)
        time.sleep(0.25)

    # Preserve useful historical candidates, but rescore them with the stricter logic
    # using their title+snippet. Weak/apartment rows will be hidden by default in UI.
    merged: dict[str, dict[str, Any]] = {r["url"]: r for r in fresh if r.get("url")}
    for url, old in existing_by_url.items():
        if url in merged:
            continue
        rescored = parse_listing(url, old.get("title", ""), old.get("snippet", ""), "")
        # Keep only plausible house candidates from history; don't keep old apartment junk.
        if rescored.get("is_coliving_candidate") and (rescored.get("score") or 0) >= 48:
            merged[url] = {**old, **rescored, "scraped_at": old.get("scraped_at") or rescored["scraped_at"]}

    opportunities = list(merged.values())
    opportunities.sort(key=lambda x: ((x.get("is_coliving_candidate") is True), x.get("score") or 0, x.get("sqm") or 0), reverse=True)
    visible_count = sum(1 for r in opportunities if r.get("is_coliving_candidate") and (r.get("score") or 0) >= 55)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(opportunities),
        "visible_count": visible_count,
        "quality_definition": "Whole houses/villas/palazzi/casali with space/nature and plausible airport access; apartments, land, garages and tiny units are penalized or hidden.",
        "sources": list(SOURCES.keys()),
        "regions": REGIONS,
        "opportunities": opportunities[:140],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written={OUT} count={len(opportunities[:140])} visible={visible_count}")
    return 0 if opportunities else 2


if __name__ == "__main__":
    raise SystemExit(main())
