from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime
import logging
import os

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
BASE_URL = "https://api.dhan.co/v2"
DATABASE_URL = "https://trading-idea-render-default-rtdb.firebaseio.com/ownerToken.json"
CLIENT_ID = "1111417630"

def fetch_token_from_firebase():
    """Fetch token from Firebase"""
    try:
        logger.info("Fetching token from Firebase...")
        response = requests.get(DATABASE_URL, timeout=10)
        
        if response.status_code == 200:
            token_data = response.json()
            if token_data and token_data.get('token'):
                current_time = datetime.now().timestamp() * 1000
                expires_at = token_data.get('expiresAt', 0)
                
                if current_time < expires_at:
                    logger.info("Token is valid")
                    return token_data['token']
                else:
                    logger.warning("Token expired")
                    return None
        return None
    except Exception as e:
        logger.error(f"Error fetching token: {e}")
        return None

def get_signal(pcr):
    """Get market signal based on PCR ratio"""
    if pcr > 1.2:
        return "UPTREND 📈"
    elif pcr < 0.8:
        return "DOWNTREND 📉"
    return "SIDEWAYS ⚖️"

def ai_signal(call_change, put_change):
    """AI Signal based on smart money movement"""
    if put_change > 0 and call_change < 0:
        return "STRONG BULLISH 🚀"
    elif call_change > 0 and put_change < 0:
        return "STRONG BEARISH 🔻"
    elif put_change > call_change:
        return "BULLISH 📈"
    elif call_change > put_change:
        return "BEARISH 📉"
    return "SIDEWAYS ⚖️"

def get_option_data():
    """Main function to fetch option chain data"""
    try:
        token = fetch_token_from_firebase()
        if not token:
            return None, "No valid token found"
        
        expiry_url = f"{BASE_URL}/optionchain/expirylist"
        headers = {
            "access-token": token,
            "client-id": CLIENT_ID,
            "Content-Type": "application/json"
        }
        payload = {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"}
        
        expiry_response = requests.post(expiry_url, headers=headers, json=payload, timeout=10)
        expiry_data = expiry_response.json()
        
        if not expiry_data.get("data"):
            return None, "No expiry data found"
        
        expiry = sorted(expiry_data["data"])[0]
        
        option_url = f"{BASE_URL}/optionchain"
        option_payload = {
            "UnderlyingScrip": 13,
            "UnderlyingSeg": "IDX_I",
            "Expiry": expiry
        }
        
        option_response = requests.post(option_url, headers=headers, json=option_payload, timeout=10)
        option_data = option_response.json()
        
        processed_data = []
        oc = option_data.get("data", {}).get("oc", {})
        
        for strike_str, item in oc.items():
            try:
                strike = float(strike_str)
                ce = item.get("ce", {})
                pe = item.get("pe", {})
                
                call_oi = ce.get("oi", 0)
                put_oi = pe.get("oi", 0)
                call_prev = ce.get("previous_oi", 0)
                put_prev = pe.get("previous_oi", 0)
                
                processed_data.append({
                    "strike": strike,
                    "call_oi": call_oi,
                    "put_oi": put_oi,
                    "call_change": call_oi - call_prev,
                    "put_change": put_oi - put_prev
                })
            except:
                continue
        
        processed_data.sort(key=lambda x: x["strike"])
        
        total_call = sum(x["call_oi"] for x in processed_data)
        total_put = sum(x["put_oi"] for x in processed_data)
        total_call_change = sum(x["call_change"] for x in processed_data)
        total_put_change = sum(x["put_change"] for x in processed_data)
        pcr = round(total_put / total_call, 2) if total_call > 0 else 0
        
        max_pain_value = None
        if processed_data:
            strikes = [x["strike"] for x in processed_data]
            min_loss = float("inf")
            for s in strikes:
                loss = 0
                for row in processed_data:
                    loss += row["call_oi"] * max(0, s - row["strike"])
                    loss += row["put_oi"] * max(0, row["strike"] - s)
                if loss < min_loss:
                    min_loss = loss
                    max_pain_value = s
        
        support = max(processed_data, key=lambda x: x["put_oi"])["strike"] if processed_data else None
        resistance = max(processed_data, key=lambda x: x["call_oi"])["strike"] if processed_data else None
        market_signal = get_signal(pcr)
        smart_money_signal = ai_signal(total_call_change, total_put_change)
        
        return {
            "success": True,
            "expiry": expiry,
            "total_call_oi": total_call,
            "total_put_oi": total_put,
            "total_call_change": total_call_change,
            "total_put_change": total_put_change,
            "pcr": pcr,
            "max_pain": max_pain_value,
            "support": support,
            "resistance": resistance,
            "market_signal": market_signal,
            "smart_money_signal": smart_money_signal,
            "data": processed_data[-30:],
            "timestamp": datetime.now().isoformat()
        }, None
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return None, str(e)

@app.route("/")
def index():
    """Serve the dashboard"""
    return render_template("index.html")

@app.route("/login")
def login_page():
    """Serve the login page"""
    return render_template("login.html")

@app.route("/api/market-data")
def market_data():
    """API endpoint for market data"""
    data, error = get_option_data()
    if error:
        return jsonify({"success": False, "error": error}), 500
    return jsonify(data)

@app.route("/api/health")
def health():
    """Health check endpoint"""
    token = fetch_token_from_firebase()
    return jsonify({
        "status": "healthy",
        "token_exists": token is not None,
        "timestamp": datetime.now().isoformat()
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print("🚀 Dhan API Server Started")
    print(f"📍 URL: http://0.0.0.0:{port}")
    print(f"🔑 Client ID: {CLIENT_ID}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
