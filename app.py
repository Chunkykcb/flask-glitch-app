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
# Set the precision for Decimal calculations to 10 decimal places.
# This helps maintain accuracy in financial calculations.
getcontext().prec = 10

# --- Logging Setup ---
# Configure logging to output messages to both the console and a file (app.log).
# This helps in debugging and monitoring the bot's operations.
logging.basicConfig(
    level=logging.INFO, # Set logging level to INFO, meaning INFO, WARNING, ERROR, CRITICAL messages will be captured.
    format='%(asctime)s - %(levelname)s - %(message)s', # Define the format of log messages.
    handlers=[logging.StreamHandler(), logging.FileHandler('app.log')] # Add handlers for console and file output.
)

app = Flask(__name__)
CORS(app) # Enable Cross-Origin Resource Sharing for the Flask app.

# --- Alpaca API Setup ---
API_KEY = 'PKEBE9SZ9SBF38BCV2MO' # !!! REPLACE WITH YOUR ACTUAL API KEY !!!
API_SECRET = 'KGHVSTQi9cCqg0qkNUHFAHmhswdcDCjJW7EJxlnq' # !!! REPLACE WITH YOUR ACTUAL API SECRET !!!
BASE_URL = 'https://paper-api.alpaca.markets' # Use 'https://api.alpaca.markets' for live trading

api = None
try:
    # Initialize Alpaca REST API client.
    api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')
    account = api.get_account() # Attempt to get account details to verify API connection.
    logging.info(f"Alpaca API initialized successfully. Account status: {account.status}")
except Exception as e:
    # Log any errors encountered during API initialization.
    logging.error(f"Alpaca API initialization failed: {e}", exc_info=True)

# --- Risk Management Settings ---
# Define parameters for risk management and position sizing.
RISK_DOLLAR = Decimal('1000.0') # The maximum dollar amount to risk per trade.
STOP_LOSS_PERCENT = Decimal('0.005') # Percentage of entry price for stop loss (0.5%).
TAKE_PROFIT_RATIO = Decimal('2.0') # Risk-to-reward ratio for take profit (2:1).
MAX_CAPITAL_ALLOCATION = Decimal('10000.0') # Maximum capital to allocate to a single position.
MIN_TRADE_QTY = Decimal('1.0') # Minimum quantity to consider for a main trade (e.g., set to 2.0 if you never want to trade just 1 share).

# --- Timezone Setup ---
# Define the Pacific Time Zone for market cutoff calculations.
PT = ZoneInfo("America/Los_Angeles") # Pacific Time Zone
# Market close is 4:00 PM ET (New York Time).
# 4:00 PM ET = 1:00 PM PT (Pacific Time).
# So, '5 minutes before market closure' means 12:55 PM PT.
CUTOFF_HOUR = 12 # 12 PM PT
CUTOFF_MINUTE = 55 # 55 minutes past the hour (12:55 PM PT)

def is_after_cutoff():
    """
    Checks if the current time is after the defined market cutoff time in Pacific Time.
    This is used to determine when to trigger full portfolio liquidation.
    """
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")) # Get current UTC time.
    now_pt = now_utc.astimezone(PT) # Convert UTC time to Pacific Time.
    # Create a datetime object for the cutoff time in Pacific Time.
    cutoff_pt = now_pt.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, second=0, microsecond=0)
    logging.info(f"Current time (PT): {now_pt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}, Cutoff time (PT): {cutoff_pt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    return now_pt >= cutoff_pt # Return True if current time is at or after cutoff.

def check_short_availability(symbol):
    """
    Checks if a stock symbol is available for shorting on Alpaca.
    Relies on the 'easy_to_borrow' attribute for real-time availability.
    """
    if not api:
        logging.error("Alpaca API not initialized, cannot check short availability.")
        return False
    try:
        asset = api.get_asset(symbol) # Retrieve asset details for the given symbol.
        # Check if the asset is shortable and easy to borrow.
        if asset.shortable and asset.easy_to_borrow:
            logging.info(f"Shorting available for {symbol} (shortable: {asset.shortable}, easy_to_borrow: {asset.easy_to_borrow}).")
            return True
        else:
            logging.warning(f"Shorting NOT available for {symbol} (shortable: {asset.shortable}, easy_to_borrow: {asset.easy_to_borrow}).")
            return False
    except tradeapi.rest.APIError as e:
        logging.error(f"Alpaca API Error checking short availability for {symbol}: {e}", exc_info=True)
        # If API call itself fails (e.g., symbol not found), assume not available for safety.
        return False
    except Exception as e:
        logging.error(f"Unhandled error checking short availability for {symbol}: {e}", exc_info=True)
        return False

@app.route('/')
def home():
    """
    Home route for the Flask application.
    """
    return "Webhook trading bot running."

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Webhook endpoint to receive trading signals.
    Handles both new trade signals and full portfolio liquidation signals.
    """
    if not api:
        # Return an error if Alpaca API is not initialized.
        return jsonify({"error": "Alpaca API not initialized."}), 500

    data = request.get_json() # Get JSON data from the incoming webhook request.
    logging.info(f"Received webhook data: {json.dumps(data)}")

    symbol = data.get('symbol') # Stock symbol.
    entry_price_raw = data.get('price') # Entry price (can be None).
    action = data.get('action') # Trade action ('buy' or 'sell').
    qty_raw_from_webhook = data.get('quantity', None) # Quantity (can be 0 for liquidation).

    if not all([symbol, action]):
        # Validate essential fields.
        return jsonify({"error": "Missing required fields (symbol, action)."}), 400

    # --- Handle qty=0 signals specially for FULL PORTFOLIO LIQUIDATION ---
    # If quantity is 0 or '0' and it's after the cutoff time, trigger full liquidation.
    if qty_raw_from_webhook is not None and (qty_raw_from_webhook == 0 or str(qty_raw_from_webhook) == '0'):
        if is_after_cutoff():
            logging.info(f"Qty=0 received after cutoff time for {symbol}. Triggering full portfolio liquidation.")
            liquidation_results = []
            
            try:
                logging.info("Cancelling all open orders across the portfolio.")
                api.cancel_all_orders() # Cancel all open orders to prepare for liquidation.
                time.sleep(2) # Give Alpaca a moment to process cancellations.
                logging.info("All open orders cancellation initiated.")
            except tradeapi.rest.APIError as e:
                logging.warning(f"Error cancelling all open orders: {e}", exc_info=True)
            except Exception as e:
                logging.warning(f"Unhandled error cancelling all open orders: {e}", exc_info=True)

            try:
                positions = api.list_positions() # Get all open positions.
                if not positions:
                    logging.info("No open positions found to close during liquidation.")
                    return jsonify({"message": "No positions to close during portfolio liquidation."}), 200

                logging.info(f"Found {len(positions)} positions to close during liquidation.")

                for pos in positions:
                    pos_symbol = pos.symbol
                    qty_to_close = abs(Decimal(pos.qty)) # Quantity to close (absolute value).
                    closing_side = 'sell' if Decimal(pos.qty) > 0 else 'buy' # Determine side for closing order.

                    logging.info(f"Attempting to close position for {pos_symbol}: {qty_to_close} shares, side: {closing_side}.")

                    remaining_qty = qty_to_close
                    max_attempts = 5 # Allow multiple attempts to close each position.
                    position_closed_fully = False

                    for attempt in range(max_attempts):
                        if remaining_qty <= 0:
                            position_closed_fully = True
                            break # Position for this symbol is fully closed.

                        logging.info(f"Closing attempt {attempt + 1}/{max_attempts} for {pos_symbol}. Remaining qty: {remaining_qty}.")
                        try:
                            # Submit a market order for the remaining quantity.
                            close_order = api.submit_order(
                                symbol=pos_symbol,
                                # Convert Decimal remaining_qty to float for API call
                                qty=float(remaining_qty),  
                                side=closing_side,
                                type='market',
                                time_in_force='day' # Day order for cutoff closure.
                            )
                            logging.info(f"Submitted close order {close_order.id} for {pos_symbol} (Qty: {remaining_qty}, Side: {closing_side}).")

                            # Poll for fill.
                            poll_timeout = 20 # seconds for each order.
                            poll_interval = 1 # seconds.
                            poll_start_time = time.time()
                            order_filled_completely_in_poll = False

                            while time.time() - poll_start_time < poll_timeout:
                                ord_status = api.get_order(close_order.id)
                                if ord_status.filled_qty and Decimal(ord_status.filled_qty) > 0:
                                    filled_this_attempt = Decimal(ord_status.filled_qty)
                                    if filled_this_attempt == remaining_qty:
                                        logging.info(f"Close order {close_order.id} for {pos_symbol} fully filled: {filled_this_attempt} shares.")
                                        remaining_qty = Decimal('0')
                                        order_filled_completely_in_poll = True
                                        break # Exit polling loop, position fully closed.
                                    else:
                                        # Partially filled, update remaining qty for next attempt.
                                        logging.warning(f"Close order {close_order.id} for {pos_symbol} partially filled: {filled_this_attempt} of {remaining_qty}. Remaining to close: {remaining_qty - filled_this_attempt}.")
                                        remaining_qty -= filled_this_attempt
                                        break # Exit polling loop to retry with new remaining_qty.
                                elif ord_status.status in ['canceled', 'expired', 'rejected']:
                                    logging.warning(f"Close order {close_order.id} for {pos_symbol} was {ord_status.status}. Remaining qty: {remaining_qty}.")
                                    break # Exit polling loop, might need new order or it's gone.
                                logging.info(f"Close order {close_order.id} for {pos_symbol} not yet filled. Retrying in {poll_interval} second...")
                                time.sleep(poll_interval)
                            
                            # If order wasn't fully filled or polling timed out, cancel it before next attempt.
                            if not order_filled_completely_in_poll and remaining_qty > 0:
                                try:
                                    api.cancel_order(close_order.id)
                                    logging.warning(f"Cancelled partially filled/unfilled order {close_order.id} for {pos_symbol}. Remaining: {remaining_qty}.")
                                    time.sleep(1) # Give Alpaca a moment.
                                except tradeapi.rest.APIError as e:
                                    logging.warning(f"Could not cancel order {close_order.id} for {pos_symbol} (might be filled/done or already cancelled): {e}")

                        except tradeapi.rest.APIError as e:
                            logging.error(f"Alpaca API Error during close order submission/polling for {pos_symbol} (Attempt {attempt + 1}): {e}", exc_info=True)
                            if "insufficient qty available for order" in str(e).lower():
                                logging.warning(f"Insufficient quantity error for {pos_symbol}. This may indicate insufficient buying power to cover the short. Will retry or fail after max attempts.")
                                time.sleep(1) # Short delay before next attempt.
                            else:
                                logging.error(f"Unrecoverable API error during close attempt for {pos_symbol}: {e}. Aborting further retries for this symbol.")
                                break # Break retry loop for unrecoverable errors.
                        except Exception as e:
                            logging.error(f"Unhandled error during close order submission/polling for {pos_symbol} (Attempt {attempt + 1}): {e}", exc_info=True)
                            break # Break retry loop for unhandled errors.
                    
                    # Log final status for each position after all attempts.
                    try:
                        final_pos_check = api.get_position(pos_symbol)
                        liquidation_results.append(f"{pos_symbol}: Failed to close fully. Remaining qty: {final_pos_check.qty}")
                        logging.warning(f"Position for {pos_symbol} not fully closed after multiple attempts. Remaining qty: {final_pos_check.qty}.")
                    except tradeapi.rest.APIError as e:
                        if e.status_code == 404:
                            liquidation_results.append(f"{pos_symbol}: Successfully closed.")
                            logging.info(f"Position for {pos_symbol} successfully closed.")
                        else:
                            liquidation_results.append(f"{pos_symbol}: Error verifying final status: {e}")
                            logging.error(f"Error verifying final position closure for {pos_symbol}: {e}", exc_info=True)
                    except Exception as e:
                        liquidation_results.append(f"{pos_symbol}: Unhandled error verifying final status: {e}")
                        logging.error(f"Unhandled error verifying final position closure for {pos_symbol}: {e}", exc_info=True)

                return jsonify({"message": "Full portfolio liquidation attempted.", "details": liquidation_results}), 200

            except tradeapi.rest.APIError as e:
                logging.error(f"Alpaca API Error listing positions for liquidation: {e}", exc_info=True)
                return jsonify({"error": f"Failed to list positions for liquidation: {e}"}), 500
            except Exception as e:
                logging.error(f"Unhandled error during full portfolio liquidation: {e}", exc_info=True)
                return jsonify({"error": "Failed to process full portfolio liquidation."}), 500
        else:
            logging.info(f"Qty=0 received before cutoff time ({CUTOFF_HOUR}:{CUTOFF_MINUTE} PT) for {symbol}, ignoring signal for full liquidation.")
            # For non-cutoff qty=0 signals, still provide a clear message.
            return jsonify({"message": "Ignored qty=0 signal before cutoff time (not triggering full liquidation)."}), 200

    # --- For non-zero qty or no qty signals, process normally ---
    # This is the existing logic for placing new bracket orders.
    # It remains outside the 'qty=0' and 'after_cutoff' blocks.

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
        # Check current position for the symbol.
        current_position = None
        current_qty = Decimal('0')
        current_side = None
        try:
            current_position = api.get_position(symbol)
            current_qty = Decimal(current_position.qty)
            current_side = 'buy' if current_qty > 0 else 'sell'
            logging.info(f"Existing position found for {symbol}: {current_side} {abs(current_qty)} shares.")

            # --- NEW LOGIC: Abort if any position already exists for the symbol ---
            # This ensures the bot does not open multiple positions for the same symbol.
            if current_qty != 0:
                logging.info(f"Position already exists for {symbol}: {current_qty} shares. Aborting new trade.")
                return jsonify({"message": f"Trade aborted. Already holding position in {symbol}."}), 400

        except tradeapi.rest.APIError as e:
            if e.status_code == 404: # 404 means no position exists for this symbol.
                logging.info(f"No existing position for {symbol}. Proceeding with new order.")
            else:
                logging.error(f"Error checking existing position for {symbol}: {e}", exc_info=True)
                return jsonify({"error": f"Failed to check existing position for {symbol}."}), 500
        except Exception as e:
            logging.error(f"Unhandled error checking position for {symbol}: {e}", exc_info=True)
            return jsonify({"error": "Unhandled error checking position."}), 500

        # --- Initial 1-share probe order ---
        # A small order to get an accurate fill price for subsequent calculations.
        logging.info(f"Submitting 1-share probe order for {symbol} ({side}).")
        temp_order = api.submit_order(
            symbol=symbol,
            qty=1,
            side=side, # This will be 'buy' or 'sell'.
            type='market',
            time_in_force='day' # Use 'day' for probe orders to avoid hanging orders.
        )

        # Poll until filled or timeout (10s).
        filled_at = None
        filled_price = None
        logging.info(f"Polling for probe order {temp_order.id} fill for {symbol}...")
        for i in range(1, 11): # Poll up to 10 times.
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
                # If already filled or otherwise un-cancellable, this will raise an error.
                logging.warning(f"Could not cancel probe order {temp_order.id} for {symbol}: {e} (might already be filled/done).")
            return jsonify({"error": "Initial probe order not filled in time and was cancelled. Trade aborted."}), 500

        # Calculate SL/TP based on filled price.
        if side == 'buy':
            sl = (filled_price * (1 - STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 + STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))
        else: # side == 'sell' (for shorting).
            sl = (filled_price * (1 + STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 - STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))

        stop_loss_dist = abs(filled_price - sl)
        if stop_loss_dist == 0:
            logging.error(f"Calculated stop loss distance is zero for {symbol}. This prevents position sizing. Aborting trade.")
            # If probe filled, close that 1 share position to clean up.
            try:
                position_after_probe = api.get_position(symbol)
                if position_after_probe and abs(Decimal(position_after_probe.qty)) > 0:
                    logging.info(f"Closing {position_after_probe.qty}-share probe position for {symbol} as calculated final qty is invalid.")
                    # Cancel any potential child orders from the 1-share probe before closing.
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
                if e.status_code == 404: # Already closed or never opened.
                    pass
                else:
                    logging.error(f"Error closing probe position after zero SL calculation: {e}", exc_info=True)
            return jsonify({"error": "Calculated stop loss distance is zero, cannot proceed with trade."}), 500

        # Calculate Position Size (based on available risk and capital allocation).
        pos_size_risk_based = RISK_DOLLAR / stop_loss_dist
        max_position_size = MAX_CAPITAL_ALLOCATION / filled_price
        
        qty = math.floor(min(pos_size_risk_based, max_position_size))
        
        # --- Handle cases where calculated qty is too small or zero ---
        if qty < MIN_TRADE_QTY:
            logging.info(f"Calculated total position size ({qty} shares) for {symbol} is less than minimum trade quantity ({MIN_TRADE_QTY}). Trade aborted.")
            # Close the 1-share probe position if it filled, as we're not proceeding with a main trade.
            try:
                position_after_probe = api.get_position(symbol)
                if position_after_probe and abs(Decimal(position_after_probe.qty)) > 0:
                    logging.info(f"Closing {position_after_probe.qty}-share probe position for {symbol} as calculated final qty is too small.")
                    # Cancel any potential child orders from the 1-share probe before closing.
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
                    pass # Already closed or never opened.
                else:
                    logging.error(f"Error closing probe position when calculated qty is too small: {e}", exc_info=True)
            return jsonify({"message": f"Calculated position size ({qty}) for {symbol} is too small (min {MIN_TRADE_QTY}), no main order placed. Probe position closed."}), 200

        # --- IMPORTANT: Close the 1-share probe position before placing the main bracket order ---
        # The probe's only purpose was price discovery.
        # This ensures we don't have a stray 1-share position and the bracket order is for the full intended qty.
        try:
            position_from_probe = api.get_position(symbol)
            # Ensure it's exactly a 1-share position matching the side of the probe.
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
                time.sleep(2) # Give Alpaca a moment for closure confirmation.
                # Verify it's gone.
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

        # Submit final market order with bracket (SL + TP) based on fill price from probe.
        bracket_order = api.submit_order(
            symbol=symbol,
            # Convert Decimal qty to float for API call
            qty=float(qty), 
            side=side,
            type='market',
            time_in_force='gtc', # GTC is generally better for longer-term bracket orders.
            order_class='bracket',
            # Convert Decimal tp to float for API call
            take_profit={"limit_price": float(tp)}, 
            # Convert Decimal sl to float for API call
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
    # Run the Flask application.
    # debug=False for production. host="0.0.0.0" makes it accessible externally.
    app.run(debug=False, host="0.0.0.0", port=5000)
