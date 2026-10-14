"""SignalPost Research & QA Agent Module.
Grounded citation-backed Q&A answering with strict abstention on unverified claims.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def answer_company_question(
    profile: Dict[str, Any],
    question: str
) -> Dict[str, Any]:
    q = question.casefold()
    facts: List[Dict[str, Any]] = []
    unsupported: List[str] = []

    name = profile.get("name", "The company")
    org = profile.get("organisation_number", "")
    ev = profile.get("evidence", {})

    # 1. Financials
    fin_terms = ("financial", "revenue", "income", "profit", "result", "debt", "equity", "regnskap", "omsetning")
    if any(t in q for t in fin_terms) or "all" in q:
        fin = ev.get("financials", {})
        if fin.get("status") == "available":
            period = fin.get("period") or {}
            curr = fin.get("currency", "NOK")
            rev = fin.get("revenue")
            res = fin.get("annual_result")
            op_res = fin.get("operating_result")
            eq = fin.get("equity")
            debt = fin.get("debt")

            if rev is not None:
                facts.append({
                    "claim": f"Total revenue: {rev:,} {curr}",
                    "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}",
                    "source_class": "official_annual_accounts"
                })
            if op_res is not None:
                facts.append({
                    "claim": f"Operating result: {op_res:,} {curr}",
                    "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}",
                    "source_class": "official_annual_accounts"
                })
            if res is not None:
                facts.append({
                    "claim": f"Annual net profit: {res:,} {curr}",
                    "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}",
                    "source_class": "official_annual_accounts"
                })
        else:
            unsupported.append("Official financial statements are not available for this entity.")

    # 2. Leadership & Board
    role_terms = ("lead", "director", "ceo", "board", "chair", "daglig leder", "styreleder", "roller")
    if any(t in q for t in role_terms) or "all" in q:
        roles_val = ev.get("roles", {}).get("value", {}) or {}
        roles_list = roles_val.get("roles", [])
        active_leaders = [r for r in roles_list if not r.get("resigned") and r.get("name")]
        if active_leaders:
            for r in active_leaders[:4]:
                facts.append({
                    "claim": f"{r.get('type', 'Role')}: {r.get('name')}",
                    "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller",
                    "source_class": "official_registry_roles"
                })
        else:
            unsupported.append("No active registered leadership roles found in the official registry.")

    # 3. Workforce & Employees
    workforce_terms = ("employee", "workforce", "headcount", "ansatte", "folk")
    if any(t in q for t in workforce_terms) or "all" in q:
        emp = profile.get("employees")
        if emp is not None:
            facts.append({
                "claim": f"Officially registered workforce: {emp} employees",
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                "source_class": "official_registry_workforce"
            })
        else:
            unsupported.append("Registered employee count is not available in Enhetsregisteret.")

    # 4. Location & Subunits
    loc_terms = ("where", "location", "address", "city", "municipality", "kommune", "adresse")
    if any(t in q for t in loc_terms) or "all" in q:
        muni = profile.get("municipality")
        if muni:
            facts.append({
                "claim": f"Registered municipality: {muni}",
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                "source_class": "official_registry_location"
            })
        subs = (ev.get("locations", {}).get("value", {}) or {}).get("locations", [])
        if subs:
            facts.append({
                "claim": f"Operating subunits / branch locations: {len(subs)}",
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}",
                "source_class": "official_registry_subunits"
            })

    # 5. Website & Digital Footprint
    web_terms = ("website", "domain", "nettside", "online", "url")
    if any(t in q for t in web_terms) or "all" in q:
        web = ev.get("website", {})
        if web.get("status") == "available" and web.get("url"):
            facts.append({
                "claim": f"Verified company website: {web.get('url')}",
                "source_url": web.get("url"),
                "source_class": "verified_company_website"
            })
        else:
            unsupported.append("No verified company website meets the identity gate requirements.")

    # Compose response
    if facts:
        answer_lines = [f"{name} ({org}):"]
        for f in facts:
            answer_lines.append(f"- {f['claim']} [{f['source_class']}]")
        answer = "\n".join(answer_lines)
        status = "answered"
    else:
        answer = f"I cannot verify facts to answer '{question}' for {name} ({org}). Grounded sources abstain."
        status = "abstained"

    return {
        "status": status,
        "question": question,
        "answer": answer,
        "facts_count": len(facts),
        "facts": facts,
        "unsupported_topics": unsupported
    }
