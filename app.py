from flask import Flask, request, jsonify
import requests
from datetime import datetime
from dateutil import parser

app = Flask(__name__)

# REPLACE these with your actual Tastytrade credentials
USERNAME = 'ghbulla@gmail.com'
PASSWORD = 'Hector0292!$'

# Authenticate and get session token
def authenticate():
    url = "https://api.tastytrade.com/sessions"
    headers = {'Content-Type': 'application/json'}
    payload = {'login': USERNAME, 'password': PASSWORD}
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()['data']['session-token']

# Find expiration date closest to 21 DTE
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

# Find option with delta closest to 0.30
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
    return '✅ Tastytrade Webhook is Running!'

@app.route('/fetch', methods=['POST'])
def fetch_data():
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        token = authenticate()
        expiration = get_closest_expiration(symbol, token)
        result = find_30_delta_options(symbol, expiration, token)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
