import math
import json
from typing import Tuple, Dict, List, Optional, Any
from jugaad_data.nse import NSELive  # live NSE endpoints (index + option chain)

STRIKE_STEP = 50  # NIFTY strike increment

def _round_to_step(x: float, step: int = 50) -> int:
    return int(round(x / step) * step)

def _get_nifty_spot(n: NSELive) -> float:
    """
    This function gets the latest NIFTY 50 spot price from the NSELive API.
    The API returns a dictionary with a 'data' key that contains a list of dicts.
    Each dict in this list represents an index/stock. We look for the one with 'symbol' == 'NIFTY 50' and get 'lastPrice'.
    If that can't be found, we try the 'last' field at other levels.
    This approach ensures code doesn't break on any internal API structure change.
    """
    snap = n.live_index("NIFTY 50")
    if isinstance(snap, dict):
        if "data" in snap and isinstance(snap["data"], list):
            for entry in snap["data"]:
                if isinstance(entry, dict) and entry.get("symbol") == "NIFTY 50":
                    return float(entry.get("lastPrice", 0.0) or 0.0)
        if "last" in snap:
            return float(snap.get("last", 0.0) or 0.0)
        if "metadata" in snap and "last" in snap["metadata"]:
            return float(snap["metadata"]["last"] or 0.0)
    return 0.0

def _fetch_chain(n: NSELive) -> Dict:
    return n.index_option_chain("NIFTY")  # full NIFTY option chain JSON

def _build_chain_lookup(oc_json: Dict) -> Tuple[Dict, str, List[int]]:
    records = oc_json.get("records", {})
    expiry = records.get("expiryDates", [""])[0]  # nearest expiry
    chain = {}
    for row in records.get("data", []):
        k = row.get("strikePrice")
        if k is None:
            continue
        ce = row.get("CE", {})
        pe = row.get("PE", {})
        if k not in chain:
            chain[k] = {}
        if ce:
            chain[k]["CE"] = ce
        if pe:
            chain[k]["PE"] = pe
    strikes = sorted(chain.keys())
    return chain, expiry, strikes

def _pick(chain: Dict, side: str, strike: int, expiry: str) -> Optional[Dict[str, Any]]:
    leg = chain.get(strike, {}).get(side, {})
    if not leg:
        return None
    return {
        "expiry": expiry,
        "strike": strike,
        "side": side,
        "ltp": leg.get("lastPrice"),
        "oi": leg.get("openInterest"),
        "change_in_oi": leg.get("changeinOpenInterest"),
        "bid": leg.get("bidprice"),
        "ask": leg.get("askPrice"),
        "symbol": "NIFTY"
    }

def get_strikes_payload() -> Dict:
    """
    Returns a dict with:
      - spot, expiry, atm
      - calls: ATM, ITM1, ITM2, OTM1, OTM2
      - puts:  ATM, ITM1, ITM2, OTM1, OTM2
    """
    n = NSELive()  # default client
    spot = _get_nifty_spot(n)
    if not spot:
        return {"status": "ERROR", "reason": "No NIFTY spot available"}

    oc = _fetch_chain(n)
    chain, expiry, strikes = _build_chain_lookup(oc)

    atm = _round_to_step(spot, STRIKE_STEP)
    if atm not in strikes:
        atm = min(strikes, key=lambda k: abs(k - spot))  # snap to closest available strike

    ce_targets = [atm, atm - STRIKE_STEP, atm - 2*STRIKE_STEP, atm + STRIKE_STEP, atm + 2*STRIKE_STEP]
    pe_targets = [atm, atm + STRIKE_STEP, atm + 2*STRIKE_STEP, atm - STRIKE_STEP, atm - 2*STRIKE_STEP]

    ce_list = [_pick(chain, "CE", k, expiry) for k in ce_targets if k in strikes]
    pe_list = [_pick(chain, "PE", k, expiry) for k in pe_targets if k in strikes]

    while len(ce_list) < 5: ce_list.append(None)
    while len(pe_list) < 5: pe_list.append(None)

    return {
        "status": "OK",
        "spot": round(spot, 2),
        "expiry": expiry,
        "atm": int(atm),
        "calls": {
            "ATM": ce_list[0], "ITM1": ce_list[1], "ITM2": ce_list[2],
            "OTM1": ce_list[3], "OTM2": ce_list[4]
        },
        "puts": {
            "ATM": pe_list[0], "ITM1": pe_list[1], "ITM2": pe_list[2],
            "OTM1": pe_list[3], "OTM2": pe_list[4]
        },
    }

if __name__ == "__main__":
    print(json.dumps(get_strikes_payload(), indent=2))

# --- PaperBroker and helper, add below your functions ---
from datetime import datetime

NSE_LOT_SIZE = {
    "NIFTY": 50,
    # Add any other symbols as needed e.g. "BANKNIFTY": 15
}

def get_lot_size(symbol):
    return NSE_LOT_SIZE.get(symbol.upper(), 50)  # Default fallback

class PaperBroker:
    def __init__(self, initial_capital=100000, log_file="trade_log.jsonl", slippage_pct=0.001):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.position = None  # {"side": "LONG"/"SHORT", "entry": ..., "qty": ..., "contract": {...}}
        self.open_time = None
        self.closed_trades = []  # list of dicts
        self.log_file = log_file
        self.slippage_pct = slippage_pct

    def enter(self, side, price, contract, timestamp=None):
        if self.position is not None:
            self.log_event("REJECTED", "Already in position")
            return False
        
        lot_size = get_lot_size(contract.get("symbol", "NIFTY"))
        qty = lot_size
        cost = price * qty
        if self.capital < cost:
            self.log_event("REJECTED", f"Insufficient capital for trade: have {self.capital:.2f}, need {cost:.2f}")
            return False

        fill_price = price + (self.slippage_pct * price if side == "LONG" else -self.slippage_pct * price)
        self.position = {
            "side": side,
            "entry": fill_price,
            "qty": qty,
            "contract": contract,
        }
        self.open_time = timestamp or datetime.now().isoformat()
        self.capital -= cost
        self.log_event("OPEN", f"Entered {side}", extra={
            "entry_price": fill_price,
            "contract": contract,
            "qty": qty,
            "capital": self.capital,
            "timestamp": self.open_time
        })
        return True

    def exit(self, price, timestamp=None, reason=None):
        if self.position is None:
            self.log_event("REJECTED", "No open position to exit")
            return False

        side = self.position["side"]
        lot = self.position["qty"]
        entry = self.position["entry"]
        contract = self.position["contract"]

        fill_price = price - (self.slippage_pct * price if side == "LONG" else -self.slippage_pct * price)

        if side == "LONG":
            pnl = (fill_price - entry) * lot
        else:
            pnl = (entry - fill_price) * lot

        self.capital += fill_price * lot  # Release cash from close
        trade = {
            "side": side,
            "entry_price": entry,
            "exit_price": fill_price,
            "qty": lot,
            "contract": contract,
            "entry_time": self.open_time,
            "exit_time": timestamp or datetime.now().isoformat(),
            "pnl": pnl,
            "capital_post": self.capital,
            "reason": reason
        }
        self.closed_trades.append(trade)
        self.position = None
        self.open_time = None
        self.log_event("CLOSE", "Closed position", extra=trade)
        return pnl

    def update_mark_to_market(self, last_price):
        if self.position is None:
            return 0
        entry = self.position["entry"]
        lot = self.position["qty"]
        side = self.position["side"]
        if side == "LONG":
            return (last_price - entry) * lot
        else:
            return (entry - last_price) * lot

    def log_event(self, event, message, extra=None):
        record = {
            "event": event,
            "message": message,
            "capital": self.capital,
            "time": datetime.now().isoformat(),
        }
        if extra:
            record.update(extra)
        with open(self.log_file, "a") as f:
            f.write(json.dumps(record) + "\n")

    def summary(self):
        total_pnl = sum(trade["pnl"] for trade in self.closed_trades)
        return {
            "initial_capital": self.initial_capital,
            "current_capital": self.capital,
            "total_pnl": total_pnl,
            "num_trades": len(self.closed_trades)
        }
