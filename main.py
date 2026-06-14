from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib, secrets, csv, io, json, os, requests
import psycopg2
import psycopg2.extras
from kotak_client import KotakClient

app = FastAPI()

MAX_TRADES_PER_DAY = 20
MAX_DAILY_LOSS = -2000
TRADING_MODE = os.getenv("TRADING_MODE", "TEST")
DATABASE_URL = os.getenv("DATABASE_URL")

security = HTTPBasic()
USERNAME = os.getenv("APP_USER", "admin")
PASSWORD = os.getenv("APP_PASSWORD", "12345")

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, USERNAME) and secrets.compare_digest(credentials.password, PASSWORD)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Login", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

class Signal(BaseModel):
    strategy_id: str = "A"
    symbol: str
    signal: str
    price: float
    trade_type: str = "EQUITY"
    option_type: str = ""
    strike: float = 0
    expiry: str = ""
    lot_size: int = 1
    lots: int = 1
    exchange: str = "NSE"
    product: str = "MIS"
    order_type: str = "MARKET"
    validity: str = "DAY"
    disclosed_qty: int = 0
    trigger_price: float = 0
    alert_id: str | None = None

def conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing in Railway web service variables")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def now():
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

def today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

def fetch_all(query, params=None):
    c = conn(); cur = c.cursor(); cur.execute(query, params or ()); rows = cur.fetchall(); cur.close(); c.close(); return rows

def fetch_one(query, params=None):
    c = conn(); cur = c.cursor(); cur.execute(query, params or ()); row = cur.fetchone(); cur.close(); c.close(); return row

def execute_db(query, params=None):
    c = conn(); cur = c.cursor(); cur.execute(query, params or ()); c.commit(); cur.close(); c.close()

def init_db():
    c = conn(); cur = c.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS trades(
        id SERIAL PRIMARY KEY, time TEXT, trade_date TEXT, strategy_id TEXT, symbol TEXT,
        trade_type TEXT, option_type TEXT, strike DOUBLE PRECISION, expiry TEXT, side TEXT,
        entry DOUBLE PRECISION, exit DOUBLE PRECISION, lot_size INTEGER, lots INTEGER,
        qty INTEGER, status TEXT, pnl DOUBLE PRECISION, exit_reason TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS alerts(
        id SERIAL PRIMARY KEY, alert_hash TEXT UNIQUE, time TEXT, raw TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS broker_orders(
        id SERIAL PRIMARY KEY, time TEXT, mode TEXT, strategy_id TEXT, symbol TEXT,
        signal TEXT, order_side TEXT, qty INTEGER, price DOUBLE PRECISION, exchange TEXT,
        product TEXT, order_type TEXT, validity TEXT, status TEXT, payload TEXT)''')
    c.commit(); cur.close(); c.close()

init_db()

def fetch_nse_oi(symbol="NIFTY"):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/option-chain"
        }
        session = requests.Session()
        session.get("https://www.nseindia.com/option-chain", headers=headers, timeout=10)
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        response = session.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return {"symbol": symbol, "error": f"NSE response status: {response.status_code}"}
        data = response.json(); records = data["records"]["data"]
        ce_total = pe_total = max_ce = max_pe = max_ce_strike = max_pe_strike = 0
        for item in records:
            strike = item.get("strikePrice", 0)
            if "CE" in item:
                ce_oi = item["CE"].get("openInterest", 0); ce_total += ce_oi
                if ce_oi > max_ce: max_ce, max_ce_strike = ce_oi, strike
            if "PE" in item:
                pe_oi = item["PE"].get("openInterest", 0); pe_total += pe_oi
                if pe_oi > max_pe: max_pe, max_pe_strike = pe_oi, strike
        pcr = round(pe_total / ce_total, 2) if ce_total else 0
        sentiment = "BULLISH" if pcr > 1.1 else "BEARISH" if pcr < 0.9 else "NEUTRAL"
        return {"symbol": symbol, "ce_total": ce_total, "pe_total": pe_total, "pcr": pcr, "max_ce": max_ce_strike, "max_pe": max_pe_strike, "sentiment": sentiment}
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

def build_broker_payload(s: Signal, order_side: str, qty: int):
    return {"strategy_id": s.strategy_id.upper(), "symbol": s.symbol.upper(), "transaction_type": order_side, "quantity": qty, "price": float(s.price), "exchange": s.exchange.upper(), "product": s.product.upper(), "order_type": s.order_type.upper(), "validity": s.validity.upper(), "disclosed_qty": s.disclosed_qty, "trigger_price": s.trigger_price, "trade_type": s.trade_type.upper(), "option_type": s.option_type.upper(), "strike": s.strike, "expiry": s.expiry}

def log_broker_order(s: Signal, order_side: str, qty: int, status_text: str):
    payload = build_broker_payload(s, order_side, qty)
    execute_db('''INSERT INTO broker_orders(time, mode, strategy_id, symbol, signal, order_side, qty, price, exchange, product, order_type, validity, status, payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (now(), TRADING_MODE, s.strategy_id.upper(), s.symbol.upper(), s.signal.upper(), order_side, qty, float(s.price), s.exchange.upper(), s.product.upper(), s.order_type.upper(), s.validity.upper(), status_text, json.dumps(payload)))

def broker_action(s: Signal, order_side: str, qty: int):
    if TRADING_MODE == "PAPER": return
    if TRADING_MODE == "TEST":
        log_broker_order(s, order_side, qty, "TEST_ORDER_NOT_SENT"); return
    if TRADING_MODE == "LIVE":
        result = KotakClient().place_order(build_broker_payload(s, order_side, qty))
        log_broker_order(s, order_side, qty, result.get("status", "LIVE_UNKNOWN")); return

@app.get("/")
def home():
    return {"status": "Algo Dashboard Running", "mode": TRADING_MODE, "database": "PostgreSQL", "dashboard": "/dashboard", "oi_page": "/oi", "webhook": "/webhook"}

@app.get("/db_check")
def db_check(user: str = Depends(authenticate)):
    try:
        row = fetch_one("SELECT NOW() AS server_time")
        return {"status": "success", "database": "PostgreSQL connected", "server_time": str(row["server_time"])}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/webhook")
def webhook(s: Signal):
    signal, symbol, strategy, price = s.signal.upper(), s.symbol.upper(), s.strategy_id.upper(), float(s.price)
    trade_type, option_type, qty = s.trade_type.upper(), s.option_type.upper(), int(s.lot_size) * int(s.lots)
    raw = f"{strategy}-{symbol}-{signal}-{price}-{qty}-{trade_type}-{s.alert_id}"
    alert_hash = hashlib.sha256(raw.encode()).hexdigest()
    try:
        execute_db("INSERT INTO alerts(alert_hash,time,raw) VALUES(%s,%s,%s)", (alert_hash, now(), raw))
    except Exception:
        return {"status": "ignored", "reason": "Duplicate alert blocked"}
    closed_today = fetch_all("SELECT * FROM trades WHERE trade_date=%s AND status='CLOSED'", (today(),))
    daily_pnl = sum(float(t["pnl"] or 0) for t in closed_today)
    if daily_pnl <= MAX_DAILY_LOSS: return {"status": "blocked", "reason": "Max daily loss hit"}
    if len(closed_today) >= MAX_TRADES_PER_DAY and signal in ["BUY", "SELL"]: return {"status": "blocked", "reason": "Max trades per day hit"}
    open_trade = fetch_one("SELECT * FROM trades WHERE symbol=%s AND strategy_id=%s AND status='OPEN' ORDER BY id DESC LIMIT 1", (symbol, strategy))
    if signal in ["BUY", "SELL"]:
        if open_trade: return {"status": "ignored", "reason": "Open trade already exists"}
        execute_db('''INSERT INTO trades(time,trade_date,strategy_id,symbol,trade_type,option_type,strike,expiry,side,entry,exit,lot_size,lots,qty,status,pnl,exit_reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (now(), today(), strategy, symbol, trade_type, option_type, s.strike, s.expiry, signal, price, None, s.lot_size, s.lots, qty, "OPEN", 0, None))
        broker_action(s, signal, qty)
        return {"status": "success", "message": f"{signal} trade opened", "mode": TRADING_MODE, "qty": qty}
    if signal in ["EXIT", "CLOSE"]:
        if not open_trade: return {"status": "ignored", "reason": "No open trade"}
        entry, side = float(open_trade["entry"]), open_trade["side"]
        pnl = (price - entry) * int(open_trade["qty"]) if side == "BUY" else (entry - price) * int(open_trade["qty"])
        execute_db("UPDATE trades SET exit=%s, status='CLOSED', pnl=%s, exit_reason=%s WHERE id=%s", (price, pnl, "WEBHOOK EXIT", open_trade["id"]))
        broker_action(s, "SELL" if side == "BUY" else "BUY", int(open_trade["qty"]))
        return {"status": "success", "message": "Trade closed", "mode": TRADING_MODE, "pnl": pnl}
    return {"status": "error", "reason": "Invalid signal"}

@app.get("/reset")
def reset(user: str = Depends(authenticate)):
    execute_db("DELETE FROM trades"); execute_db("DELETE FROM alerts"); execute_db("DELETE FROM broker_orders")
    return RedirectResponse("/dashboard")

@app.get("/export_csv")
def export_csv(user: str = Depends(authenticate)):
    trades = fetch_all("SELECT * FROM trades ORDER BY id DESC")
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["ID", "Time", "Date", "Strategy", "Symbol", "Trade Type", "Option Type", "Strike", "Expiry", "Side", "Entry", "Exit", "Lot Size", "Lots", "Qty", "Status", "PnL", "Exit Reason"])
    for t in trades:
        writer.writerow([t["id"], t["time"], t["trade_date"], t["strategy_id"], t["symbol"], t["trade_type"], t["option_type"], t["strike"], t["expiry"], t["side"], t["entry"], t["exit"], t["lot_size"], t["lots"], t["qty"], t["status"], t["pnl"], t["exit_reason"]])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=algo_trades_report.csv"})

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(strategy_filter: str = "ALL", user: str = Depends(authenticate)):
    selected_strategy = strategy_filter.upper()
    all_trades = fetch_all("SELECT * FROM trades ORDER BY id DESC")
    broker_orders = fetch_all("SELECT * FROM broker_orders ORDER BY id DESC LIMIT 50")
    trades = all_trades if selected_strategy == "ALL" else fetch_all("SELECT * FROM trades WHERE strategy_id=%s ORDER BY id DESC", (selected_strategy,))
    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_trades = [t for t in trades if t["status"] == "OPEN"]
    today_closed = [t for t in closed if t["trade_date"] == today()]
    option_closed = [t for t in closed if t["trade_type"] == "OPTION"]
    equity_closed = [t for t in closed if t["trade_type"] == "EQUITY"]
    total_pnl = sum(float(t["pnl"] or 0) for t in closed)
    daily_pnl = sum(float(t["pnl"] or 0) for t in today_closed)
    option_pnl = sum(float(t["pnl"] or 0) for t in option_closed)
    equity_pnl = sum(float(t["pnl"] or 0) for t in equity_closed)
    total_trades = len(closed)
    wins = len([t for t in closed if float(t["pnl"] or 0) > 0])
    losses = len([t for t in closed if float(t["pnl"] or 0) < 0])
    winrate = round((wins / total_trades) * 100, 2) if total_trades else 0
    strategy_stats, daily_stats = {}, {}
    for t in all_trades:
        sid, d = t["strategy_id"], t["trade_date"]
        strategy_stats.setdefault(sid, {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "open": 0})
        if t["status"] == "OPEN": strategy_stats[sid]["open"] += 1
        if t["status"] == "CLOSED":
            pnl_value = float(t["pnl"] or 0); strategy_stats[sid]["pnl"] += pnl_value; strategy_stats[sid]["trades"] += 1
            if pnl_value > 0: strategy_stats[sid]["wins"] += 1
            elif pnl_value < 0: strategy_stats[sid]["losses"] += 1
            daily_stats.setdefault(d, {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
            daily_stats[d]["pnl"] += pnl_value; daily_stats[d]["trades"] += 1
            if pnl_value > 0: daily_stats[d]["wins"] += 1
            elif pnl_value < 0: daily_stats[d]["losses"] += 1
    strategy_options = '<option value="ALL">ALL</option>' + ''.join([f'<option value="{sid}" {"selected" if selected_strategy == sid else ""}>{sid}</option>' for sid in sorted(strategy_stats.keys())])
    strategy_cards = ''.join([f'''<div class="card"><h3>Strategy {sid}</h3><h2 style="color:{"green" if st["pnl"] > 0 else "red" if st["pnl"] < 0 else "black"};">{round(st["pnl"], 2)}</h2><p>Closed: {st["trades"]}</p><p>Open: {st["open"]}</p><p>Win Rate: {round((st["wins"] / st["trades"]) * 100, 2) if st["trades"] else 0}%</p></div>''' for sid, st in sorted(strategy_stats.items())])
    daily_rows = ''.join([f'''<tr><td>{d}</td><td>{st["trades"]}</td><td>{st["wins"]}</td><td>{st["losses"]}</td><td>{round((st["wins"] / st["trades"]) * 100, 2) if st["trades"] else 0}%</td><td style="color:{"green" if st["pnl"] > 0 else "red" if st["pnl"] < 0 else "black"};font-weight:bold;">{round(st["pnl"],2)}</td></tr>''' for d, st in sorted(daily_stats.items(), reverse=True)])
    rows = ''.join([f'''<tr><td>{t["id"]}</td><td>{t["time"]}</td><td>{t["strategy_id"]}</td><td>{t["symbol"]}</td><td>{t["trade_type"]}</td><td>{t["side"]}</td><td>{t["entry"]}</td><td>{t["exit"]}</td><td>{t["qty"]}</td><td>{t["status"]}</td><td style="color:{"green" if float(t["pnl"] or 0) > 0 else "red" if float(t["pnl"] or 0) < 0 else "black"};font-weight:bold;">{round(float(t["pnl"] or 0), 2)}</td><td>{t["exit_reason"]}</td></tr>''' for t in trades])
    broker_rows = ''.join([f'''<tr><td>{b["id"]}</td><td>{b["time"]}</td><td>{b["mode"]}</td><td>{b["strategy_id"]}</td><td>{b["symbol"]}</td><td>{b["signal"]}</td><td>{b["order_side"]}</td><td>{b["qty"]}</td><td>{b["price"]}</td><td>{b["exchange"]}</td><td>{b["product"]}</td><td>{b["order_type"]}</td><td>{b["status"]}</td></tr>''' for b in broker_orders])
    return f'''
    <html><head><title>Algo Dashboard</title><style>
    body {{ font-family: Arial; background:#f4f6f8; padding:20px; }} h1,h2 {{ color:#111; }}
    .topbar {{ display:flex; gap:10px; margin-bottom:15px; flex-wrap:wrap; align-items:center; }}
    .btn {{ padding:10px 15px; background:#111; color:white; border-radius:6px; text-decoration:none; border:none; cursor:pointer; }} .danger {{ background:#c0392b; }} .download {{ background:#2980b9; }} .oi {{ background:#16a085; }}
    .mode {{ background:#fff3cd; padding:10px; border-radius:6px; margin-bottom:15px; font-weight:bold; }} .cards {{ display:flex; gap:15px; flex-wrap:wrap; margin-bottom:20px; }} .card {{ background:white; padding:18px; border-radius:10px; min-width:170px; box-shadow:0 2px 6px #ccc; }}
    table {{ width:100%; border-collapse:collapse; background:white; margin-top:15px; margin-bottom:30px; font-size:13px; }} th,td {{ padding:8px; border:1px solid #ddd; text-align:center; }} th {{ background:#111; color:white; }} input,select {{ padding:8px; border-radius:5px; border:1px solid #aaa; }} button {{ padding:8px 12px; background:#111; color:white; border:none; border-radius:5px; cursor:pointer; }}
    </style></head><body><h1>Algo Paper + Broker Test Dashboard</h1><div class="mode">Current Mode: {TRADING_MODE} | Database: PostgreSQL</div>
    <div class="topbar"><a class="btn" href="/dashboard">Manual Refresh</a><a class="btn oi" href="/oi">NSE OI Page</a><a class="btn download" href="/export_csv">Download CSV</a><a class="btn danger" href="/reset">Reset All Trades</a><form method="get" action="/dashboard"><label><b>Filter Strategy:</b></label><select name="strategy_filter">{strategy_options}</select><button type="submit">Apply</button></form></div>
    <div class="cards"><div class="card"><h3>Selected Strategy</h3><h2>{selected_strategy}</h2></div><div class="card"><h3>Total P&L</h3><h2>{round(total_pnl,2)}</h2></div><div class="card"><h3>Today P&L</h3><h2>{round(daily_pnl,2)}</h2></div><div class="card"><h3>Option P&L</h3><h2>{round(option_pnl,2)}</h2></div><div class="card"><h3>Equity P&L</h3><h2>{round(equity_pnl,2)}</h2></div><div class="card"><h3>Open Trades</h3><h2>{len(open_trades)}</h2></div><div class="card"><h3>Closed Trades</h3><h2>{total_trades}</h2></div><div class="card"><h3>Win Rate</h3><h2>{winrate}%</h2></div></div>
    <h2>Strategy Performance</h2><div class="cards">{strategy_cards}</div><h2>Trade Log</h2><table><tr><th>ID</th><th>Time</th><th>Strategy</th><th>Symbol</th><th>Type</th><th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Status</th><th>P&L</th><th>Exit Reason</th></tr>{rows}</table>
    <h2>Broker Order Test Log</h2><table><tr><th>ID</th><th>Time</th><th>Mode</th><th>Strategy</th><th>Symbol</th><th>Signal</th><th>Order Side</th><th>Qty</th><th>Price</th><th>Exchange</th><th>Product</th><th>Order Type</th><th>Status</th></tr>{broker_rows}</table>
    <h2>Daily Report</h2><table><tr><th>Date</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>P&L</th></tr>{daily_rows}</table></body></html>
    '''

@app.get("/oi", response_class=HTMLResponse)
def oi_page(user: str = Depends(authenticate)):
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    rows = ""
    for sym in symbols:
        oi = fetch_nse_oi(sym)
        if "error" in oi:
            rows += f'<tr><td>{oi["symbol"]}</td><td colspan="6" style="color:red;font-weight:bold;">{oi["error"]}</td></tr>'
        else:
            color = "green" if oi["sentiment"] == "BULLISH" else "red" if oi["sentiment"] == "BEARISH" else "black"
            rows += f'<tr><td>{oi["symbol"]}</td><td>{oi["ce_total"]}</td><td>{oi["pe_total"]}</td><td>{oi["pcr"]}</td><td>{oi["max_ce"]}</td><td>{oi["max_pe"]}</td><td style="color:{color};font-weight:bold;">{oi["sentiment"]}</td></tr>'
    return f'''<html><head><title>NSE OI Dashboard</title><style>body{{font-family:Arial;background:#f4f6f8;padding:20px}}.btn{{padding:10px 15px;background:#111;color:white;border-radius:6px;text-decoration:none}}.refresh{{background:#16a085}}table{{width:100%;border-collapse:collapse;background:white;margin-top:20px;font-size:14px}}th,td{{padding:10px;border:1px solid #ddd;text-align:center}}th{{background:#111;color:white}}.note{{background:#fff3cd;padding:10px;border-radius:6px;margin-bottom:15px;font-weight:bold}}</style></head><body><h1>NSE OI Dashboard</h1><div class="note">This page is separate from trading dashboard. If NSE blocks Railway/server, status error will show here.</div><p><a class="btn" href="/dashboard">Back to Dashboard</a> <a class="btn refresh" href="/oi">Refresh OI</a></p><table><tr><th>Symbol</th><th>CE Total OI</th><th>PE Total OI</th><th>PCR</th><th>Max CE OI Strike</th><th>Max PE OI Strike</th><th>Sentiment</th></tr>{rows}</table></body></html>'''
