import os
import logging
import alpaca_trade_api as tradeapi
from flask import Flask, request, jsonify
from flask_cors import CORS
from decimal import Decimal, getcontext # Import getcontext for precision setting
import math
import json # Added for logging full JSON data, good for debugging webhook payloads

# Set Decimal precision - Good practice for financial calculations
getcontext().prec = 10 

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log')
    ]
)

app = Flask(__name__)
CORS(app)

# --- Alpaca API Setup ---
# Directly using your Alpaca API key and secret (hard-coded for testing purposes)
API_KEY = 'PKEBE9SZ9SBF38BCV2MO'  # Replace with your actual API key
API_SECRET = 'KGHVSTQi9cCqg0qkNUHFAHmhswdcDCjJW7EJxlnq'  # Replace with your actual API secret
BASE_URL = 'https://paper-api.alpaca.markets'  # Paper trading URL

# Initialize Alpaca API client
api = None
if not API_KEY or not API_SECRET:
    logging.error("ALPACA_API_KEY or ALPACA_SECRET_KEY environment variables are not set. API will not be initialized.")
else:
    try:
        api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')
        api.get_account()  # Test connection to Alpaca
        logging.info("Alpaca API initialized and connected successfully.")
    except Exception as e:
        logging.error(f"Failed to connect to Alpaca API: {e}", exc_info=True)
        api = None

# --- Constants for Risk Management ---
RISK_DOLLAR = Decimal('1000.0')
STOP_LOSS_PERCENT = Decimal('0.005')
TAKE_PROFIT_RATIO = Decimal('2.0')
MAX_CAPITAL_ALLOCATION = Decimal('10000.0')

# --- Home Route (Health check) ---
@app.route('/')
def home():
    return "Welcome to the Flask Webhook App! Listening for TradingView alerts."

# --- Main Webhook Route ---
@app.route('/webhook', methods=['POST'])
def webhook():
    if api is None:
        logging.error("Alpaca API not initialized. Cannot process webhook.")
        return jsonify({"error": "Server not configured for trading."}), 500

    data = request.get_json()  # Get the webhook payload
    logging.info(f"Received webhook data: {json.dumps(data)}")

    # Extract data from the webhook
    symbol = data.get('symbol')
    entry_price_raw = data.get('price')
    action = data.get('action')

    # Validate data
    if not all([symbol, entry_price_raw, action]):
        logging.error("Missing required data in webhook payload.")
        return jsonify({"error": "Missing required data (symbol, price, or action)"}), 400

    try:
        entry_price = Decimal(str(entry_price_raw))
        if entry_price <= 0:
            logging.error(f"Invalid entry_price ({entry_price}) for {symbol}.")
            return jsonify({"error": "Invalid entry price."}), 400
    except Exception as e:
        logging.error(f"Error converting entry_price '{entry_price_raw}' for {symbol}: {e}")
        return jsonify({"error": "Invalid price format."}), 400

    if action not in ['buy', 'sell']:
        logging.error(f"Invalid action '{action}' for {symbol}.")
        return jsonify({"error": "Invalid action."}), 400

    side = 'buy' if action == 'buy' else 'sell'
    logging.info(f"Processing '{action}' signal for {symbol} at price: {entry_price}")

    try:
        # Calculate Stop Loss and Take Profit
        if side == 'buy':
            stop_loss_price = entry_price * (Decimal('1') - STOP_LOSS_PERCENT)
            take_profit_price = entry_price * (Decimal('1') + (STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO))
            stop_loss_limit_price = stop_loss_price * Decimal('0.99')
        else:
            stop_loss_price = entry_price * (Decimal('1') + STOP_LOSS_PERCENT)
            take_profit_price = entry_price * (Decimal('1') - (STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO))
            stop_loss_limit_price = stop_loss_price * Decimal('1.01')

        # Basic validation for stop-loss and take-profit prices
        if stop_loss_price <= 0 or take_profit_price <= 0:
            logging.error(f"Invalid SL/TP prices: SL={stop_loss_price}, TP={take_profit_price}")
            return jsonify({"error": "Invalid stop loss or take profit price."}), 400

        stop_loss_dist = abs(entry_price - stop_loss_price)
        if stop_loss_dist == 0:
            logging.error("Stop loss distance is zero.")
            return jsonify({"error": "Invalid stop loss distance."}), 400

        pos_size_risk_based = RISK_DOLLAR / stop_loss_dist
        max_position_size = MAX_CAPITAL_ALLOCATION / entry_price

        final_qty = math.floor(min(pos_size_risk_based, max_position_size))

        if final_qty <= 0:
            logging.warning(f"Calculated position size is zero for {symbol}.")
            return jsonify({"message": f"Position size is zero for {symbol}."}), 200

        logging.info(f"Final Quantity for {symbol}: {final_qty} shares. SL={stop_loss_price:.4f}, TP={take_profit_price:.4f}")

        # Submit Bracket Order
        bracket_order = api.submit_order(
            symbol=symbol,
            qty=int(final_qty),
            side=side,
            type='market',
            time_in_force='gtc',
            order_class='bracket',
            stop_loss={
                'stop_price': float(stop_loss_price),
                'limit_price': float(stop_loss_limit_price)
            },
            take_profit={
                'limit_price': float(take_profit_price)
            }
        )

        logging.info(f"Bracket order submitted for {symbol} (Order ID: {bracket_order.id}).")
        return jsonify({
            "message": f"Order placed for {symbol} with SL={stop_loss_price:.4f} and TP={take_profit_price:.4f}",
            "order_id": bracket_order.id
        }), 200

    except tradeapi.rest.APIError as e:
        logging.error(f"API Error: {e}")
        return jsonify({"error": f"API Error: {e.status_code}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
        return jsonify({"error": "Unexpected error."}), 500

# --- Application Entry Point ---
if __name__ == "__main__":
    app.run(debug=False, host='0.0.0.0', port=5000)
