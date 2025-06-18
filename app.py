from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if data.get('action') == 'buy':
        print("Buying!")
        return jsonify({"message": "Success"}), 200
    elif data.get('action') == 'sell':
        print("Selling!")
        return jsonify({"message": "Success"}), 200
    else:
        return jsonify({"error": "Invalid action"}), 400

if __name__ == "__main__":
    app.run(debug=True)
