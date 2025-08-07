from flask import Flask, request, jsonify
import requests
from datetime import datetime
from dateutil import parser
import os

app = Flask(__name__)

# Set these as environment variables in Render
CLIENT_ID = os.environ.get('TT_CLIENT_ID')
CLIENT_SECRET = os.environ.get('TT_CLIENT_SECRET')
REDIRECT_URI = "https://tastytrade-webhook.onrender.com/callback"

# Store tokens in memory (reset on redeploy)
tokens = {}

def exchange_code_for_token(auth_code):
    url = "https://api.tastytrade.com/oauth/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    payload = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI
    }
    response = requests.post(url, data=payload, headers=headers)
    response.raise_for_status()
    return response.json()

def refresh_access_token(refresh_token):
    url = "https://api.tastytrade.com/oauth/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI
    }
    response = requests.post(url, data=payload, headers=headers)
    response.raise_for_status()
    return response.json()

def get_access_token():
    if not os.path.exists("tokens.json"):
        raise Exception("Token file not found. Please visit /authorize to authenticate.")

    with open("tokens.json", "r") as f:
        tokens = json.load(f)

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token or not refresh_token:
        raise Exception("Access or refresh token missing. Please re-authenticate via /authorize.")

    return access_token, refresh_token

@app.route('/')
def home():
    return '✅ Tastytrade Webhook is Running!'

@app.route('/authorize')
def authorize_link():
    auth_url = (
        f"https://my.tastytrade.com/auth.html?"
        f"response_type=code&client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}"
    )
    return f'👉 <a href="{auth_url}" target="_blank">Click here to authorize</a>'

@app.route('/callback')
def oauth_callback():
    auth_code = request.args.get('code')
    if not auth_code:
        return "❌ Authorization code not found", 400

    try:
        token_data = exchange_code_for_token(auth_code)
        tokens["access_token"] = token_data["access_token"]
        tokens["refresh_token"] = token_data["refresh_token"]
        return "✅ Tokens received and stored. You can now use /fetch"
    except Exception as e:
        return f"❌ Failed to get tokens: {str(e)}", 500

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
