from flask import Flask, request, jsonify
import requests
from datetime import datetime
from dateutil import parser

app = Flask(__name__)

# --- REPLACE with your actual OAuth2 credentials ---
CLIENT_ID = '784e2047-583f-4e47-8c41-283439346d07'
CLIENT_SECRET = '3fc915eae7624dd2a46b276316e09e049ee25c79'
REFRESH_TOKEN = 'eb3f57f5-00da-4502-b144-7545d43b5043'  # You got this when you authenticated

# --- Get new access token using refresh token ---
def get_access_token():
    url = "https://api.tastytrade.com/oauth2/token"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()['access_token']

# --- Find expiration date closest to 21 DTE ---
def get_closest_expiration(symbol, token):
    url = f"https://api.tastytrade.com/option-chains/{symbol}/expiration-and-strikes"
    headers = {'Authorization': f'Bearer {token}'}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    expirations = response.json()['data']['expirations']
    today = datetime.now()
    target_dte = 21

    closest_exp = min(
        expirations,
        key=lambda exp: abs((parser.parse(exp) - today).days - target_dte)
    )
    return closest_exp

# --- Find PUT and CALL options closest to 0.30 delta ---
def find_30_delta_options(symbol, expiration, token):
    url = f"https://api.tastytrade.com/option-chains/{symbol}/nested"
    headers = {'Authorization': f'Bearer {token}'}
    params = {'expiration-date': expiration, 'include-quotes': True}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    options = response.json()['data']['items']

    puts = []
    calls = []
    for strike_data in options:
        for option in strike_data['options']:
            if option['option_type'] == 'P' and option['greeks'] and option['greeks']['delta'] is not None:
                puts.append(option)
            elif option['option_type'] == 'C' and option['greeks'] and option['greeks']['delta'] is not None:
                calls.append(option)

    closest_put = min(puts, key=lambda x: abs(abs(x['greeks']['delta']) - 0.30))
    closest_call = min(calls, key=lambda x: abs(abs(x['greeks']['delta']) - 0.30))

    return {
        "expiration": expiration,
        "put": {
            "strike": closest_put['strike-price'],
            "bid": closest_put['bid-price'],
            "ask": closest_put['ask-price'],
            "delta": closest_put['greeks']['delta']
        },
        "call": {
            "strike": closest_call['strike-price'],
            "bid": closest_call['bid-price'],
            "ask": closest_call['ask-price'],
            "delta": closest_call['greeks']['delta']
        }
    }

@app.route('/')
def home():
    return '✅ Tastytrade Webhook is Running with OAuth2!'

@app.route('/fetch', methods=['POST'])
def fetch_data():
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        token = get_access_token()
        expiration = get_closest_expiration(symbol, token)
        result = find_30_delta_options(symbol, expiration, token)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
