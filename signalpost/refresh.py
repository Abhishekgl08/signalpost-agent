"""SignalPost Refresh & Snapshot Diffing Module.
Calculates high-precision deterministic diffs between company snapshots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


def diff_datasets(
    previous: List[Dict[str, Any]],
    current: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    prev_map = {str(p["organisation_number"]): p for p in previous}
    curr_map = {str(p["organisation_number"]): p for p in current}
    all_orgs = sorted(set(prev_map) | set(curr_map))

    events: List[Dict[str, Any]] = []

    for org in all_orgs:
        p_old = prev_map.get(org)
        p_new = curr_map.get(org)

        if not p_old or not p_new:
            continue

        # 1. Registered Name & Legal Form
        if p_old.get("name") != p_new.get("name"):
            events.append({
                "organisation_number": org,
                "field": "name",
                "old_value": p_old.get("name"),
                "new_value": p_new.get("name"),
                "event_type": "changed_name"
            })
        if p_old.get("legal_form") != p_new.get("legal_form"):
            events.append({
                "organisation_number": org,
                "field": "legal_form",
                "old_value": p_old.get("legal_form"),
                "new_value": p_new.get("legal_form"),
                "event_type": "changed_legal_form"
            })

        # 2. Workforce Changes
        emp_old = p_old.get("employees")
        emp_new = p_new.get("employees")
        if emp_old != emp_new and emp_old is not None and emp_new is not None:
            events.append({
                "organisation_number": org,
                "field": "employees",
                "old_value": emp_old,
                "new_value": emp_new,
                "delta": emp_new - emp_old,
                "event_type": "changed_workforce"
            })

        # 3. Roles / Leadership Changes
        ev_old = p_old.get("evidence", {})
        ev_new = p_new.get("evidence", {})

        roles_old = {r.get("name") for r in (ev_old.get("roles", {}).get("value", {}) or {}).get("roles", []) if r.get("name")}
        roles_new = {r.get("name") for r in (ev_new.get("roles", {}).get("value", {}) or {}).get("roles", []) if r.get("name")}

        for added in (roles_new - roles_old):
            events.append({
                "organisation_number": org,
                "field": "roles",
                "person": added,
                "event_type": "new_role"
            })
        for removed in (roles_old - roles_new):
            events.append({
                "organisation_number": org,
                "field": "roles",
                "person": removed,
                "event_type": "resigned_role"
            })

        # 4. Financial Filings
        fin_old = ev_old.get("financials", {})
        fin_new = ev_new.get("financials", {})
        period_old = str(fin_old.get("period") or "")
        period_new = str(fin_new.get("period") or "")
        if period_old != period_new and period_new:
            events.append({
                "organisation_number": org,
                "field": "financials",
                "old_period": period_old,
                "new_period": period_new,
                "event_type": "new_annual_filing"
            })

        # 5. Website Changes
        web_old = (ev_old.get("website") or {}).get("url")
        web_new = (ev_new.get("website") or {}).get("url")
        if web_old != web_new:
            events.append({
                "organisation_number": org,
                "field": "website",
                "old_url": web_old,
                "new_url": web_new,
                "event_type": "changed_website"
            })

    return events
