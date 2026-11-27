import requests

url = "https://api.apollo.io/api/v1/organizations/enrich?domain=apollo.io"

headers = {
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "accept": "application/json",
    "x-api-key": "r7Mmg98BAx34nekHEKteig"
}

response = requests.get(url, headers=headers)

print(response.text)