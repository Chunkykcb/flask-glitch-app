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
from zoneinfo import ZoneInfo  # Python 3.9+

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
API_KEY = 'PKEBE9SZ9SBF38BCV2MO'
API_SECRET = 'KGHVSTQi9cCqg0qkNUHFAHmhswdcDCjJW7EJxlnq'
BASE_URL = 'https://paper-api.alpaca.markets'

api = None
try:
    api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')
    api.get_account()
    logging.info("Alpaca API initialized successfully.")
except Exception as e:
    logging.error(f"Alpaca API initialization failed: {e}", exc_info=True)

# --- Risk Management Settings ---
RISK_DOLLAR = Decimal('1000.0')
STOP_LOSS_PERCENT = Decimal('0.005')
TAKE_PROFIT_RATIO = Decimal('2.0')
MAX_CAPITAL_ALLOCATION = Decimal('10000.0')

# --- Timezone Setup ---
PT = ZoneInfo("America/Los_Angeles")
CUTOFF_HOUR = 12
CUTOFF_MINUTE = 55

def is_after_cutoff():
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    now_pt = now_utc.astimezone(PT)
    cutoff_pt = now_pt.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, second=0, microsecond=0)
    return now_pt >= cutoff_pt

@app.route('/')
def home():
    return "Webhook trading bot running."

@app.route('/webhook', methods=['POST'])
def webhook():
    if not api:
        return jsonify({"error": "Alpaca not initialized."}), 500

    data = request.get_json()
    logging.info(f"Received webhook data: {json.dumps(data)}")

    symbol = data.get('symbol')
    entry_price_raw = data.get('price')
    action = data.get('action')
    qty_raw = data.get('qty', None)  # qty might come in webhook payload

    if not all([symbol, action]):
        return jsonify({"error": "Missing required fields."}), 400

    # Handle qty=0 signals specially
    if qty_raw == 0 or qty_raw == '0':
        if is_after_cutoff():
            logging.info(f"Qty=0 received after cutoff time, closing all positions.")
            try:
                positions = api.list_positions()
                for position in positions:
                    logging.info(f"Closing position for {position.symbol}, qty: {position.qty}")
                    api.close_position(position.symbol)
                return jsonify({"message": "Closed all positions after cutoff."}), 200
            except Exception as e:
                logging.error(f"Error closing positions: {e}", exc_info=True)
                return jsonify({"error": "Failed to close all positions."}), 500
        else:
            logging.info(f"Qty=0 received before cutoff time, ignoring signal.")
            return jsonify({"message": "Ignored qty=0 signal before cutoff time."}), 200

    # For non-zero qty or no qty signals, process normally

    try:
        entry_price = Decimal(str(entry_price_raw)) if entry_price_raw else None
    except Exception:
        entry_price = None

    side = 'buy' if action == 'buy' else 'sell' if action == 'sell' else None

    if side is None:
        return jsonify({"error": "Invalid action."}), 400

    if not entry_price:
        logging.warning(f"No valid entry price provided, will fetch fill price after order execution.")

    try:
        # Place a small initial market order to get filled price for sizing
        temp_order = api.submit_order(
            symbol=symbol,
            qty=1,
            side=side,
            type='market',
            time_in_force='gtc'
        )

        # Poll until filled or timeout (10s)
        for _ in range(10):
            ord = api.get_order(temp_order.id)
            if ord.filled_at:
                break
            time.sleep(1)

        ord = api.get_order(temp_order.id)
        if not ord.filled_at:
            api.cancel_order(temp_order.id)
            return jsonify({"error": "Initial order not filled in time."}), 500

        filled_price = Decimal(ord.filled_avg_price)

        # Calculate SL/TP based on filled price
        if side == 'buy':
            sl = (filled_price * (1 - STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 + STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))
        else:
            sl = (filled_price * (1 + STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 - STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))

        stop_loss_dist = abs(filled_price - sl)
        pos_size_risk_based = RISK_DOLLAR / stop_loss_dist
        max_position_size = MAX_CAPITAL_ALLOCATION / filled_price
        qty = math.floor(min(pos_size_risk_based, max_position_size))

        if qty <= 0:
            return jsonify({"message": "Calculated position size is zero."}), 200

        logging.info(f"Submitting final order for {qty} shares at side {side} with SL={sl}, TP={tp}")

        # Submit final market order with correct qty
        final_order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            type='market',
            time_in_force='gtc'
        )

        # Wait for fill
        for _ in range(10):
            fo_status = api.get_order(final_order.id)
            if fo_status.filled_at:
                break
            time.sleep(1)

        fo_status = api.get_order(final_order.id)
        if not fo_status.filled_at:
            api.cancel_order(final_order.id)
            return jsonify({"error": "Final order did not fill in time."}), 500

        filled_price = Decimal(fo_status.filled_avg_price)

        # Recalculate SL/TP with actual fill price
        if side == 'buy':
            sl = (filled_price * (1 - STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 + STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))
        else:
            sl = (filled_price * (1 + STOP_LOSS_PERCENT)).quantize(Decimal('0.01'))
            tp = (filled_price * (1 - STOP_LOSS_PERCENT * TAKE_PROFIT_RATIO)).quantize(Decimal('0.01'))

        # Submit SL order
        sl_order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell' if side == 'buy' else 'buy',
            type='stop',
            stop_price=float(sl),
            time_in_force='gtc'
        )
        # Submit TP order
        tp_order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell' if side == 'buy' else 'buy',
            type='limit',
            limit_price=float(tp),
            time_in_force='gtc'
        )

        logging.info(f"Submitted SL ({sl_order.id}) and TP ({tp_order.id}) orders.")

        # Poll for fill on SL/TP for up to 60s
        for _ in range(60):
            sl_status = api.get_order(sl_order.id)
            tp_status = api.get_order(tp_order.id)
            if sl_status.filled_at:
                api.cancel_order(tp_order.id)
                logging.info("Stop-loss filled, take-profit canceled.")
                break
            elif tp_status.filled_at:
                api.cancel_order(sl_order.id)
                logging.info("Take-profit filled, stop-loss canceled.")
                break
            time.sleep(1)

        return jsonify({
            "message": f"Order filled at {filled_price}, SL={sl}, TP={tp}, Qty={qty}"
        }), 200

    except tradeapi.rest.APIError as e:
        logging.error(f"Alpaca API Error: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logging.error(f"Unhandled error: {e}", exc_info=True)
        return jsonify({"error": "Unhandled server error"}), 500

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
