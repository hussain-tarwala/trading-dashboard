import streamlit as st
import pandas as pd
import json
from streamlit_autorefresh import st_autorefresh

# ---- MUST be FIRST Streamlit command ----
st.set_page_config(page_title="NIFTY Options Paper Trading Dashboard", layout="wide")

# ---- Autorefresh (every 10 seconds) ----
st_autorefresh(interval=10000, key="refresh-trades")

LOG_FILE = "trade_log.jsonl"

def load_trades():
    try:
        with open(LOG_FILE, "r") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []

st.title("NIFTY Options Strategy Paper Trading Dashboard")

trades = load_trades()
if trades:
    df = pd.DataFrame(trades)
    st.header("Trade Log")
    st.dataframe(df.tail(15), use_container_width=True)

    pnl = df["pnl"].sum() if "pnl" in df else 0
    curr_cap = df["capital_post"].iloc[-1] if "capital_post" in df else None
    st.metric("Total PnL", f"₹{pnl:,.2f}")
    if curr_cap:
        st.metric("Current Capital", f"₹{curr_cap:,.2f}")
    st.metric("Number of Trades", len(df))

    # Open position info
    open_pos = df[df["event"] == "OPEN"]
    closed_pos = df[df["event"] == "CLOSE"]
    if len(open_pos) > len(closed_pos):
        last_open = open_pos.iloc[-1]
        st.info(
            f"**Current Open Position:** {last_open['side']} | Qty: {last_open['qty']} | Price: {last_open['entry_price']} | Contract: {last_open['contract']}"
        )
else:
    st.warning("No trades yet, or log is missing.")
