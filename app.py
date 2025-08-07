from flask import Flask, request, jsonify, redirect
import requests
import os
from urllib.parse import urlencode

app = Flask(__name__)

# ⬇️ Store your client ID and secret in Render's ENV vars (Settings > Environment)
CLIENT_ID = os.getenv("TT_CLIENT_ID")
CLIENT_SECRET = os.getenv("TT_CLIENT_SECRET")

# ⬇️ Access and refresh tokens will be stored in environment as well
ACCESS_TOKEN = os.getenv("TT_ACCESS_TOKEN")
REFRESH_TOKEN = os.getenv("TT_REFRESH_TOKEN")

# OAuth2 redirect URI
REDIRECT_URI = "https://tastytrade-webhook.onrender.com/authorize/callback"

# 🔐 Step 1: Redirect user to tastytrade auth
@app.route("/authorize")
def authorize():
    auth_url = (
        "https://my.tastytrade.com/auth.html?"
        + urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "read",
        })
    )
    return redirect(auth_url)


# 🔐 Step 2: Callback to exchange code for tokens
@app.route("/authorize/callback")
def callback():
    code = request.args.get("code")
    token_url = "https://api.tastytrade.com/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI
    }
    response = requests.post(token_url, data=data)
    if response.status_code != 200:
        return jsonify({"error": "Failed to get tokens", "details": response.text}), 500

    tokens = response.json()
    return jsonify({
        "message": "✅ Tokens received. Please add these to Render ENV.",
        "access_token": tokens['access_token'],
        "refresh_token": tokens['refresh_token']
    })

# ✅ Automatically refresh token if expired
def get_valid_access_token():
    global ACCESS_TOKEN, REFRESH_TOKEN

    # Try using current access token
    test = requests.get(
        "https://api.tastytrade.com/accounts",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
    )
    if test.status_code == 200:
        return ACCESS_TOKEN

    # Refresh token
    data = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    r = requests.post("https://api.tastytrade.com/oauth2/token", data=data)
    if r.status_code != 200:
        raise Exception("Failed to refresh access token: " + r.text)

    tokens = r.json()
    ACCESS_TOKEN = tokens["access_token"]
    REFRESH_TOKEN = tokens["refresh_token"]

    # ➕ Now update the Render environment manually with new tokens
    return ACCESS_TOKEN

# ✅ GET closest expiration to 21 DTE
from datetime import datetime
from dateutil import parser

def get_closest_expiration(symbol, token):
    url = f"https://api.tastytrade.com/option-chains/{symbol}/expiration-and-strikes"
    headers = {'Authorization': f'Bearer {token}'}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    expirations = response.json()['data']['expirations']
    today = datetime.now()
    target_dte = 21
    return min(expirations, key=lambda exp: abs((parser.parse(exp) - today).days - target_dte))

# ✅ Find options closest to 30 delta
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
        token = get_valid_access_token()
        expiration = get_closest_expiration(symbol, token)
        result = find_30_delta_options(symbol, expiration, token)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
