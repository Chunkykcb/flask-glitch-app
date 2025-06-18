from flask import Flask, request, jsonify

app = Flask(__name__)

# Root endpoint
@app.route('/')
def home():
    return "Welcome to the Flask Webhook App!"

# Webhook endpoint for POST requests
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if data.get('action') == 'buy':
        print("Buying!")
    elif data.get('action') == 'sell':
        print("Selling!")
    else:
        return jsonify({"error": "Invalid action"}), 400

    return jsonify({"message": "Success"}), 200

if __name__ == '__main__':
    app.run(debug=True)
