from flask import Flask, request, jsonify
import os
from stripe_checker import check_card

app = Flask(__name__)


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "stripe-checker",
        "status": "online",
        "usage": "POST /check  {\"card\": \"4111111111111111|12|29|123\"}",
        "health": "/health",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/check", methods=["POST"])
def check():
    data = request.get_json(silent=True) or {}
    card = (data.get("card") or "").strip()
    if not card:
        return jsonify({"error": "missing 'card' field"}), 400
    result = check_card(card)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
