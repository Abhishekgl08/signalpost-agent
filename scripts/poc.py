"""POC: does the approach work end-to-end on a handful of companies?

Stages tested:  Brreg registry -> website discovery -> identity gate -> crawl -> extract -> LLM brief.
Usage: .venv/bin/python scripts/poc.py --n 30 --seed 1
"""
import argparse, asyncio, gzip, hashlib, json, random, re, time, unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

BRREG = "https://data.brreg.no"
UA = "SignalpostResearchBot/0.1 (hackathon research; contact: see repo)"
PAGES = ["", "om-oss", "kontakt", "kontakt-oss", "about", "contact", "karriere", "ledige-stillinger", "jobs", "nyheter"]
SUFFIX = re.compile(r"\b(as|asa|ans|da|ba|sa|enk|ks|nuf|stiftelse|holding)\b", re.I)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(b: bytes):
    return hashlib.sha256(b).hexdigest()


def slugs(name: str):
    n = name.lower()
    for a, b in (("æ", "ae"), ("ø", "o"), ("å", "a")):
        n = n.replace(a, b)
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = SUFFIX.sub(" ", n)
    toks = [t for t in re.split(r"[^a-z0-9]+", n) if t]
    if not toks:
        return []
    out = ["".join(toks), "-".join(toks)]
    if len(toks) > 2:
        out.append("".join(toks[:2]))
    return list(dict.fromkeys(out))


def load_sample(n, seed):
    rows = [json.loads(l) for l in gzip.open("data/universe.jsonl.gz", "rt")]
    rnd = random.Random(seed)
    with_site = [r for r in rows if r["website"]]
    no_site = [r for r in rows if not r["website"]]
    return rnd.sample(with_site, n // 3) + rnd.sample(no_site, n - n // 3)


async def get(client, url, **kw):
    try:
        r = await client.get(url, **kw)
        return r
    except Exception as e:
        return None


async def registry(client, org):
    out = {}
    for key, path in (("entity", f"/enhetsregisteret/api/enheter/{org}"),
                      ("roles", f"/enhetsregisteret/api/enheter/{org}/roller"),
                      ("subunits", f"/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100"),
                      ("accounts", f"/regnskapsregisteret/regnskap/{org}")):
        r = await get(client, BRREG + path, headers={"Accept": "application/json"})
        if r is not None and r.status_code == 200:
            out[key] = {"status": "available", "source": BRREG + path, "retrievedAt": now(),
                        "hash": sha(r.content), "data": r.json()}
        else:
            out[key] = {"status": "not_available" if (r is not None and r.status_code in (404, 204)) else "failed",
                        "source": BRREG + path, "http": getattr(r, "status_code", None)}
    return out


def text_of(html: str):
    t = HTMLParser(html)
    for n in t.css("script,style,noscript"):
        n.decompose()
    return re.sub(r"\s+", " ", t.text(separator=" ")).strip()


async def fetch_page(client, url):
    r = await get(client, url)
    if r is None and url.startswith("https://"):
        r = await get(client, "http://" + url[8:])
    if r is None or r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None
    html = r.text
    return {"url": str(r.url), "html": html, "text": text_of(html), "hash": sha(r.content), "retrievedAt": now()}


def org_variants(org):
    return [org, f"{org[:3]} {org[3:6]} {org[6:]}", f"{org[:3]}.{org[3:6]}.{org[6:]}"]


def _norm(s):
    s = (s or "").lower()
    for a, b in (("æ", "ae"), ("ø", "o"), ("å", "a")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def gate(org, name, entity, pages, host="", people=()):
    """Deterministic identity gate.
    exact  = org number on site, OR distinctive name (host/text) + a hard local corroborator
             (street address or a registered leader's full name, or postcode+city).
    probable/ambiguous are NEVER published."""
    blob = " ".join(p["text"] for p in pages)
    if any(v in blob for v in org_variants(org)):
        return {"status": "exact", "reason": "organisation number found on site"}
    nb = _norm(blob)
    toks = [t for t in _norm(SUFFIX.sub(" ", name)).split() if len(t) > 1]
    parts = (host or "").removeprefix("www.").split(".")
    stem = re.sub(r"[^a-z0-9]", "", _norm(parts[-2] if len(parts) >= 2 else parts[0]))
    name_c = "".join(toks)
    name_host = bool(name_c and stem and (name_c in stem or (len(stem) >= 5 and stem in name_c)))
    name_text = bool(toks and all(t in nb for t in toks))
    addr = entity.get("forretningsadresse") or entity.get("postadresse") or {}
    street = _norm((addr.get("adresse") or [""])[0])
    zipc, city = addr.get("postnummer"), _norm(addr.get("poststed"))
    sig = []
    if street and len(street) > 6 and street in nb:
        sig.append("street address")
    if zipc and city and zipc in blob and city in nb:
        sig.append("postcode+city")
    for p in people:
        w = _norm(p).split()
        if len(w) >= 2 and (f"{w[0]} {w[-1]}" in nb or f"{w[-1]} {w[0]}" in nb):
            sig.append(f"leader {p}")
            break
    name_ok = name_host or name_text
    hard = [s for s in sig if s == "street address" or s.startswith("leader")]
    if name_ok and (hard or (name_host and "postcode+city" in sig)):
        return {"status": "exact", "reason": f"name({'host' if name_host else 'text'}) + " + ", ".join(sig)}
    if name_ok and sig:
        return {"status": "probable", "reason": "name + " + ", ".join(sig)}
    return {"status": "ambiguous", "reason": f"insufficient corroboration (name_ok={name_ok}, signals={sig})"}



BLOCKED = ("proff.no", "purehelp.no", "1881.no", "gulesider.no", "firmalisten.no", "companywall.no",
           "firmadatabasen.no", "sokfirma.no", "yra.no", "northdata.com", "linkedin.com", "facebook.com",
           "instagram.com", "x.com", "twitter.com", "youtube.com", "tiktok.com", "wikipedia.org", "brreg.no",
           "regnskaperne.no", "bisnode.no", "nrk.no", "e24.no", "finn.no", "difi.no", "norge.no")
_SEARCH_LOCK = None


def _ddg(q):
    from ddgs import DDGS
    try:
        return list(DDGS().text(q, region="no-no", max_results=8))
    except Exception:
        return []


async def web_search(q):
    global _SEARCH_LOCK
    _SEARCH_LOCK = _SEARCH_LOCK or asyncio.Semaphore(2)
    async with _SEARCH_LOCK:
        res = await asyncio.to_thread(_ddg, q)
        await asyncio.sleep(0.7)
        return res


def search_candidates(results):
    seen, out = set(), []
    for r in results:
        host = (urlparse(r.get("href", "")).hostname or "").removeprefix("www.")
        if not host or any(host == b or host.endswith("." + b) for b in BLOCKED) or host in seen:
            continue
        seen.add(host)
        out.append("https://" + host + "/")
    return out[:3]


async def crawl_site(client, u):
    home = await fetch_page(client, u)
    if not home:
        return None
    base = home["url"]
    subs = await asyncio.gather(*[fetch_page(client, urljoin(base, p)) for p in PAGES[1:]])
    pages = [home] + [pg for pg in subs if pg and pg["hash"] != home["hash"]]
    return base, pages


async def discover(client, org, name, entity, reg_site, municipality="", people=()):
    cands = []
    if reg_site:
        cands.append(("registry", reg_site if reg_site.startswith("http") else "https://" + reg_site))
    for s in slugs(name)[:2]:
        cands.append(("guess", f"https://www.{s}.no/"))
    best, tried_hosts, queries = None, set(), [f'"{name}" {org}', f"{name} {municipality}"]
    while True:
        if not cands:
            if not queries:
                break
            q = queries.pop(0)
            try:
                res = await asyncio.wait_for(web_search(q), 15)
            except Exception:
                res = []
            cands = [("search", cu) for cu in search_candidates(res)]
            continue
        how, u = cands.pop(0)
        host = (urlparse(u).hostname or u).removeprefix("www.")
        if host in tried_hosts:
            continue
        tried_hosts.add(host)
        got = await crawl_site(client, u)
        if not got:
            continue
        base, pages = got
        g = gate(org, name, entity, pages, urlparse(base).hostname or "", people)
        hit = {"how": how, "site": base, "gate": g, "pages": pages}
        if g["status"] == "exact":
            return hit
        rank = {"probable": 2, "ambiguous": 1}
        if best is None or rank.get(g["status"], 0) > rank.get(best["gate"]["status"], 0):
            best = hit
    return best




EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?:\+47\s?)?(?:\d{2}\s?){4}")


def extract(site):
    pages = site["pages"]
    blob = " ".join(p["text"] for p in pages)
    home = pages[0]["html"]
    t = HTMLParser(home)
    title = t.css_first("title").text().strip() if t.css_first("title") else None
    md = t.css_first('meta[name="description"]')
    socials = sorted({a.attributes.get("href") for a in t.css("a[href]")
                      if re.search(r"(linkedin|facebook|instagram|youtube)\.com", a.attributes.get("href") or "")})
    careers = [p["url"] for p in pages if re.search(r"karriere|stilling|jobs|career", p["url"])]
    return {"title": title, "description": md.attributes.get("content") if md else None,
            "emails": sorted(set(EMAIL.findall(blob)))[:5], "social_links": socials[:6],
            "careers_pages": careers, "pages_crawled": [p["url"] for p in pages]}


def fin(acc):
    if acc.get("status") != "available":
        return {"status": acc.get("status")}
    d = acc["data"]
    if isinstance(d, list) and d:
        d = max(d, key=lambda x: ((x.get("regnskapsperiode") or {}).get("tilDato") or ""))
    if not isinstance(d, dict):
        return {"status": "not_available"}
    g = lambda *ks: _dig(d, ks)
    return {"status": "available", "period": d.get("regnskapsperiode"),
            "revenue": g("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "annual_result": g("resultatregnskapResultat", "aarsresultat"),
            "assets": g("eiendeler", "sumEiendeler"), "equity": g("egenkapitalGjeld", "egenkapital", "sumEgenkapital")}


def _dig(d, ks):
    for k in ks:
        if not isinstance(d, dict) or k not in d:
            return None  # missing stays None, never 0
        d = d[k]
    return d


def roles(r):
    if r.get("status") != "available":
        return []
    out = []
    for g in r["data"].get("rollegrupper", []):
        for it in g.get("roller", []):
            p = it.get("person", {}).get("navn", {}) or it.get("enhet", {}).get("navn")
            if isinstance(p, dict):
                p = " ".join(filter(None, [p.get("fornavn"), p.get("mellomnavn"), p.get("etternavn")]))
            if isinstance(p, list):
                p = " ".join(p)
            out.append({"role": it["type"]["beskrivelse"], "name": p, "resigned": it.get("fratraadt")})
    return out


async def research(sem, client, row):
    async with sem:
        org, name = row["organisation_number"], row["name"]
        t0 = time.time()
        env = {"org": org, "name": name, "universe_website": row["website"] or None}
        reg = await registry(client, org)
        ent = reg["entity"].get("data", {}) if reg["entity"]["status"] == "available" else {}
        env["registry_status"] = reg["entity"]["status"]
        env["financials"] = fin(reg["accounts"])
        env["roles"] = roles(reg["roles"])
        env["n_subunits"] = len(reg["subunits"].get("data", {}).get("_embedded", {}).get("underenheter", [])) if reg["subunits"]["status"] == "available" else None
        people = [r["name"] for r in env["roles"] if r.get("name") and not r.get("resigned")]
        site = await discover(client, org, name, ent, ent.get("hjemmeside") or row["website"], row.get("municipality", ""), people)
        if site:
            exact = site["gate"]["status"] == "exact"
            env["website"] = {"state": "available" if exact else "ambiguous",
                              "url": site["site"], "found_by": site["how"], "identity": site["gate"], **extract(site)}
        else:
            env["website"] = {"state": "not_available"}
        env["seconds"] = round(time.time() - t0, 1)
        return env


async def main(a):
    rows = load_sample(a.n, a.seed)
    sem = asyncio.Semaphore(a.conc)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=10, follow_redirects=True) as c:
        res = await asyncio.gather(*[research(sem, c, r) for r in rows])
    json.dump(res, open(a.out, "w"), ensure_ascii=False, indent=1)
    ok = lambda e: e["website"]["state"] == "available"
    print(f"companies={len(res)} registry_ok={sum(e['registry_status']=='available' for e in res)} "
          f"financials_ok={sum(e['financials']['status']=='available' for e in res)} "
          f"roles_ok={sum(bool(e['roles']) for e in res)} website_found={sum(ok(e) for e in res)} "
          f"exact={sum(ok(e) and e['website']['identity']['status']=='exact' for e in res)} "
          f"avg_s={sum(e['seconds'] for e in res)/len(res):.1f}")
    for e in res:
        w = e["website"]
        print(f"{e['org']} {e['name'][:32]:32} site={w.get('url','-')[:38]:38} {w.get('found_by','-'):8} "
              f"{w.get('identity',{}).get('status','-'):9} {w.get('identity',{}).get('reason','')[:70]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--out", default="out/poc.json")
    asyncio.run(main(ap.parse_args()))
