import MetaTrader5 as mt5
from google import genai
import pandas as pd
import pandas_ta as ta
import time
import os
import json
import pytz
from datetime import datetime
from dotenv import load_dotenv

# ==========================================
# 1. LOAD CONFIG FROM .ENV
# ==========================================
load_dotenv()

API_KEYS = [
    os.getenv("GEMINI_KEY_1"),
    os.getenv("GEMINI_KEY_2")
]
LOG_DIRECTORY = os.getenv("MT5_LOG_PATH")
SYMBOL = "XAUUSD"
RISK_PERCENT = 5.0
key_index = 0
ACTIVE_MODEL = None

# ==========================================
# 2. TIME GUARD (Harare CAT)
# ==========================================
def is_trading_time():
    tz = pytz.timezone('Africa/Harare')
    now = datetime.now(tz)
    weekday = now.weekday()
    # Mon-Thu: 10:00 to 19:00 | Fri: 10:00 to 17:00
    if 0 <= weekday <= 3: return 10 <= now.hour < 19
    elif weekday == 4: return 10 <= now.hour < 17
    return False

# ==========================================
# 3. INITIALIZATION
# ==========================================
def initialize_system():
    global ACTIVE_MODEL, key_index
    if not mt5.initialize(): 
        print("❌ MT5 Initialization Failed")
        return False
    
    account_info = mt5.account_info()
    if account_info:
        print(f"✅ Connected to MT5 account: {account_info.login}")
    
    # Priority check for available models
    priority_models = ["models/gemini-2.5-flash", "models/gemini-1.5-flash"]
    for i in range(len(API_KEYS)):
        if not API_KEYS[i]: continue
        try:
            client = genai.Client(api_key=API_KEYS[i])
            for model_name in priority_models:
                try:
                    client.models.generate_content(model=model_name, contents="ping")
                    ACTIVE_MODEL, key_index = model_name, i
                    print(f"🚀 SMC Agent V3 Online! Key_{i} verified.")
                    return True
                except: continue
        except: continue
    print("❌ All API Keys failed. Check your .env file.")
    return False

# ==========================================
# 4. MARKET CONTEXT (M15 Trend | M5 Health)
# ==========================================
def get_smc_context(signal_type):
    try:
        # Fetch data for Trend (M15) and Momentum (M5)
        m15_r = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 100)
        m5_r = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 20)
        
        if m15_r is None or m5_r is None: return {"passed": False, "msg": "MT5 Data Link Down"}
        
        df_m15 = pd.DataFrame(m15_r)
        df_m5 = pd.DataFrame(m5_r)
        
        # M15 Trend Filter
        ema50_m15 = ta.ema(df_m15['close'], length=50).iloc[-1]
        
        tick = mt5.symbol_info_tick(SYMBOL)
        curr_price = tick.ask if signal_type == "BUY" else tick.bid
        
        # Trend Alignment
        trend_ok = (signal_type == "BUY" and curr_price > ema50_m15) or \
                   (signal_type == "SELL" and curr_price < ema50_m15)
        
        # M5 Momentum Health Check
        last_m5 = df_m5.iloc[-1]
        m5_body = abs(last_m5['open'] - last_m5['close'])
        health_ok = True 
        
        # Filter out if current candle is strongly opposite
        if signal_type == "BUY" and last_m5['close'] < last_m5['open'] and m5_body > 2.0:
            health_ok = False
        if signal_type == "SELL" and last_m5['close'] > last_m5['open'] and m5_body > 2.0:
            health_ok = False

        return {
            "passed": trend_ok and health_ok,
            "data": {
                "signal": signal_type,
                "curr_price": curr_price,
                "trend_pos": "BULLISH" if curr_price > ema50_m15 else "BEARISH",
                "atr_m5": ta.atr(df_m5['high'], df_m5['low'], df_m5['close']).iloc[-1],
                "balance": mt5.account_info().balance
            },
            "msg": f"M15 Trend:{trend_ok} | M5 Health:{health_ok}"
        }
    except Exception as e:
        return {"passed": False, "msg": f"SMC Error: {str(e)}"}

# ==========================================
# 5. AI ANALYST (Fixed 1:2 RR)
# ==========================================
def ask_ai(ctx):
    global key_index
    prompt = f"""
    Act as an SMC Trading Expert. An M5 {ctx['signal']} Signal detected.
    Current Price: {ctx['curr_price']}.
    M15 Trend Filter (EMA50): {ctx['trend_pos']}.
    M5 ATR: {ctx['atr_m5']}.
    Output JSON: {{'action': 'EXECUTE'/'REJECT', 'sl': float, 'tp': float, 'reason': str}}
    Rule: Target 1:2 Risk-to-Reward Ratio. SL behind M5 structure.
    """
    
    for attempt in range(len(API_KEYS) * 2):
        client = genai.Client(api_key=API_KEYS[key_index % len(API_KEYS)])
        try:
            res = client.models.generate_content(
                model=ACTIVE_MODEL,
                config={'response_mime_type': 'application/json', 'temperature': 0.1},
                contents=prompt
            )
            return json.loads(res.text)
        except Exception as e:
            if "429" in str(e):
                key_index += 1
                time.sleep(2)
                continue
            break
    return {"action": "REJECT", "reason": "AI Connection Exhausted"}

# ==========================================
# 6. EXECUTION ENGINE
# ==========================================
def execute_trade(decision, ctx):
    if decision['action'] == "EXECUTE":
        info = mt5.symbol_info(SYMBOL)
        risk_amt = mt5.account_info().balance * (RISK_PERCENT / 100)
        sl_dist = abs(ctx['curr_price'] - decision['sl'])
        
        if sl_dist < 0.1: return 
        
        lots = risk_amt / (sl_dist * 100)
        lots = max(info.volume_min, min(round(lots / info.volume_step) * info.volume_step, 10.0))
        
        order_type = mt5.ORDER_TYPE_BUY if ctx['signal'] == "BUY" else mt5.ORDER_TYPE_SELL
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": lots,
            "type": order_type, "price": ctx['curr_price'],
            "sl": float(decision['sl']), "tp": float(decision['tp']),
            "magic": 2026, "comment": "SMC_V3_ENV", "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(request)
        if res.retcode == 10009: 
            print(f"✅ SUCCESS: {ctx['signal']} {lots} Lots. Logic: {decision['reason']}")
        else: 
            print(f"❌ FAILED: {res.comment}")
    else:
        print(f"✋ AI REJECTED: {decision['reason']}")

# ==========================================
# 7. MAIN LISTENER LOOP
# ==========================================
def main_loop():
    if not initialize_system(): return
    current_log, file_handle = None, None
    print(f"🎧 SMC Agent V3 Active: Listening for M5 Signals...")

    while True:
        if not is_trading_time():
            now_str = datetime.now().strftime('%H:%M:%S')
            print(f"😴 Resting... {now_str} (Outside Harare Trading Hours)        ", end="\r")
            time.sleep(30); continue

        try:
            today = datetime.now().strftime("%Y%m%d") + ".log"
            log_path = os.path.join(LOG_DIRECTORY, today)

            if log_path != current_log:
                if file_handle: file_handle.close()
                current_log = log_path
                if os.path.exists(current_log):
                    file_handle = open(current_log, "r", encoding="utf-16-le", errors="ignore")
                    file_handle.seek(0, os.SEEK_END)

            if not file_handle:
                time.sleep(1); continue

            line = file_handle.readline()
            if "Alert: (OB)" in line:
                signal_type = "BUY" if "+OB" in line else "SELL"
                print(f"\n🔔 SIGNAL DETECTED: {signal_type} [{datetime.now().strftime('%H:%M:%S')}]")
                
                ctx = get_smc_context(signal_type)
                if ctx['passed']:
                    decision = ask_ai(ctx['data'])
                    execute_trade(decision, ctx['data'])
                else:
                    print(f"✋ FILTERED: {ctx['msg']}")
                
                time.sleep(10) # Cooldown

        except Exception as e: 
            print(f"\n⚠️ Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\n\nexception: Agent stopped by user")
    finally:
        mt5.shutdown()