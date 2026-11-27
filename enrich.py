import requests

from config import APOLLO_API_KEY

url = "https://api.apollo.io/api/v1/organizations/enrich?domain=apollo.io"

headers = {
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "accept": "application/json",
    "x-api-key": APOLLO_API_KEY
}

response = requests.get(url, headers=headers)

print(response.text)