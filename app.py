import json
import alpaca_trade_api as tradeapi
from flask import Flask, request, jsonify
from flask_cors import CORS
import math

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Alpaca API setup (make sure you set up your keys in the environment variables or directly)
API_KEY = 'PKEBE9SZ9SBF38BCV2MO'
API_SECRET = 'KGHVSTQi9cCqg0qkNUHFAHmhswdcDCjJW7EJxlnq'
BASE_URL = 'https://paper-api.alpaca.markets'  # Use for paper trading
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

# Constants for risk management (as in your TradingView strategy)
risk_dollar = 1000.0   # $1000 risk per trade
take_profit_multiplier = 2.0  # Take profit multiplier (2x)
stop_loss_multiplier = 1.0    # Stop loss multiplier (1x)

# This is your existing home route, it's fine to keep it
@app.route('/')
def home():
    return "Welcome to the Flask Webhook App!"

# This is the /webhook route where you'll handle the POST requests
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()  # Get the JSON data sent to the webhook

    print(f"Received webhook data: {data}")

    # Extract data from the webhook (symbol, quantity, price, action)
    symbol = data.get('symbol')
    qty = data.get('quantity')
    entry_price = data.get('price')  # Entry price sent in the alert data

    # Stop Loss and Take Profit calculation (based on TradingView's strategy)
    if symbol and entry_price and qty:
        # Calculate Stop Loss based on TradingView logic
        stop_loss_price = entry_price - stop_loss_multiplier
        take_profit_price = entry_price + take_profit_multiplier * (entry_price - stop_loss_price)

        # Round the stop loss and take profit prices to 2 decimal places
        stop_loss_price = round(stop_loss_price, 2)
        take_profit_price = round(take_profit_price, 2)

        # Adjust position size to match $1000 risk (adjust for slippage)
        stop_loss_dist = abs(entry_price - stop_loss_price)
        pos_size_risk_based = risk_dollar / stop_loss_dist  # Risk-based position size

        # Max position size based on $25,000 limit
        max_position_size = 25000 / entry_price

        # Final position size: the smaller value between risk-based size and the max position size
        pos_size = min(pos_size_risk_based, max_position_size)

        # Create the market order (Long or Short) and set the OCO (Stop-Loss & Take-Profit) orders
        try:
            # Submit the buy order
            market_order = api.submit_order(
                symbol=symbol,
                qty=math.floor(pos_size),  # Ensure integer qty for Alpaca
                side='buy',
                type='market',
                time_in_force='gtc'  # Good till canceled
            )

            # Place OCO (One Cancels Other) order: One for stop loss, one for take profit
            oco_order = api.submit_order(
                symbol=symbol,
                qty=math.floor(pos_size),
                side='sell',
                type='oco',  # OCO order type
                stop_loss={'stop_price': stop_loss_price, 'limit_price': stop_loss_price * 0.99},
                take_profit={'limit_price': take_profit_price},
                time_in_force='gtc'
            )

            return jsonify({
                "message": f"Buy order placed for {symbol} with OCO stop loss at {stop_loss_price} and take profit at {take_profit_price}"
            }), 200

        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "Missing required data (symbol, quantity, or price)"}), 400


if __name__ == "__main__":
    app.run(debug=True)
