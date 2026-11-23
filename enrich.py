import requests

resp = requests.get(
    "https://api.apollo.io/api/v1/organizations/enrich",
    headers={"x-api-key": "r7Mmg98BAx34nekHEKteig", "accept": "application/json"},
    params={"domain": "apollo.io"},
)

if resp.status_code == 200:
    org = resp.json()["organization"]
    print(org["name"], org.get("estimated_num_employees"), org.get("industry"))
elif resp.status_code == 429:
    print("Rate limited; retry later:", resp.json())
else:
    print(resp.status_code, resp.text)