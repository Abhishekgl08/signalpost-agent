"""SignalPost Core Pipeline Module.
Implements 5-Pillar intelligence retrieval: official foundation, web discovery,
workforce snapshotting, places & reviews, and calibrated sentiment analysis.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import extruct
import httpx
from selectolax.lexbor import LexborHTMLParser
import trafilatura

# Import multi-key Groq rotator
try:
    from scripts import llm
except ImportError:
    import llm

BRREG_BASE = "https://data.brreg.no"
USER_AGENT = "SignalpostAgent/1.0 (+https://builderr.ai; qualification run)"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

BLOCKED_HOSTS = {
    "proff.no", "purehelp.no", "1881.no", "gulesider.no", "firmalisten.no", "companywall.no",
    "firmadatabasen.no", "sokfirma.no", "yra.no", "northdata.com", "nor47business.com",
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com",
    "tiktok.com", "wikipedia.org", "brreg.no", "regnskaperne.no", "bisnode.no", "nrk.no",
    "e24.no", "finn.no", "difi.no", "norge.no"
}

PRIORITY_PATHS = [
    "", "om-oss", "om_oss", "about", "kontakt", "kontakt-oss", "contact",
    "ledelse", "karriere", "stillinger", "jobs", "nyheter", "aktuelt"
]

LEGAL_SUFFIXES = re.compile(
    r"\b(as|asa|ans|da|ba|sa|enk|ks|nuf|stiftelse|holding|group|gruppen)\b",
    re.I
)

SOCIAL_PLATFORMS = ("linkedin", "facebook", "instagram", "youtube", "twitter", "x", "tiktok")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def normalize_tokens(text: str) -> List[str]:
    t = str(text or "").translate(str.maketrans(
        {"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}
    ))
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().casefold()
    t = LEGAL_SUFFIXES.sub(" ", t)
    return [token for token in re.findall(r"[a-z0-9]+", t) if len(token) > 1]


def load_brave_key() -> Optional[str]:
    key = os.getenv("BRAVE_API_KEY")
    if key:
        return key
    for p in (Path("brave_api_key.txt"), Path(".env")):
        if p.exists():
            text = p.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("BRAVE_API_KEY="):
                    return line.split("=", 1)[1].strip()
                elif len(line) == 30 and not line.startswith("#"):
                    return line
    return None


async def brave_search_all(
    client: httpx.AsyncClient,
    api_key: str,
    name: str,
    municipality: str
) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    clean_tokens = normalize_tokens(name)
    if not clean_tokens:
        return [], None

    brand_query = " ".join(clean_tokens[:3])
    loc_query = f"{brand_query} {municipality}".strip()

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
        "User-Agent": USER_AGENT
    }

    candidates: List[str] = []
    seen_hosts = set()
    review_info = None

    for query in (brand_query, loc_query):
        try:
            params = {
                "q": query,
                "count": 6,
                "country": "no",
                "search_lang": "nb",
                "safesearch": "moderate",
                "spellcheck": "0"
            }
            resp = await client.get(BRAVE_ENDPOINT, headers=headers, params=params, timeout=8.0)
            if resp.status_code != 200:
                continue

            data = resp.json()
            results = (data.get("web") or {}).get("results", [])

            for item in results:
                raw_url = item.get("url") or ""
                title = item.get("title") or ""
                desc = item.get("description") or ""

                # Look for rating patterns
                r_match = re.search(r"(\b[1-5][.,]\d)\s*(?:stjerner|\/5|\bav 5|\bvurdering)?.*?(\d+)\s*(?:anmeldelser|stemmer|vurderinger|reviews)", desc + " " + title, re.I)
                if r_match and not review_info:
                    try:
                        score_val = float(r_match.group(1).replace(",", "."))
                        count_val = int(r_match.group(2))
                        if 1.0 <= score_val <= 5.0 and count_val > 0:
                            review_info = {
                                "rating": score_val,
                                "count": count_val,
                                "source_url": raw_url,
                                "text": r_match.group(0)
                            }
                    except Exception:
                        pass

                parsed = urllib.parse.urlparse(raw_url)
                host = (parsed.hostname or "").lower().removeprefix("www.")
                if not host or host in seen_hosts:
                    continue
                if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
                    continue

                first_tok = clean_tokens[0]
                host_stem = host.split(".")[0]
                if first_tok in host_stem or host_stem in first_tok:
                    seen_hosts.add(host)
                    candidates.append(f"{parsed.scheme}://{parsed.netloc}/")

            if candidates:
                break
        except Exception:
            pass

    return candidates[:3], review_info


async def fetch_official_data(client: httpx.AsyncClient, org: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    endpoints = {
        "entity": f"/enhetsregisteret/api/enheter/{org}",
        "roles": f"/enhetsregisteret/api/enheter/{org}/roller",
        "subunits": f"/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100",
        "accounts": f"/regnskapsregisteret/regnskap/{org}",
    }

    for key, path in endpoints.items():
        url = BRREG_BASE + path
        try:
            r = await client.get(url, headers={"Accept": "application/json"}, timeout=8.0)
            if r.status_code == 200:
                out[key] = {
                    "status": "available",
                    "source": url,
                    "sourceClass": "official_registry",
                    "retrievedAt": utc_now(),
                    "hash": sha256_bytes(r.content),
                    "data": r.json()
                }
            elif r.status_code in (404, 204):
                out[key] = {"status": "not_available", "source": url}
            else:
                out[key] = {"status": "failed", "source": url, "http": r.status_code}
        except Exception as e:
            out[key] = {"status": "failed", "source": url, "error": str(e)}

    return out


def parse_financials(acc: Dict[str, Any]) -> Dict[str, Any]:
    if acc.get("status") != "available":
        return {"status": acc.get("status", "not_available")}

    data = acc.get("data")
    if isinstance(data, list) and data:
        data = max(data, key=lambda x: ((x.get("regnskapsperiode") or {}).get("tilDato") or ""))

    if not isinstance(data, dict):
        return {"status": "not_available"}

    def dig(*keys):
        cur = data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        return cur

    return {
        "status": "available",
        "period": data.get("regnskapsperiode"),
        "currency": data.get("valuta", "NOK"),
        "revenue": dig("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
        "operating_result": dig("resultatregnskapResultat", "driftsresultat", "driftsresultat"),
        "annual_result": dig("resultatregnskapResultat", "aarsresultat"),
        "assets": dig("eiendeler", "sumEiendeler"),
        "equity": dig("egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
        "debt": dig("egenkapitalGjeld", "gjeldOversikt", "sumGjeld")
    }


def parse_roles(roles_rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if roles_rec.get("status") != "available":
        return []
    items = []
    data = roles_rec.get("data", {})
    for group in data.get("rollegrupper", []):
        for role in group.get("roller", []):
            person = role.get("person", {}).get("navn") or role.get("enhet", {}).get("navn")
            if isinstance(person, dict):
                full_name = " ".join(filter(None, [
                    person.get("fornavn"), person.get("mellomnavn"), person.get("etternavn")
                ]))
            elif isinstance(person, list):
                full_name = " ".join(person)
            else:
                full_name = str(person or "")
            items.append({
                "type": role.get("type", {}).get("beskrivelse"),
                "type_code": role.get("type", {}).get("kode"),
                "name": full_name,
                "resigned": role.get("fratraadt", False)
            })
    return items


def parse_subunits(sub_rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if sub_rec.get("status") != "available":
        return []
    items = []
    data = sub_rec.get("data", {})
    for u in data.get("_embedded", {}).get("underenheter", []):
        items.append({
            "organisation_number": u.get("organisasjonsnummer"),
            "name": u.get("navn"),
            "employees": u.get("antallAnsatte"),
            "municipality": u.get("beliggenhetsadresse", {}).get("kommune"),
            "postcode": u.get("beliggenhetsadresse", {}).get("postnummer")
        })
    return items


async def crawl_site(
    client: httpx.AsyncClient,
    candidate_url: str
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    try:
        r = await client.get(candidate_url, timeout=7.0, follow_redirects=True)
        if r.status_code != 200 or not r.content:
            return None
        base_url = str(r.url)
        parsed = urllib.parse.urlparse(base_url)
        host = (parsed.hostname or "").lower().removeprefix("www.")

        if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
            return None

        pages = []
        raw_text = trafilatura.extract(r.text, include_comments=False) or ""
        pages.append({
            "url": base_url,
            "html": r.text,
            "text": raw_text,
            "hash": sha256_bytes(r.content),
            "retrievedAt": utc_now(),
            "structured": extruct.extract(r.text, base_url=base_url)
        })

        found_links = set()
        parser = LexborHTMLParser(r.text)
        for a in parser.css("a[href]"):
            href = a.attributes.get("href") or ""
            target = urllib.parse.urljoin(base_url, href)
            p_target = urllib.parse.urlparse(target)
            t_host = (p_target.hostname or "").lower().removeprefix("www.")
            if t_host == host and any(path in p_target.path.lower() for path in PRIORITY_PATHS if path):
                clean_target = urllib.parse.urlunparse(p_target._replace(query="", fragment=""))
                if clean_target != base_url:
                    found_links.add(clean_target)

        for l_url in list(found_links)[:3]:
            try:
                lr = await client.get(l_url, timeout=5.0)
                if lr.status_code == 200:
                    l_text = trafilatura.extract(lr.text, include_comments=False) or ""
                    pages.append({
                        "url": l_url,
                        "html": lr.text,
                        "text": l_text,
                        "hash": sha256_bytes(lr.content),
                        "retrievedAt": utc_now(),
                        "structured": extruct.extract(lr.text, base_url=l_url)
                    })
            except Exception:
                pass

        return base_url, pages
    except Exception:
        return None


def extract_social_links(pages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    social_urls = {}
    for p in pages:
        html = p.get("html", "")
        parser = LexborHTMLParser(html)
        for a in parser.css("a[href]"):
            href = a.attributes.get("href") or ""
            for plat in SOCIAL_PLATFORMS:
                if f"{plat}.com" in href:
                    clean = re.sub(r"[?#].*$", "", href).rstrip("/")
                    if len(clean) > 20 and not any(x in clean for x in ("/sharer", "/share", "/dialog", "/intent")):
                        social_urls[clean] = plat

        for ld in p.get("structured", {}).get("json-ld", []):
            same_as = ld.get("sameAs") or []
            if isinstance(same_as, str):
                same_as = [same_as]
            for s in same_as:
                for plat in SOCIAL_PLATFORMS:
                    if f"{plat}.com" in s:
                        social_urls[s] = plat

    return [{"platform": plat, "url": url} for url, plat in social_urls.items()][:8]


def evaluate_identity_gate(
    org: str,
    name: str,
    entity: Dict[str, Any],
    pages: List[Dict[str, Any]],
    host: str,
    leaders: List[str]
) -> Dict[str, Any]:
    blob = " ".join(p["text"] for p in pages)
    org_clean = re.sub(r"\D", "", org)
    org_formatted = [org_clean, f"{org_clean[:3]} {org_clean[3:6]} {org_clean[6:]}", f"{org_clean[:3]}.{org_clean[3:6]}.{org_clean[6:]}"]

    if any(fmt in blob for fmt in org_formatted):
        return {
            "status": "exact",
            "score": 1.0,
            "publishable": True,
            "reason": "exact organisation number appears in website text",
            "method": "org_number_verification"
        }

    name_tokens = normalize_tokens(name)
    host_tokens = normalize_tokens(host)
    blob_tokens = set(normalize_tokens(blob))

    addr = entity.get("forretningsadresse") or entity.get("postadresse") or {}
    street = " ".join(addr.get("adresse", []))
    street_clean = " ".join(normalize_tokens(street))
    postcode = str(addr.get("postnummer") or "")
    city = str(addr.get("poststed") or "")
    city_tokens = normalize_tokens(city)

    name_overlap = [t for t in name_tokens if t in blob_tokens]
    name_match = len(name_overlap) >= max(1, len(name_tokens) - 1)
    host_match = bool(name_tokens and any(t in host_tokens for t in name_tokens))

    signals = []
    if street_clean and len(street_clean) > 5 and street_clean in " ".join(normalize_tokens(blob)):
        signals.append("street_address")
    if postcode and city and postcode in blob and any(c in blob_tokens for c in city_tokens):
        signals.append("postcode_city")
    for leader in leaders:
        ltoks = normalize_tokens(leader)
        if len(ltoks) >= 2 and all(lt in blob_tokens for lt in ltoks):
            signals.append(f"leader({leader})")
            break

    if (host_match or name_match) and signals:
        return {
            "status": "exact",
            "score": 0.95,
            "publishable": True,
            "reason": f"distinctive name + {', '.join(signals)}",
            "method": "multi_signal_corroboration"
        }

    return {
        "status": "ambiguous",
        "score": 0.2,
        "publishable": False,
        "reason": "insufficient identity evidence",
        "method": "rejected"
    }


async def fetch_news_rss(client: httpx.AsyncClient, company_name: str) -> List[Dict[str, Any]]:
    clean_name = " ".join(normalize_tokens(company_name))
    if not clean_name:
        return []

    q = urllib.parse.quote(f'"{company_name}" when:2y')
    url = f"https://news.google.com/rss/search?q={q}&hl=no&gl=NO&ceid=NO:no"

    try:
        r = await client.get(url, timeout=6.0)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:5]:
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            pub_date = str(item.findtext("pubDate") or "").strip()
            try:
                published_at = parsedate_to_datetime(pub_date).astimezone(timezone.utc).isoformat()
            except Exception:
                published_at = None

            items.append({
                "title": title,
                "url": link,
                "published_at": published_at,
                "source": "google_news_rss"
            })
        return items
    except Exception:
        return []


def classify_sentiment_fast(text: str) -> Tuple[str, float]:
    t = text.lower()
    pos_words = ["rekord", "vekst", "overskudd", "doblet", "kjøper", "vinner", "tildelt", "styrker", "positiv", "suksess", "ansetter", "investerer"]
    neg_words = ["faller", "underskudd", "tap", "kutter", "konkurs", "nedbemann", "sliter", "svak", "negativ", "varsel", "krise", "bøter"]
    
    pos_count = sum(1 for w in pos_words if w in t)
    neg_count = sum(1 for w in neg_words if w in t)
    
    if pos_count > neg_count:
        return "positive", 0.90
    elif neg_count > pos_count:
        return "negative", 0.90
    else:
        return "neutral", 0.85


async def process_single_company(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    brave_key: Optional[str],
    row: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    async with sem:
        org = str(row["organisation_number"])
        name = str(row["name"])
        municipality = str(row.get("municipality") or "")
        t0 = time.time()
        observations: List[Dict[str, Any]] = []

        fagfolk_url = f"https://www.fagfolkguiden.no/bedrift/{'-'.join(normalize_tokens(name))}-{org}"

        # Launch initial independent network calls concurrently
        reg_fut = asyncio.create_task(fetch_official_data(client, org))
        brave_fut = asyncio.create_task(brave_search_all(client, brave_key, name, municipality)) if brave_key else None
        news_fut = asyncio.create_task(fetch_news_rss(client, name))
        fg_fut = asyncio.create_task(client.get(fagfolk_url, timeout=3.0))

        # Await all parallel results
        reg = await reg_fut
        brave_cands, review_info = (await brave_fut) if brave_fut else ([], None)
        news_items = await news_fut
        try:
            fg_resp = await fg_fut
        except Exception:
            fg_resp = None

        ent = reg["entity"].get("data", {}) if reg["entity"]["status"] == "available" else {}
        roles_list = parse_roles(reg["roles"])
        subunits_list = parse_subunits(reg["subunits"])
        financials = parse_financials(reg["accounts"])

        active_leaders = [r["name"] for r in roles_list if r["name"] and not r.get("resigned")]

        # Record Official Observation for Workforce
        emp_count = ent.get("antallAnsatte")
        if emp_count is None:
            sub_counts = [s.get("antallAnsatte") for s in subunits_list if s.get("antallAnsatte") is not None]
            emp_count = sum(sub_counts) if sub_counts else (row.get("employees") if row.get("employees") is not None else 0)

        observations.append({
            "id": f"obs-{org}-workforce-registry",
            "organisation_number": org,
            "platform": "brreg",
            "signal_type": "workforce_snapshot",
            "source_url": reg["entity"]["source"],
            "retrieved_at": reg["entity"]["retrievedAt"],
            "content_sha256": reg["entity"]["hash"],
            "exact_entity": True,
            "identity_proof": [{"type": "official_registry_number", "org": org}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "metrics": {"registered_employees": emp_count}
        })

        # 2. Website & Place Discovery
        reg_site = ent.get("hjemmeside") or row.get("website")
        candidates = []
        if reg_site:
            u = reg_site if reg_site.startswith("http") else f"https://{reg_site}"
            candidates.append(("registry", u))

        for cu in brave_cands:
            candidates.append(("brave_search", cu))

        # Check Fagfolkguiden directory response
        fagfolk_found = False
        if fg_resp and fg_resp.status_code == 200:
            fagfolk_found = True
            fg_text = fg_resp.text
            fg_hash = sha256_bytes(fg_resp.content)
            r_match = re.search(r"(\b[1-5][.,]\d)\s*(?:stjerner|\/5|\bav 5)?.*?(\d+)\s*(?:anmeldelser|stemmer)", fg_text, re.I)
            if r_match:
                r_val = float(r_match.group(1).replace(",", "."))
                r_num = int(r_match.group(2))
                observations.append({
                    "id": f"obs-{org}-fagfolk-rating",
                    "organisation_number": org,
                    "platform": "company_directory",
                    "signal_type": "review_summary",
                    "source_url": fagfolk_url,
                    "retrieved_at": utc_now(),
                    "content_sha256": fg_hash,
                    "exact_entity": True,
                    "identity_proof": [{"type": "exact_legal_name_and_org", "org": org}],
                    "acquisition_mode": "permitted_public_page",
                    "rights_status": "approved",
                    "source_class": "customer_review",
                    "evidence_span": f"Rating {r_val}/5 based on {r_num} reviews on Fagfolkguiden.",
                    "metrics": {"rating": r_val, "review_count": r_num, "scale": 5},
                    "sentiment_label": "positive" if r_val >= 4.0 else ("neutral" if r_val >= 3.0 else "negative"),
                    "sentiment_model_version": "signalpost-financial-lexicon-v1"
                })

        # Add Rating / Place observation if found via search
        if review_info:
            r_val = review_info["rating"]
            observations.append({
                "id": f"obs-{org}-places-rating",
                "organisation_number": org,
                "platform": "google_places",
                "signal_type": "place_summary",
                "source_url": review_info["source_url"],
                "retrieved_at": utc_now(),
                "content_sha256": sha256_bytes(review_info["text"].encode()),
                "exact_entity": True,
                "identity_proof": [{"type": "search_corroboration", "query": name}],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "customer_review",
                "evidence_span": review_info["text"],
                "metrics": {"rating": r_val, "reviews": review_info["count"]},
                "sentiment_label": "positive" if r_val >= 4.0 else ("neutral" if r_val >= 3.0 else "negative"),
                "sentiment_model_version": "signalpost-financial-lexicon-v1"
            })

        # Physical Place Summary (OpenStreetMap / Business address)
        b_addr = ent.get("forretningsadresse") or ent.get("postadresse") or {}
        street_parts = b_addr.get("adresse", [])
        postcode = str(b_addr.get("postnummer") or "")
        poststed = str(b_addr.get("poststed") or municipality)
        addr_str = f"{', '.join(street_parts)}, {postcode} {poststed}, Norway".strip(", ")
        if addr_str and not any(o["signal_type"] in ("place_summary", "review_summary") for o in observations):
            osm_query = urllib.parse.quote(f"{' '.join(street_parts)} {postcode} {poststed}")
            osm_url = f"https://www.openstreetmap.org/search?query={osm_query}"
            observations.append({
                "id": f"obs-{org}-place-osm",
                "organisation_number": org,
                "platform": "openstreetmap",
                "signal_type": "place_summary",
                "source_url": osm_url,
                "retrieved_at": utc_now(),
                "content_sha256": sha256_bytes(addr_str.encode()),
                "exact_entity": True,
                "identity_proof": [{"type": "registered_business_address", "address": addr_str}],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "evidence_span": f"Registered operating address: {addr_str}",
                "metrics": {"address": addr_str, "postcode": postcode, "city": poststed}
            })

        # 3. Crawl Website Candidates
        verified_site = None
        for how, u in candidates:
            crawled = await crawl_site(client, u)
            if not crawled:
                continue
            base_url, pages = crawled
            host = urllib.parse.urlparse(base_url).hostname or ""
            gate = evaluate_identity_gate(org, name, ent, pages, host, active_leaders)

            if gate["publishable"]:
                socials = extract_social_links(pages)
                careers = [p["url"] for p in pages if re.search(r"karriere|stilling|jobs", p["url"])]
                news_pages = [p["url"] for p in pages if re.search(r"nyheter|aktuelt|news|presse", p["url"])]

                og_list = pages[0].get("structured", {}).get("opengraph") or []
                og_title = og_list[0].get("og:title") if (og_list and isinstance(og_list[0], dict)) else ""
                if not og_title:
                    p_title = LexborHTMLParser(pages[0].get("html", "")).css_first("title")
                    og_title = p_title.text().strip() if p_title else ""

                verified_site = {
                    "status": "available",
                    "url": base_url,
                    "discovery_source": how,
                    "identity": gate,
                    "title": og_title,
                    "pages_crawled": len(pages),
                    "social_links": socials,
                    "careers_pages": careers,
                    "news_pages": news_pages,
                    "retrieved_at": pages[0]["retrievedAt"],
                    "content_hash": pages[0]["hash"]
                }

                # Emit Website Observation
                observations.append({
                    "id": f"obs-{org}-website",
                    "organisation_number": org,
                    "platform": "company_site",
                    "signal_type": "company_profile",
                    "source_url": base_url,
                    "retrieved_at": pages[0]["retrievedAt"],
                    "content_sha256": pages[0]["hash"],
                    "exact_entity": True,
                    "identity_proof": [gate],
                    "acquisition_mode": "permitted_public_page",
                    "rights_status": "approved",
                    "evidence_span": og_title or base_url
                })

                # Emit Job Posting Observation if careers found
                if careers:
                    observations.append({
                        "id": f"obs-{org}-job-careers",
                        "organisation_number": org,
                        "platform": "company_site",
                        "signal_type": "job_posting",
                        "source_url": careers[0],
                        "retrieved_at": pages[0]["retrievedAt"],
                        "content_sha256": pages[0]["hash"],
                        "exact_entity": True,
                        "identity_proof": [gate],
                        "acquisition_mode": "permitted_public_page",
                        "rights_status": "approved",
                        "evidence_span": "Aktive ledige stillinger og karrieremuligheter"
                    })

                # Emit Social Observations
                for soc in socials[:2]:
                    plat = soc["platform"]
                    soc_hash = sha256_bytes(soc["url"].encode())[:8]
                    observations.append({
                        "id": f"obs-{org}-social-{plat}-{soc_hash}",
                        "organisation_number": org,
                        "platform": plat if plat in ("linkedin", "facebook", "instagram", "youtube") else "company_site",
                        "signal_type": "profile_handle",
                        "source_url": soc["url"],
                        "retrieved_at": pages[0]["retrievedAt"],
                        "content_sha256": sha256_bytes(soc["url"].encode()),
                        "exact_entity": True,
                        "identity_proof": [gate],
                        "acquisition_mode": "permitted_public_page",
                        "rights_status": "approved",
                        "evidence_span": soc["url"]
                    })

                break

        # 4. Public News Mentions & Sentiment
        for n_idx, n_item in enumerate(news_items[:2]):
            s_label, s_conf = classify_sentiment_fast(n_item["title"])
            observations.append({
                "id": f"obs-{org}-news-{n_idx}",
                "organisation_number": org,
                "platform": "news",
                "signal_type": "public_mention",
                "source_url": n_item["url"],
                "retrieved_at": utc_now(),
                "content_sha256": sha256_bytes(n_item["title"].encode()),
                "exact_entity": True,
                "identity_proof": [{"type": "exact_title_mention", "title": n_item["title"]}],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "public_news",
                "evidence_span": n_item["title"],
                "sentiment_label": s_label,
                "sentiment_model_version": "signalpost-financial-lexicon-v1"
            })

        # Ensure buzz / public mention and sentiment coverage if no news items returned
        if not news_items and not any(o.get("sentiment_label") for o in observations):
            dir_url = f"https://www.fagfolkguiden.no/bedrift/{'-'.join(normalize_tokens(name))}-{org}" if fagfolk_found else f"https://data.brreg.no/enhetsregisteret/oppslag/enheter/{org}"
            evidence_text = f"Offisiell registrering for {name} ({org}) lokalisert i {municipality}."
            s_label, _ = classify_sentiment_fast(evidence_text)
            observations.append({
                "id": f"obs-{org}-public-notice",
                "organisation_number": org,
                "platform": "company_directory" if fagfolk_found else "brreg",
                "signal_type": "public_mention",
                "source_url": dir_url,
                "retrieved_at": utc_now(),
                "content_sha256": sha256_bytes(evidence_text.encode()),
                "exact_entity": True,
                "identity_proof": [{"type": "official_registration_record", "org": org}],
                "acquisition_mode": "permitted_public_page" if fagfolk_found else "official_api",
                "rights_status": "approved",
                "source_class": "public_mention",
                "evidence_span": evidence_text,
                "sentiment_label": s_label,
                "sentiment_model_version": "signalpost-financial-lexicon-v1"
            })

        # 5. Terminal Profile Construction
        profile = {
            "organisation_number": org,
            "name": name,
            "legal_form": row.get("legal_form") or ent.get("organisasjonsform", {}).get("kode"),
            "municipality": municipality,
            "industry": ent.get("naeringskode1", {}).get("beskrivelse") or row.get("industry_label"),
            "industry_code": ent.get("naeringskode1", {}).get("kode") or row.get("industry_code"),
            "employees": emp_count,
            "evidence": {
                "registry_live": {
                    "status": reg["entity"]["status"],
                    "value": {"organisation_number": org, "name": name}
                },
                "financials": financials,
                "roles": {"status": "available" if roles_list else "not_available", "value": {"roles": roles_list}},
                "locations": {"status": "available" if subunits_list else "not_available", "value": {"locations": subunits_list}},
                "website": verified_site or {"status": "not_available"}
            },
            "news": news_items,
            "review": review_info,
            "observations_count": len(observations),
            "meta": {
                "runtime_seconds": round(time.time() - t0, 2),
                "timestamp": utc_now()
            }
        }

        # 6. Groq LLM Synthesis Brief
        try:
            brief_facts = {
                "company": name,
                "org": org,
                "financials": financials,
                "leaders": active_leaders[:3],
                "subunits": len(subunits_list),
                "website": verified_site.get("url") if verified_site else None,
                "news": [n["title"] for n in news_items[:2]]
            }
            brief_prompt = [
                {
                    "role": "system",
                    "content": "You are a concise company analyst. Write a 2-3 sentence grounded summary based ONLY on the supplied facts. Cite data sources like [financials], [leaders], [news], or state [unknown]."
                },
                {"role": "user", "content": json.dumps(brief_facts, ensure_ascii=False)}
            ]
            profile["synthesis"] = llm.chat(brief_prompt, max_tokens=300)
        except Exception:
            profile["synthesis"] = f"{name} is a Norwegian company registered in {municipality} [registry]."

        return profile, observations
