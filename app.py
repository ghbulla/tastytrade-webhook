from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/')
def home():
    return '✅ Tastytrade Webhook is Running!'

@app.route('/fetch', methods=['POST'])
def fetch_data():
    data = request.get_json()
    symbol = data.get('symbol')
    return jsonify({"message": f"Received symbol {symbol}"}), 200
