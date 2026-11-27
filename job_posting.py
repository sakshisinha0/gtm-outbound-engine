import requests

from config import APOLLO_API_KEY

url = "https://api.apollo.io/api/v1/organizations/5e66b6381e05b4008c8331b8/job_postings"

headers = {
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "accept": "application/json",
    "x-api-key": APOLLO_API_KEY
}

response = requests.get(url, headers=headers)

print(response.text)