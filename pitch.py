#!/usr/bin/env python3
"""Competitor Layoff Pitch Builder.

Watches a client's competitors for headcount drops, lists the people who just left
them, matches those people to the client's open roles and drafts the pitch email.
Data layer: Crustdata POST /watch/company and POST /person/search (https://docs.crustdata.com).

  watch    -> create a company watch that fires when a competitor's headcount drops
  search   -> one /person/search call per competitor: past employer = competitor, left recently
  qualify  -> deterministic rules that re-check every result (unit tested)
  match    -> score each leaver against each open role (title, function, seniority, country)
  draft    -> pitch email: Claude when configured, a plain template otherwise
  summary  -> aggregate numbers (safe to show in a README)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from crustdata_api import Crustdata, day, load_env, normalize_domain
from notify import ask_claude, claude_available, send_slack

FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.headline",
    "basic_profile.current_title",
    "basic_profile.location",
    "social_handles.professional_network_identifier.profile_url",
    "experience.employment_details",
]

STOPWORDS = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "head", "of", "the", "and",
             "a", "an", "for", "to", "in", "at", "manager", "director", "vp", "chief", "ii", "iii"}


# --- 1. watch: a headcount drop at a competitor ---------------------------------------------

def watch_body(domains: List[str], webhook_url: str = "", every_hours: int = 24) -> Dict[str, Any]:
    """Body for POST /watch/company: fire when `headcount.total` goes down.

    Company watches only accept refresh_frequency_days = 30 (the standard tier).
    Without a webhook the watch is pull-only: read its runs from the Crustdata API.
    """
    return {
        "entities": {"domains": sorted({normalize_domain(d) for d in domains if d})},
        "track": {"field": "headcount.total", "type": "decreased"},
        "fields": ["basic_info", "headcount"],
        "config": {"trigger": {"type": "interval", "every_hours": every_hours},
                   "refresh_frequency_days": 30},
        "notifications": [{"type": "webhook", "url": webhook_url}] if webhook_url else [],
    }


# --- 2. search: who just left a competitor --------------------------------------------------

def build_filters(domain: str, since: str) -> Dict[str, Any]:
    # Fields checked against the Person Search reference. `end_date` has no `past.` variant,
    # so the date and the employer filters can match different roles: qualify() re-checks.
    return {"op": "and", "conditions": [
        {"field": "experience.employment_details.past.company_website_domain", "type": "=", "value": domain},
        {"field": "experience.employment_details.end_date", "type": "=>", "value": since},
        {"field": "recently_changed_jobs", "type": "=", "value": True},
    ]}


def qualify(item: Dict[str, Any], domain: str, since: str, today: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return a flat record or None. Rules (each is unit tested):
    1. the person has no current role at the competitor,
    2. one single past role at the competitor,
    3. that role ended on or after `since` (and not in the future).
    """
    today = today or dt.date.today().isoformat()
    domain = normalize_domain(domain)
    details = (item.get("experience") or {}).get("employment_details") or {}
    current, past = details.get("current") or [], details.get("past") or []
    if any(normalize_domain(c.get("company_website")) == domain for c in current):
        return None
    roles = [r for r in past if normalize_domain(r.get("company_website")) == domain]
    roles = [r for r in roles if since <= day(r.get("end_date")) <= today]
    if not roles:
        return None
    left = max(roles, key=lambda r: day(r.get("end_date")))
    basic = item.get("basic_profile") or {}
    new = next(iter(current), None)
    profile = ((item.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url", "")
    return {
        "id": item.get("crustdata_person_id"),
        "name": basic.get("name", ""),
        "profile_url": profile,
        "headline": basic.get("headline", ""),
        "country": (basic.get("location") or {}).get("country", ""),
        "left_domain": domain,
        "left_company": left.get("name", ""),
        "left_title": left.get("title", ""),
        "left_function": left.get("function_category", ""),
        "left_seniority": left.get("seniority_level", ""),
        "left_date": day(left.get("end_date")),
        "new_title": (new or {}).get("title", ""),
        "new_company": (new or {}).get("name", ""),
        "available": new is None,
    }


# --- 3. match: leavers to the client's open roles -------------------------------------------

def tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z][a-z+#.]*", (text or "").lower()) if t not in STOPWORDS and len(t) > 1}


def score(person: Dict[str, Any], role: Dict[str, Any]) -> int:
    """Title/headline keyword hits (2 each, max 4) + function (2) + seniority (1) + country (1)."""
    keywords = {k.lower() for k in role.get("keywords") or []} | tokens(role.get("title", ""))
    haystack = tokens(person.get("left_title", "")) | tokens(person.get("headline", ""))
    s = min(4, 2 * len(keywords & haystack))
    if role.get("function") and role["function"].lower() == (person.get("left_function") or "").lower():
        s += 2
    if role.get("seniority") and role["seniority"].lower() == (person.get("left_seniority") or "").lower():
        s += 1
    if role.get("country") and role["country"].lower() == (person.get("country") or "").lower():
        s += 1
    return s


def match(people: List[Dict[str, Any]], roles: List[Dict[str, Any]], min_score: int = 3) -> List[Dict[str, Any]]:
    """Attach the best-scoring open role (>= min_score) to each person; sort best first."""
    out = []
    for p in people:
        scored = sorted(((score(p, r), r) for r in roles), key=lambda x: -x[0])
        if scored and scored[0][0] >= min_score:
            out.append(dict(p, role=scored[0][1].get("title", ""), score=scored[0][0]))
    return sorted(out, key=lambda r: (-r["score"], r["left_date"]))


# --- 4. draft: the pitch --------------------------------------------------------------------

def pitch_prompt(client: Dict[str, Any], shortlist: List[Dict[str, Any]]) -> str:
    lines = [f"- {c['left_title'] or 'Professional'} (left {c['left_company']} {c['left_date']}), "
             f"{c['country'] or 'location n/a'}, fits open role: {c['role']}" for c in shortlist]
    return (f"Write a short, plain pitch email from a recruiting agency to {client['name']}. "
            "No hype, no names. Say that people from a competitor's teams are on the market now, "
            "list these anonymised profiles, and propose a call this week.\n" + "\n".join(lines))


def template_pitch(client: Dict[str, Any], shortlist: List[Dict[str, Any]]) -> str:
    bullets = "\n".join(
        f"- {c['left_title'] or 'Professional'} ({c['country'] or 'n/a'}), left {c['left_company']} "
        f"in {c['left_date'][:7]}: fits your {c['role']} role" for c in shortlist)
    return (f"Subject: {len(shortlist)} candidates for your open roles\n\nHi {client.get('contact', 'there')},\n\n"
            "People from a competitor's teams came onto the market in the last few weeks. "
            f"{len(shortlist)} of them match roles you have open:\n\n{bullets}\n\n"
            "I can share full profiles and availability today. Do you have 15 minutes this week?")


def draft_pitch(client: Dict[str, Any], shortlist: List[Dict[str, Any]], use_claude: bool = False) -> str:
    if use_claude and shortlist:
        text = ask_claude(pitch_prompt(client, shortlist))
        if text:
            return text
    return template_pitch(client, shortlist)


# --- run, summary, Slack, state -------------------------------------------------------------

def leavers(crust: Crustdata, domain: str, since: str, max_results: int) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for item in crust.person_search(build_filters(domain, since), FIELDS, max_results=max_results):
        rec = qualify(item, domain, since)
        if rec and rec["id"] not in seen:
            seen.add(rec["id"])
            out.append(rec)
    return out


def run(crust: Crustdata, clients: List[Dict[str, Any]], since: str, max_results: int,
        only_domains: Optional[List[str]] = None, include_placed: bool = False,
        min_score: int = 3) -> List[Dict[str, Any]]:
    """One entry per client: its shortlist drawn from all of its competitors."""
    only = {normalize_domain(d) for d in only_domains or []}
    results = []
    for client in clients:
        domains = [normalize_domain(d) for d in client.get("competitors", [])]
        domains = [d for d in domains if d and (not only or d in only)]
        people: List[Dict[str, Any]] = []
        for d in domains:
            people += leavers(crust, d, since, max_results)
        scanned = len(people)
        if not include_placed:
            people = [p for p in people if p["available"]]
        shortlist = match(people, client.get("open_roles", []), min_score)
        results.append({"client": client["name"], "competitors": domains, "leavers": scanned,
                        "shortlist": shortlist, "contact": client.get("contact", "")})
    return results


def summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    short = [c for r in results for c in r["shortlist"]]
    return {
        "clients": len(results),
        "leavers_found": sum(r["leavers"] for r in results),
        "shortlisted": len(short),
        "by_open_role": dict(Counter(c["role"] for c in short)),
        "by_function": dict(Counter(c["left_function"] or "unknown" for c in short)),
    }


def slack_text(results: List[Dict[str, Any]], limit: int = 5) -> str:
    lines = [f":bell: {sum(len(r['shortlist']) for r in results)} candidates ready to pitch"]
    for r in results:
        if not r["shortlist"]:
            continue
        lines.append(f"*{r['client']}*: {len(r['shortlist'])} matches from {len(r['competitors'])} competitors")
        for c in r["shortlist"][:limit]:
            lines.append(f"• [Name], {c['left_title'] or '[Title]'}, left {c['left_company']} {c['left_date'][:7]}"
                         f" -> {c['role']} (score {c['score']})")
    return "\n".join(lines)


def _read_state(state_path: str) -> set:
    if not os.path.exists(state_path):
        return set()
    with open(state_path, encoding="utf-8") as fh:
        return set(json.load(fh))


def only_new(results: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = _read_state(state_path)
    out = []
    for r in results:
        keep = [c for c in r["shortlist"] if f"{r['client']}:{c['id']}" not in seen]
        out.append(dict(r, shortlist=keep))
    return out


def save_state(results: List[Dict[str, Any]], state_path: str) -> None:
    seen = _read_state(state_path)
    seen.update(f"{r['client']}:{c['id']}" for r in results for c in r["shortlist"])
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(seen), fh, indent=1)


def load_clients(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["clients"]


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Competitor layoff pitch builder (Crustdata data layer)")
    p.add_argument("command", choices=["watch", "pitch"])
    p.add_argument("--clients", default="clients.json", help="clients file (see examples/clients.example.json)")
    p.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "45")))
    p.add_argument("--max-results", type=int, default=200, help="leavers scanned per competitor")
    p.add_argument("--only-domains", default="", help="comma list: only competitors whose watch fired")
    p.add_argument("--include-placed", action="store_true", help="keep people who already have a new role")
    p.add_argument("--min-score", type=int, default=3)
    p.add_argument("--claude", action="store_true", help="draft the pitch with Claude")
    p.add_argument("--dry-run", action="store_true", help="never posts; for `watch`, prints the body only")
    p.add_argument("--all", action="store_true", help="ignore state, show everything")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)
    clients = load_clients(args.clients)

    if args.command == "watch":
        domains = [d for c in clients for d in c.get("competitors", [])]
        body = watch_body(domains, os.environ.get("WATCH_WEBHOOK_URL", ""))
        if args.dry_run:
            print(json.dumps(body, indent=1))
            return 0
        print(json.dumps(Crustdata().watch_company(body), indent=1)[:2000])
        return 0

    since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
    only = [d for d in args.only_domains.split(",") if d.strip()]
    results = run(Crustdata(), clients, since, args.max_results, only, args.include_placed, args.min_score)
    stats = summary(results)
    if not args.all:
        results = only_new(results, args.state)
    use_claude = args.claude and claude_available()
    for r in results:
        r["pitch"] = draft_pitch({"name": r["client"], "contact": r["contact"]}, r["shortlist"], use_claude) \
            if r["shortlist"] else ""
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "pitches.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": stats, "results": results}, fh, indent=1)
    print(json.dumps(stats, indent=1))
    if any(r["shortlist"] for r in results):
        print(send_slack(slack_text(results), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(results, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
