from flask import Flask, request, jsonify
import requests
import json
import datetime
import pytz

app = Flask(__name__)

# Global variable to store the session token
session_token = None

# Function to log in to Tastytrade API
def login_to_tastytrade():
    global session_token
    url = "https://api.tastyworks.com/sessions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "login": "ghbulla@gmail.com",     # <-- UPDATE THIS
        "password": "Hector0292!$" # <-- UPDATE THIS
    }

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 201:
        session_token = response.json()["data"]["session-token"]
        return True
    else:
        print("Login failed:", response.text)
        return False

@app.route('/')
def home():
    return '✅ Tastytrade Webhook is Running!'

@app.route('/fetch', methods=['POST'])
def fetch_data():
    global session_token
    data = request.get_json()
    symbol = data.get("symbol")
    if not symbol:
        return jsonify({"error": "No symbol provided"}), 400

    # Ensure we are logged in
    if not session_token and not login_to_tastytrade():
        return jsonify({"error": "Login to Tastytrade failed"}), 500

    headers = {
        "Authorization": f"Bearer {session_token}"
    }

    # Step 1: Get Expiration Dates
    expiration_url = f"https://api.tastyworks.com/option-chains/{symbol}/expiration-and-strikes"
    exp_response = requests.get(expiration_url, headers=headers)
    if exp_response.status_code != 200:
        return jsonify({"error": f"Failed to get expirations: {exp_response.text}"}), 500

    expirations = exp_response.json().get("expirations", [])
    if not expirations:
        return jsonify({"error": "No expiration dates found"}), 404

    # Step 2: Find expiration closest to 21 DTE
    today = datetime.datetime.now(pytz.timezone("US/Eastern")).date()
    closest_exp = min(expirations, key=lambda x: abs((datetime.datetime.strptime(x, "%Y-%m-%d").date() - today).days))

    # Step 3: Get option chain
    chain_url = f"https://api.tastyworks.com/option-chains/{symbol}/nested"
    params = {
        "expiration": closest_exp,
        "includeStrategies": "true"
    }

    chain_response = requests.get(chain_url, headers=headers, params=params)
    if chain_response.status_code != 200:
        return jsonify({"error": f"Failed to get option chain: {chain_response.text}"}), 500

    options = chain_response.json().get("data", {}).get("items", [])
    if not options:
        return jsonify({"error": "No options found"}), 404

    # Step 4: Find ATM strike
    underlying_price = float(chain_response.json().get("data", {}).get("underlying-price", 0))
    closest_call = None
    closest_put = None
    min_call_diff = float('inf')
    min_put_diff = float('inf')

    for item in options:
        option_type = item.get("instrument-type")
        strike = float(item.get("strike-price"))
        bid = item.get("bid")
        ask = item.get("ask")

        if None in (bid, ask):
            continue

        if option_type == "CALL":
            diff = abs(strike - underlying_price)
            if diff < min_call_diff:
                min_call_diff = diff
                closest_call = {"strike": strike, "bid": bid, "ask": ask}
        elif option_type == "PUT":
            diff = abs(strike - underlying_price)
            if diff < min_put_diff:
                min_put_diff = diff
                closest_put = {"strike": strike, "bid": bid, "ask": ask}

    return jsonify({
        "symbol": symbol,
        "underlying_price": underlying_price,
        "expiration": closest_exp,
        "call": closest_call,
        "put": closest_put
    }), 200
