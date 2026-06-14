from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import secrets
import csv
import io
import json
import os
from kotak_client import KotakClient
import requests
app = FastAPI()
DB = "trades.db"

# ================= BASIC SETTINGS =================
MAX_TRADES_PER_DAY = 20
MAX_DAILY_LOSS = -2000

# PAPER = only paper trade
# TEST  = paper trade + broker order payload log
# LIVE  = future Kotak API real order
TRADING_MODE = os.getenv("TRADING_MODE", "TEST")

# ================= LOGIN =================
security = HTTPBasic()
USERNAME = os.getenv("APP_USER", "admin")
PASSWORD = os.getenv("APP_PASSWORD", "12345")


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    if not (
        secrets.compare_digest(credentials.username, USERNAME)
        and secrets.compare_digest(credentials.password, PASSWORD)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Login",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ================= SIGNAL MODEL =================
class Signal(BaseModel):
    strategy_id: str = "A"
    symbol: str
    signal: str
    price: float

    trade_type: str = "EQUITY"      # EQUITY / OPTION
    option_type: str = ""           # CE / PE
    strike: float = 0
    expiry: str = ""
    lot_size: int = 1
    lots: int = 1

    exchange: str = "NSE"           # NSE / NFO
    product: str = "MIS"            # MIS / CNC / NRML
    order_type: str = "MARKET"      # MARKET / LIMIT
    validity: str = "DAY"
    disclosed_qty: int = 0
    trigger_price: float = 0

    alert_id: str | None = None


# ================= DB HELPERS =================
def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")


def init_db():
    c = conn()

    c.execute("""
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        trade_date TEXT,
        strategy_id TEXT,
        symbol TEXT,
        trade_type TEXT,
        option_type TEXT,
        strike REAL,
        expiry TEXT,
        side TEXT,
        entry REAL,
        exit REAL,
        lot_size INTEGER,
        lots INTEGER,
        qty INTEGER,
        status TEXT,
        pnl REAL,
        exit_reason TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_hash TEXT UNIQUE,
        time TEXT,
        raw TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS broker_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        mode TEXT,
        strategy_id TEXT,
        symbol TEXT,
        signal TEXT,
        order_side TEXT,
        qty INTEGER,
        price REAL,
        exchange TEXT,
        product TEXT,
        order_type TEXT,
        validity TEXT,
        status TEXT,
        payload TEXT
    )
    """)

    c.commit()
    c.close()
    oi_symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    oi_data = []

    for sym in oi_symbols:
        oi_data.append(fetch_nse_oi(sym))


init_db()
def fetch_nse_oi(symbol="NIFTY"):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }

        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=10)

        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        data = session.get(url, headers=headers, timeout=10).json()

        records = data["records"]["data"]

        ce_total = 0
        pe_total = 0
        max_ce = 0
        max_pe = 0
        max_ce_strike = 0
        max_pe_strike = 0

        for item in records:
            strike = item.get("strikePrice", 0)

            if "CE" in item:
                ce_oi = item["CE"].get("openInterest", 0)
                ce_total += ce_oi
                if ce_oi > max_ce:
                    max_ce = ce_oi
                    max_ce_strike = strike

            if "PE" in item:
                pe_oi = item["PE"].get("openInterest", 0)
                pe_total += pe_oi
                if pe_oi > max_pe:
                    max_pe = pe_oi
                    max_pe_strike = strike

        pcr = round(pe_total / ce_total, 2) if ce_total else 0

        sentiment = "NEUTRAL"
        if pcr > 1.1:
            sentiment = "BULLISH"
        elif pcr < 0.9:
            sentiment = "BEARISH"

        return {
            "symbol": symbol,
            "ce_total": ce_total,
            "pe_total": pe_total,
            "pcr": pcr,
            "max_ce": max_ce_strike,
            "max_pe": max_pe_strike,
            "sentiment": sentiment
        }

    except Exception as e:
        return {
            "symbol": symbol,
            "error": str(e)
        }


# ================= BROKER PAYLOAD =================
def build_broker_payload(s: Signal, order_side: str, qty: int):
    payload = {
        "strategy_id": s.strategy_id.upper(),
        "symbol": s.symbol.upper(),
        "transaction_type": order_side,
        "quantity": qty,
        "price": float(s.price),
        "exchange": s.exchange.upper(),
        "product": s.product.upper(),
        "order_type": s.order_type.upper(),
        "validity": s.validity.upper(),
        "disclosed_qty": s.disclosed_qty,
        "trigger_price": s.trigger_price,
        "trade_type": s.trade_type.upper(),
        "option_type": s.option_type.upper(),
        "strike": s.strike,
        "expiry": s.expiry
    }
    return payload


def log_broker_order(s: Signal, order_side: str, qty: int, status_text: str):
    payload = build_broker_payload(s, order_side, qty)

    c = conn()
    c.execute("""
    INSERT INTO broker_orders(
        time, mode, strategy_id, symbol, signal, order_side, qty, price,
        exchange, product, order_type, validity, status, payload
    )
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now(),
        TRADING_MODE,
        s.strategy_id.upper(),
        s.symbol.upper(),
        s.signal.upper(),
        order_side,
        qty,
        float(s.price),
        s.exchange.upper(),
        s.product.upper(),
        s.order_type.upper(),
        s.validity.upper(),
        status_text,
        json.dumps(payload)
    ))
    c.commit()
    c.close()


def broker_action(s: Signal, order_side: str, qty: int):

    if TRADING_MODE == "PAPER":
        return

    if TRADING_MODE == "TEST":
        log_broker_order(
            s,
            order_side,
            qty,
            "TEST_ORDER_NOT_SENT"
        )
        return

    if TRADING_MODE == "LIVE":

        payload = build_broker_payload(
            s,
            order_side,
            qty
        )

        kotak = KotakClient()

        result = kotak.place_order(
            payload
        )

        status_text = result.get(
            "status",
            "LIVE_UNKNOWN"
        )

        log_broker_order(
            s,
            order_side,
            qty,
            status_text
        )

        return
    if TRADING_MODE == "TEST":
        log_broker_order(s, order_side, qty, "TEST_ORDER_NOT_SENT")
        return

    if TRADING_MODE == "LIVE":
        # Future Kotak Neo API order placement yaha add hoga
        log_broker_order(s, order_side, qty, "LIVE_PLACEHOLDER_NOT_SENT")
        return


# ================= ROUTES =================
@app.get("/")
def home():
    return {
        "status": "Algo Dashboard Running",
        "mode": TRADING_MODE,
        "dashboard": "/dashboard",
        "webhook": "/webhook"
    }


@app.post("/webhook")
def webhook(s: Signal):
    signal = s.signal.upper()
    symbol = s.symbol.upper()
    strategy = s.strategy_id.upper()
    price = float(s.price)
    trade_type = s.trade_type.upper()
    option_type = s.option_type.upper()
    qty = int(s.lot_size) * int(s.lots)

    raw = f"{strategy}-{symbol}-{signal}-{price}-{qty}-{trade_type}-{s.alert_id}"
    alert_hash = hashlib.sha256(raw.encode()).hexdigest()

    c = conn()

    try:
        c.execute(
            "INSERT INTO alerts(alert_hash,time,raw) VALUES(?,?,?)",
            (alert_hash, now(), raw)
        )
        c.commit()
    except:
        c.close()
        return {"status": "ignored", "reason": "Duplicate alert blocked"}

    closed_today = c.execute(
        "SELECT * FROM trades WHERE trade_date=? AND status='CLOSED'",
        (today(),)
    ).fetchall()

    daily_pnl = sum(t["pnl"] for t in closed_today)
    trades_today = len(closed_today)

    if daily_pnl <= MAX_DAILY_LOSS:
        c.close()
        return {"status": "blocked", "reason": "Max daily loss hit"}

    if trades_today >= MAX_TRADES_PER_DAY and signal in ["BUY", "SELL"]:
        c.close()
        return {"status": "blocked", "reason": "Max trades per day hit"}

    open_trade = c.execute("""
        SELECT * FROM trades
        WHERE symbol=? AND strategy_id=? AND status='OPEN'
        ORDER BY id DESC LIMIT 1
    """, (symbol, strategy)).fetchone()

    if signal in ["BUY", "SELL"]:
        if open_trade:
            c.close()
            return {"status": "ignored", "reason": "Open trade already exists"}

        c.execute("""
        INSERT INTO trades(
            time,trade_date,strategy_id,symbol,trade_type,option_type,
            strike,expiry,side,entry,exit,lot_size,lots,qty,status,pnl,exit_reason
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now(), today(), strategy, symbol, trade_type, option_type,
            s.strike, s.expiry, signal, price, None,
            s.lot_size, s.lots, qty, "OPEN", 0, None
        ))

        c.commit()
        c.close()

        broker_action(s, signal, qty)

        return {
            "status": "success",
            "message": f"{signal} trade opened",
            "mode": TRADING_MODE,
            "qty": qty
        }

    if signal in ["EXIT", "CLOSE"]:
        if not open_trade:
            c.close()
            return {"status": "ignored", "reason": "No open trade"}

        entry = open_trade["entry"]
        side = open_trade["side"]

        pnl = (price - entry) * open_trade["qty"] if side == "BUY" else (entry - price) * open_trade["qty"]

        c.execute("""
        UPDATE trades
        SET exit=?, status='CLOSED', pnl=?, exit_reason=?
        WHERE id=?
        """, (price, pnl, "WEBHOOK EXIT", open_trade["id"]))

        c.commit()
        c.close()

        exit_side = "SELL" if side == "BUY" else "BUY"
        broker_action(s, exit_side, open_trade["qty"])

        return {
            "status": "success",
            "message": "Trade closed",
            "mode": TRADING_MODE,
            "pnl": pnl
        }

    c.close()
    return {"status": "error", "reason": "Invalid signal"}


@app.get("/reset")
def reset(user: str = Depends(authenticate)):
    c = conn()
    c.execute("DELETE FROM trades")
    c.execute("DELETE FROM alerts")
    c.execute("DELETE FROM broker_orders")
    c.commit()
    c.close()
    return RedirectResponse("/dashboard")


@app.get("/export_csv")
def export_csv(user: str = Depends(authenticate)):
    c = conn()
    trades = c.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
    c.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID", "Time", "Date", "Strategy", "Symbol", "Trade Type",
        "Option Type", "Strike", "Expiry", "Side", "Entry", "Exit",
        "Lot Size", "Lots", "Qty", "Status", "PnL", "Exit Reason"
    ])

    for t in trades:
        writer.writerow([
            t["id"], t["time"], t["trade_date"], t["strategy_id"],
            t["symbol"], t["trade_type"], t["option_type"], t["strike"],
            t["expiry"], t["side"], t["entry"], t["exit"], t["lot_size"],
            t["lots"], t["qty"], t["status"], t["pnl"], t["exit_reason"]
        ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=algo_trades_report.csv"}
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(strategy_filter: str = "ALL", user: str = Depends(authenticate)):
    selected_strategy = strategy_filter.upper()

    c = conn()
    all_trades = c.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
    broker_orders = c.execute("SELECT * FROM broker_orders ORDER BY id DESC LIMIT 50").fetchall()

    if selected_strategy == "ALL":
        trades = all_trades
    else:
        trades = c.execute(
            "SELECT * FROM trades WHERE strategy_id=? ORDER BY id DESC",
            (selected_strategy,)
        ).fetchall()

    c.close()

    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_trades = [t for t in trades if t["status"] == "OPEN"]
    today_closed = [t for t in closed if t["trade_date"] == today()]

    option_closed = [t for t in closed if t["trade_type"] == "OPTION"]
    equity_closed = [t for t in closed if t["trade_type"] == "EQUITY"]

    total_pnl = sum(t["pnl"] for t in closed)
    daily_pnl = sum(t["pnl"] for t in today_closed)
    option_pnl = sum(t["pnl"] for t in option_closed)
    equity_pnl = sum(t["pnl"] for t in equity_closed)

    total_trades = len(closed)
    wins = len([t for t in closed if t["pnl"] > 0])
    losses = len([t for t in closed if t["pnl"] < 0])
    winrate = round((wins / total_trades) * 100, 2) if total_trades else 0

    strategy_stats = {}
    daily_stats = {}

    for t in all_trades:
        sid = t["strategy_id"]
        d = t["trade_date"]

        if sid not in strategy_stats:
            strategy_stats[sid] = {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "open": 0}

        if t["status"] == "OPEN":
            strategy_stats[sid]["open"] += 1

        if t["status"] == "CLOSED":
            strategy_stats[sid]["pnl"] += t["pnl"]
            strategy_stats[sid]["trades"] += 1

            if t["pnl"] > 0:
                strategy_stats[sid]["wins"] += 1
            elif t["pnl"] < 0:
                strategy_stats[sid]["losses"] += 1

            if d not in daily_stats:
                daily_stats[d] = {"pnl": 0, "trades": 0, "wins": 0, "losses": 0}

            daily_stats[d]["pnl"] += t["pnl"]
            daily_stats[d]["trades"] += 1

            if t["pnl"] > 0:
                daily_stats[d]["wins"] += 1
            elif t["pnl"] < 0:
                daily_stats[d]["losses"] += 1

    strategy_options = '<option value="ALL">ALL</option>'
    for sid in sorted(strategy_stats.keys()):
        selected = "selected" if selected_strategy == sid else ""
        strategy_options += f'<option value="{sid}" {selected}>{sid}</option>'

    strategy_cards = ""
    for sid, st in sorted(strategy_stats.items()):
        wr = round((st["wins"] / st["trades"]) * 100, 2) if st["trades"] else 0
        color = "green" if st["pnl"] > 0 else "red" if st["pnl"] < 0 else "black"

        strategy_cards += f"""
        <div class="card">
            <h3>Strategy {sid}</h3>
            <h2 style="color:{color};">{round(st['pnl'], 2)}</h2>
            <p>Closed: {st['trades']}</p>
            <p>Open: {st['open']}</p>
            <p>Win Rate: {wr}%</p>
        </div>
        """

    daily_rows = ""
    for d, st in sorted(daily_stats.items(), reverse=True):
        wr = round((st["wins"] / st["trades"]) * 100, 2) if st["trades"] else 0
        color = "green" if st["pnl"] > 0 else "red" if st["pnl"] < 0 else "black"

        daily_rows += f"""
        <tr>
            <td>{d}</td>
            <td>{st['trades']}</td>
            <td>{st['wins']}</td>
            <td>{st['losses']}</td>
            <td>{wr}%</td>
            <td style="color:{color};font-weight:bold;">{round(st['pnl'],2)}</td>
        </tr>
        """

    rows = ""
    for t in trades:
        pnl_color = "green" if t["pnl"] > 0 else "red" if t["pnl"] < 0 else "black"

        rows += f"""
        <tr>
            <td>{t['id']}</td>
            <td>{t['time']}</td>
            <td>{t['strategy_id']}</td>
            <td>{t['symbol']}</td>
            <td>{t['trade_type']}</td>
            <td>{t['side']}</td>
            <td>{t['entry']}</td>
            <td>{t['exit']}</td>
            <td>{t['qty']}</td>
            <td>{t['status']}</td>
            <td style="color:{pnl_color};font-weight:bold;">{round(t['pnl'], 2)}</td>
            <td>{t['exit_reason']}</td>
        </tr>
        """

    broker_rows = ""
    for b in broker_orders:
        broker_rows += f"""
        <tr>
            <td>{b['id']}</td>
            <td>{b['time']}</td>
            <td>{b['mode']}</td>
            <td>{b['strategy_id']}</td>
            <td>{b['symbol']}</td>
            <td>{b['signal']}</td>
            <td>{b['order_side']}</td>
            <td>{b['qty']}</td>
            <td>{b['price']}</td>
            <td>{b['exchange']}</td>
            <td>{b['product']}</td>
            <td>{b['order_type']}</td>
            <td>{b['status']}</td>
        </tr>
        """
        oi_rows = ""

for oi in oi_data:

    if "error" in oi:

        oi_rows += f"""
        <tr>
            <td>{oi['symbol']}</td>
            <td colspan="6">{oi['error']}</td>
        </tr>
        """

    else:

        oi_rows += f"""
        <tr>
            <td>{oi['symbol']}</td>
            <td>{oi['ce_total']}</td>
            <td>{oi['pe_total']}</td>
            <td>{oi['pcr']}</td>
            <td>{oi['max_ce']}</td>
            <td>{oi['max_pe']}</td>
            <td>{oi['sentiment']}</td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <title>Algo Dashboard</title>
        <style>
            body {{ font-family: Arial; background:#f4f6f8; padding:20px; }}
            h1,h2 {{ color:#111; }}
            .topbar {{ display:flex; gap:10px; margin-bottom:15px; flex-wrap:wrap; align-items:center; }}
            .btn {{ padding:10px 15px; background:#111; color:white; border-radius:6px; text-decoration:none; border:none; cursor:pointer; }}
            .danger {{ background:#c0392b; }}
            .download {{ background:#2980b9; }}
            .mode {{ background:#fff3cd; padding:10px; border-radius:6px; margin-bottom:15px; font-weight:bold; }}
            .cards {{ display:flex; gap:15px; flex-wrap:wrap; margin-bottom:20px; }}
            .card {{ background:white; padding:18px; border-radius:10px; min-width:170px; box-shadow:0 2px 6px #ccc; }}
            table {{ width:100%; border-collapse:collapse; background:white; margin-top:15px; margin-bottom:30px; font-size:13px; }}
            th,td {{ padding:8px; border:1px solid #ddd; text-align:center; }}
            th {{ background:#111; color:white; }}
            input,select {{ padding:8px; border-radius:5px; border:1px solid #aaa; }}
            button {{ padding:8px 12px; background:#111; color:white; border:none; border-radius:5px; cursor:pointer; }}
        </style>
    </head>
    <body>
        <h1>Algo Paper + Broker Test Dashboard</h1>

        <div class="mode">Current Mode: {TRADING_MODE}</div>

        <div class="topbar">
            <a class="btn" href="/dashboard">Manual Refresh</a>
            <a class="btn download" href="/export_csv">Download CSV</a>
            <a class="btn danger" href="/reset">Reset All Trades</a>

            <form method="get" action="/dashboard">
                <label><b>Filter Strategy:</b></label>
                <select name="strategy_filter">{strategy_options}</select>
                <button type="submit">Apply</button>
            </form>
        </div>

        <div class="cards">
            <div class="card"><h3>Selected Strategy</h3><h2>{selected_strategy}</h2></div>
            <div class="card"><h3>Total P&L</h3><h2>{round(total_pnl,2)}</h2></div>
            <div class="card"><h3>Today P&L</h3><h2>{round(daily_pnl,2)}</h2></div>
            <div class="card"><h3>Option P&L</h3><h2>{round(option_pnl,2)}</h2></div>
            <div class="card"><h3>Equity P&L</h3><h2>{round(equity_pnl,2)}</h2></div>
            <div class="card"><h3>Open Trades</h3><h2>{len(open_trades)}</h2></div>
            <div class="card"><h3>Closed Trades</h3><h2>{total_trades}</h2></div>
            <div class="card"><h3>Win Rate</h3><h2>{winrate}%</h2></div>
        </div>
        <h2>NSE OI Dashboard</h2>
        <table>
            <tr>
                <th>Symbol</th>
                <th>CE OI</th>
                <th>PE OI</th>
                <th>PCR</th>
                <th>Max CE Strike</th>
                <th>Max PE Strike</th>
                <th>Sentiment</th>
            </tr>
            {oi_rows}
        </table>
        <h2>Strategy Performance</h2>
        <div class="cards">{strategy_cards}</div>

        <h2>Trade Log</h2>
        <table>
            <tr>
                <th>ID</th><th>Time</th><th>Strategy</th><th>Symbol</th>
                <th>Type</th><th>Side</th><th>Entry</th><th>Exit</th>
                <th>Qty</th><th>Status</th><th>P&L</th><th>Exit Reason</th>
            </tr>
            {rows}
        </table>

        <h2>Broker Order Test Log</h2>
        <table>
            <tr>
                <th>ID</th><th>Time</th><th>Mode</th><th>Strategy</th><th>Symbol</th>
                <th>Signal</th><th>Order Side</th><th>Qty</th><th>Price</th>
                <th>Exchange</th><th>Product</th><th>Order Type</th><th>Status</th>
            </tr>
            {broker_rows}
        </table>

        <h2>Daily Report</h2>
        <table>
            <tr>
                <th>Date</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>P&L</th>
            </tr>
            {daily_rows}
        </table>
    </body>
    </html>
    """
