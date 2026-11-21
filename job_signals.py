#!/usr/bin/env python3
"""
Job signal puller for the GTM Outbound Engine (Step 2: Capture signals).

Searches job boards for roles that match your ICP rules and writes them to a CSV.

  Pulled automatically:  Himalayas (public API) and Y Combinator (Work at a Startup)
  Search links only:     hiring.cafe, Welcome to the Jungle, Glassdoor and LinkedIn.
                         They have no public API and block scripts, so the script
                         writes ready-made search URLs to open yourself.

Uses only the Python standard library, so there is nothing to install.

Usage:
    python3 job_signals.py
    python3 job_signals.py --config icp_config.json --out jobs.csv --links search_links.csv
    python3 job_signals.py --max-age-days 14
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER_AGENT = "Mozilla/5.0 (compatible; job-signals-script/2.0)"
TIMEOUT_SECONDS = 20
REQUEST_DELAY_SECONDS = 1
HIMALAYAS_MAX_PAGES = 3
DEFAULT_YC_ROLES = ["marketing", "operations", "sales-manager"]

CSV_COLUMNS = [
    "is_new", "company", "title", "location", "workplace", "date",
    "date_type", "days_old", "tier_hint", "tool_mentions",
    "automation_mentions", "build_mentions", "url", "source",
]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_text(url):
    """GET a URL and return the body as text. Retries once on a network error."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError("page not found") from e
            last_error = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
        if attempt == 0:
            time.sleep(2)
    raise RuntimeError(str(last_error))


def pause(fetch):
    """Be polite to real servers; skip the wait when tests pass in a fake fetch."""
    if fetch is fetch_text:
        time.sleep(REQUEST_DELAY_SECONDS)


def strip_html(text):
    """Unescape, strip tags, unescape again (some sources double-escape)."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value):
    """Accept ISO strings, or epoch timestamps in seconds or milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Source: Himalayas (https://himalayas.app/api)
# ---------------------------------------------------------------------------

def himalayas_url(keyword, page):
    query = urllib.parse.urlencode({"q": keyword, "sort": "recent", "page": page})
    return f"https://himalayas.app/jobs/api/search?{query}"


def normalize_himalayas(data):
    jobs = []
    for job in data.get("jobs", []):
        countries = job.get("locationRestrictions") or []
        jobs.append({
            "company": job.get("companyName") or "",
            "title": job.get("title") or "",
            "location": ", ".join(countries) if countries else "Worldwide",
            # All Himalayas jobs are remote, but a country list means only people
            # in those countries can take it, so only worldwide jobs count as "remote".
            "workplace": "" if countries else "remote",
            "date": parse_date(job.get("pubDate")),
            "date_type": "published",
            "url": job.get("applicationLink") or job.get("guid") or "",
            "description": strip_html(job.get("description")),
        })
    return jobs


def pull_himalayas(keywords, fetch, max_age_days, now):
    """Search each title keyword. Results are newest first, so stop paging at old posts."""
    jobs, errors = [], []
    cutoff = now - timedelta(days=max_age_days)
    for keyword in keywords:
        for page in range(1, HIMALAYAS_MAX_PAGES + 1):
            try:
                pause(fetch)
                batch = normalize_himalayas(json.loads(fetch(himalayas_url(keyword, page))))
            except Exception as e:  # one bad query shouldn't stop the run
                errors.append(f"himalayas '{keyword}' page {page}: {e}")
                break
            if not batch:
                break
            jobs.extend(batch)
            if all(j["date"] and j["date"] < cutoff for j in batch):
                break
    return jobs, errors


# ---------------------------------------------------------------------------
# Source: Y Combinator Work at a Startup (https://www.ycombinator.com/jobs)
# ---------------------------------------------------------------------------

def yc_url(role):
    return f"https://www.ycombinator.com/jobs/role/{urllib.parse.quote(role)}"


def relative_age(text, now):
    """Turn YC's '8 months' / 'over 1 year' / 'about 3 hours' into an approximate date."""
    match = re.search(r"(\d+|a|an)\s+(minute|hour|day|week|month|year)", (text or "").lower())
    if not match:
        return None
    amount = 1 if match.group(1) in ("a", "an") else int(match.group(1))
    days = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[match.group(2)]
    return now - timedelta(days=amount * days)


def normalize_yc(page_html, now):
    match = re.search(r'data-page="([^"]+)"', page_html)
    if not match:
        raise RuntimeError("YC page format changed (no data-page found)")
    props = json.loads(html.unescape(match.group(1))).get("props", {})
    jobs = []
    for job in props.get("jobPostings", []):
        location = job.get("location") or ""
        jobs.append({
            "company": job.get("companyName") or "",
            "title": job.get("title") or "",
            "location": location,
            "workplace": "remote" if "remote" in location.lower() else "",
            "date": relative_age(job.get("createdAt"), now),
            # YC only says things like "8 months", so the date is approximate.
            "date_type": "approx",
            "url": "https://www.ycombinator.com" + job["url"] if job.get("url") else "",
            # The list page has no job text, so YC jobs rarely get tool mentions.
            "description": job.get("companyOneLiner") or "",
        })
    return jobs


def pull_yc(roles, fetch, now):
    jobs, errors = [], []
    for role in roles:
        try:
            pause(fetch)
            jobs.extend(normalize_yc(fetch(yc_url(role)), now))
        except Exception as e:
            errors.append(f"yc '{role}': {e}")
    return jobs, errors


# ---------------------------------------------------------------------------
# Search links for boards that can't be pulled automatically
# ---------------------------------------------------------------------------

def search_links(keyword, max_age_days):
    quoted = urllib.parse.quote(keyword)
    state = json.dumps({"searchQuery": keyword, "dateFetchedPastNDays": max_age_days},
                       separators=(",", ":"))
    return [
        ("hiring.cafe",
         "https://hiring.cafe/?searchState=" + urllib.parse.quote(state)),
        ("welcome to the jungle",
         f"https://www.welcometothejungle.com/en/jobs?query={quoted}"),
        ("glassdoor",
         f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={quoted}&fromAge={max_age_days}"),
        ("linkedin",
         f"https://www.linkedin.com/jobs/search/?keywords={quoted}&f_TPR=r{max_age_days * 86400}"),
    ]


def write_links(path, keywords, max_age_days):
    rows = [(board, keyword, url)
            for keyword in keywords
            for board, url in search_links(keyword, max_age_days)]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["board", "keyword", "url"])
        writer.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Matching jobs against the ICP
# ---------------------------------------------------------------------------

def keyword_pattern(keyword):
    """Match a keyword as a whole word, so 'intern' doesn't match 'internal'."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])")


def compile_rules(config):
    def compiled(key):
        return [(k, keyword_pattern(k)) for k in config.get(key, []) if k.strip()]
    return {
        "max_age_days": config.get("max_age_days", 30),
        "title_keywords": [k for k in config.get("title_include", []) if k.strip()],
        "yc_roles": config.get("yc_roles", DEFAULT_YC_ROLES),
        "title_include": compiled("title_include"),
        "title_exclude": compiled("title_exclude"),
        "locations_include": compiled("locations_include"),
        "tool_keywords": compiled("tool_keywords"),
        "automation_keywords": compiled("automation_keywords"),
        "build_keywords": compiled("build_keywords"),
    }


def found(patterns, text):
    text = text.lower()
    return [keyword for keyword, pattern in patterns if pattern.search(text)]


def match_job(job, rules, now):
    """Return the job with match details, or None if it doesn't fit the ICP."""
    title = job["title"]
    if not found(rules["title_include"], title):
        return None
    if found(rules["title_exclude"], title):
        return None

    if rules["locations_include"]:
        where = f"{job['location']} {job['workplace']}"
        if not found(rules["locations_include"], where):
            return None

    days_old = None
    if job["date"]:
        days_old = max(0, (now - job["date"]).days)
        if days_old > rules["max_age_days"]:
            return None

    text = f"{title} {job['description']}"
    tools = found(rules["tool_keywords"], text)
    automation = found(rules["automation_keywords"], text)
    build = found(rules["build_keywords"], text)

    # This is a signal hint only. Company size and funding are checked in
    # Step 3 (enrichment) before a lead gets its final tier.
    tier_hint = "Tier 1 candidate" if tools else "Tier 2 candidate"

    return {
        **job,
        "days_old": days_old,
        "tier_hint": tier_hint,
        "tool_mentions": "; ".join(tools),
        "automation_mentions": "; ".join(automation),
        "build_mentions": "; ".join(build),
    }


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def load_seen(path):
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(path, urls):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=0)


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["date"] = row["date"].date().isoformat() if row["date"] else ""
            out["days_old"] = "" if row["days_old"] is None else row["days_old"]
            out["is_new"] = "yes" if row["is_new"] else "no"
            writer.writerow(out)


def run(config_path, out_path, links_path, max_age_days=None, fetch=fetch_text, now=None):
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    if max_age_days is not None:
        config["max_age_days"] = max_age_days
    rules = compile_rules(config)
    now = now or datetime.now(timezone.utc)

    if not rules["title_keywords"]:
        print("title_include in", config_path, "is empty, so there is nothing to search for.")
        return 1

    print("Pulling jobs from Himalayas and Y Combinator...")
    sourced, errors = [], []
    for source, (jobs, errs) in (
        ("himalayas", pull_himalayas(rules["title_keywords"], fetch, rules["max_age_days"], now)),
        ("yc", pull_yc(rules["yc_roles"], fetch, now)),
    ):
        for job in jobs:
            job["source"] = source
        sourced.extend(jobs)
        errors.extend(errs)

    seen_path = os.path.join(os.path.dirname(os.path.abspath(out_path)), "seen_jobs.json")
    seen = load_seen(seen_path)

    # One row per job URL, even if several searches returned it.
    unique, urls = [], set()
    for job in sourced:
        match = match_job(job, rules, now)
        if not match or not match["url"] or match["url"] in urls:
            continue
        urls.add(match["url"])
        match["is_new"] = match["url"] not in seen
        unique.append(match)

    unique.sort(key=lambda r: (
        r["tier_hint"] != "Tier 1 candidate",
        r["days_old"] if r["days_old"] is not None else 9999,
    ))
    write_csv(out_path, unique)
    save_seen(seen_path, seen | urls)
    link_count = write_links(links_path, rules["title_keywords"], rules["max_age_days"])

    new_count = sum(1 for r in unique if r["is_new"])
    tier1 = sum(1 for r in unique if r["tier_hint"] == "Tier 1 candidate")
    print(f"Jobs scanned:      {len(sourced)}")
    print(f"Matching jobs:     {len(unique)} ({tier1} Tier 1 candidates)")
    print(f"New since last run: {new_count}")
    print(f"Saved to:          {out_path}")
    print(f"Search links:      {link_count} for hiring.cafe, Welcome to the Jungle, Glassdoor "
          f"and LinkedIn, saved to {links_path}")
    if errors:
        print("\nSearches that failed:")
        for e in errors:
            print("  -", e)
    return 0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Pull ICP-matching job posts from job boards.")
    parser.add_argument("--config", default=os.path.join(here, "icp_config.json"))
    parser.add_argument("--out", default=os.path.join(here, "jobs.csv"))
    parser.add_argument("--links", default=os.path.join(here, "search_links.csv"))
    parser.add_argument("--max-age-days", type=int, help="override max_age_days in the config")
    args = parser.parse_args()
    sys.exit(run(args.config, args.out, args.links, args.max_age_days))


if __name__ == "__main__":
    main()
