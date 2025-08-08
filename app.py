from flask import Flask, request, jsonify, redirect
import requests
import os
from urllib.parse import urlencode
from datetime import datetime
from dateutil import parser
import json
import time

# NEW: websocket client for DxLink
from websocket import create_connection, WebSocketTimeoutException

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
    }
    r = SESSION.post(TOKEN_URL, data=data)
    _raise_for_status_with_context(r, "token_refresh_failed")

    tokens = r.json()
    ACCESS_TOKEN = tokens.get("access_token")
    REFRESH_TOKEN = tokens.get("refresh_token") or REFRESH_TOKEN
    return ACCESS_TOKEN

# ✅ Get an API Quote Token for DxLink
def get_api_quote_token(access_token):
    r = SESSION.get(
        f"{BASE_URL}/api-quote-tokens",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    _raise_for_status_with_context(r, "api_quote_token_failed")
    payload = r.json().get("data", {})
    return payload.get("token"), payload.get("dxlink-url")

# ✅ Find the closest expiration that actually has strikes (via nested)
def get_closest_expiration(symbol, token):
    url = f"{BASE_URL}/option-chains/{symbol}/nested"
    headers = {'Authorization': f'Bearer {token}'}
    r = SESSION.get(url, headers=headers)
    _raise_for_status_with_context(r, "expirations_fetch_failed")

    data = r.json().get('data', {})
    items = data.get('items', [])
    if not items:
        raise Exception(f"No option chain items found for {symbol}")

    expirations = []
    for chain in items:
        for exp in chain.get('expirations', []):
            d = exp.get('expiration-date')
            if d:
                expirations.append(d)
    if not expirations:
        raise Exception(f"No expirations found for {symbol}")

    today = datetime.now()
    target_dte = 21
    expirations_sorted = sorted(
        set(expirations),
        key=lambda d: abs((parser.parse(d) - today).days - target_dte)
    )

    # Probe a few closest, pick the first that has strikes listed for that exp
    headers_q = {'Authorization': f'Bearer {token}'}
    for exp in expirations_sorted[:6]:
        r2 = SESSION.get(
            f"{BASE_URL}/option-chains/{symbol}/nested",
            headers=headers_q,
            params={'expiration-date': exp}
        )
        _raise_for_status_with_context(r2, "nested_probe_failed")
        items2 = r2.json().get('data', {}).get('items', [])
        has_any = False
        for row in items2:
            if row.get('expirations'):
                # when filtered by expiration-date, there is usually one row with strikes for that expiration
                for e in row.get('expirations', []):
                    if e.get('expiration-date') == exp and e.get('strikes'):
                        has_any = True
                        break
            if has_any:
                break
        if has_any:
            return exp

    # Fallback to the closest date even if it looked empty
    return expirations_sorted[0]

# ✅ Collect streamer symbols for all strikes of that expiration
def get_streamer_symbols_for_expiration(symbol, expiration, token):
    url = f"{BASE_URL}/option-chains/{symbol}/nested"
    headers = {'Authorization': f'Bearer {token}'}
    params = {'expiration-date': expiration}
    r = SESSION.get(url, headers=headers, params=params)
    _raise_for_status_with_context(r, "nested_for_symbols_failed")

    items = r.json().get('data', {}).get('items', [])
    if not items:
        raise Exception(f"No option data found for {symbol} @ {expiration}")

    # Build maps: streamer_symbol -> strike, and also separate puts/calls lists
    put_streamers = []
    call_streamers = []
    sym_to_strike = {}

    for chain in items:
        for exp in chain.get('expirations', []):
            if exp.get('expiration-date') != expiration:
                continue
            for s in exp.get('strikes', []):
                strike = float(s.get('strike-price'))
                call_stream = s.get('call-streamer-symbol')
                put_stream = s.get('put-streamer-symbol')
                if call_stream:
                    call_streamers.append(call_stream)
                    sym_to_strike[call_stream] = strike
                if put_stream:
                    put_streamers.append(put_stream)
                    sym_to_strike[put_stream] = strike

    if not put_streamers or not call_streamers:
        raise Exception(f"No streamer symbols found for {symbol} @ {expiration}")

    return put_streamers, call_streamers, sym_to_strike

# ✅ Subscribe via DxLink and gather Quote + Greeks quickly
def dxlink_fetch_quotes_and_greeks(dx_url, dx_token, symbols, timeout_sec=3.0):
    # Open websocket
    ws = create_connection(dx_url, timeout=10)
    ws.settimeout(1.0)

    def send(obj):
        ws.send(json.dumps(obj))

    # 1) SETUP
    send({"type": "SETUP", "channel": 0, "version": "wheelwatchlist/1.0",
          "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})

    # Expect AUTH_STATE -> UNAUTHORIZED (we’ll auth immediately anyway)
    # 2) AUTH
    send({"type": "AUTH", "channel": 0, "token": dx_token})

    # 3) CHANNEL_REQUEST
    FEED_CH = 3
    send({"type": "CHANNEL_REQUEST", "channel": FEED_CH,
          "service": "FEED", "parameters": {"contract": "AUTO"}})

    # 4) FEED_SETUP (request JSON format for easier parsing)
    send({
        "type": "FEED_SETUP",
        "channel": FEED_CH,
        "acceptAggregationPeriod": 0.1,
        "acceptDataFormat": "JSON",
        "acceptEventFields": {
            "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
            "Greeks": ["eventType", "eventSymbol", "volatility", "delta", "gamma", "theta", "rho", "vega"]
        }
    })

    # 5) FEED_SUBSCRIPTION
    add_list = []
    for s in symbols:
        add_list.append({"type": "Quote", "symbol": s})
        add_list.append({"type": "Greeks", "symbol": s})

    send({"type": "FEED_SUBSCRIPTION", "channel": FEED_CH, "reset": True, "add": add_list})

    # Gather data for a short window
    quotes = {}   # symbol -> {"bid":..., "ask":...}
    greeks = {}   # symbol -> {"delta":...}
    t_end = time.time() + timeout_sec

    try:
        while time.time() < t_end:
            try:
                raw = ws.recv()
            except WebSocketTimeoutException:
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue

            # Expect FEED_DATA with "data": list of events or single event object
            if msg.get("type") == "FEED_DATA":
                data = msg.get("data")
                if not data:
                    continue

                # DXLink "JSON" format may deliver either a single event object or a list
                # Normalize to list of event dicts
                events = []
                if isinstance(data, list):
                    # Each element might already be an object like {"eventType":"Quote",...}
                    for ev in data:
                        if isinstance(ev, dict) and ev.get("eventType") in ("Quote", "Greeks"):
                            events.append(ev)
                elif isinstance(data, dict):
                    if data.get("eventType") in ("Quote", "Greeks"):
                        events.append(data)

                for ev in events:
                    et = ev.get("eventType")
                    es = ev.get("eventSymbol")
                    if not es:
                        continue
                    if et == "Quote":
                        bp = ev.get("bidPrice")
                        ap = ev.get("askPrice")
                        if bp is not None or ap is not None:
                            q = quotes.get(es, {})
                            if bp is not None:
                                q["bid"] = bp
                            if ap is not None:
                                q["ask"] = ap
                            quotes[es] = q
                    elif et == "Greeks":
                        d = ev.get("delta")
                        if d is not None:
                            greeks[es] = {"delta": d}

            # Early exit if we already have coverage for all symbols
            # (both quote + greeks present)
            complete = True
            for s in symbols:
                if s not in greeks or s not in quotes:
                    complete = False
                    break
            if complete:
                break
    finally:
        try:
            ws.close()
        except Exception:
            pass

    return quotes, greeks

# ✅ Find options closest to 30 delta using DxLink for quotes + greeks
def find_30_delta_options(symbol, expiration, token):
    # 1) get streamer symbols for this expiration
    put_syms, call_syms, sym_to_strike = get_streamer_symbols_for_expiration(symbol, expiration, token)

    # 2) get api quote token + dxlink url
    dx_token, dx_url = get_api_quote_token(token)
    if not dx_token or not dx_url:
        raise Exception("Failed to obtain DxLink token/url")

    # 3) subscribe via DxLink and collect quick snapshot
    symbols = put_syms + call_syms
    quotes, greeks = dxlink_fetch_quotes_and_greeks(dx_url, dx_token, symbols, timeout_sec=3.0)

    # 4) pick closest to 0.30 |delta| for each side, using only symbols we have greeks for
    def pick_closest(sym_list):
        best = None
        best_abs = 999
        for s in sym_list:
            g = greeks.get(s)
            if not g or g.get("delta") is None:
                continue
            d = abs(abs(float(g["delta"])) - 0.30)
            if d < best_abs:
                best_abs = d
                best = s
        return best

    best_put_sym = pick_closest(put_syms)
    best_call_sym = pick_closest(call_syms)

    if not best_put_sym or not best_call_sym:
        raise Exception(f"Insufficient options with greeks for {symbol} @ {expiration}")

    def pack(side_sym):
        q = quotes.get(side_sym, {})
        return {
            "strike": sym_to_strike.get(side_sym),
            "bid": q.get("bid"),
            "ask": q.get("ask"),
            "delta": greeks.get(side_sym, {}).get("delta")
        }

    return {
        "expiration": expiration,
        "put": pack(best_put_sym),
        "call": pack(best_call_sym)
    }

@app.route('/')
def home():
    return '✅ Tastytrade Webhook is Running!'

# 🔎 Debug: token validity
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

# 🔎 Debug: nested raw (kept)
@app.route('/debug/nested-raw', methods=['GET'])
def nested_raw():
    try:
        symbol = request.args.get('symbol', 'AMAT')
        token = get_valid_access_token()
        exp = get_closest_expiration(symbol, token)

        url = f"{BASE_URL}/option-chains/{symbol}/nested"
        headers = {'Authorization': f'Bearer {token}'}
        params = {'expiration-date': exp}
        r = SESSION.get(url, headers=headers, params=params)
        _raise_for_status_with_context(r, "nested_chain_fetch_failed")

        return jsonify({
            "symbol": symbol,
            "expiration": exp,
            "status_code": r.status_code,
            "url": r.url,
            "body_head": r.text[:2000]
        }), 200
    except requests.HTTPError as e:
        return jsonify({"error": "HTTPError", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 🔎 Debug: sample (kept)
@app.route('/debug/nested-sample', methods=['GET'])
def nested_sample():
    try:
        symbol = request.args.get('symbol', 'AMAT')
        token = get_valid_access_token()
        expiration = get_closest_expiration(symbol, token)

        url = f"{BASE_URL}/option-chains/{symbol}/nested"
        headers = {'Authorization': f'Bearer {token}'}
        params = {'expiration-date': expiration}
        r = SESSION.get(url, headers=headers, params=params)
        _raise_for_status_with_context(r, "nested_chain_fetch_failed")

        data = r.json().get('data', {})
        items = data.get('items', [])

        total_options = 0
        with_greeks = 0
        examples = []

        # quotes/greeks aren't included in REST; this stays as a structure probe
        for strike_data in items:
            for _exp in strike_data.get('expirations', []):
                for _s in _exp.get('strikes', []):
                    total_options += 2  # put + call placeholders

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
        return jsonify({"error": "HTTPError", "details": str(http_err)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
