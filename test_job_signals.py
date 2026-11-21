"""Offline tests for job_signals.py, using sample responses (no network needed).

Run with:  python3 test_job_signals.py
"""
import csv
import html
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import job_signals as js

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
HERE = os.path.dirname(os.path.abspath(__file__))


def epoch(year, month, day):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


HIMALAYAS = {"jobs": [
    {"title": "GTM Engineer", "companyName": "Beta", "locationRestrictions": [],
     "pubDate": epoch(2026, 11, 1), "applicationLink": "https://himalayas.app/companies/beta/jobs/gtm",
     "description": "<p>Build our outbound engine from scratch with <b>Clay</b> and n8n.</p>"},
    {"title": "Marketing Operations Manager", "companyName": "Old Co", "locationRestrictions": ["India"],
     "pubDate": epoch(2026, 8, 1), "applicationLink": "https://himalayas.app/companies/old/jobs/mo",
     "description": "Old post."},
    {"title": "Sales Development Representative", "companyName": "Acme", "locationRestrictions": [],
     "pubDate": epoch(2026, 11, 12), "applicationLink": "https://himalayas.app/companies/acme/jobs/sdr",
     "description": ""},
]}

YC_PAGE = {"props": {"jobPostings": [
    {"title": "Growth Operations Lead", "url": "/companies/gamma/jobs/abc-growth",
     "location": "Bengaluru, India", "companyName": "Gamma", "createdAt": "2 weeks",
     "companyOneLiner": "Payroll for startups."},
    {"title": "Growth Marketer", "url": "/companies/delta/jobs/xyz",
     "location": "San Francisco, CA", "companyName": "Delta", "createdAt": "3 days",
     "companyOneLiner": ""},
    {"title": "RevOps Analyst", "url": "/companies/eps/jobs/old",
     "location": "Remote (US)", "companyName": "Epsilon", "createdAt": "over 1 year",
     "companyOneLiner": ""},
]}}
YC_HTML = '<div id="app" data-page="%s"></div>' % html.escape(json.dumps(YC_PAGE), quote=True)


def fake_fetch(url):
    if "himalayas.app" in url:
        # Only the first page of results has jobs.
        return json.dumps(HIMALAYAS if "page=1" in url else {"jobs": []})
    if "ycombinator.com" in url:
        return YC_HTML
    raise RuntimeError("unexpected url " + url)


class HelperTests(unittest.TestCase):
    def test_relative_age(self):
        self.assertEqual((NOW - js.relative_age("2 weeks", NOW)).days, 14)
        self.assertEqual((NOW - js.relative_age("over 1 year", NOW)).days, 365)
        self.assertEqual((NOW - js.relative_age("about 3 hours", NOW)).days, 0)
        self.assertIsNone(js.relative_age("", NOW))

    def test_search_links(self):
        links = dict(js.search_links("revenue operations", 14))
        self.assertEqual(set(links), {"hiring.cafe", "welcome to the jungle", "glassdoor", "linkedin"})
        self.assertIn("keywords=revenue%20operations", links["linkedin"])
        self.assertIn("f_TPR=r1209600", links["linkedin"])
        self.assertIn("fromAge=14", links["glassdoor"])


class RunTests(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "jobs.csv")
            links = os.path.join(tmp, "search_links.csv")
            config = os.path.join(HERE, "icp_config.json")

            self.assertEqual(js.run(config, out, links, fetch=fake_fetch, now=NOW), 0)
            with open(out) as f:
                rows = list(csv.DictReader(f))

            titles = [r["title"] for r in rows]
            # Himalayas returns the same jobs for every keyword search, but each
            # job appears once. SDR, old, US-only and year-old posts are dropped.
            # Tier 1 candidates first, then newest first.
            self.assertEqual(titles, ["GTM Engineer", "Growth Operations Lead"])
            gtm, growth = rows
            self.assertEqual(gtm["source"], "himalayas")
            self.assertEqual(gtm["tier_hint"], "Tier 1 candidate")
            self.assertEqual(gtm["tool_mentions"], "clay; n8n")
            self.assertEqual(gtm["build_mentions"], "from scratch")
            self.assertEqual(gtm["days_old"], "16")
            self.assertEqual(gtm["location"], "Worldwide")
            self.assertEqual(growth["source"], "yc")
            self.assertEqual(growth["date_type"], "approx")
            self.assertEqual(growth["url"], "https://www.ycombinator.com/companies/gamma/jobs/abc-growth")
            self.assertEqual(growth["tier_hint"], "Tier 2 candidate")
            self.assertTrue(all(r["is_new"] == "yes" for r in rows))

            with open(links) as f:
                link_rows = list(csv.DictReader(f))
            self.assertEqual({r["board"] for r in link_rows},
                             {"hiring.cafe", "welcome to the jungle", "glassdoor", "linkedin"})

            # A second run marks the same jobs as already seen.
            js.run(config, out, links, fetch=fake_fetch, now=NOW)
            with open(out) as f:
                self.assertTrue(all(r["is_new"] == "no" for r in csv.DictReader(f)))

    def test_failed_source_is_reported_and_run_continues(self):
        def fetch(url):
            if "himalayas.app" in url:
                raise RuntimeError("boom")
            return fake_fetch(url)

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "jobs.csv")
            config = os.path.join(HERE, "icp_config.json")
            self.assertEqual(js.run(config, out, os.path.join(tmp, "l.csv"), fetch=fetch, now=NOW), 0)
            with open(out) as f:
                self.assertEqual([r["title"] for r in csv.DictReader(f)], ["Growth Operations Lead"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
