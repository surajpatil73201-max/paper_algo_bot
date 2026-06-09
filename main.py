from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3
from datetime import datetime

app = FastAPI()
DB = "trades.db"

class Signal(BaseModel):
    strategy_id: str = "A"
    symbol: str
    signal: str
    price: float
    qty: int = 1

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    c.execute("""
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        strategy_id TEXT,
        symbol TEXT,
        side TEXT,
        entry REAL,
        exit REAL,
        qty INTEGER,
        status TEXT,
        pnl REAL
    )
    """)
    c.commit()
    c.close()

init_db()

@app.get("/")
def home():
    return {"status": "Paper Trading Bot Running"}

@app.post("/webhook")
def webhook(s: Signal):
    signal = s.signal.upper()
    symbol = s.symbol.upper()
    strategy = s.strategy_id.upper()
    price = float(s.price)

    c = conn()
    open_trade = c.execute(
        "SELECT * FROM trades WHERE symbol=? AND strategy_id=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
        (symbol, strategy)
    ).fetchone()

    if signal in ["BUY", "SELL"]:
        if open_trade:
            c.close()
            return {"status": "ignored", "reason": "Open trade already exists"}

        c.execute("""
        INSERT INTO trades(time,strategy_id,symbol,side,entry,exit,qty,status,pnl)
        VALUES(?,?,?,?,?,?,?,?,?)
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), strategy, symbol, signal, price, None, s.qty, "OPEN", 0))

        c.commit()
        c.close()
        return {"status": "success", "message": f"{signal} opened"}

    if signal in ["EXIT", "CLOSE"]:
        if not open_trade:
            c.close()
            return {"status": "ignored", "reason": "No open trade"}

        entry = open_trade["entry"]
        side = open_trade["side"]

        pnl = (price - entry) * open_trade["qty"] if side == "BUY" else (entry - price) * open_trade["qty"]

        c.execute(
            "UPDATE trades SET exit=?, status='CLOSED', pnl=? WHERE id=?",
            (price, pnl, open_trade["id"])
        )

        c.commit()
        c.close()
        return {"status": "success", "message": "Trade closed", "pnl": pnl}

    c.close()
    return {"status": "error", "reason": "Invalid signal"}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    c = conn()
    trades = c.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
    c.close()

    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    total_pnl = sum(t["pnl"] for t in closed)
    total_trades = len(closed)
    wins = len([t for t in closed if t["pnl"] > 0])
    losses = len([t for t in closed if t["pnl"] < 0])
    winrate = round((wins / total_trades) * 100, 2) if total_trades else 0

    rows = ""
    for t in trades:
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
            <td>{round(t['pnl'],2)}</td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <title>Paper Trading Dashboard</title>
        <meta http-equiv="refresh" content="5">
        <style>
            body {{font-family:Arial;background:#f4f6f8;padding:20px;}}
            h1 {{color:#111;}}
            .cards {{display:flex;gap:15px;flex-wrap:wrap;}}
            .card {{background:white;padding:18px;border-radius:10px;min-width:180px;box-shadow:0 2px 6px #ccc;}}
            .green {{color:green;}}
            .red {{color:red;}}
            table {{width:100%;border-collapse:collapse;background:white;margin-top:20px;}}
            th,td {{padding:10px;border:1px solid #ddd;text-align:center;}}
            th {{background:#111;color:white;}}
        </style>
    </head>
    <body>
        <h1>Paper Trading Dashboard</h1>

        <div class="cards">
            <div class="card"><h3>Total P&L</h3><h2>{round(total_pnl,2)}</h2></div>
            <div class="card"><h3>Closed Trades</h3><h2>{total_trades}</h2></div>
            <div class="card"><h3>Open Trades</h3><h2>{len(open_trades)}</h2></div>
            <div class="card"><h3>Win Rate</h3><h2>{winrate}%</h2></div>
            <div class="card"><h3>Wins</h3><h2 class="green">{wins}</h2></div>
            <div class="card"><h3>Losses</h3><h2 class="red">{losses}</h2></div>
        </div>

        <table>
            <tr>
                <th>ID</th><th>Time</th><th>Strategy</th><th>Symbol</th>
                <th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th>
                <th>Status</th><th>P&L</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>
    """
