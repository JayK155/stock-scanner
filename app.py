import os
import sqlite3
import requests
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

app = Flask(__name__)
API_KEY = os.environ.get("POLYGON_API_KEY", "BjNzevikpEG7Yvnxbp0oVzS1MZ1K8TxA")
BASE = "https://api.polygon.io"
ET = pytz.timezone("America/New_York")
DB = "alerts.db"

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT,
            name        TEXT,
            price       REAL,
            prev_high   REAL,
            volume      INTEGER,
            float_m     REAL,
            change_pct  REAL,
            break_pct   REAL,
            exchange    TEXT,
            scanned_at  TEXT,
            date        TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at  TEXT,
            scanned     INTEGER,
            found       INTEGER,
            status      TEXT
        )
    """)
    con.commit()
    con.close()

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_et_time():
    return datetime.now(ET).strftime("%H:%M:%S")

def get_et_date():
    return datetime.now(ET).strftime("%Y-%m-%d")

def is_market_hours():
    now = datetime.now(ET)
    t = now.hour * 60 + now.minute
    return 360 <= t <= 1140  # 6:00 AM – 7:00 PM ET

def get_prev_trading_day():
    d = datetime.now(ET).date() - timedelta(days=1)
    while d.weekday() >= 5:  # skip weekends
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def fetch(url):
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

# ── Scanner ───────────────────────────────────────────────────────────────────
scan_status = {"last": "Never", "next": "60s", "running": False, "log": "Ready — waiting for first scan."}

def run_scan():
    if scan_status["running"]:
        return
    scan_status["running"] = True
    scan_status["log"] = "Fetching market data..."
    found = 0
    scanned = 0

    try:
        prev_day = get_prev_trading_day()
        today = get_et_date()

        # Step 1: Grouped daily — get all US stocks from previous day
        scan_status["log"] = f"Pulling previous day data ({prev_day})..."
        grouped = fetch(f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{prev_day}?adjusted=true&include_otc=false&apiKey={API_KEY}")

        if not grouped.get("results"):
            scan_status["log"] = f"No data for {prev_day}. Market may have been closed."
            return

        # Filter candidates: price $1–$25, volume > 15K, clean ticker
        candidates = [
            r for r in grouped["results"]
            if 1 <= r.get("c", 0) <= 25
            and r.get("v", 0) >= 15000
            and r.get("T", "")
            and r["T"].isalpha()
            and len(r["T"]) <= 5
        ]
        candidates = sorted(candidates, key=lambda x: x["v"], reverse=True)[:300]
        scanned = len(candidates)

        # Build prev high map
        prev_map = {r["T"]: {"prevHigh": r["h"], "prevClose": r["c"]} for r in candidates}

        # Step 2: Bulk snapshot
        scan_status["log"] = f"Fetching live snapshots for {scanned} candidates..."
        ticker_str = ",".join([c["T"] for c in candidates])
        snap = fetch(f"{BASE}/v2/snapshot/locale/us/markets/stocks/tickers?tickers={requests.utils.quote(ticker_str)}&apiKey={API_KEY}")

        if not snap.get("tickers"):
            scan_status["log"] = "No snapshot data. Markets may be closed."
            return

        # Load already-alerted symbols for today to avoid duplicates
        con = db()
        alerted_today = set(row["symbol"] for row in con.execute("SELECT symbol FROM alerts WHERE date=?", (today,)).fetchall())
        con.close()

        new_alerts = []
        for s in snap["tickers"]:
            sym = s.get("ticker", "")
            if sym in alerted_today:
                continue
            prev = prev_map.get(sym)
            if not prev:
                continue

            prev_high = prev["prevHigh"]
            prev_close = prev["prevClose"]
            today_price = s.get("day", {}).get("c") or s.get("lastTrade", {}).get("p")
            today_vol = s.get("day", {}).get("v", 0)

            if not today_price or not prev_high:
                continue
            if today_price < 1 or today_price > 25:
                continue
            if today_vol < 15000:
                continue
            if today_price <= prev_high:
                continue

            break_pct = round(((today_price - prev_high) / prev_high) * 100, 2)
            change_pct = round(((today_price - prev_close) / prev_close) * 100, 2) if prev_close else 0

            new_alerts.append({
                "symbol": sym,
                "name": sym,
                "price": round(today_price, 2),
                "prev_high": round(prev_high, 2),
                "volume": int(today_vol),
                "float_m": None,
                "change_pct": change_pct,
                "break_pct": break_pct,
                "exchange": "",
                "scanned_at": get_et_time(),
                "date": today
            })

        # Step 3: Enrich top results with name, exchange, float
        scan_status["log"] = f"Found {len(new_alerts)} breakout(s)! Enriching data..."
        for i, alert in enumerate(new_alerts[:20]):
            try:
                det = fetch(f"{BASE}/v3/reference/tickers/{alert['symbol']}?apiKey={API_KEY}")
                if det.get("results"):
                    r = det["results"]
                    alert["name"] = r.get("name", alert["symbol"])
                    alert["exchange"] = r.get("primary_exchange", "")
                    shares = r.get("share_class_shares_outstanding")
                    if shares:
                        alert["float_m"] = round(shares / 1e6, 1)
                        if alert["float_m"] > 25:  # float filter
                            new_alerts[i] = None
                            continue
                time.sleep(0.12)
            except:
                pass

        new_alerts = [a for a in new_alerts if a is not None]
        found = len(new_alerts)

        # Save to DB
        if new_alerts:
            con = db()
            for a in new_alerts:
                con.execute("""
                    INSERT INTO alerts (symbol,name,price,prev_high,volume,float_m,change_pct,break_pct,exchange,scanned_at,date)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (a["symbol"],a["name"],a["price"],a["prev_high"],a["volume"],
                      a["float_m"],a["change_pct"],a["break_pct"],a["exchange"],a["scanned_at"],a["date"]))
            con.commit()
            con.close()

        scan_status["last"] = get_et_time()
        scan_status["log"] = f"✓ Scan complete — {found} breakout(s) from {scanned} stocks. Next scan in 60s."

        # Log the scan
        con = db()
        con.execute("INSERT INTO scan_log (scanned_at,scanned,found,status) VALUES (?,?,?,?)",
                    (get_et_time(), scanned, found, "ok"))
        con.commit()
        con.close()

    except Exception as e:
        scan_status["log"] = f"Error: {str(e)}"
        try:
            con = db()
            con.execute("INSERT INTO scan_log (scanned_at,scanned,found,status) VALUES (?,?,?,?)",
                        (get_et_time(), scanned, 0, f"error: {str(e)[:200]}"))
            con.commit()
            con.close()
        except:
            pass
    finally:
        scan_status["running"] = False

# ── API Routes ────────────────────────────────────────────────────────────────
@app.route("/api/alerts")
def api_alerts():
    today = get_et_date()
    con = db()
    rows = con.execute(
        "SELECT * FROM alerts WHERE date=? ORDER BY id DESC LIMIT 100", (today,)
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/status")
def api_status():
    today = get_et_date()
    con = db()
    today_count = con.execute("SELECT COUNT(*) FROM alerts WHERE date=?", (today,)).fetchone()[0]
    total_count = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    last_scan = con.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return jsonify({
        "last_scan": scan_status["last"],
        "running": scan_status["running"],
        "log": scan_status["log"],
        "market_hours": is_market_hours(),
        "today_count": today_count,
        "total_count": total_count,
        "scanned": dict(last_scan)["scanned"] if last_scan else 0
    })

@app.route("/api/scan", methods=["POST"])
def api_scan():
    if not scan_status["running"]:
        threading.Thread(target=run_scan).start()
    return jsonify({"ok": True})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    today = get_et_date()
    con = db()
    con.execute("DELETE FROM alerts WHERE date=?", (today,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Real-time Stock Alerts</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d1117;color:#e6edf3;font-family:'JetBrains Mono',monospace;min-height:100vh}
  ::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px}
  #topbar{background:#161b22;border-bottom:1px solid #21262d;padding:14px 24px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100}
  #topbar h1{font-family:'Space Grotesk',sans-serif;font-size:22px;font-weight:700}
  #topbar p{font-size:11px;color:#7d8590;margin-top:2px}
  #scan-info{text-align:right;font-size:11px;color:#7d8590;line-height:1.9}
  #scan-info span{color:#58a6ff}
  #stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:20px 24px}
  .stat-card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:18px 20px;position:relative;overflow:hidden}
  .stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
  .sc-green::before{background:linear-gradient(90deg,#238636,#2ea043)}
  .sc-blue::before{background:linear-gradient(90deg,#1f6feb,#388bfd)}
  .sc-orange::before{background:linear-gradient(90deg,#9e6a03,#d29922)}
  .sc-purple::before{background:linear-gradient(90deg,#6e40c9,#8b5cf6)}
  .stat-icon{font-size:22px;margin-bottom:10px}
  .stat-val{font-size:30px;font-weight:700;font-family:'Space Grotesk',sans-serif}
  .stat-label{font-size:10px;color:#7d8590;margin-top:4px;text-transform:uppercase;letter-spacing:1px}
  #criteria{background:#161b22;border-top:1px solid #21262d;border-bottom:1px solid #21262d;padding:12px 24px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .tag{padding:5px 11px;border-radius:6px;font-size:11px;font-weight:500;border:1px solid;white-space:nowrap}
  .t-price{background:#0d2137;border-color:#1f6feb;color:#58a6ff}
  .t-float{background:#0d2d1a;border-color:#238636;color:#3fb950}
  .t-vol{background:#2d1b00;border-color:#9e6a03;color:#e3b341}
  .t-break{background:#1a2d00;border-color:#4d7c0f;color:#86efac}
  .t-time{background:#1a1f2e;border-color:#30363d;color:#8b949e}
  #controls{padding:12px 24px;border-bottom:1px solid #21262d;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .search-wrap{position:relative;flex:1;max-width:400px}
  .search-wrap span{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#484f58}
  #search{width:100%;background:#21262d;border:1px solid #30363d;border-radius:7px;padding:9px 12px 9px 34px;color:#e6edf3;font-family:'JetBrains Mono',monospace;font-size:12px;outline:none}
  #search:focus{border-color:#1f6feb}
  #search::placeholder{color:#484f58}
  .btn{padding:9px 16px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;border:1px solid;font-family:'JetBrains Mono',monospace;transition:all 0.15s;white-space:nowrap}
  .btn:disabled{opacity:0.6;cursor:not-allowed}
  .btn-blue{background:#1f6feb;border-color:#1f6feb;color:#fff}
  .btn-gray{background:#21262d;border-color:#30363d;color:#c9d1d9}
  .btn-green{background:#0d4429;border-color:#238636;color:#3fb950}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}
  .scanning{animation:pulse 0.8s infinite}
  #table-wrap{margin:16px 24px 28px;border:1px solid #21262d;border-radius:10px;overflow:hidden}
  #table-header{background:#161b22;padding:13px 16px;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:10px}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:0.2}}
  .live-dot{width:8px;height:8px;background:#3fb950;border-radius:50%;display:inline-block;animation:blink 1.5s infinite}
  .section-title{font-family:'Space Grotesk',sans-serif;font-size:15px;font-weight:600}
  .count-badge{background:#1f6feb;color:#fff;border-radius:100px;padding:2px 9px;font-size:11px;font-weight:700}
  #market-status{margin-left:auto;font-size:11px}
  .col-heads{display:grid;grid-template-columns:70px 1fr 105px 95px 80px 90px 100px 95px;gap:8px;padding:8px 16px;border-bottom:1px solid #30363d}
  .col-h{font-size:10px;color:#7d8590;text-transform:uppercase;letter-spacing:0.8px;font-weight:600}
  #alerts-body{max-height:500px;overflow-y:auto}
  .alert-row{display:grid;grid-template-columns:70px 1fr 105px 95px 80px 90px 100px 95px;gap:8px;padding:11px 16px;border-bottom:1px solid #21262d;align-items:center;transition:background 0.15s}
  .alert-row:hover{background:#1c2128}
  @keyframes slideIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:translateX(0)}}
  .row-new{animation:slideIn 0.4s ease;background:#0c1f40}
  .sym{font-size:13px;font-weight:700;color:#58a6ff}
  .company{font-size:12px;color:#c9d1d9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .price-val{font-size:13px;font-weight:600}
  .chg-pos{font-size:11px;color:#3fb950;font-weight:600}
  .chg-neg{font-size:11px;color:#f85149;font-weight:600}
  .vol{font-size:12px;color:#c9d1d9}
  .flt{font-size:12px;color:#e3b341}
  .brk{background:#0d2d1a;border:1px solid #238636;color:#3fb950;padding:3px 8px;border-radius:5px;font-size:11px;font-weight:600}
  .tm{font-size:11px;color:#7d8590}
  .exch-badge{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:600}
  .exch-nasdaq{background:#0c2d6b;color:#58a6ff;border:1px solid #1f6feb}
  .exch-nyse{background:#2d1b00;color:#e3b341;border:1px solid #9e6a03}
  .exch-other{background:#21262d;color:#8b949e;border:1px solid #30363d}
  #empty-state{padding:55px 20px;text-align:center;color:#484f58}
  #empty-state .big{font-size:40px;margin-bottom:12px}
  #empty-state p{font-size:13px;line-height:1.7}
  #empty-state strong{color:#58a6ff}
  #log-bar{padding:10px 24px;font-size:11px;color:#484f58;border-top:1px solid #21262d;background:#0d1117}
  .log-error{color:#f85149}
  .log-success{color:#3fb950}
  .log-info{color:#58a6ff}
</style>
</head>
<body>
<div id="topbar">
  <div>
    <h1>📈 Real-time Stock Alerts</h1>
    <p>Cloud scanner · NASDAQ &amp; NYSE · runs 24/7 every 60 seconds</p>
  </div>
  <div id="scan-info">
    <div>🔄 Last scan: <span id="last-scan">--</span></div>
    <div>⏱ Next scan: <span id="countdown">60s</span></div>
    <div>🕐 <span id="clock">--</span> ET</div>
  </div>
</div>

<div id="stats">
  <div class="stat-card sc-green"><div class="stat-icon">🔔</div><div class="stat-val" id="s-today">0</div><div class="stat-label">Today's Alerts</div></div>
  <div class="stat-card sc-blue"><div class="stat-icon">🗄️</div><div class="stat-val" id="s-total">0</div><div class="stat-label">Total Alerts</div></div>
  <div class="stat-card sc-orange"><div class="stat-icon">📊</div><div class="stat-val" id="s-scanned">0</div><div class="stat-label">Stocks Scanned</div></div>
  <div class="stat-card sc-purple"><div class="stat-icon">🚀</div><div class="stat-val">1m</div><div class="stat-label">Scan Interval</div></div>
</div>

<div id="criteria">
  <span style="font-size:12px;font-weight:600;color:#8b949e;margin-right:4px">▼ Alert Criteria</span>
  <span class="tag t-price">$ Price: $1–$25</span>
  <span class="tag t-float">▲ Float &lt; 25M</span>
  <span class="tag t-vol">≡ Volume &gt; 15K (1min)</span>
  <span class="tag t-break">↑ Breaks Previous Day High</span>
  <span class="tag t-time">⏰ 6:00 AM – 7:00 PM ET</span>
</div>

<div id="controls">
  <div class="search-wrap">
    <span>🔍</span>
    <input id="search" type="text" placeholder="Search by symbol or name..." oninput="filterAlerts()"/>
  </div>
  <button class="btn btn-blue" id="scan-btn" onclick="triggerScan()">⟳ Scan Now</button>
  <button class="btn btn-gray" onclick="clearAlerts()">🗑 Clear Today</button>
  <button class="btn btn-gray" onclick="exportCSV()">⬇ Export CSV</button>
</div>

<div id="table-wrap">
  <div id="table-header">
    <span class="live-dot"></span>
    <span class="section-title">Live Alerts</span>
    <span class="count-badge" id="alert-count">0</span>
    <span id="market-status" style="margin-left:auto;font-size:11px"></span>
  </div>
  <div class="col-heads">
    <div class="col-h">Symbol</div><div class="col-h">Company</div><div class="col-h">Price</div>
    <div class="col-h">Volume</div><div class="col-h">Float</div><div class="col-h">Break %</div>
    <div class="col-h">Exchange</div><div class="col-h">Time ET</div>
  </div>
  <div id="alerts-body">
    <div id="empty-state"><div class="big">📭</div><p>Waiting for scan results...<br/>The server scans automatically every <strong>60 seconds</strong>.</p></div>
  </div>
</div>
<div id="log-bar">⬡ Connecting to scanner...</div>

<script>
let allAlerts = [];
let countdown = 60;
let knownIds = new Set();

function fmtVol(v){if(!v)return"–";if(v>=1e6)return(v/1e6).toFixed(1)+"M";if(v>=1e3)return(v/1e3).toFixed(1)+"K";return""+v;}
function exchLabel(ex){
  if(!ex)return'<span class="exch-badge exch-other">US</span>';
  const u=ex.toUpperCase();
  if(u.includes("XNAS"))return'<span class="exch-badge exch-nasdaq">NASDAQ</span>';
  if(u.includes("XNYS"))return'<span class="exch-badge exch-nyse">NYSE</span>';
  return`<span class="exch-badge exch-other">${u.replace(/^X/,"").slice(0,5)}</span>`;
}

async function fetchStatus(){
  try{
    const r=await fetch("/api/status");
    const s=await r.json();
    document.getElementById("last-scan").textContent=s.last_scan||"--";
    document.getElementById("s-today").textContent=s.today_count;
    document.getElementById("s-total").textContent=s.total_count;
    document.getElementById("s-scanned").textContent=s.scanned;
    const log=document.getElementById("log-bar");
    log.textContent="⬡ "+s.log;
    log.className=s.log.startsWith("✓")?"log-success":s.log.startsWith("Error")?"log-error":"log-info";
    const ms=document.getElementById("market-status");
    ms.textContent=s.market_hours?"● Market Hours Active":"● Outside Market Hours";
    ms.style.color=s.market_hours?"#3fb950":"#f85149";
    const btn=document.getElementById("scan-btn");
    if(s.running){btn.textContent="⟳ Scanning...";btn.classList.add("scanning");btn.disabled=true;}
    else{btn.textContent="⟳ Scan Now";btn.classList.remove("scanning");btn.disabled=false;}
  }catch(e){}
}

async function fetchAlerts(){
  try{
    const r=await fetch("/api/alerts");
    const data=await r.json();
    const newOnes=data.filter(a=>!knownIds.has(a.id));
    newOnes.forEach(a=>knownIds.add(a.id));
    allAlerts=data.map(a=>({...a,isNew:newOnes.some(n=>n.id===a.id)}));
    filterAlerts();
  }catch(e){}
}

function filterAlerts(){
  const q=document.getElementById("search").value.toLowerCase();
  const list=q?allAlerts.filter(a=>a.symbol.toLowerCase().includes(q)||(a.name||"").toLowerCase().includes(q)):allAlerts;
  renderAlerts(list);
}

function renderAlerts(list){
  document.getElementById("alert-count").textContent=list.length;
  const body=document.getElementById("alerts-body");
  if(!list.length){
    body.innerHTML='<div id="empty-state"><div class="big">📭</div><p>No alerts yet.<br/>The server scans automatically every <strong>60 seconds</strong>.</p></div>';
    return;
  }
  body.innerHTML=list.map(a=>`
    <div class="alert-row ${a.isNew?'row-new':''}">
      <div class="sym">${a.symbol}</div>
      <div class="company" title="${a.name}">${a.name}</div>
      <div><div class="price-val">$${(+a.price).toFixed(2)}</div><div class="${a.change_pct>=0?'chg-pos':'chg-neg'}">${a.change_pct>=0?'+':''}${(+a.change_pct).toFixed(2)}%</div></div>
      <div class="vol">${fmtVol(a.volume)}</div>
      <div class="flt">${a.float_m!==null&&a.float_m!==undefined?a.float_m+'M':'–'}</div>
      <div><span class="brk">+${(+a.break_pct).toFixed(2)}%</span></div>
      <div>${exchLabel(a.exchange)}</div>
      <div class="tm">${a.scanned_at}</div>
    </div>`).join("");
}

async function triggerScan(){
  await fetch("/api/scan",{method:"POST"});
  countdown=60;
  setTimeout(fetchStatus,500);
  setTimeout(fetchAlerts,3000);
}

async function clearAlerts(){
  if(!confirm("Clear today's alerts?"))return;
  await fetch("/api/clear",{method:"POST"});
  allAlerts=[];knownIds.clear();
  filterAlerts();
}

function exportCSV(){
  if(!allAlerts.length){alert("No alerts to export.");return;}
  const csv=["Symbol,Name,Price,Change%,Volume,Float,Break%,PrevHigh,Exchange,Time",
    ...allAlerts.map(a=>`${a.symbol},"${a.name}",${a.price},${a.change_pct}%,${a.volume},${a.float_m?a.float_m+'M':'N/A'},+${a.break_pct}%,$${a.prev_high},${a.exchange},${a.scanned_at}`)
  ].join("\n");
  const url=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  Object.assign(document.createElement("a"),{href:url,download:"stock-alerts.csv"}).click();
}

// Clock
setInterval(()=>{
  document.getElementById("clock").textContent=new Date().toLocaleTimeString("en-US",{timeZone:"America/New_York",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
  countdown=countdown<=1?60:countdown-1;
  document.getElementById("countdown").textContent=countdown+"s";
},1000);

// Poll server every 10s
setInterval(fetchStatus,10000);
setInterval(fetchAlerts,15000);

// Initial load
fetchStatus();
fetchAlerts();
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD)

# ── Scheduler ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_scan, "interval", minutes=1, id="scanner")
    scheduler.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
