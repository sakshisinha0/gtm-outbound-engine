"""Offline tests for apollo.py, company_domains.py and company_enrich.py (no network, no credits).

Run with:  python3 test_company_pipeline.py
"""
import csv
import os
import tempfile
import unittest

import apollo
import company_domains
import company_enrich


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


JOBS_HEADER = ["company", "url", "source"]
JOBS = [
    ["Sprinto", "https://himalayas.app/companies/sprinto/jobs/a", "himalayas"],
    ["Sprinto", "https://himalayas.app/companies/sprinto/jobs/b", "himalayas"],   # duplicate company
    ["Omnistrate", "https://www.ycombinator.com/companies/omnistrate/jobs/x", "yc"],
    ["Nectar Life", "https://himalayas.app/companies/nectar-life/jobs/c", "himalayas"],
    ["Ghost Co", "https://himalayas.app/companies/ghost/jobs/d", "himalayas"],
]


class ApolloModuleTests(unittest.TestCase):
    """The real adapter's own logic, driven by a fake transport."""

    def make(self, responses, assume_yes=True, confirm=input):
        calls = []

        def post(path, body):
            calls.append((path, body))
            return responses(path, body)

        return apollo.Apollo(post=post, assume_yes=assume_yes, confirm=confirm), calls

    def test_paid_calls_refuse_without_authorization(self):
        client, calls = self.make(lambda p, b: {})
        with self.assertRaises(apollo.ApolloError):
            client.find_domain("Sprinto")
        with self.assertRaises(apollo.ApolloError):
            client.enrich(["sprinto.com"])
        self.assertEqual(calls, [])

    def test_declined_confirmation_spends_nothing(self):
        client, calls = self.make(lambda p, b: {}, assume_yes=False, confirm=lambda prompt: "n")
        self.assertFalse(client.authorize(searches=3))
        with self.assertRaises(apollo.ApolloError):
            client.find_domain("Sprinto")
        self.assertEqual(calls, [])

    def test_authorization_is_a_budget(self):
        client, _ = self.make(lambda p, b: {"organizations": []})
        client.authorize(searches=1)
        client.find_domain("One")
        with self.assertRaises(apollo.ApolloError):
            client.find_domain("Two")

    def test_find_domain_confidence_and_buckets(self):
        def responses(path, body):
            name = body["q_organization_name"]
            if name == "Sprinto":   # net-new bucket, name matches
                return {"organizations": [{"primary_domain": "sprinto.se"}, {"primary_domain": "sprinto.com"}]}
            if name == "Acme Rockets":   # saved-account bucket uses 'domain'
                return {"accounts": [{"domain": "acmerockets.io"}]}
            if name == "Nectar Life":   # junk domain is skipped, next result is unrelated
                return {"organizations": [{"primary_domain": "www.nectarlifecom"},
                                          {"primary_domain": "neclife.com"}]}
            return {"organizations": []}

        client, _ = self.make(responses)
        client.authorize(searches=4)
        self.assertEqual(client.find_domain("Sprinto"), ("https://sprinto.se", "high"))
        self.assertEqual(client.find_domain("Acme Rockets"), ("https://acmerockets.io", "high"))
        self.assertEqual(client.find_domain("Nectar Life"), ("https://neclife.com", "low"))
        self.assertEqual(client.find_domain("Nobody"), ("", ""))

    def test_enrich_batches_of_ten_and_reports_missing(self):
        domains = [f"co{i}.com" for i in range(12)]

        def responses(path, body):
            self.assertLessEqual(len(body["domains"]), 10)
            return {"organizations": [{"name": d, "primary_domain": d}
                                      for d in body["domains"] if d != "co3.com"]}

        client, calls = self.make(responses)
        client.authorize(enrichments=12)
        profiles, missing = client.enrich(domains)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(profiles), 11)
        self.assertEqual(missing, ["co3.com"])

    def test_enrich_matches_a_canonicalised_domain_and_cleans_values(self):
        def responses(path, body):
            return {"organizations": [{
                "name": "Clera", "primary_domain": "clera.ai", "website_url": "http://www.getclera.com",
                "organization_revenue": 0.0, "estimated_num_employees": 27,
                "organization_headcount_twelve_month_growth": 1.6667,
                "primary_phone": {"number": "+1 415"}, "short_description": "One. Two.",
            }]}

        client, _ = self.make(responses)
        client.authorize(enrichments=1)
        profiles, missing = client.enrich(["getclera.com"])
        self.assertEqual(missing, [])
        profile = profiles["getclera.com"]
        self.assertEqual(profile["revenue_usd"], "")        # zero revenue stays blank
        self.assertEqual(profile["headcount_growth_12m"], "+167%")
        self.assertEqual(profile["phone"], "+1 415")


class CompanyDomainsTests(unittest.TestCase):
    def run_domains(self, fake, use_apollo=True):
        yc_html = lambda url: ('<div data-page="%s"></div>'
                               % '{&quot;props&quot;:{&quot;company&quot;:{&quot;website&quot;:'
                                 '&quot;http://www.omnistrate.com&quot;}}}')
        with tempfile.TemporaryDirectory() as tmp:
            jobs, out = os.path.join(tmp, "jobs.csv"), os.path.join(tmp, "d.csv")
            write_csv(jobs, JOBS_HEADER, JOBS)
            code = company_domains.run(jobs, out, use_apollo=use_apollo, fetch=yc_html, apollo_client=fake)
            return code, read_csv(out)

    def test_yc_is_free_and_only_the_rest_is_searched(self):
        fake = apollo_fake_domains()
        code, rows = self.run_domains(fake)
        self.assertEqual(code, 0)
        by_company = {r["company"]: r for r in rows}
        self.assertEqual(len(rows), 4)    # duplicate company collapsed
        self.assertEqual(by_company["Omnistrate"]["method"], "yc page")
        self.assertEqual(by_company["Sprinto"]["domain"], "sprinto.com")
        self.assertEqual(by_company["Nectar Life"]["confidence"], "low")
        self.assertEqual(by_company["Ghost Co"]["domain"], "")
        self.assertNotIn("Omnistrate", fake.searched)     # YC company never costs a credit
        self.assertEqual(sorted(fake.searched), ["Ghost Co", "Nectar Life", "Sprinto"])

    def test_declined_confirmation_still_writes_yc_rows_and_searches_nothing(self):
        fake = apollo_fake_domains(assume_yes=False, confirm=lambda prompt: "n")
        code, rows = self.run_domains(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.searched, [])
        self.assertEqual({r["company"]: r["domain"] for r in rows}["Omnistrate"], "omnistrate.com")

    def test_no_apollo_flag_uses_no_credits(self):
        code, rows = self.run_domains(None, use_apollo=False)
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for r in rows if r["domain"]), 1)


def apollo_fake_domains(**kwargs):
    return apollo.FakeApollo(domains={
        "Sprinto": ("https://sprinto.com", "high"),
        "Nectar Life": ("https://neclife.com", "low"),
    }, **kwargs)


class CompanyEnrichTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.domains = os.path.join(self.tmp.name, "d.csv")
        self.out = os.path.join(self.tmp.name, "e.csv")
        write_csv(self.domains, company_domains.OUT_COLUMNS, [
            ["Sprinto", "sprinto.com", "https://sprinto.com", "high", "apollo search", ""],
            ["Nectar Life", "nectarlife.com", "https://nectarlife.com", "low", "apollo search", ""],
            ["Ghost Co", "", "", "", "", ""],
            ["Sprinto Again", "sprinto.com", "", "high", "", ""],
        ])

    def test_enriches_each_domain_once_and_joins_the_source_row(self):
        fake = apollo.FakeApollo(profiles={"sprinto.com": {"name": "Sprinto Inc", "employees": 550}})
        self.assertEqual(company_enrich.run(self.domains, self.out, apollo_client=fake), 0)
        self.assertEqual(fake.enriched, ["sprinto.com", "nectarlife.com"])   # blank + duplicate skipped
        rows = read_csv(self.out)
        self.assertEqual(len(rows), 1)                                        # nectarlife not in Apollo
        self.assertEqual(rows[0]["company"], "Sprinto")
        self.assertEqual(rows[0]["matched_name"], "Sprinto Inc")
        self.assertEqual(rows[0]["employees"], "550")
        self.assertEqual(rows[0]["domain_confidence"], "high")

    def test_skip_low_confidence_saves_credits(self):
        fake = apollo.FakeApollo()
        company_enrich.run(self.domains, self.out, skip_low_confidence=True, apollo_client=fake)
        self.assertEqual(fake.enriched, ["sprinto.com"])

    def test_declined_confirmation_writes_nothing(self):
        fake = apollo.FakeApollo(assume_yes=False, confirm=lambda prompt: "n")
        self.assertEqual(company_enrich.run(self.domains, self.out, apollo_client=fake), 0)
        self.assertEqual(fake.enriched, [])
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
