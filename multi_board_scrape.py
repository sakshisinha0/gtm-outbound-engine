#!/usr/bin/env python3
"""
Multi-board job scraper for the GTM Outbound Engine.

Pulls jobs that match icp_config.json from several job boards and writes one CSV
with the columns: job board, title, company name, job link, company domain.

  hiringcafe  Browser scrape with Playwright (the site renders in JavaScript).
              The company domain comes straight from the job card.
  himalayas   Public API. No domain in the data, so that column is left blank.
  yc          Work at a Startup. The domain comes from the company's YC page.

Filtering (title include/exclude, locations, max age) reuses job_signals.py, so the
rules are the same as the rest of the pipeline. Blank domains can be filled with
company_domains.py afterwards.

Setup (HiringCafe only):
    pip install playwright
    playwright install chromium

Usage:
    python3 multi_board_scrape.py
    python3 multi_board_scrape.py --boards hiringcafe,yc
    python3 multi_board_scrape.py --config icp_config.json --out board_jobs.csv --headed
"""

import argparse
import csv
import json
import sys
import urllib.parse
from datetime import datetime, timezone

import company_domains
import job_signals

BOARDS = ["hiringcafe", "himalayas", "yc"]
CSV_COLUMNS = ["job board", "title", "company name", "job link", "company domain"]

HIRINGCAFE_NAV_TIMEOUT_MS = 45000
HIRINGCAFE_CARD_TIMEOUT_MS = 20000
HIRINGCAFE_SCROLLS = 4
WORKPLACES = {"remote", "hybrid", "onsite", "field"}

# Runs in the page: one dict per job card.
HIRINGCAFE_CARD_JS = """cards => cards.map(c => {
  const job = c.querySelector('a[href*="/job/"]');
  const org = c.querySelector('a[href*="/org/"]');
  return {
    title: c.querySelector('span.font-bold')?.innerText || '',
    company: c.querySelector('img')?.alt || '',
    link: job?.href || '',
    domain: org ? new URL(org.href).pathname.split('/org/')[1] : '',
    lines: c.innerText.split('\\n').map(s => s.trim()).filter(Boolean),
  };
})"""


# ---------------------------------------------------------------------------
# Board: HiringCafe (Playwright)
# ---------------------------------------------------------------------------

def hiringcafe_url(keyword, max_age_days):
    state = json.dumps({"searchQuery": keyword, "dateFetchedPastNDays": max_age_days},
                       separators=(",", ":"))
    return "https://hiringcafe.com/classic?searchState=" + urllib.parse.quote(state)


def normalize_hiringcafe(card):
    """Card text reads: age, title, location, workplace, ..."""
    lines = card["lines"]
    after = lines[lines.index(card["title"]) + 1:] if card["title"] in lines else []
    location = after[0] if after else ""
    workplace = next((l.lower() for l in after[:3] if l.lower() in WORKPLACES), "")
    return {
        "board": "HiringCafe",
        "company": card["company"],
        "title": card["title"],
        "location": location,
        "workplace": workplace,
        "date": None,  # the search is already limited to the max age
        "url": card["link"],
        "domain": card["domain"],
        "description": "",
    }


def pull_hiringcafe(keywords, max_age_days, headed=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], ["hiringcafe: playwright not installed (pip install playwright && "
                    "playwright install chromium)"]

    jobs, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        for keyword in keywords:
            try:
                page.goto(hiringcafe_url(keyword, max_age_days),
                          wait_until="domcontentloaded", timeout=HIRINGCAFE_NAV_TIMEOUT_MS)
                page.wait_for_selector("[data-quick-job-card]",
                                       timeout=HIRINGCAFE_CARD_TIMEOUT_MS)
                for _ in range(HIRINGCAFE_SCROLLS):  # cards load as you scroll
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1000)
                cards = page.eval_on_selector_all("[data-quick-job-card]", HIRINGCAFE_CARD_JS)
                jobs.extend(normalize_hiringcafe(c) for c in cards if c["link"])
            except Exception as e:  # one bad keyword shouldn't stop the run
                errors.append(f"hiringcafe '{keyword}': {str(e).splitlines()[0]}")
        browser.close()
    return jobs, errors


# ---------------------------------------------------------------------------
# Boards pulled by job_signals.py
# ---------------------------------------------------------------------------

def with_board(jobs, board):
    return [{**j, "board": board, "domain": ""} for j in jobs]


def fill_yc_domains(jobs, fetch=job_signals.fetch_text):
    """YC job pages link to a company page that lists the website (cached per company)."""
    cache = {}
    for job in jobs:
        if job["board"] != "Y Combinator" or job["company"] in cache:
            job["domain"] = cache.get(job["company"], "")
            continue
        try:
            job_signals.pause(fetch)
            page_url = company_domains.yc_company_url(job["url"])
            website = company_domains.website_from_yc_page(fetch(page_url)) if page_url else ""
        except Exception:
            website = ""
        cache[job["company"]] = job["domain"] = domain_of(website)


def domain_of(website):
    host = urllib.parse.urlparse(website if "//" in website else "//" + website).netloc
    return host.lower().removeprefix("www.")


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def run(config_path, out_path, boards, headed=False, now=None):
    now = now or datetime.now(timezone.utc)
    with open(config_path, encoding="utf-8") as f:
        rules = job_signals.compile_rules(json.load(f))

    raw, errors = [], []
    if "hiringcafe" in boards:
        jobs, errs = pull_hiringcafe(rules["title_keywords"], rules["max_age_days"], headed)
        raw += jobs
        errors += errs
    if "himalayas" in boards:
        jobs, errs = job_signals.pull_himalayas(
            rules["title_keywords"], job_signals.fetch_text, rules["max_age_days"], now)
        raw += with_board(jobs, "Himalayas")
        errors += errs
    if "yc" in boards:
        jobs, errs = job_signals.pull_yc(rules["yc_roles"], job_signals.fetch_text, now)
        raw += with_board(jobs, "Y Combinator")
        errors += errs

    rows, seen = [], set()
    for job in raw:
        if not job["url"] or job["url"] in seen:
            continue
        if job_signals.match_job(job, rules, now):
            seen.add(job["url"])
            rows.append(job)

    if "yc" in boards:
        fill_yc_domains(rows)

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for j in rows:
            writer.writerow([j["board"], j["title"], j["company"], j["url"], j["domain"]])

    by_board = {}
    for j in rows:
        by_board[j["board"]] = by_board.get(j["board"], 0) + 1
    print(f"Scraped {len(raw)} jobs, kept {len(rows)} that match the ICP: {by_board}")
    print(f"Blank domains: {sum(1 for j in rows if not j['domain'])} "
          "(fill them with company_domains.py)")
    print(f"Wrote {out_path}")
    for error in errors:
        print(f"  warning: {error}", file=sys.stderr)
    return rows, errors


def main():
    parser = argparse.ArgumentParser(description="Scrape ICP-matching jobs from several boards.")
    parser.add_argument("--config", default="icp_config.json")
    parser.add_argument("--out", default="board_jobs.csv")
    parser.add_argument("--boards", default=",".join(BOARDS),
                        help=f"comma-separated, from: {', '.join(BOARDS)}")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    boards = [b.strip().lower() for b in args.boards.split(",") if b.strip()]
    unknown = [b for b in boards if b not in BOARDS]
    if unknown:
        parser.error(f"unknown board(s): {', '.join(unknown)}")
    run(args.config, args.out, boards, args.headed)


if __name__ == "__main__":
    main()
