from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import sqlite3
from datetime import datetime, date
import hashlib

app = FastAPI()
DB = "trades.db"

MAX_TRADES_PER_DAY = 20
MAX_DAILY_LOSS = -2000


class Signal(BaseModel):
    strategy_id: str = "A"
    symbol: str
    signal: str
    price: float

    trade_type: str = "EQUITY"   # EQUITY / OPTION
    option_type: str = ""        # CE / PE
    strike: float = 0
    expiry: str = ""
    lot_size: int = 1
    lots: int = 1

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

    c.commit()
    c.close()


init_db()


@app.get("/")
def home():
    return {
        "status": "Equity + Option Paper Algo Running",
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
        return {
            "status": "success",
            "message": f"{signal} trade opened",
            "symbol": symbol,
            "strategy_id": strategy,
            "trade_type": trade_type,
            "qty": qty
        }

    if signal in ["EXIT", "CLOSE"]:
        if not open_trade:
            c.close()
            return {"status": "ignored", "reason": "No open trade"}

        entry = open_trade["entry"]
        side = open_trade["side"]

        if side == "BUY":
            pnl = (price - entry) * open_trade["qty"]
        else:
            pnl = (entry - price) * open_trade["qty"]

        c.execute("""
        UPDATE trades
        SET exit=?, status='CLOSED', pnl=?, exit_reason=?
        WHERE id=?
        """, (price, pnl, "WEBHOOK EXIT", open_trade["id"]))

        c.commit()
        c.close()
        return {
            "status": "success",
            "message": "Trade closed",
            "strategy_id": strategy,
            "pnl": pnl
        }

    c.close()
    return {"status": "error", "reason": "Invalid signal"}


@app.get("/squareoff/{trade_id}")
def squareoff(trade_id: int, price: float):
    c = conn()
    t = c.execute(
        "SELECT * FROM trades WHERE id=? AND status='OPEN'",
        (trade_id,)
    ).fetchone()

    if not t:
        c.close()
        return RedirectResponse("/dashboard")

    if t["side"] == "BUY":
        pnl = (price - t["entry"]) * t["qty"]
    else:
        pnl = (t["entry"] - price) * t["qty"]

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
def dashboard(strategy_filter: str = "ALL"):
    selected_strategy = strategy_filter.upper()

    c = conn()

    all_trades = c.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()

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

    option_pnl = sum(t["pnl"] for t in option_closed)
    equity_pnl = sum(t["pnl"] for t in equity_closed)

    option_trades = len(option_closed)
    equity_trades = len(equity_closed)

    total_pnl = sum(t["pnl"] for t in closed)
    daily_pnl = sum(t["pnl"] for t in today_closed)

    total_trades = len(closed)
    wins = len([t for t in closed if t["pnl"] > 0])
    losses = len([t for t in closed if t["pnl"] < 0])
    winrate = round((wins / total_trades) * 100, 2) if total_trades else 0

    strategy_stats = {}
    for t in all_trades:
        sid = t["strategy_id"]

        if sid not in strategy_stats:
            strategy_stats[sid] = {
                "pnl": 0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "open": 0
            }

        if t["status"] == "OPEN":
            strategy_stats[sid]["open"] += 1

        if t["status"] == "CLOSED":
            strategy_stats[sid]["pnl"] += t["pnl"]
            strategy_stats[sid]["trades"] += 1

            if t["pnl"] > 0:
                strategy_stats[sid]["wins"] += 1
            elif t["pnl"] < 0:
                strategy_stats[sid]["losses"] += 1

    strategy_options = '<option value="ALL">ALL</option>'
    for sid in sorted(strategy_stats.keys()):
        selected = "selected" if selected_strategy == sid else ""
        strategy_options += f'<option value="{sid}" {selected}>{sid}</option>'

    strategy_cards = ""
    for sid, st in strategy_stats.items():
        wr = round((st["wins"] / st["trades"]) * 100, 2) if st["trades"] else 0
        pnl_color = "green" if st["pnl"] > 0 else "red" if st["pnl"] < 0 else "black"

        strategy_cards += f"""
        <div class="card">
            <h3>Strategy {sid}</h3>
            <h2 style="color:{pnl_color};">{round(st['pnl'], 2)}</h2>
            <p>Closed Trades: {st['trades']}</p>
            <p>Open Trades: {st['open']}</p>
            <p>Wins/Losses: {st['wins']} / {st['losses']}</p>
            <p>Win Rate: {wr}%</p>
        </div>
        """

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
            <td>{t['trade_type']}</td>
            <td>{t['option_type']}</td>
            <td>{t['strike']}</td>
            <td>{t['expiry']}</td>
            <td>{t['side']}</td>
            <td>{t['entry']}</td>
            <td>{t['exit']}</td>
            <td>{t['lot_size']}</td>
            <td>{t['lots']}</td>
            <td>{t['qty']}</td>
            <td>{t['status']}</td>
            <td style="color:{pnl_color};font-weight:bold;">{round(t['pnl'], 2)}</td>
            <td>{t['exit_reason']}</td>
            <td>{square_btn}</td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <title>Equity + Option Paper Algo</title>
        <style>
            body {{
                font-family: Arial;
                background:#f4f6f8;
                padding:20px;
            }}
            h1 {{
                color:#111;
            }}
            .topbar {{
                display:flex;
                gap:10px;
                margin-bottom:15px;
                flex-wrap:wrap;
                align-items:center;
            }}
            .btn {{
                padding:10px 15px;
                background:#111;
                color:white;
                border-radius:6px;
                text-decoration:none;
                border:none;
                cursor:pointer;
            }}
            .danger {{
                background:#c0392b;
            }}
            .cards {{
                display:flex;
                gap:15px;
                flex-wrap:wrap;
                margin-bottom:20px;
            }}
            .card {{
                background:white;
                padding:18px;
                border-radius:10px;
                min-width:170px;
                box-shadow:0 2px 6px #ccc;
            }}
            table {{
                width:100%;
                border-collapse:collapse;
                background:white;
                margin-top:20px;
                font-size:13px;
            }}
            th,td {{
                padding:8px;
                border:1px solid #ddd;
                text-align:center;
            }}
            th {{
                background:#111;
                color:white;
            }}
            input, select {{
                padding:8px;
                border-radius:5px;
                border:1px solid #aaa;
            }}
            input {{
                width:85px;
            }}
            button {{
                padding:8px 12px;
                background:#111;
                color:white;
                border:none;
                border-radius:5px;
                cursor:pointer;
            }}
        </style>
    </head>
    <body>
        <h1>Equity + Option Paper Algo Dashboard</h1>

        <div class="topbar">
            <a class="btn" href="/dashboard">Manual Refresh</a>
            <a class="btn danger" href="/reset">Reset All Trades</a>

            <form method="get" action="/dashboard">
                <label><b>Filter Strategy:</b></label>
                <select name="strategy_filter">
                    {strategy_options}
                </select>
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
            <div class="card"><h3>Option Trades</h3><h2>{option_trades}</h2></div>
            <div class="card"><h3>Equity Trades</h3><h2>{equity_trades}</h2></div>
            <div class="card"><h3>Win Rate</h3><h2>{winrate}%</h2></div>
            <div class="card"><h3>Wins / Losses</h3><h2>{wins} / {losses}</h2></div>
        </div>

        <h2>Strategy-wise Performance</h2>
        <div class="cards">
            {strategy_cards}
        </div>

        <table>
            <tr>
                <th>ID</th><th>Time</th><th>Strategy</th><th>Symbol</th>
                <th>Type</th><th>CE/PE</th><th>Strike</th><th>Expiry</th>
                <th>Side</th><th>Entry</th><th>Exit</th>
                <th>Lot Size</th><th>Lots</th><th>Qty</th>
                <th>Status</th><th>P&L</th><th>Exit Reason</th><th>Action</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>
    """
