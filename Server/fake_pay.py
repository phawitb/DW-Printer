import requests
import json


api_url = "https://e6ae93928e64.ngrok-free.app/pay_completed"
ref_id_to_update = "Ud4182b71d88670ab7c347c6fcf6752c6_1756113420.19174"

# The data to be sent in the request body
payload = {
    "ref_id": ref_id_to_update
}

headers = {
    "Content-Type": "application/json"
}

try:
    response = requests.post(api_url, data=json.dumps(payload), headers=headers)

    if response.status_code == 200:
        print("✅ Request successful!")
        print("Response:", response.json())
    else:
        print(f"❌ Request failed with status code: {response.status_code}")
        print("Response:", response.text)

except requests.exceptions.RequestException as e:
    print(f"An error occurred: {e}")