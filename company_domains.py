#!/usr/bin/env python3
"""
Company domain finder for the GTM Outbound Engine (Step 3 input: enrichment).

Reads the companies in jobs.csv and writes each company's website and domain
to company_domains.csv.

How each company is looked up:
  Y Combinator jobs  The website listed on the company's YC page (free, most reliable).
  Everything else    Apollo's company search by name. Himalayas blocks scripts and its
                     API has no website field. Needs APOLLO_API_KEY. Apollo charges
                     1 credit per search that finds something, so the script tells you
                     the maximum cost and asks before searching.

The `confidence` column tells you how far to trust a row:
  high   YC page, or the domain contains the company name
  low    An Apollo result that doesn't obviously match the name. Check by hand
  (blank domain)  Nothing found. Look it up by hand

Usage:
    python3 company_domains.py
    python3 company_domains.py --jobs jobs.csv --out company_domains.csv
    python3 company_domains.py --yes          # skip the credit confirmation prompt
    python3 company_domains.py --no-apollo    # YC pages only, no credits used
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
import urllib.request

import apollo

USER_AGENT = "Mozilla/5.0 (compatible; job-signals-script/2.0)"
TIMEOUT_SECONDS = 20
REQUEST_DELAY_SECONDS = 1

OUT_COLUMNS = ["company", "domain", "website", "confidence", "method", "source_url"]


def fetch_text(url):
    """GET a URL and return the body as text. Retries once on a network error."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                raise RuntimeError(f"HTTP {e.code}") from e
            last_error = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
        if attempt == 0:
            time.sleep(2)
    raise RuntimeError(str(last_error))


# ---------------------------------------------------------------------------
# Y Combinator lookup
# ---------------------------------------------------------------------------

def yc_company_url(job_url):
    """https://www.ycombinator.com/companies/omnistrate/jobs/... -> .../companies/omnistrate"""
    match = re.match(r"(https://www\.ycombinator\.com/companies/[^/?#]+)", job_url)
    return match.group(1) if match else ""


def website_from_yc_page(page_html):
    """The YC page embeds its data as escaped JSON in data-page; the company has a 'website'."""
    match = re.search(r'data-page="([^"]+)"', page_html)
    if not match:
        return ""
    props = json.loads(html.unescape(match.group(1))).get("props", {})
    company = props.get("company") or {}
    return company.get("website") or ""


def find_via_yc(company, job_url, source, fetch, pause=lambda: None):
    """Result row from the company's YC page, or None if it isn't a YC job or the page has no site."""
    yc_url = yc_company_url(job_url) if source == "yc" else ""
    if not yc_url:
        return None
    try:
        pause()
        website = website_from_yc_page(fetch(yc_url))
    except Exception as e:
        print(f"  yc page failed for {company}: {e}", file=sys.stderr)
        return None
    if not website:
        return None
    return {"company": company, "domain": apollo.domain_of(website), "website": website,
            "confidence": "high", "method": "yc page", "source_url": yc_url}


def blank_row(company):
    return {"company": company, "domain": "", "website": "", "confidence": "",
            "method": "", "source_url": ""}


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def read_companies(jobs_path):
    """Unique companies in jobs.csv, in file order, with one job URL and source each."""
    companies = {}
    with open(jobs_path, newline="", encoding="utf-8") as f:
        for job in csv.DictReader(f):
            name = (job.get("company") or "").strip()
            if not name:
                continue
            key = name.lower()
            # Prefer a YC job for the company if there is one, since that has a reliable page.
            if key not in companies or (job.get("source") == "yc" and companies[key][2] != "yc"):
                companies[key] = (name, job.get("url") or "", job.get("source") or "")
    return list(companies.values())


def run(jobs_path, out_path, use_apollo=True, assume_yes=False, fetch=fetch_text, apollo_client=None):
    try:
        companies = read_companies(jobs_path)
    except FileNotFoundError:
        print(f"{jobs_path} not found. Run job_signals.py first.")
        return 1
    if not companies:
        print(f"No companies in {jobs_path}.")
        return 1

    if use_apollo and apollo_client is None:
        try:
            apollo_client = apollo.Apollo(assume_yes=assume_yes)
        except apollo.ApolloError as e:
            print(e)
            return 1

    pause = (lambda: time.sleep(REQUEST_DELAY_SECONDS)) if fetch is fetch_text else (lambda: None)
    print(f"Looking up {len(companies)} companies...")

    # Free lookups first, so we know how many Apollo searches are really needed.
    rows = {}
    for name, job_url, source in companies:
        rows[name] = find_via_yc(name, job_url, source, fetch, pause) or blank_row(name)
    unresolved = [name for name, _, _ in companies if not rows[name]["domain"]]

    if unresolved and use_apollo and apollo_client.authorize(searches=len(unresolved)):
        for name in unresolved:
            try:
                website, confidence = apollo_client.find_domain(name)
            except apollo.ApolloError as e:
                print(f"  apollo search failed for {name}: {e}", file=sys.stderr)
                continue
            if website:
                rows[name].update(domain=apollo.domain_of(website), website=website,
                                  confidence=confidence, method="apollo search")

    ordered = [rows[name] for name, _, _ in companies]
    for row in ordered:
        print(f"  {row['company']}: {row['domain'] or 'not found'}"
              + (f" ({row['confidence']}, {row['method']})" if row["domain"] else ""))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(ordered)

    found = sum(1 for r in ordered if r["domain"])
    low = sum(1 for r in ordered if r["confidence"] == "low")
    print(f"\nFound {found} of {len(ordered)} domains ({low} low confidence). Saved to {out_path}")
    return 0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Find the website of each company in jobs.csv.")
    parser.add_argument("--jobs", default=os.path.join(here, "jobs.csv"))
    parser.add_argument("--out", default=os.path.join(here, "company_domains.csv"))
    parser.add_argument("--yes", action="store_true", help="skip the credit confirmation prompt")
    parser.add_argument("--no-apollo", action="store_true",
                        help="only use YC pages; don't call Apollo (no credits used)")
    args = parser.parse_args()
    sys.exit(run(args.jobs, args.out, use_apollo=not args.no_apollo, assume_yes=args.yes))


if __name__ == "__main__":
    main()
