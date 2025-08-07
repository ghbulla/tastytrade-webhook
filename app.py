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

# ✅ Pick closest expiration to 21 DTE that actually has options (probes a few dates)
def get_closest_expiration(symbol, token):
    # 1) Get all expirations from nested (same as before)
    url = f"{BASE_URL}/option-chains/{symbol}/nested"
    headers = {'Authorization': f'Bearer {token}'}
    r = SESSION.get(url, headers=headers)
    _raise_for_status_with_context(r, "expirations_fetch_failed")

    data = r.json().get('data', {})
    items = data.get('items', [])
    if not items:
        raise Exception(f"No option chain items found for {symbol}")

    # collect all expiration dates in list form
    expirations = []
    for chain in items:
        for exp in chain.get('expirations', []):
            d = exp.get('expiration-date')
            if d:
                expirations.append(d)

    if not expirations:
        raise Exception(f"No expirations found for {symbol}")

    # 2) Sort by proximity to 21 DTE
    from datetime import datetime
    from dateutil import parser as _parser
    today = datetime.now()
    target_dte = 21

    expirations_sorted = sorted(
        set(expirations),
        key=lambda d: abs((_parser.parse(d) - today).days - target_dte)
    )

    # 3) Probe each candidate (closest first) until one returns options
    for exp in expirations_sorted[:6]:  # check a few closest to keep it quick
        url_nested = f"{BASE_URL}/option-chains/{symbol}/nested"
        params = {'expiration-date': exp, 'include-quotes': True}
        r2 = SESSION.get(url_nested, headers=headers, params=params)
        _raise_for_status_with_context(r2, "nested_probe_failed")

        items2 = r2.json().get('data', {}).get('items', [])
        total_opts = 0
        for row in items2:
            total_opts += len(row.get('options', []))

        if total_opts > 0:
            return exp

    # If none of the closest few had options, just fall back to the very first available
    return expirations_sorted[0]


# ✅ Find options closest to 30 delta (read greeks from option.quote.greeks)
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
            q = option.get('quote') or {}
            greeks = q.get('greeks') or {}
            delta = greeks.get('delta')
            if delta is None:
                continue

            opt_type = option.get('option_type')  # 'P' or 'C'
            if opt_type == 'P':
                puts.append((option, q, delta))
            elif opt_type == 'C':
                calls.append((option, q, delta))

    if not puts or not calls:
        raise Exception(f"Insufficient options with greeks for {symbol} @ {expiration}")

    closest_put = min(puts, key=lambda x: abs(abs(x[2]) - 0.30))
    closest_call = min(calls, key=lambda x: abs(abs(x[2]) - 0.30))

    put_opt, put_q, _ = closest_put
    call_opt, call_q, _ = closest_call

    return {
        "expiration": expiration,
        "put": {
            "strike": put_opt.get('strike-price'),
            "bid": put_q.get('bid'),
            "ask": put_q.get('ask'),
            "delta": (put_q.get('greeks') or {}).get('delta')
        },
        "call": {
            "strike": call_opt.get('strike-price'),
            "bid": call_q.get('bid'),
            "ask": call_q.get('ask'),
            "delta": (call_q.get('greeks') or {}).get('delta')
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

@app.route('/debug/nested-sample', methods=['GET'])
def nested_sample():
    try:
        symbol = request.args.get('symbol', 'AMAT')
        token = get_valid_access_token()
        # use the same expiration finder you already have
        expiration = get_closest_expiration(symbol, token)

        url = f"{BASE_URL}/option-chains/{symbol}/nested"
        headers = {'Authorization': f'Bearer {token}'}
        params = {'expiration-date': expiration, 'include-quotes': True}
        r = SESSION.get(url, headers=headers, params=params)
        _raise_for_status_with_context(r, "nested_chain_fetch_failed")

        data = r.json().get('data', {})
        items = data.get('items', [])

        total_options = 0
        with_greeks = 0
        examples = []

        for strike_data in items:
            for opt in strike_data.get('options', []):
                total_options += 1
                q = opt.get('quote') or {}
                greeks = q.get('greeks') or {}
                delta = greeks.get('delta')
                if delta is not None:
                    with_greeks += 1
                    # collect a few examples to see structure
                    if len(examples) < 5:
                        examples.append({
                            "option_type": opt.get("option_type"),
                            "strike": opt.get("strike-price"),
                            "bid": q.get("bid"),
                            "ask": q.get("ask"),
                            "delta": delta
                        })

        return jsonify({
            "symbol": symbol,
            "expiration": expiration,
            "items_count": len(items),
            "total_options_seen": total_options,
            "options_with_greeks": with_greeks,
            "examples": examples[:5]
        }), 200

    except requests.HTTPError as e:
        return jsonify({"error": "HTTPError", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/debug/nested-raw', methods=['GET'])
def nested_raw():
    try:
        symbol = request.args.get('symbol', 'AMAT')
        token = get_valid_access_token()
        exp = get_closest_expiration(symbol, token)

        url = f"{BASE_URL}/option-chains/{symbol}/nested"
        headers = {'Authorization': f'Bearer {token}'}
        params = {'expiration-date': exp, 'include-quotes': True}
        r = SESSION.get(url, headers=headers, params=params)
        _raise_for_status_with_context(r, "nested_chain_fetch_failed")

        return jsonify({
            "symbol": symbol,
            "expiration": exp,
            "status_code": r.status_code,
            "url": r.url,
            "body_head": r.text[:2000]  # first 2000 chars only
        }), 200
    except requests.HTTPError as e:
        return jsonify({"error": "HTTPError", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
