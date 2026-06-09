from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import sqlite3
from datetime import datetime, date
import hashlib

app = FastAPI()
DB = "trades.db"

# ================= SETTINGS =================
MAX_TRADES_PER_DAY = 5
MAX_DAILY_LOSS = -500
DEFAULT_QTY = 1

class Signal(BaseModel):
    strategy_id: str = "A"
    symbol: str
    signal: str
    price: float
    qty: int = DEFAULT_QTY
    alert_id: str | None = None

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today():
    return date.today().strftime("%Y-%m-%d")

def init_db():
    c = conn()

    c.execute("""
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        trade_date TEXT,
        strategy_id TEXT,
        symbol TEXT,
        side TEXT,
        entry REAL,
        exit REAL,
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

    c.commit()
    c.close()

init_db()

@app.get("/")
def home():
    return {
        "status": "Paper Algo Software Running",
        "dashboard": "/dashboard",
        "webhook": "/webhook"
    }

@app.post("/webhook")
def webhook(s: Signal):
    signal = s.signal.upper()
    symbol = s.symbol.upper()
    strategy = s.strategy_id.upper()
    price = float(s.price)
    qty = int(s.qty)

    raw = f"{strategy}-{symbol}-{signal}-{price}-{qty}-{s.alert_id}"
    alert_hash = hashlib.sha256(raw.encode()).hexdigest()

    c = conn()

    # Duplicate alert block
    try:
        c.execute(
            "INSERT INTO alerts(alert_hash,time,raw) VALUES(?,?,?)",
            (alert_hash, now(), raw)
        )
        c.commit()
    except:
        c.close()
        return {"status": "ignored", "reason": "Duplicate alert blocked"}

    # Daily risk check
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

    # Entry
    if signal in ["BUY", "SELL"]:
        if open_trade:
            c.close()
            return {"status": "ignored", "reason": "Open trade already exists"}

        c.execute("""
        INSERT INTO trades(time,trade_date,strategy_id,symbol,side,entry,exit,qty,status,pnl,exit_reason)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now(), today(), strategy, symbol, signal, price, None,
            qty, "OPEN", 0, None
        ))

        c.commit()
        c.close()
        return {"status": "success", "message": f"{signal} opened", "price": price}

    # Exit
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
        return {"status": "success", "message": "Trade closed", "pnl": pnl}

    c.close()
    return {"status": "error", "reason": "Invalid signal"}

@app.get("/squareoff/{trade_id}")
def squareoff(trade_id: int, price: float):
    c = conn()
    t = c.execute("SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()

    if not t:
        c.close()
        return RedirectResponse("/dashboard")

    pnl = (price - t["entry"]) * t["qty"] if t["side"] == "BUY" else (t["entry"] - price) * t["qty"]

    c.execute("""
    UPDATE trades
    SET exit=?, status='CLOSED', pnl=?, exit_reason=?
    WHERE id=?
    """, (price, pnl, "MANUAL SQUAREOFF", trade_id))

    c.commit()
    c.close()
    return RedirectResponse("/dashboard")

@app.get("/reset")
def reset():
    c = conn()
    c.execute("DELETE FROM trades")
    c.execute("DELETE FROM alerts")
    c.commit()
    c.close()
    return RedirectResponse("/dashboard")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    c = conn()
    trades = c.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
    c.close()

    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_trades = [t for t in trades if t["status"] == "OPEN"]
    today_closed = [t for t in closed if t["trade_date"] == today()]

    total_pnl = sum(t["pnl"] for t in closed)
    daily_pnl = sum(t["pnl"] for t in today_closed)
    total_trades = len(closed)
    wins = len([t for t in closed if t["pnl"] > 0])
    losses = len([t for t in closed if t["pnl"] < 0])
    winrate = round((wins / total_trades) * 100, 2) if total_trades else 0

    rows = ""
    for t in trades:
        square_btn = ""
        if t["status"] == "OPEN":
            square_btn = f"""
            <form action="/squareoff/{t['id']}" method="get">
                <input name="price" placeholder="Exit Price" required>
                <button>Square Off</button>
            </form>
            """

        pnl_color = "green" if t["pnl"] > 0 else "red" if t["pnl"] < 0 else "black"

        rows += f"""
        <tr>
            <td>{t['id']}</td>
            <td>{t['time']}</td>
            <td>{t['strategy_id']}</td>
            <td>{t['symbol']}</td>
            <td>{t['side']}</td>
            <td>{t['entry']}</td>
            <td>{t['exit']}</td>
            <td>{t['qty']}</td>
            <td>{t['status']}</td>
            <td style="color:{pnl_color};font-weight:bold;">{round(t['pnl'],2)}</td>
            <td>{t['exit_reason']}</td>
            <td>{square_btn}</td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <title>Algo Paper Dashboard</title>
        <meta http-equiv="refresh" content="5">
        <style>
            body {{font-family:Arial;background:#f4f6f8;padding:20px;}}
            h1 {{color:#111;}}
            .cards {{display:flex;gap:15px;flex-wrap:wrap;}}
            .card {{background:white;padding:18px;border-radius:10px;min-width:180px;box-shadow:0 2px 6px #ccc;}}
            table {{width:100%;border-collapse:collapse;background:white;margin-top:20px;font-size:14px;}}
            th,td {{padding:8px;border:1px solid #ddd;text-align:center;}}
            th {{background:#111;color:white;}}
            button {{padding:6px 10px;background:#111;color:white;border:none;border-radius:5px;}}
            input {{width:90px;padding:5px;}}
            .danger {{background:#c0392b;color:white;padding:10px;border-radius:6px;text-decoration:none;}}
        </style>
    </head>
    <body>
        <h1>Algo Paper Trading Dashboard</h1>

        <div class="cards">
            <div class="card"><h3>Total P&L</h3><h2>{round(total_pnl,2)}</h2></div>
            <div class="card"><h3>Today P&L</h3><h2>{round(daily_pnl,2)}</h2></div>
            <div class="card"><h3>Open Trades</h3><h2>{len(open_trades)}</h2></div>
            <div class="card"><h3>Closed Trades</h3><h2>{total_trades}</h2></div>
            <div class="card"><h3>Win Rate</h3><h2>{winrate}%</h2></div>
            <div class="card"><h3>Wins / Losses</h3><h2>{wins} / {losses}</h2></div>
            <div class="card"><h3>Risk Limit</h3><h2>{MAX_DAILY_LOSS}</h2></div>
            <div class="card"><h3>Max Trades/Day</h3><h2>{MAX_TRADES_PER_DAY}</h2></div>
        </div>

        <br>
        <a class="danger" href="/reset">Reset All Trades</a>

        <table>
            <tr>
                <th>ID</th><th>Time</th><th>Strategy</th><th>Symbol</th>
                <th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th>
                <th>Status</th><th>P&L</th><th>Exit Reason</th><th>Action</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>
    """
