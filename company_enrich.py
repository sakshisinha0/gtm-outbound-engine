#!/usr/bin/env python3
"""
Company enrichment for the GTM Outbound Engine (Step 3: Enrich).

Takes the domains in company_domains.csv, enriches them through Apollo, and writes
company_enriched.csv (industry, headcount, revenue, funding, location, LinkedIn,
description).

Apollo charges 1 credit per matched company (nothing for unmatched), so the script
tells you the maximum cost and asks before spending anything.
Needs the APOLLO_API_KEY environment variable.

Usage:
    python3 company_enrich.py
    python3 company_enrich.py --domains company_domains.csv --out company_enriched.csv
    python3 company_enrich.py --yes                 # skip the confirmation prompt
    python3 company_enrich.py --skip-low-confidence # don't spend credits on shaky domains
"""

import argparse
import csv
import os
import sys

import apollo

OUT_COLUMNS = [
    "company", "domain", "domain_confidence", "matched_name", "industry", "employees",
    "revenue_usd", "founded_year", "city", "state", "country", "total_funding_usd",
    "latest_funding_stage", "latest_funding_date", "headcount_growth_12m",
    "linkedin_url", "phone", "short_description",
]


def read_domains(path, skip_low_confidence):
    """Rows from company_domains.csv that have a domain, one per domain."""
    rows, seen = [], set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            domain = (row.get("domain") or "").strip().lower()
            if not domain or domain in seen:
                continue
            if skip_low_confidence and row.get("confidence") == "low":
                continue
            seen.add(domain)
            rows.append({"company": row["company"], "domain": domain,
                         "confidence": row.get("confidence") or ""})
    return rows


def to_row(source, profile):
    """Join a company_domains.csv row with the profile Apollo returned for its domain."""
    return {"company": source["company"], "domain_confidence": source["confidence"],
            "matched_name": profile["name"],
            **{key: profile[key] for key in OUT_COLUMNS if key in profile and key != "name"}}


def run(domains_path, out_path, assume_yes=False, skip_low_confidence=False, apollo_client=None):
    try:
        rows = read_domains(domains_path, skip_low_confidence)
    except FileNotFoundError:
        print(f"{domains_path} not found. Run company_domains.py first.")
        return 1
    if not rows:
        print(f"No domains to enrich in {domains_path}.")
        return 1

    if apollo_client is None:
        try:
            apollo_client = apollo.Apollo(assume_yes=assume_yes)
        except apollo.ApolloError as e:
            print(e)
            return 1

    if not apollo_client.authorize(enrichments=len(rows)):
        return 0

    try:
        profiles, missing = apollo_client.enrich([r["domain"] for r in rows])
    except apollo.ApolloError as e:
        print(f"Apollo error: {e}")
        return 1

    results = [to_row(r, profiles[r["domain"]]) for r in rows if r["domain"] in profiles]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(results)

    print(f"Enriched {len(results)} of {len(rows)} companies. Saved to {out_path}")
    if missing:
        print("Not found in Apollo (no credit charged): " + ", ".join(missing))
    return 0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Enrich company domains through Apollo.")
    parser.add_argument("--domains", default=os.path.join(here, "company_domains.csv"))
    parser.add_argument("--out", default=os.path.join(here, "company_enriched.csv"))
    parser.add_argument("--yes", action="store_true", help="skip the credit confirmation prompt")
    parser.add_argument("--skip-low-confidence", action="store_true",
                        help="don't enrich domains marked low confidence")
    args = parser.parse_args()
    sys.exit(run(args.domains, args.out, args.yes, args.skip_low_confidence))


if __name__ == "__main__":
    main()
