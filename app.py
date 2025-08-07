from flask import Flask, request, jsonify, redirect
import requests
import os
from urllib.parse import urlencode
from datetime import datetime
from dateutil import parser

app = Flask(__name__)

# ⬇️ ENV VARS (unchanged names)
CLIENT_ID = os.getenv("TT_CLIENT_ID")
CLIENT_SECRET = os.getenv("TT_CLIENT_SECRET")
ACCESS_TOKEN = os.getenv("TT_ACCESS_TOKEN")
REFRESH_TOKEN = os.getenv("TT_REFRESH_TOKEN")

# OAuth2 redirect URI (unchanged)
REDIRECT_URI = "https://tastytrade-webhook.onrender.com/authorize/callback"

# ---- Requests session with required headers ----
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "wheelwatchlist/1.0",   # required by Tastytrade
    "Accept": "application/json"
})

# ---- Token endpoint (fixed path: /oauth/token) ----
TOKEN_URL = "https://api.tastytrade.com/oauth/token"

# 🔐 Step 1: Redirect user to Tastytrade auth (unchanged route & query params)
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

# 🔐 Step 2: Callback to exchange code for tokens (uses SESSION + fixed TOKEN_URL)
@app.route("/authorize/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing authorization code"}), 400
    try:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI
        }
        r = SESSION.post(TOKEN_URL, data=data)
        if r.status_code != 200:
            return jsonify({"error": "Failed to get tokens", "details": r.text}), 500

        tokens = r.json()
        return jsonify({
            "message": "✅ Tokens received. Please add these to Render ENV.",
            "access_token": tokens.get('access_token'),
            "refresh_token": tokens.get('refresh_token')
        }), 200
    except Exception as e:
        return jsonify({"error": "Exception during token exchange", "details": str(e)}), 500

# ✅ Automatically refresh token if expired (fixed URL + headers + correct probe endpoint)
def get_valid_access_token():
    global ACCESS_TOKEN, REFRESH_TOKEN

    # If we have an access token, test it
    if ACCESS_TOKEN:
        test = SESSION.get(
            "https://api.tastytrade.com/customers/me/accounts",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )
        if test.status_code == 200:
            return ACCESS_TOKEN
        # fall through to refresh

    # Refresh with refresh token
    data = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    r = SESSION.post(TOKEN_URL, data=data)
    if r.status_code != 200:
        raise Exception("Failed to refresh access token: " + r.text)

    tokens = r.json()
    ACCESS_TOKEN = tokens.get("access_token")
    REFRESH_TOKEN = tokens.get("refresh_token") or REFRESH_TOKEN  # some providers omit new RT

    # Note: ACCESS_TOKEN/REFRESH_TOKEN are updated in memory for this instance.
    # If you want persistence across restarts, manually update Render ENV with the values above.
    return ACCESS_TOKEN

# ✅ GET closest expiration to 21 DTE (unchanged logic; now uses SESSION)
def get_closest_expiration(symbol, token):
    url = f"https://api.tastytrade.com/option-chains/{symbol}/expiration-and-strikes"
    headers = {'Authorization': f'Bearer {token}'}
    response = SESSION.get(url, headers=headers)
    response.raise_for_status()

    payload = response.json()
    expirations = payload.get('data', {}).get('expirations', [])
    if not expirations:
        raise Exception(f"No expirations found for {symbol}")

    today = datetime.now()
    target_dte = 21
    closest = min(expirations, key=lambda exp: abs((parser.parse(exp) - today).days - target_dte))
    return closest

# ✅ Find options closest to 30 delta (unchanged logic; now uses SESSION)
def find_30_delta_options(symbol, expiration, token):
    url = f"https://api.tastytrade.com/option-chains/{symbol}/nested"
    headers = {'Authorization': f'Bearer {token}'}
    params = {'expiration-date': expiration, 'include-quotes': True}
    response = SESSION.get(url, headers=headers, params=params)
    response.raise_for_status()

    items = response.json().get('data', {}).get('items', [])
    if not items:
        raise Exception(f"No option data found for {symbol} @ {expiration}")

    puts = []
    calls = []
    for strike_data in items:
        for option in strike_data.get('options', []):
            greeks = option.get('greeks')
            if not greeks or greeks.get('delta') is None:
                continue
            if option.get('option_type') == 'P':
                puts.append(option)
            elif option.get('option_type') == 'C':
                calls.append(option)

    if not puts or not calls:
        raise Exception(f"Insufficient options with greeks for {symbol} @ {expiration}")

    closest_put = min(puts, key=lambda x: abs(abs(x['greeks']['delta']) - 0.30))
    closest_call = min(calls, key=lambda x: abs(abs(x['greeks']['delta']) - 0.30))

    return {
        "expiration": expiration,
        "put": {
            "strike": closest_put.get('strike-price'),
            "bid": closest_put.get('bid-price'),
            "ask": closest_put.get('ask-price'),
            "delta": closest_put['greeks'].get('delta')
        },
        "call": {
            "strike": closest_call.get('strike-price'),
            "bid": closest_call.get('bid-price'),
            "ask": closest_call.get('ask-price'),
            "delta": closest_call['greeks'].get('delta')
        }
    }

@app.route('/')
def home():
    return '✅ Tastytrade Webhook is Running!'

@app.route('/fetch', methods=['POST'])
def fetch_data():
    try:
        data = request.get_json() or {}
        symbol = data.get('symbol')
        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400

        token = get_valid_access_token()
        expiration = get_closest_expiration(symbol, token)
        result = find_30_delta_options(symbol, expiration, token)
        return jsonify(result), 200
    except requests.HTTPError as http_err:
        try:
            return jsonify({"error": "HTTPError", "details": http_err.response.text}), 500
        except Exception:
            return jsonify({"error": "HTTPError", "details": str(http_err)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
