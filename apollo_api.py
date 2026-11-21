"""
Minimal Apollo REST API client (standard library only).

The key comes from the APOLLO_API_KEY environment variable, never from a
command-line argument, so it doesn't end up in your shell history.

Docs: https://docs.apollo.io/reference
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE_URL = "https://api.apollo.io/api/v1"
TIMEOUT_SECONDS = 30
USER_AGENT = "gtm-outbound-engine/1.0"


class ApolloError(RuntimeError):
    pass


def get_api_key():
    key = os.environ.get("APOLLO_API_KEY", "").strip()
    if not key:
        raise ApolloError(
            "APOLLO_API_KEY is not set. Create an API key in Apollo (Settings > Integrations > API), "
            "then set it:\n  PowerShell:  $env:APOLLO_API_KEY = 'your-key'\n"
            "  bash:        export APOLLO_API_KEY='your-key'"
        )
    return key


def api_post(path, body, api_key=None):
    """POST JSON to an Apollo endpoint and return the parsed response. Retries once on 429/5xx."""
    api_key = api_key or get_api_key()
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/{path}", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-Api-Key": api_key, "User-Agent": USER_AGENT},
    )
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code in (401, 403):
                raise ApolloError(f"Apollo rejected the API key or this endpoint (HTTP {e.code}): "
                                  f"{detail}") from e
            last_error = ApolloError(f"HTTP {e.code}: {detail}")
            if e.code != 429 and e.code < 500:
                raise last_error from e
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = ApolloError(str(e))
        if attempt == 0:
            time.sleep(5)
    raise last_error
