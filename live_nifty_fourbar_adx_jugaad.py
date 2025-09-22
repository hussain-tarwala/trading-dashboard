import os
import math
import time
import json
import requests
import pandas as pd
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime, timedelta, timezone

from jugaad_data.nse import NSELive
from option_chain import get_strikes_payload, PaperBroker

# -------- Config --------
IST = timezone(timedelta(hours=5, minutes=30))
INDEX_NAME = "NIFTY 50"
SESSION_START = (9, 15)
SESSION_END   = (15, 30)
STRIKE_STEP = 50
ADX_PERIOD = 14
ADX_THRESHOLD = 20.0

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# -------- Network hardening --------
def make_nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    })
    retries = Retry(total=5, backoff_factor=1.5,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET", "HEAD"],
                    raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retries))
    try:
        s.get("https://www.nseindia.com/", timeout=6)
    except Exception:
        pass
    return s

def build_nselive():
    n = NSELive()
    try:
        n.session = make_nse_session()
    except Exception:
        pass
    return n

def safe_live(func, *args, **kwargs):
    for i in range(5):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException:
            time.sleep(2.5 * (i+1))
        except Exception:
            time.sleep(1.5 * (i+1))
    return None

# -------- Time helpers --------
def ist_now():
    return datetime.now(tz=IST)

def in_session(ts):
    return ts.weekday() < 5 and (ts.hour, ts.minute) >= SESSION_START and (ts.hour, ts.minute) <= SESSION_END

def floor_15m(ts):
    mins = ts.minute - (ts.minute % 15)
    return ts.replace(minute=mins, second=0, microsecond=0)

# -------- Minimal daily ADX tracker (incremental Wilder) --------
class ADXTracker:
    def __init__(self, period=14):
        self.p = period
        self.ready = False
        self.prev = None
    def seed(self, close):
        self.prev = {"close": float(close), "high": float(close), "low": float(close),
                     "tr_s": 1e-9, "+dm_s": 0.0, "-dm_s": 0.0, "adx": 0.0}
        self.ready = True
    def update(self, high, low, close):
        if not self.ready:
            self.seed(close)
        p = self.p
        tr = max(high - low, abs(high - self.prev["close"]), abs(low - self.prev["close"]))
        plus_dm = max(high - self.prev["high"], 0.0)
        minus_dm = max(self.prev["low"] - low, 0.0)
        if plus_dm < minus_dm: plus_dm = 0.0
        else: minus_dm = 0.0
        tr_s = self.prev["tr_s"] - (self.prev["tr_s"]/p) + tr
        pdm_s = self.prev["+dm_s"] - (self.prev["+dm_s"]/p) + plus_dm
        mdm_s = self.prev["-dm_s"] - (self.prev["-dm_s"]/p) + minus_dm
        plus_di = 100.0 * (pdm_s / tr_s) if tr_s else 0.0
        minus_di = 100.0 * (mdm_s / tr_s) if tr_s else 0.0
        dx = 100.0 * (abs(plus_di - minus_di) / (plus_di + minus_di)) if (plus_di + minus_di) else 0.0
        adx = ((self.prev["adx"] * (p - 1)) + dx) / p if self.prev["adx"] else dx
        self.prev.update({"close": close, "high": high, "low": low, "tr_s": tr_s, "+dm_s": pdm_s, "-dm_s": mdm_s, "adx": adx})
        return adx

# -------- 4-bar Darvas-style breakout on 15m --------
def four_bar_signal(df_15m: pd.DataFrame):
    if len(df_15m) < 4:
        return None
    last4 = df_15m.tail(4)
    c = last4["close"].values; h = last4["high"].values; l = last4["low"].values
    inc = all(c[i] > c[i-1] for i in range(1,4))
    dec = all(c[i] < c[i-1] for i in range(1,4))
    box_high = float(max(h)); box_low = float(min(l))
    if inc:
        return {"dir":"LONG",  "trigger":box_high, "box_high":box_high, "box_low":box_low}
    if dec:
        return {"dir":"SHORT", "trigger":box_low,  "box_high":box_high, "box_low":box_low}
    return None

# -------- Runner --------
def run():
    n = build_nselive()
    bars = pd.DataFrame(columns=["datetime","open","high","low","close"])
    last_completed = None
    adx = ADXTracker(ADX_PERIOD)
    broker = PaperBroker(initial_capital=100000, log_file="trade_log.jsonl", slippage_pct=0.001)
    print("Live NIFTY 15m Darvas + ADX(14) | CE on up-breakout, PE on breakdown")

    while True:
        nowi = ist_now()
        # market state check
        try:
            mstat = safe_live(n.market_status) or {}
            ms_list = mstat.get("marketState", []) if isinstance(mstat, dict) else []
            cap = next((x for x in ms_list if x.get("market") == "Capital Market"), ms_list if ms_list else {})
            is_closed = str(cap.get("marketStatus", "")).lower().startswith("close")
        except Exception:
            is_closed = False

        if not in_session(nowi) or is_closed:
            print(f"[{nowi.strftime('%a %H:%M:%S')}] Market CLOSED; sleeping 60s.")
            time.sleep(60); continue

        # spot snapshot
        li = safe_live(n.live_index, INDEX_NAME) or {}
        spot = float(li.get("data", {}).get("last", li.get("last", 0.0)) or li.get("last") or 0.0)
        if not spot:
            time.sleep(3); continue

        # 15m aggregation
        slot = floor_15m(nowi)
        if len(bars) == 0 or pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime().replace(tzinfo=IST) < slot:
            bars = pd.concat([bars, pd.DataFrame([{
                "datetime": pd.Timestamp(slot), "open": spot, "high": spot, "low": spot, "close": spot
            }])], ignore_index=True)
        else:
            bars.loc[bars.index[-1], ["high","low","close"]] = [
                max(bars.loc[bars.index[-1], "high"], spot),
                min(bars.loc[bars.index[-1], "low"],  spot),
                spot,
            ]

        # evaluate on completed bar
        completed = slot - timedelta(minutes=15)
        if last_completed is None or completed > last_completed:
            last_completed = completed

            # approximate daily ADX update
            prev_close = adx.prev["close"] if adx.ready else spot
            day_high = max(prev_close, float(bars["high"].iloc[-1]))
            day_low  = min(prev_close, float(bars["low"].iloc[-1]))
            day_close = float(bars["close"].iloc[-1])
            adx_val = adx.update(day_high, day_low, day_close)
            df_comp = bars[bars["datetime"] <= pd.Timestamp(completed)].copy()
            sig = four_bar_signal(df_comp) if len(df_comp) >= 4 else None

            # ---- ENTRY logic (on SIGNAL, during market, no open position) ----
            if sig and adx_val > ADX_THRESHOLD and broker.position is None:
                strikes = get_strikes_payload()
                oc_leg = None
                price = None
                if strikes.get("status") == "OK":
                    if sig["dir"] == "LONG":
                        oc_leg = strikes["calls"]["ATM"] or strikes["calls"]["OTM1"]
                    else:
                        oc_leg = strikes["puts"]["ATM"] or strikes["puts"]["OTM1"]
                    if oc_leg and oc_leg.get("ask", 0) > 0:
                        price = oc_leg["ask"]
                        broker.enter(sig["dir"], price, oc_leg, timestamp=nowi.isoformat())

            # ---- EXIT logic (example: close on opposite signal, add your own SL/TP as needed) ----
            if broker.position is not None:
                # Always get latest option leg for the open position type
                strikes = get_strikes_payload()
                if broker.position["side"] == "LONG":
                    oc_leg = strikes["calls"]["ATM"] or strikes["calls"]["OTM1"]
                    price = oc_leg["bid"] if oc_leg and oc_leg.get("bid", 0) > 0 else None
                    # Example: Exit on SHORT signal or your own stop/TP logic
                    if sig and sig["dir"] == "SHORT" and price is not None:
                        broker.exit(price, timestamp=nowi.isoformat(), reason="Opposite signal (SHORT)")
                else:
                    oc_leg = strikes["puts"]["ATM"] or strikes["puts"]["OTM1"]
                    price = oc_leg["bid"] if oc_leg and oc_leg.get("bid", 0) > 0 else None
                    # Example: Exit on LONG signal or your own stop/TP logic
                    if sig and sig["dir"] == "LONG" and price is not None:
                        broker.exit(price, timestamp=nowi.isoformat(), reason="Opposite signal (LONG)")

            # Optional: print paper broker summary/logs
            summ = broker.summary()
            print(f"Current capital: {summ['current_capital']:.2f}, PnL: {summ['total_pnl']:.2f}, Trades: {summ['num_trades']}")

        time.sleep(3)

if __name__ == "__main__":
    run()
