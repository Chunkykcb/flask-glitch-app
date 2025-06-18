from flask import Flask, request, jsonify
from flask_cors import CORS  # Import CORS

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# This is your existing home route, it's fine to keep it
@app.route('/')
def home():
    return "Welcome to the Flask Webhook App!"

# This is the /webhook route where you'll handle the POST requests
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()  # Get the JSON data sent to the webhook

    print(f"Received webhook data: {data}")

    if data and 'action' in data:
        if data['action'] == 'buy':
            print("Buying!")
            return jsonify({"message": "Buy action triggered"}), 200
        elif data['action'] == 'sell':
            print("Selling!")
            return jsonify({"message": "Sell action triggered"}), 200
        else:
            return jsonify({"error": "Invalid action"}), 400
    else:
        return jsonify({"error": "No action provided"}), 400


if __name__ == "__main__":
    app.run(debug=True)
