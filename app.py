from flask import Flask, render_template, request, jsonify
import requests

app = Flask(__name__)

BASE_URL = "https://api.dhan.co/v2"


# =========================
# GET EXPIRY
# =========================
def get_expiry(token, client_id):
    try:
        url = f"{BASE_URL}/optionchain/expirylist"

        headers = {
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json"
        }

        payload = {
            "UnderlyingScrip": 13,
            "UnderlyingSeg": "IDX_I"
        }

        res = requests.post(url, headers=headers, json=payload, timeout=10)
        res.raise_for_status()
        data = res.json()

        if "data" in data and len(data["data"]) > 0:
            return sorted(data["data"])[0]

    except Exception as e:
        print("Expiry Error:", e)

    return None


# =========================
# GET OPTION CHAIN
# =========================
def get_option_chain(token, client_id, expiry):
    try:
        url = f"{BASE_URL}/optionchain"

        headers = {
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json"
        }

        payload = {
            "UnderlyingScrip": 13,
            "UnderlyingSeg": "IDX_I",
            "Expiry": expiry
        }

        res = requests.post(url, headers=headers, json=payload, timeout=10)
        res.raise_for_status()
        return res.json()

    except Exception as e:
        print("Option Chain Error:", e)
        return {}


# =========================
# PROCESS DATA (FILTERED)
# =========================
def process_data(raw):
    result = []

    oc = raw.get("data", {}).get("oc", {})

    if not isinstance(oc, dict):
        return []

    for strike_str, item in oc.items():
        try:
            strike = float(strike_str)
        except:
            continue

        ce = item.get("ce", {}) or {}
        pe = item.get("pe", {}) or {}

        call_oi = ce.get("oi", 0) or 0
        put_oi = pe.get("oi", 0) or 0

        call_prev = ce.get("previous_oi", 0) or 0
        put_prev = pe.get("previous_oi", 0) or 0

        # ❌ REMOVE EMPTY STRIKES
        if call_oi == 0 and put_oi == 0:
            continue

        result.append({
            "strike": strike,
            "call_oi": call_oi,
            "put_oi": put_oi,
            "call_change": call_oi - call_prev,
            "put_change": put_oi - put_prev
        })

    result.sort(key=lambda x: x["strike"])
    return result


# =========================
# MAX PAIN
# =========================
def max_pain(data):
    if not data:
        return None

    strikes = [x["strike"] for x in data]
    min_loss = float("inf")
    best = None

    for s in strikes:
        loss = 0
        for row in data:
            loss += row["call_oi"] * max(0, s - row["strike"])
            loss += row["put_oi"] * max(0, row["strike"] - s)

        if loss < min_loss:
            min_loss = loss
            best = s

    return best


# =========================
# SUPPORT / RESISTANCE
# =========================
def find_levels(data):
    if not data:
        return None, None

    resistance = max(data, key=lambda x: x["call_oi"])["strike"]
    support = max(data, key=lambda x: x["put_oi"])["strike"]

    return support, resistance


# =========================
# PCR SIGNAL
# =========================
def get_signal(pcr):
    if pcr > 1.2:
        return "UPTREND 📈"
    elif pcr < 0.8:
        return "DOWNTREND 📉"
    return "SIDEWAYS ⚖️"


# =========================
# AI SIGNAL (SMART MONEY)
# =========================
def ai_signal(call_change, put_change):
    if put_change > 0 and call_change < 0:
        return "STRONG BULLISH 🚀"
    elif call_change > 0 and put_change < 0:
        return "STRONG BEARISH 🔻"
    elif put_change > call_change:
        return "BULLISH 📈"
    elif call_change > put_change:
        return "BEARISH 📉"
    return "SIDEWAYS ⚖️"


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/data", methods=["POST"])
def data():
    try:
        body = request.json

        token = body.get("token", "").strip()
        client_id = body.get("client_id", "").strip()

        if not token or not client_id:
            return jsonify({"error": "Token & Client ID required"}), 400

        # Step 1: Expiry
        expiry = get_expiry(token, client_id)
        if not expiry:
            return jsonify({"error": "Expiry fetch failed"}), 500

        # Step 2: Option Chain
        raw = get_option_chain(token, client_id, expiry)
        parsed = process_data(raw)

        if not parsed:
            return jsonify({"error": "No Data Found"}), 404

        # Step 3: Calculations
        total_call = sum(x["call_oi"] for x in parsed)
        total_put = sum(x["put_oi"] for x in parsed)

        total_call_change = sum(x["call_change"] for x in parsed)
        total_put_change = sum(x["put_change"] for x in parsed)

        pcr = round(total_put / total_call, 2) if total_call else 0

        support, resistance = find_levels(parsed)

        return jsonify({
            "pcr": pcr,
            "call_oi": total_call,
            "put_oi": total_put,
            "call_change_oi": total_call_change,
            "put_change_oi": total_put_change,
            "max_pain": max_pain(parsed),
            "signal": get_signal(pcr),
            "ai_signal": ai_signal(total_call_change, total_put_change),
            "support": support,
            "resistance": resistance,
            "expiry": expiry,
            "data": parsed[-15:]  # clean filtered strikes
        })

    except Exception as e:
        print("API Error:", e)
        return jsonify({"error": "Internal Server Error"}), 500


# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)