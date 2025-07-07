import os
import time
import logging
import alpaca_trade_api as tradeapi
from flask import Flask, request, jsonify
from flask_cors import CORS
from decimal import Decimal, getcontext
import math
import json
from datetime import datetime
from zoneinfo import ZoneInfo # Python 3.9+

# --- Precision for Decimal ---
getcontext().prec = 10

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('app.log')]
)

app = Flask(__name__)
CORS(app)

# --- Alpaca API Setup ---
API_KEY = 'PKEBE9SZ9SBF38BCV2MO' # !!! REPLACE WITH YOUR ACTUAL API KEY !!!
API_SECRET = 'KGHVSTQi9cCqg0qkNUHFAHmhswdcDCjJW7EJxlnq' # !!! REPLACE WITH YOUR ACTUAL API SECRET !!!
BASE_URL = 'https://paper-api.alpaca.markets' # Use 'https://api.alpaca.markets' for live trading

api = None
try:
    api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')
    account = api.get_account() # Verify API connection
    logging.info(f"Alpaca API initialized successfully. Account status: {account.status}")
except Exception as e:
    logging.error(f"Alpaca API initialization failed: {e}", exc_info=True)

# --- Risk Management Settings ---
RISK_DOLLAR = Decimal('1000.0')
STOP_LOSS_PERCENT = Decimal('0.005')
TAKE_PROFIT_RATIO = Decimal('2.0')
MAX_CAPITAL_ALLOCATION = Decimal('10000.0')
MIN_TRADE_QTY = Decimal('1.0') # Minimum quantity to consider for a main trade (e.g., set to 2.0 if you never want to trade just 1 share)

# --- Timezone Setup ---
PT = ZoneInfo("America/Los_Angeles") # Pacific Time Zone
CUTOFF_HOUR = 12 # 12 PM PT
CUTOFF_MINUTE = 55 # 55 minutes past the hour (12:55 PM PT)

def is_after_cutoff():
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    now_pt = now_utc.astimezone(PT)
    cutoff_pt = now_pt.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, second=0, microsecond=0)
    logging.info(f"Current time (PT): {now_pt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}, Cutoff time (PT): {cutoff_pt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    return now_pt >= cutoff_pt

def check_short_availability(symbol):
    """
    Checks if a stock symbol is available for shorting on Alpaca.
    Relies on the 'easy_to_borrow' attribute for real-time availability.
    """
    if not api:
        logging.error("Alpaca API not initialized, cannot check short availability.")
        return False
    try:
        asset = api.get_asset(symbol)
        if asset.shortable and asset.easy_to_borrow:
            logging.info(f"Shorting available for {symbol} (shortable: {asset.shortable}, easy_to_borrow: {asset.easy_to_borrow}).")
            return True
        else:
            logging.warning(f"Shorting NOT available for {symbol} (shortable: {asset.shortable}, easy_to_borrow: {asset.easy_to_borrow}).")
            return False
    except tradeapi.rest.APIError as e:
        logging.error(f"Alpaca API Error checking short availability for {symbol}: {e}", exc_info=True)
        # If API call itself fails (e.g., symbol not found), assume not available for safety
        return False
    except Exception as e:
        logging.error(f"Unhandled error checking short availability for {symbol}: {e}", exc_info=True)
        return False

@app.route('/')
def home():
    return "Webhook trading bot running."

@app.route('/webhook', methods=['POST'])
def webhook():
    if not api:
        return jsonify({"error": "Alpaca API not initialized."}), 500

    data = request.get_json()
    logging.info(f"Received webhook data: {json.dumps(data)}")

    symbol = data.get('symbol')
    entry_price_raw = data.get('price')
    action = data.get('action')
    qty_raw_from_webhook = data.get('quantity', None)

    if not all([symbol, action]):
        return jsonify({"error": "Missing required fields (symbol, action)."}), 400

    # --- Handle qty=0 signals specially ---
    if qty_raw_from_webhook is not None and (qty_raw_from_webhook == 0 or str(qty_raw_from_webhook) == '0'):
        if is_after_cutoff():
            logging.info(f"Qty=0 received after cutoff time for {symbol}. Attempting to close all positions for this symbol.")
            try:
                # Cancel all open orders for the specific symbol first
                try:
                    open_orders = api.list_orders(status="open", symbols=[symbol])
                    for order in open_orders:
                        logging.info(f"Cancelling open order {order.id} for {symbol} before closing position.")
                        api.cancel_order(order.id)
                    time.sleep(1)  # Short delay to ensure cancellations are processed
                except Exception as e:
                    logging.warning(f"Error cancelling open orders for {symbol} during cutoff closure: {e}", exc_info=True)

                # Then attempt to close the position for that symbol
                try:
                    position_to_close = api.get_position(symbol)
                    logging.info(f"Closing position for {position_to_close.symbol}, qty: {position_to_close.qty}")
                    api.close_position(position_to_close.symbol)
                    time.sleep(2) # Give Alpaca a moment

                    # Verify closure
                    try:
                        api.get_position(symbol)
                        logging.warning(f"Position for {symbol} not fully closed after qty=0 signal and cutoff.")
                        return jsonify({"message": f"Attempted to close position for {symbol} but it might not be fully closed."}), 202
                    except tradeapi.rest.APIError as e:
                        if e.status_code == 404:
                            logging.info(f"Position for {symbol} successfully closed after cutoff.")
                            return jsonify({"message": f"Closed position for {symbol} after cutoff."}), 200
                        else:
                            logging.error(f"Error verifying position closure for {symbol} after cutoff: {e}", exc_info=True)
                            return jsonify({"error": f"Failed to verify closure of position for {symbol}."}), 500
                except tradeapi.rest.APIError as e:
                    if e.status_code == 404:
                        logging.info(f"No open position found for {symbol} to close after cutoff.")
                        return jsonify({"message": f"No position for {symbol} to close after cutoff."}), 200
                    else:
                        logging.error(f"Error closing position for {symbol} during cutoff: {e}", exc_info=True)
                        return jsonify({"error": f"Failed to close position for {symbol}."}), 500
                except Exception as e:
                    logging.error(f"Unhandled error during cutoff position closure for {symbol}: {e}", exc_info=True)
                    return jsonify({"error": "Failed to close position after cutoff."}), 500

            except Exception as e:
                logging.error(f"Error handling qty=0 signal after cutoff: {e}", exc_info=True)
                return jsonify({"error": "Failed to process cutoff closure."}), 500
        else:
            logging.info(f"Qty=0 received before cutoff time ({CUTOFF_HOUR}:{CUTOFF_MINUTE} PT) for {symbol}, ignoring signal.")
            return jsonify({"message": "Ignored qty=0 signal before cutoff time."}), 200

    # For non-zero qty or no qty signals, process normally

    try:
        entry_price = Decimal(str(entry_price_raw)) if entry_price_raw else None
    except Exception:
        entry_price = None
        logging.error(f"Invalid entry price format received for {symbol}: {entry_price_raw}", exc_info=True)
        return jsonify({"error": "Invalid entry price format."}), 400

    side = 'buy' if action == 'buy' else 'sell' if action == 'sell' else None

    if side is None:
        return jsonify({"error": "Invalid action. Must be 'buy' or 'sell'."}), 400

    # --- Check short availability for 'sell' actions ---
    if side == 'sell' and not check_short_availability(symbol):
        return jsonify({"error": f"Shorting {symbol} is not available at this time. Trade aborted."}), 400

    if not entry_price:
        logging.warning(f"No valid entry price provided for {symbol}. Will fetch fill price after order execution.")

    try:
        # Check current position for the symbol
        current_position = None
        current_qty = Decimal('0')
        current_side = None
        try:
            current_position = api.get_position(symbol)
            current_qty = Decimal(current_position.qty)
            current_side = 'buy' if current_qty > 0 else 'sell'
            logging.info(f"Existing position found for {symbol}: {current_side} {abs(current_qty)} shares.")

            # --- NEW LOGIC: Abort if any position already exists for the symbol ---
            if current_qty != 0:
                logging.info(f"Position already exists for {symbol}: {current_qty} shares. Aborting new trade.")
                return jsonify({"message": f"Trade aborted. Already holding position in {symbol}."}), 400

        except tradeapi.rest.APIError as e:
            if e.status_code == 404: # 404 means no position exists for this symbol
                logging.info(f"No existing position for {symbol}. Proceeding with new order.")
            else:
                logging.error(f"Error checking existing position for {symbol}: {e}", exc_info=True)
                return jsonify({"error": f"Failed to check existing position for {symbol}."}), 500
        except Exception as e:
            logging.error(f"Unhandled error checking position for {symbol}: {e}", exc_info=True)
            return jsonify({"error": "Unhandled error checking position."}), 500

        # --- Initial 1-share probe order ---
        logging.info(f"Submitting 1-share probe order for {symbol} ({side}).")
        temp_order = api.submit_order(
            symbol=symbol,
            qty=1,
            side=side, # This will be 'buy' or 'sell'
            type='market',
            time_in_force='day' # Use 'day' for probe orders to avoid hanging orders
        )

        # Poll until filled or timeout (10s)
        filled_at = None
        filled_price = None
        logging.info(f"Polling for probe order {temp_order.id} fill for {symbol}...")
        for i in range(1, 11): # Poll up to 10 times
            ord = api.get_order(temp_order.id)
            if ord.filled_at:
                filled_at = ord.filled_at
                filled_price = Decimal(ord.filled_avg_price)
                logging.info(f"Initial probe order {temp_order.id} for {symbol} filled at {filled_price} after {i} seconds.")
                break
            logging.info(f"Probe order {temp_order.id} not yet filled. Retrying in 1 second ({i}/10)...")
            time.sleep(1)

        if not filled_at:
            logging.warning(f"Initial probe order {temp_order.id} for {symbol} not filled within 10 seconds. Attempting to cancel.")
            try:
                api.cancel_order(temp_order.id)
                logging.info(f"Probe order {temp_order.id} cancelled successfully.")
            except tradeapi.rest.APIError as e:
                # If already filled or otherwise un-cancellable, this will raise an error
                logging.warning(f"Could not cancel probe order {temp_order.id} for {symbol}: {e} (might already be filled/done).")
            return jsonify({"error": "Initial probe order not filled in time and was cancelled. Trade aborted."}), 500

        # Calculate SL/TP based on filled price
        if side == 'buy':
            sl = (filled_price * (1 - STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 + STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))
        else: # side == 'sell' (for shorting)
            sl = (filled_price * (1 + STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 - STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))

        stop_loss_dist = abs(filled_price - sl)
        if stop_loss_dist == 0:
            logging.error(f"Calculated stop loss distance is zero for {symbol}. This prevents position sizing. Aborting trade.")
            # If probe filled, close that 1 share position to clean up
            try:
                position_after_probe = api.get_position(symbol)
                if position_after_probe and abs(Decimal(position_after_probe.qty)) > 0:
                    logging.info(f"Closing {position_after_probe.qty}-share probe position for {symbol} as calculated final qty is invalid.")
                    # Cancel any potential child orders from the 1-share probe before closing
                    try:
                        open_probe_orders = api.list_orders(status="open", symbols=[symbol])
                        for order in open_probe_orders:
                            logging.info(f"Cancelling open order {order.id} for {symbol} before cleaning up zero SL probe.")
                            api.cancel_order(order.id)
                        time.sleep(1)
                    except Exception as e:
                        logging.warning(f"Error cancelling probe related orders for {symbol} before zero SL cleanup: {e}", exc_info=True)
                    api.close_position(symbol)
            except tradeapi.rest.APIError as e:
                if e.status_code == 404: # Already closed or never opened
                    pass
                else:
                    logging.error(f"Error closing probe position after zero SL calculation: {e}", exc_info=True)
            return jsonify({"error": "Calculated stop loss distance is zero, cannot proceed with trade."}), 500

        # Calculate Position Size (based on available risk and capital allocation)
        pos_size_risk_based = RISK_DOLLAR / stop_loss_dist
        max_position_size = MAX_CAPITAL_ALLOCATION / filled_price
        
        qty = math.floor(min(pos_size_risk_based, max_position_size))
        
        # --- Handle cases where calculated qty is too small or zero ---
        if qty < MIN_TRADE_QTY:
            logging.info(f"Calculated total position size ({qty} shares) for {symbol} is less than minimum trade quantity ({MIN_TRADE_QTY}). Trade aborted.")
            # Close the 1-share probe position if it filled, as we're not proceeding with a main trade
            try:
                position_after_probe = api.get_position(symbol)
                if position_after_probe and abs(Decimal(position_after_probe.qty)) > 0:
                    logging.info(f"Closing {position_after_probe.qty}-share probe position for {symbol} as calculated final qty is too small.")
                    # Cancel any potential child orders from the 1-share probe before closing
                    try:
                        open_probe_orders = api.list_orders(status="open", symbols=[symbol])
                        for order in open_probe_orders:
                            logging.info(f"Cancelling open order {order.id} for {symbol} before cleaning up too small qty probe.")
                            api.cancel_order(order.id)
                        time.sleep(1)
                    except Exception as e:
                        logging.warning(f"Error cancelling probe related orders for {symbol} before too small qty cleanup: {e}", exc_info=True)
                    api.close_position(symbol)
            except tradeapi.rest.APIError as e:
                if e.status_code == 404:
                    pass # Already closed or never opened
                else:
                    logging.error(f"Error closing probe position when calculated qty is too small: {e}", exc_info=True)
            return jsonify({"message": f"Calculated position size ({qty}) for {symbol} is too small (min {MIN_TRADE_QTY}), no main order placed. Probe position closed."}), 200

        # --- IMPORTANT: Close the 1-share probe position before placing the main bracket order ---
        # The probe's only purpose was price discovery.
        # This ensures we don't have a stray 1-share position and the bracket order is for the full intended qty.
        try:
            position_from_probe = api.get_position(symbol)
            # Ensure it's exactly a 1-share position matching the side of the probe
            if position_from_probe and abs(Decimal(position_from_probe.qty)) == Decimal('1') and \
               ((side == 'buy' and Decimal(position_from_probe.qty) > 0) or \
                (side == 'sell' and Decimal(position_from_probe.qty) < 0)):
                logging.info(f"Closing 1-share probe position for {symbol} to prepare for full bracket order.")
                # --- CANCEL ANY OPEN ORDERS FOR THE PROBE BEFORE CLOSING ---
                try:
                    open_probe_orders = api.list_orders(status="open", symbols=[symbol])
                    for order in open_probe_orders:
                        logging.info(f"Cancelling open order {order.id} for {symbol} before probe position close.")
                        api.cancel_order(order.id)
                    time.sleep(1)
                except Exception as e:
                    logging.warning(f"Error cancelling probe orders for {symbol} before probe close: {e}", exc_info=True)

                api.close_position(symbol)
                time.sleep(2) # Give Alpaca a moment for closure confirmation
                # Verify it's gone
                try:
                    api.get_position(symbol)
                    logging.warning(f"Probe position for {symbol} not fully closed after close_position call. Proceeding anyway, but monitor.")
                except tradeapi.rest.APIError as e:
                    if e.status_code == 404:
                        logging.info(f"1-share probe position for {symbol} successfully closed.")
                    else:
                        logging.error(f"Error verifying probe position closure for {symbol}: {e}", exc_info=True)
                        return jsonify({"error": f"Failed to verify closure of probe position for {symbol}."}), 500
                except Exception as e:
                    logging.error(f"Unhandled error verifying probe position closure for {symbol}: {e}", exc_info=True)
                    return jsonify({"error": "Unhandled error verifying probe position closure."}), 500
            else:
                logging.info(f"No 1-share probe position found to close for {symbol} or it's not a 1-share position, which is unexpected after probe. Proceeding.")
        except tradeapi.rest.APIError as e:
            if e.status_code == 404:
                logging.info(f"No 1-share probe position found to close for {symbol} (already closed/never opened).")
            else:
                logging.error(f"Error closing 1-share probe position for {symbol}: {e}", exc_info=True)
                return jsonify({"error": f"Failed to gracefully close probe position for {symbol} before main order."}), 500
        except Exception as e:
            logging.error(f"Unhandled error during probe position closure for {symbol}: {e}", exc_info=True)
            return jsonify({"error": "Unhandled error closing probe position."}), 500


        logging.info(f"Submitting final bracket order for {qty} shares of {symbol} (side: {side}) with filled_price={filled_price}, SL={sl}, TP={tp}.")

        # Submit final market order with bracket (SL + TP) based on fill price from probe
        bracket_order = api.submit_order(
            symbol=symbol,
            qty=qty, # This is the TOTAL calculated quantity
            side=side,
            type='market',
            time_in_force='gtc', # GTC is generally better for longer-term bracket orders
            order_class='bracket',
            take_profit={"limit_price": float(tp)},
            stop_loss={"stop_price": float(sl)}
        )

        logging.info(f"Bracket order submitted for {symbol} with ID: {bracket_order.id}")

        return jsonify({
            "message": f"Bracket order submitted for {symbol} with total qty={qty}, entry based on {filled_price}, SL={sl}, TP={tp}",
            "order_id": bracket_order.id
        }), 200

    except tradeapi.rest.APIError as e:
        logging.error(f"Alpaca API Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logging.error(f"Unhandled error: {e}", exc_info=True)
        return jsonify({"error": "Unhandled server error"}), 500

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
