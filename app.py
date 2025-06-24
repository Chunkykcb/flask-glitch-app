import os
import logging
import alpaca_trade_api as tradeapi
from flask import Flask, request, jsonify
from flask_cors import CORS
from decimal import Decimal, getcontext # Import getcontext for precision setting
import math
import json # Added for logging full JSON data, good for debugging webhook payloads

# Set Decimal precision - Good practice for financial calculations
# Adjust precision as needed (e.g., 10 for prices, 2 for currency amounts)
getcontext().prec = 10 

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,  # Set to INFO for production, DEBUG for verbose development logs
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Log to console (useful for Render logs)
        logging.FileHandler('app.log')  # Log to a file (will be stored on the Render instance)
    ]
)

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes (useful if you have a frontend interacting)

# --- Alpaca API Setup ---
# Load API keys securely from environment variables provided by Render
API_KEY = os.environ.get('ALPACA_API_KEY')
API_SECRET = os.environ.get('ALPACA_SECRET_KEY')
# Use the paper trading URL for testing. Change to 'https://api.alpaca.markets' for live trading.
BASE_URL = 'https://paper-api.alpaca.markets' 

# Initialize Alpaca API client
api = None # Initialize api to None, will be set after validation
if not API_KEY or not API_SECRET:
    logging.error("ALPACA_API_KEY or ALPACA_SECRET_KEY environment variables are not set. API will not be initialized.")
else:
    try:
        api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')
        # Optional: Test connection to Alpaca (good for startup checks)
        # This will raise an exception if credentials are bad or connection fails
        api.get_account() 
        logging.info("Alpaca API initialized and connected successfully.")
    except Exception as e:
        logging.error(f"Failed to connect to Alpaca API: {e}", exc_info=True)
        api = None # Ensure api remains None if connection fails

# --- Constants for Risk Management ---
# Define constants as Decimal for consistent precision
RISK_DOLLAR = Decimal('1000.0')   # Max dollar amount to risk per trade
STOP_LOSS_PERCENT = Decimal('0.005') # 0.5% stop loss relative to entry price
TAKE_PROFIT_RATIO = Decimal('2.0')   # Take profit target is 2 times the risk distance
MAX_CAPITAL_ALLOCATION = Decimal('25000.0') # Max total capital to allocate per trade

# --- Home Route (for basic health check) ---
@app.route('/')
def home():
    return "Welcome to the Flask Webhook App! Listening for TradingView alerts."

# --- Main Webhook Route ---
@app.route('/webhook', methods=['POST'])
def webhook():
    # Pre-check: Ensure API is initialized before attempting any trade operations
    if api is None:
        logging.error("Alpaca API not initialized. Cannot process webhook. Check server configuration (API keys).")
        return jsonify({"error": "Server not configured for trading. Check API keys."}), 500

    data = request.get_json()  # Get the JSON data sent in the webhook payload

    # Log the entire received payload for debugging/auditing purposes
    logging.info(f"Received webhook data: {json.dumps(data)}")

    # Extract required data points from the webhook payload
    symbol = data.get('symbol')
    entry_price_raw = data.get('price') # Raw price (float or string) from TradingView
    action = data.get('action')  # 'buy' or 'sell'

    # --- Input Validation ---
    # Ensure all critical fields are present in the webhook payload
    if not all([symbol, entry_price_raw, action]):
        logging.error("Missing required data (symbol, price, or action) in webhook payload.")
        return jsonify({"error": "Missing required data (symbol, price, or action)"}), 400

    # Validate and convert entry_price to Decimal for precise calculations
    try:
        entry_price = Decimal(str(entry_price_raw)) # Convert to string first to avoid float precision issues
        if entry_price <= 0: # Ensure the entry price is valid (non-zero/positive)
            logging.error(f"Invalid entry_price ({entry_price}) received for {symbol}. Price must be positive.")
            return jsonify({"error": "Invalid entry price received. Price must be positive."}), 400
    except Exception as e:
        logging.error(f"Failed to convert entry_price '{entry_price_raw}' to Decimal for {symbol}. Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid price format in webhook data."}), 400

    # Validate the trading action
    if action not in ['buy', 'sell']:
        logging.error(f"Invalid action '{action}' received for {symbol}. Action must be 'buy' or 'sell'.")
        return jsonify({"error": "Invalid action. Must be 'buy' or 'sell'."}), 400

    # Determine the order side ('buy' for long, 'sell' for short)
    side = 'buy' if action == 'buy' else 'sell'
    logging.info(f"Processing '{action}' signal for {symbol} at entry price: {entry_price}")

    try:
        # --- Calculate Stop Loss and Take Profit prices ---
        # These calculations are based on the entry_price from the TradingView alert.
        # For a non-blocking webhook using Alpaca's bracket orders, this is the standard approach.
        if side == 'buy': # Long position calculations
            stop_loss_price = entry_price * (Decimal('1') - STOP_LOSS_PERCENT)
            take_profit_price = entry_price * (Decimal('1') + (STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO))
            # Limit price for stop-loss order is slightly below the stop price for long positions
            stop_loss_limit_price = stop_loss_price * Decimal('0.99') 
            logging.debug(f"Long calc: SL={stop_loss_price:.4f}, TP={take_profit_price:.4f}, SL_Limit={stop_loss_limit_price:.4f}")
        else: # 'sell' for short position calculations
            stop_loss_price = entry_price * (Decimal('1') + STOP_LOSS_PERCENT)
            take_profit_price = entry_price * (Decimal('1') - (STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO))
            # Limit price for stop-loss order is slightly above the stop price for short positions
            stop_loss_limit_price = stop_loss_price * Decimal('1.01') 
            logging.debug(f"Short calc: SL={stop_loss_price:.4f}, TP={take_profit_price:.4f}, SL_Limit={stop_loss_limit_price:.4f}")

        # Basic validation for calculated prices (e.g., ensuring they are positive)
        if stop_loss_price <= 0 or take_profit_price <= 0:
            logging.error(f"Calculated SL/TP prices are non-positive: SL={stop_loss_price}, TP={take_profit_price}. Adjust risk settings or entry price.")
            return jsonify({"error": "Calculated stop loss or take profit price is non-positive. Adjust risk settings or entry price."}), 400

        # --- Calculate Position Size based on Risk Management ---
        # Determine the dollar distance from entry to stop loss
        stop_loss_dist = abs(entry_price - stop_loss_price)
        if stop_loss_dist == 0:
            logging.error("Calculated stop loss distance is zero (entry_price equals stop_loss_price). Cannot determine position size. Adjust STOP_LOSS_PERCENT to be non-zero.")
            return jsonify({"error": "Calculated stop loss distance is zero. Adjust STOP_LOSS_PERCENT."}), 400

        # Calculate position size based on dollar risk per trade
        pos_size_risk_based = RISK_DOLLAR / stop_loss_dist
        # Calculate maximum position size based on total capital allocation limit
        max_position_size = MAX_CAPITAL_ALLOCATION / entry_price

        # The final quantity to trade is the minimum of risk-based and capital-based limits, rounded down to an integer
        # Alpaca requires whole numbers for equity shares.
        final_qty = math.floor(min(pos_size_risk_based, max_position_size))

        if final_qty <= 0:
            logging.warning(f"Calculated final_qty is zero or negative ({final_qty}) for {symbol}. Order will not be placed.")
            return jsonify({"message": f"Calculated position size is zero for {symbol}. Order not placed."}), 200

        logging.info(f"Final Quantity for {symbol}: {final_qty} shares. Target SL: {stop_loss_price:.4f}, Target TP: {take_profit_price:.4f}")

        # --- Submit the Bracket Order to Alpaca ---
        # This is the single, atomic order submission.
        # It creates a market order for entry, and automatically links a stop-loss and take-profit order.
        bracket_order = api.submit_order(
            symbol=symbol,
            qty=int(final_qty), # Convert final_qty to int as required by Alpaca API
            side=side,
            type='market',  # The primary leg of the bracket order is a market order
            time_in_force='gtc', # Good 'Til Canceled is typical for bracket orders
            order_class='bracket', # This crucial parameter tells Alpaca to link the orders
            stop_loss={
                'stop_price': float(stop_loss_price), # Alpaca API expects float for price values
                'limit_price': float(stop_loss_limit_price) # Convert Decimal back to float for API
            },
            take_profit={
                'limit_price': float(take_profit_price) # Convert Decimal back to float for API
            }
        )

        logging.info(f"Bracket order submitted successfully for {symbol} (Order ID: {bracket_order.id}).")
        return jsonify({
            "message": f"{action.capitalize()} bracket order placed for {symbol} ({final_qty} shares) with SL at {stop_loss_price:.4f} and TP at {take_profit_price:.4f}",
            "order_id": bracket_order.id
        }), 200

    except tradeapi.rest.APIError as e:
        # Catch specific Alpaca API errors for detailed logging and response
        logging.error(f"Alpaca API Error submitting order for {symbol}: {e}", exc_info=True)
        # Provide more detail in the JSON response for API errors
        return jsonify({"error": f"Alpaca API Error: {e.status_code} - {e.response}"}), 500
    except Exception as e:
        # Catch any other unexpected errors during webhook processing
        logging.error(f"An unexpected error occurred during webhook processing for {symbol}: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected server error occurred."}), 500

# --- Application Entry Point ---
if __name__ == "__main__":
    # When deploying with Gunicorn on Render, Gunicorn handles running the app.
    # This block is primarily for local development/testing.
    # Ensure debug=False for any production-like environment.
    # host='0.0.0.0' makes the server accessible externally (e.g., from your local network).
    # port=5000 is a common default, but Render will assign its own port.
    app.run(debug=False, host='0.0.0.0', port=5000)
