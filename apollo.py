"""
The Apollo module: the one place that knows what Apollo returns and what it costs.

Callers ask for results ("what is this company's website?", "tell me about these
domains") and get plain answers back. They never see Apollo's endpoints or its
response format.

Credits: Apollo charges 1 credit per name search that finds a result, and 1 credit
per company enriched. Paid calls only work after authorize() has been approved, so
no caller can spend credits without the question being asked.

There are two adapters behind this interface:
  Apollo      the real one, talking to Apollo over HTTP (see apollo_api.py)
  FakeApollo  canned answers, for tests (no network, no credits)
"""

import re
import urllib.parse

import apollo_api
from apollo_api import ApolloError  # re-exported so callers import from one place

ENRICH_BATCH_SIZE = 10  # Apollo's limit per bulk enrichment call
SEARCH_RESULTS_PER_QUERY = 5


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def domain_of(url):
    """'https://www.Sprinto.com/about' -> 'sprinto.com'."""
    url = (url or "").strip()
    if not url:
        return ""
    if "//" not in url:
        url = "http://" + url
    host = urllib.parse.urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def valid_domain(domain):
    """Rejects junk like 'www.nectarlifecom' (no real TLD after the last dot)."""
    return bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", domain)) \
        and not domain.startswith("www.")


def _name_tokens(company):
    """'Nectar Life, Inc.' -> 'nectarlife' (letters and digits only)."""
    cleaned = re.sub(r"\b(inc|llc|ltd|limited|corp|co|pvt|private|technologies|labs)\b\.?", " ",
                     company.lower())
    return re.sub(r"[^a-z0-9]", "", cleaned)


def name_matches(company, domain):
    """True if the domain's main label contains the company name (or the reverse)."""
    name = _name_tokens(company)
    label = domain.split(".")[0].replace("-", "")
    if not name or not label:
        return False
    return name in label or (len(label) >= 4 and label in name)


# ---------------------------------------------------------------------------
# Shaping Apollo's responses into plain answers
# ---------------------------------------------------------------------------

def _growth_text(value):
    """0.2307 -> '+23%'; None -> ''."""
    return "" if value is None else f"{value * 100:+.0f}%"


def _short_text(text, limit=240):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if ". " in cut:  # prefer ending on a full sentence
        return cut[:cut.rindex(". ") + 1]
    return cut.rsplit(" ", 1)[0] + "..."


def _profile(org):
    """One Apollo organization -> a plain company profile. Zero or missing revenue stays blank."""
    revenue = org.get("organization_revenue")
    return {
        "domain": domain_of(org.get("primary_domain") or org.get("website_url") or ""),
        "name": org.get("name") or "",
        "industry": org.get("industry") or "",
        "employees": org.get("estimated_num_employees") or "",
        "revenue_usd": int(revenue) if revenue else "",
        "founded_year": org.get("founded_year") or "",
        "city": org.get("city") or "",
        "state": org.get("state") or "",
        "country": org.get("country") or "",
        "total_funding_usd": org.get("total_funding") or "",
        "latest_funding_stage": org.get("latest_funding_stage") or "",
        "latest_funding_date": (org.get("latest_funding_round_date") or "")[:10],
        "headcount_growth_12m": _growth_text(org.get("organization_headcount_twelve_month_growth")),
        "linkedin_url": org.get("linkedin_url") or "",
        "phone": (org.get("primary_phone") or {}).get("number") or org.get("phone") or "",
        "short_description": _short_text(org.get("short_description")),
    }


# ---------------------------------------------------------------------------
# Spend policy (shared by every adapter)
# ---------------------------------------------------------------------------

class _SpendPolicy:
    """Tracks what the user has approved. Paid calls draw down the approved amount."""

    def __init__(self, assume_yes=False, confirm=input):
        self.assume_yes = assume_yes
        self.confirm = confirm
        self.searches_left = 0
        self.enrichments_left = 0

    def authorize(self, searches=0, enrichments=0):
        """Say what is about to be spent and ask once. Returns True if approved."""
        total = searches + enrichments
        if total == 0:
            return True
        parts = []
        if searches:
            parts.append(f"{searches} name search{'es' if searches != 1 else ''}")
        if enrichments:
            parts.append(f"{enrichments} enrichment{'s' if enrichments != 1 else ''}")
        print(f"This will use up to {total} Apollo credit{'s' if total != 1 else ''} "
              f"({' + '.join(parts)}). "
              f"1 credit per result found, no charge for no match.")
        if not self.assume_yes and self.confirm("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled. No credits used.")
            return False
        self.searches_left += searches
        self.enrichments_left += enrichments
        return True

    def use_search(self):
        if self.searches_left <= 0:
            raise ApolloError("Name searches were not authorized. Call authorize(searches=N) first.")
        self.searches_left -= 1

    def use_enrichments(self, count):
        if self.enrichments_left < count:
            raise ApolloError("Enrichments were not authorized. Call authorize(enrichments=N) first.")
        self.enrichments_left -= count


# ---------------------------------------------------------------------------
# The real adapter
# ---------------------------------------------------------------------------

class Apollo(_SpendPolicy):
    """Apollo over HTTP. `post` is injectable so the module's own logic can be tested."""

    def __init__(self, post=None, assume_yes=False, confirm=input):
        super().__init__(assume_yes, confirm)
        if post is None:
            apollo_api.get_api_key()  # fail early with a clear message if the key is missing
            post = apollo_api.api_post
        self._post = post

    def find_domain(self, company):
        """Best-guess website for a company name: (website, confidence).

        confidence is 'high' when the domain contains the company name, 'low' when it is
        only the first result, and ('', '') when Apollo found nothing usable.
        """
        self.use_search()
        response = self._post("mixed_companies/search",
                              {"q_organization_name": company, "per_page": SEARCH_RESULTS_PER_QUERY})
        # Net-new companies carry primary_domain; ones your team already saved carry domain.
        results = (response.get("organizations") or []) + (response.get("accounts") or [])
        domains = []
        for org in results:
            domain = domain_of(org.get("primary_domain") or org.get("domain") or "")
            if valid_domain(domain):
                domains.append(domain)
        if not domains:
            return "", ""
        for domain in domains:
            if name_matches(company, domain):
                return f"https://{domain}", "high"
        return f"https://{domains[0]}", "low"

    def enrich(self, domains):
        """Company profiles for domains: ({domain: profile}, [domains Apollo didn't know])."""
        domains = list(dict.fromkeys(d.lower() for d in domains))
        self.use_enrichments(len(domains))
        profiles = {}
        for start in range(0, len(domains), ENRICH_BATCH_SIZE):
            batch = domains[start:start + ENRICH_BATCH_SIZE]
            response = self._post("organizations/bulk_enrich", {"domains": batch})
            for org in response.get("organizations") or []:
                if not org:
                    continue
                profile = _profile(org)
                # Apollo can canonicalise a domain, so also try the website host.
                if profile["domain"] in batch:
                    profiles[profile["domain"]] = profile
                else:
                    host = domain_of(org.get("website_url") or "")
                    if host in batch:
                        profiles[host] = profile
        missing = [d for d in domains if d not in profiles]
        return profiles, missing


# ---------------------------------------------------------------------------
# The test adapter
# ---------------------------------------------------------------------------

class FakeApollo(_SpendPolicy):
    """Canned Apollo for tests: no network, no credits, same interface and same spend rule.

    domains:  {"Sprinto": ("https://sprinto.com", "high")}
    profiles: {"sprinto.com": {"name": "Sprinto", "employees": 550}}  (missing keys are blank)
    """

    def __init__(self, domains=None, profiles=None, assume_yes=True, confirm=input):
        super().__init__(assume_yes, confirm)
        self.domains = domains or {}
        self.profiles = profiles or {}
        self.searched = []
        self.enriched = []

    def find_domain(self, company):
        self.use_search()
        self.searched.append(company)
        return self.domains.get(company, ("", ""))

    def enrich(self, domains):
        domains = list(dict.fromkeys(d.lower() for d in domains))
        self.use_enrichments(len(domains))
        self.enriched.extend(domains)
        blank = _profile({})
        found = {d: {**blank, "domain": d, **self.profiles[d]} for d in domains if d in self.profiles}
        return found, [d for d in domains if d not in found]
