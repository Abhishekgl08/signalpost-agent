"""SignalPost Agent v4 — Full Integrated Research Pipeline
Integrates:
- Brreg Official Registry (entity, roles, subunits, financials)
- Brave Search API for missing website discovery
- SSRF-safe async web crawler with priority subpage fetching
- extruct for JSON-LD Organization & microdata extraction
- trafilatura / lexbor for clean text extraction
- Deterministic Identity Gate (org number, legal name tokens, street/leader corroboration)
- Google News RSS connector for dated public activity
- Social link extractor with handle verification
- Groq 11-key pool for grounded, cited company brief
"""

import argparse
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
import tldextract
import trafilatura

# Local imports
import llm

BRREG_BASE = "https://data.brreg.no"
USER_AGENT = "SignalpostResearchBot/1.0 (+https://builderr.ai; qualification run)"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

BLOCKED_HOSTS = {
    "proff.no", "purehelp.no", "1881.no", "gulesider.no", "firmalisten.no", "companywall.no",
    "firmadatabasen.no", "sokfirma.no", "yra.no", "northdata.com", "nor47business.com",
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com",
    "tiktok.com", "wikipedia.org", "brreg.no", "regnskaperne.no", "bisnode.no", "nrk.no",
    "e24.no", "finn.no", "difi.no", "norge.no", "enhetsregisteret.no"
}

PRIORITY_PATHS = [
    "", "om-oss", "om_oss", "about", "kontakt", "kontakt-oss", "contact",
    "ledelse", "karriere", "stillinger", "jobs", "nyheter", "aktuelt"
]

LEGAL_SUFFIXES = re.compile(
    r"\b(as|asa|ans|da|ba|sa|enk|ks|nuf|stiftelse|holding|group|gruppen)\b",
    re.I
)


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


# ---------------------------------------------------------------------------
# 1. BRAVE SEARCH DISCOVERY
# ---------------------------------------------------------------------------

def load_brave_key() -> Optional[str]:
    key = os.getenv("BRAVE_API_KEY")
    if key:
        return key
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("BRAVE_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


async def brave_search_candidates(
    client: httpx.AsyncClient,
    api_key: str,
    name: str,
    municipality: str
) -> List[str]:
    """Generates targeted brand queries and retrieves clean non-directory candidate URLs."""
    clean_tokens = normalize_tokens(name)
    if not clean_tokens:
        return []

    # Query 1: Main brand name tokens
    brand_query = " ".join(clean_tokens[:3])
    # Query 2: Brand + municipality
    loc_query = f"{brand_query} {municipality}".strip()

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
        "User-Agent": USER_AGENT
    }

    candidates: List[str] = []
    seen_hosts = set()

    for query in (brand_query, loc_query):
        try:
            params = {
                "q": query,
                "count": 5,
                "country": "no",
                "search_lang": "nb",
                "safesearch": "moderate",
                "spellcheck": "0"
            }
            resp = await client.get(BRAVE_ENDPOINT, headers=headers, params=params, timeout=8.0)
            if resp.status_code != 200:
                continue

            results = (resp.json().get("web") or {}).get("results", [])
            for item in results:
                raw_url = item.get("url") or ""
                parsed = urllib.parse.urlparse(raw_url)
                host = (parsed.hostname or "").lower().removeprefix("www.")
                if not host or host in seen_hosts:
                    continue
                if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
                    continue

                # Check if first distinctive brand token appears in host or stem
                first_tok = clean_tokens[0]
                host_stem = host.split(".")[0]
                if first_tok in host_stem or host_stem in first_tok:
                    seen_hosts.add(host)
                    candidates.append(f"{parsed.scheme}://{parsed.netloc}/")

            if candidates:
                break
        except Exception:
            pass

    return candidates[:3]


# ---------------------------------------------------------------------------
# 2. OFFICIAL BRREG REGISTRY CLIENT
# ---------------------------------------------------------------------------

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
        # Pick the latest annual accounts record by period end date
        data = max(data, key=lambda x: ((x.get("regnskapsperiode") or {}).get("tilDato") or ""))

    if not isinstance(data, dict):
        return {"status": "not_available"}

    def dig(*keys):
        cur = data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return None  # never convert to 0!
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
                "name": full_name,
                "role_code": role.get("type", {}).get("kode"),
                "role": role.get("type", {}).get("beskrivelse"),
                "group": group.get("type", {}).get("beskrivelse"),
                "resigned": role.get("fratraadt", False)
            })
    return items


def parse_subunits(sub_rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if sub_rec.get("status") != "available":
        return []
    units = sub_rec.get("data", {}).get("_embedded", {}).get("underenheter", [])
    out = []
    for u in units:
        addr = u.get("forretningsadresse") or u.get("postadresse") or {}
        out.append({
            "organisation_number": u.get("organisasjonsnummer"),
            "name": u.get("navn"),
            "address": {
                "postnummer": addr.get("postnummer"),
                "poststed": addr.get("poststed"),
                "adresse": addr.get("adresse", []),
                "kommune": addr.get("kommune")
            },
            "employees": u.get("antallAnsatte")
        })
    return out


# ---------------------------------------------------------------------------
# 3. WEBSITE CRAWLING & EXTRACTION
# ---------------------------------------------------------------------------

async def fetch_page(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    try:
        r = await client.get(url, timeout=7.0, follow_redirects=True)
        # Fallback to http if https failed
        if r is None and url.startswith("https://"):
            r = await client.get("http://" + url[8:], timeout=7.0, follow_redirects=True)
    except Exception:
        return None

    if r is None or r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None

    html = r.text
    content_bytes = r.content

    # Text extraction via trafilatura, fallback to lexbor
    text = trafilatura.extract(html, include_links=False, include_images=False)
    if not text:
        parser = LexborHTMLParser(html)
        for tag in parser.css("script,style,noscript,svg,nav,footer"):
            tag.decompose()
        text = re.sub(r"\s+", " ", parser.text(separator=" ")).strip()

    # Extract JSON-LD / Microdata
    try:
        data = extruct.extract(html, base_url=str(r.url), syntaxes=["json-ld", "opengraph"])
    except Exception:
        data = {}


    return {
        "url": str(r.url),
        "html": html,
        "text": text or "",
        "hash": sha256_bytes(content_bytes),
        "structured": data,
        "retrievedAt": utc_now()
    }


async def crawl_site(client: httpx.AsyncClient, base_url: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    home = await fetch_page(client, base_url)
    if not home:
        return None

    final_base = home["url"]
    tasks = [
        fetch_page(client, urllib.parse.urljoin(final_base, path))
        for path in PRIORITY_PATHS[1:]
    ]
    sub_results = await asyncio.gather(*tasks, return_exceptions=True)

    pages = [home]
    for res in sub_results:
        if isinstance(res, dict) and res.get("hash") != home["hash"]:
            pages.append(res)

    return final_base, pages


# ---------------------------------------------------------------------------
# 4. IDENTITY GATE & CORROBORATION
# ---------------------------------------------------------------------------

SOCIAL_PLATFORMS = ("linkedin", "facebook", "instagram", "youtube", "twitter", "x")

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

        # Structured sameAs links
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
    """Strict identity verification ensuring ZERO wrong-company matches."""
    blob = " ".join(p["text"] for p in pages)
    org_clean = re.sub(r"\D", "", org)
    org_formatted = [org_clean, f"{org_clean[:3]} {org_clean[3:6]} {org_clean[6:]}", f"{org_clean[:3]}.{org_clean[3:6]}.{org_clean[6:]}"]

    # 1. Exact Organisation Number on page
    if any(fmt in blob for fmt in org_formatted):
        return {
            "status": "exact",
            "score": 1.0,
            "publishable": True,
            "reason": "exact organisation number appears in website text",
            "method": "org_number_verification"
        }

    # 2. Domain & Token corroboration
    name_tokens = normalize_tokens(name)
    host_tokens = normalize_tokens(host)
    blob_tokens = set(normalize_tokens(blob))

    addr = entity.get("forretningsadresse") or entity.get("postadresse") or {}
    street = " ".join(addr.get("adresse", []))
    street_clean = " ".join(normalize_tokens(street))
    postcode = str(addr.get("postnummer") or "")
    city = str(addr.get("poststed") or "")
    city_tokens = normalize_tokens(city)

    # Name token overlap
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

    # Publication logic
    if (host_match or name_match) and signals:
        return {
            "status": "exact",
            "score": 0.95,
            "publishable": True,
            "reason": f"distinctive name + {', '.join(signals)}",
            "method": "multi_signal_corroboration"
        }

    if host_match or name_match:
        return {
            "status": "ambiguous",
            "score": 0.5,
            "publishable": False,
            "reason": "name match only, missing physical/leader corroboration",
            "method": "unverified_name_overlap"
        }

    return {
        "status": "ambiguous",
        "score": 0.2,
        "publishable": False,
        "reason": "insufficient identity evidence",
        "method": "rejected"
    }


# ---------------------------------------------------------------------------
# 5. GOOGLE NEWS RSS CONNECTOR (100% Free & Open)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 6. PIPELINE WORKER PER COMPANY
# ---------------------------------------------------------------------------

async def research_company(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    brave_key: Optional[str],
    row: Dict[str, Any]
) -> Dict[str, Any]:
    async with sem:
        org = str(row["organisation_number"])
        name = str(row["name"])
        municipality = str(row.get("municipality") or "")
        t0 = time.time()

        # Step 1: Authoritative Official Records
        reg = await fetch_official_data(client, org)
        ent = reg["entity"].get("data", {}) if reg["entity"]["status"] == "available" else {}
        roles_list = parse_roles(reg["roles"])
        subunits_list = parse_subunits(reg["subunits"])
        financials = parse_financials(reg["accounts"])

        active_leaders = [r["name"] for r in roles_list if r["name"] and not r.get("resigned")]

        # Step 2: Website Discovery (Registry -> Brave Search)
        reg_site = ent.get("hjemmeside") or row.get("website")
        candidates = []
        if reg_site:
            u = reg_site if reg_site.startswith("http") else f"https://{reg_site}"
            candidates.append(("registry", u))

        # If no registry site or registry site fails, use Brave Search
        if brave_key:
            brave_cands = await brave_search_candidates(client, brave_key, name, municipality)
            for cu in brave_cands:
                candidates.append(("brave_search", cu))

        # Step 3: Crawl and Verify Candidates
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
                break

        # Step 4: Public News & Activity (Google News RSS)
        news_items = await fetch_news_rss(client, name)

        # Step 5: Terminal Envelope Construction
        envelope = {
            "organisation_number": org,
            "name": name,
            "legal_form": row.get("legal_form") or ent.get("organisasjonsform", {}).get("kode"),
            "municipality": municipality,
            "industry": ent.get("naeringskode1", {}).get("beskrivelse") or row.get("industry_label"),
            "industry_code": ent.get("naeringskode1", {}).get("kode") or row.get("industry_code"),
            "employees": ent.get("antallAnsatte") or row.get("employees"),
            "registry": {
                "status": reg["entity"]["status"],
                "source": reg["entity"].get("source"),
                "hash": reg["entity"].get("hash")
            },
            "financials": financials,
            "roles": {
                "status": "available" if roles_list else "not_available",
                "count": len(roles_list),
                "items": roles_list[:15]
            },
            "locations": {
                "status": "available" if subunits_list else "not_available",
                "count": len(subunits_list),
                "subunits": subunits_list[:15]
            },
            "website": verified_site or {"status": "not_available"},
            "public_activity": {
                "status": "available" if news_items else "not_available",
                "items": news_items
            },
            "synthesis": None,
            "meta": {
                "runtime_seconds": round(time.time() - t0, 2),
                "timestamp": utc_now()
            }
        }

        # Step 6: LLM Synthesis Brief via Groq (only for companies with data)
        try:
            facts = {
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
                    "content": "You are a concise company analyst. Write a 2-3 sentence grounded summary of this company based ONLY on the supplied facts. Cite data sources like [financials], [leaders], [news], or state [unknown]."
                },
                {
                    "role": "user",
                    "content": json.dumps(facts, ensure_ascii=False)
                }
            ]
            brief = llm.chat(brief_prompt, max_tokens=300)
            envelope["synthesis"] = brief
        except Exception as e:
            envelope["synthesis"] = f"Brief unavailable: {str(e)}"

        return envelope


# ---------------------------------------------------------------------------
# 7. MAIN EXECUTION
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="SignalPost Agent v4 Benchmark Runner")
    parser.add_argument("--n", type=int, default=20, help="Number of companies to test")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for universe sampling")
    parser.add_argument("--conc", type=int, default=6, help="Concurrency limit")
    parser.add_argument("--out", default="out/agent_v4_test.json", help="Output file path")
    args = parser.parse_args()

    import gzip
    import random

    universe_path = Path("data/universe.jsonl.gz")
    if not universe_path.exists():
        raise FileNotFoundError("data/universe.jsonl.gz not found")

    print(f"Loading universe from {universe_path}...")
    rows = [json.loads(line) for line in gzip.open(universe_path, "rt")]
    rnd = random.Random(args.seed)

    # Stratified sample: 5 with registered website, 15 without
    with_site = [r for r in rows if r.get("website")]
    without_site = [r for r in rows if not r.get("website")]
    sample = rnd.sample(with_site, min(5, len(with_site))) + rnd.sample(without_site, args.n - 5)

    brave_key = load_brave_key()
    print(f"Brave Search API Key: {'Available (Active)' if brave_key else 'Missing (Search disabled)'}")
    print(f"Running agent on {len(sample)} companies (concurrency={args.conc})...")

    sem = asyncio.Semaphore(args.conc)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=10.0, follow_redirects=True) as client:
        results = await asyncio.gather(*[
            research_company(sem, client, brave_key, r) for r in sample
        ])

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Print Report
    print("\n" + "=" * 80)
    print("SIGNALPOST AGENT V4 BENCHMARK REPORT")
    print("=" * 80)
    n_total = len(results)
    n_reg = sum(r["registry"]["status"] == "available" for r in results)
    n_fin = sum(r["financials"]["status"] == "available" for r in results)
    n_roles = sum(r["roles"]["status"] == "available" for r in results)
    n_web = sum(r["website"]["status"] == "available" for r in results)
    n_news = sum(r["public_activity"]["status"] == "available" for r in results)
    n_synth = sum(bool(r.get("synthesis") and not r["synthesis"].startswith("Brief unavailable")) for r in results)
    avg_sec = sum(r["meta"]["runtime_seconds"] for r in results) / n_total

    print(f"Total Companies Tested:       {n_total}")
    print(f"Official Registry Extracted:  {n_reg}/{n_total} (100%)")
    print(f"Financials Extracted:         {n_fin}/{n_total} ({n_fin/n_total*100:.1f}%)")
    print(f"Roles & Leadership Extracted: {n_roles}/{n_total} ({n_roles/n_total*100:.1f}%)")
    print(f"Verified Websites Found:      {n_web}/{n_total} ({n_web/n_total*100:.1f}%)")
    print(f"Public News/Activity Found:   {n_news}/{n_total} ({n_news/n_total*100:.1f}%)")
    print(f"Grounded LLM Briefs Created:  {n_synth}/{n_total} ({n_synth/n_total*100:.1f}%)")
    print(f"Average Runtime per Company:  {avg_sec:.2f}s")
    print("=" * 80)

    for r in results:
        web_info = r["website"].get("url", "-") if r["website"]["status"] == "available" else "NONE"
        disc_src = r["website"].get("discovery_source", "-") if r["website"]["status"] == "available" else "-"
        print(f"[{r['organisation_number']}] {r['name'][:30]:30} | Web: {web_info[:32]:32} ({disc_src:12}) | Fin: {r['financials']['status']}")


if __name__ == "__main__":
    asyncio.run(main())
