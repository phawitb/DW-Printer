import requests
import json

# Replace with the actual URL of your FastAPI application
# For local testing:
# api_url = "http://127.0.0.1:8000/pay_completed"
# For a deployed app (e.g., on Render):
api_url = "https://283d6329f959.ngrok-free.app/pay_completed"

# Replace 'your_reference_id' with the actual ref_id of a payment document
# from your MongoDB, which currently has a status of "waiting".
# You can get this ID from the console output of the /generate_qr endpoint.
ref_id_to_update = "Ud4182b71d88670ab7c347c6fcf6752c6_1756032156.058502"

# The data to be sent in the request body
payload = {
    "ref_id": ref_id_to_update
}

# Set the headers to specify that the body is in JSON format
headers = {
    "Content-Type": "application/json"
}

try:
    # Send the POST request
    response = requests.post(api_url, data=json.dumps(payload), headers=headers)

    # Check the response status code and content
    if response.status_code == 200:
        print("✅ Request successful!")
        print("Response:", response.json())
    else:
        print(f"❌ Request failed with status code: {response.status_code}")
        print("Response:", response.text)

except requests.exceptions.RequestException as e:
    print(f"An error occurred: {e}")