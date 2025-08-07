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

# ---- API base + token endpoints (tastyworks per docs) ----
BASE_URL = "https://api.tastyworks.com"
TOKEN_URL = f"{BASE_URL}/oauth/token"

# ---- Requests session with required headers ----
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "wheelwatchlist/1.0",   # required
    "Accept": "application/json"
})

def _raise_for_status_with_context(resp, context):
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Surface URL, status, and text for easier debugging
        raise requests.HTTPError(
            f"{context} | url={resp.request.method} {resp.url} | "
            f"status={resp.status_code} | body={resp.text}"
        )

# 🔐 Step 1: Redirect user to Tastytrade auth
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
        _raise_for_status_with_context(r, "token_exchange_failed")

        tokens = r.json()
        return jsonify({
            "message": "✅ Tokens received. Please add these to Render ENV.",
            "access_token": tokens.get('access_token'),
            "refresh_token": tokens.get('refresh_token')
        }), 200
    except requests.HTTPError as e:
        return jsonify({"error": "Failed to get tokens", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "Exception during token exchange", "details": str(e)}), 500

# ✅ Automatically refresh token if expired
def get_valid_access_token():
    global ACCESS_TOKEN, REFRESH_TOKEN

    # If we have an access token, test it
    if ACCESS_TOKEN:
        test = SESSION.get(
            f"{BASE_URL}/customers/me/accounts",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )
        if test.status_code == 200:
            return ACCESS_TOKEN
        # else fall through to refresh

    # Refresh with refresh token (per docs)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_secret": CLIENT_SECRET,
        # docs only require client_secret for refresh; leaving out client_id
    }
    r = SESSION.post(TOKEN_URL, data=data)
    _raise_for_status_with_context(r, "token_refresh_failed")

    tokens = r.json()
    ACCESS_TOKEN = tokens.get("access_token")
    REFRESH_TOKEN = tokens.get("refresh_token") or REFRESH_TOKEN
    return ACCESS_TOKEN

# ✅ GET closest expiration to 21 DTE  — fixed endpoint
def get_closest_expiration(symbol, token):
    url = f"{BASE_URL}/option-chains/{symbol}"   # was .../expiration-and-strikes (wrong)
    headers = {'Authorization': f'Bearer {token}'}
    response = SESSION.get(url, headers=headers)
    _raise_for_status_with_context(response, "expirations_fetch_failed")

    payload = response.json()
    expirations = payload.get('data', {}).get('expirations', [])
    if not expirations:
        raise Exception(f"No expirations found for {symbol}")

    today = datetime.now()
    target_dte = 21
    closest = min(
        expirations,
        key=lambda exp: abs((parser.parse(exp) - today).days - target_dte)
    )
    return closest

# ✅ Find options closest to 30 delta
def find_30_delta_options(symbol, expiration, token):
    url = f"{BASE_URL}/option-chains/{symbol}/nested"
    headers = {'Authorization': f'Bearer {token}'}
    params = {'expiration-date': expiration, 'include-quotes': True}
    response = SESSION.get(url, headers=headers, params=params)
    _raise_for_status_with_context(response, "nested_chain_fetch_failed")

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

# 🔎 New: quick token validity & refresh probe
@app.route('/debug/token-status', methods=['GET'])
def token_status():
    try:
        token = get_valid_access_token()
        probe = SESSION.get(
            f"{BASE_URL}/customers/me/accounts",
            headers={"Authorization": f"Bearer {token}"}
        )
        return jsonify({
            "ok": probe.status_code == 200,
            "status_code": probe.status_code,
            "url": f"{BASE_URL}/customers/me/accounts",
            "body": probe.text[:500]
        }), 200
    except requests.HTTPError as e:
        return jsonify({"ok": False, "where": "token_status_http_error", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "where": "token_status_exception", "details": str(e)}), 500

@app.route('/fetch', methods=['POST'])
def fetch_data():
    try:
        data = request.get_json() or {}
        symbol = data.get('symbol')
        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400

        # Step 1: token
        token = get_valid_access_token()

        # Step 2: expiration
        try:
            expiration = get_closest_expiration(symbol, token)
        except requests.HTTPError as e:
            return jsonify({"error": "expirations_fetch_failed", "details": str(e)}), 500

        # Step 3: nested chain (30Δ legs)
        try:
            result = find_30_delta_options(symbol, expiration, token)
        except requests.HTTPError as e:
            return jsonify({"error": "nested_chain_fetch_failed", "details": str(e)}), 500

        return jsonify(result), 200

    except requests.HTTPError as http_err:
        # Catch-all for anything not explicitly wrapped above
        return jsonify({"error": "HTTPError", "details": str(http_err)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
