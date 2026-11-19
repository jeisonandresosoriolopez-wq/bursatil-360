"""Capa de ingesta: precios históricos y cotizaciones en vivo (Yahoo Finance, endpoints públicos),
fundamentales desde CSV local, caché en disco y refresco en segundo plano.

En producción esta capa se sustituiría por un proveedor con licencia comercial + Airflow/cron
+ PostgreSQL/TimescaleDB + Redis (ver pestaña Metodología)."""

import csv
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Bursatil360/2.0", "Accept": "application/json"}

# Benchmarks y series macro leídas en vivo
BENCHMARKS = {
    "^GSPC": "S&P 500",
    "^IBEX": "IBEX 35",
    "^STOXX50E": "Euro Stoxx 50",
    "^IXIC": "Nasdaq Composite",
    "^VIX": "VIX (volatilidad implícita)",
    "^TNX": "Bono EE.UU. 10 años (%)",
    "DX-Y.NYB": "Índice dólar (DXY)",
    "EURUSD=X": "EUR/USD",
}
# Conversión aproximada a USD para la métrica de liquidez (se actualiza EUR en vivo)
FX_TO_USD = {"USD": 1.0, "EUR": 1.08, "CHF": 1.12, "DKK": 0.145, "GBP": 1.27}

NUMERIC_FIELDS = [
    "mcap_bn", "pe", "pb", "ev_ebitda", "fcf_yield", "peg", "roe", "roic", "op_margin", "eps_stability",
    "piotroski", "rev_cagr", "eps_cagr", "est_revisions", "debt_ebitda", "interest_cov", "current_ratio",
    "altman_z", "div_yield", "payout", "div_growth_years", "fcf_div_cover", "analyst_rating",
    "analyst_dispersion", "short_interest", "insider_net", "moat", "management", "reg_risk", "cust_conc",
    "esg_risk", "controversy", "ter", "aum_bn",
]


def _get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- fundamentales
def load_universe():
    """Lee fundamentals.csv, sectors.json, macro.json y red_flags.json. Valida y limpia."""
    assets, issues = [], []
    with open(DATA_DIR / "fundamentals.csv", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):
            if None in row or any(v is None for v in row.values()):
                issues.append(f"Fila {i}: número de columnas incorrecto, ignorada")
                continue
            a = {
                "symbol": row["symbol"].strip(),
                "name": row["name"].strip(),
                "market": row["market"].strip(),
                "type": row["type"].strip(),
                "sector": row["sector"].strip(),
                "currency": row["currency"].strip(),
                "as_of": row["as_of"].strip(),
                "f": {},
            }
            for k in NUMERIC_FIELDS:
                v = (row.get(k) or "").strip()
                if v == "":
                    a["f"][k] = None
                    continue
                try:
                    a["f"][k] = float(v)
                except ValueError:
                    issues.append(f"{a['symbol']}: valor no numérico en {k} ('{v}'), tratado como faltante")
                    a["f"][k] = None
            assets.append(a)
    sectors = json.loads((DATA_DIR / "sectors.json").read_text(encoding="utf-8"))
    sectors.pop("_doc", None)
    macro = json.loads((DATA_DIR / "macro.json").read_text(encoding="utf-8"))
    flags = json.loads((DATA_DIR / "red_flags.json").read_text(encoding="utf-8"))
    flags.pop("_doc", None)
    for a in assets:
        if a["sector"] not in sectors:
            issues.append(f"{a['symbol']}: sector '{a['sector']}' sin referencia en sectors.json")
    return assets, sectors, macro, flags, issues


# ---------------------------------------------------------------- históricos
def fetch_history(symbol: str, rng: str = "5y"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval=1d&events=div%2Csplit")
    data = _get_json(url, timeout=12)
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q.get("close")
    out = {"dates": [], "close": [], "adj": [], "volume": []}
    for i, t in enumerate(ts):
        c, a = q["close"][i], adj[i]
        if c is None or a is None:
            continue  # limpieza: se descartan barras incompletas
        out["dates"].append(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"))
        out["close"].append(float(c))
        out["adj"].append(float(a))
        out["volume"].append(float(q["volume"][i] or 0))
    out["currency"] = res["meta"].get("currency")
    out["fetched_at"] = time.time()
    return out


class MarketData:
    """Estado compartido: históricos (refresco cada 6 h) y cotizaciones (cada 30 s)."""

    QUOTE_INTERVAL = 30
    HISTORY_MAX_AGE = 6 * 3600

    def __init__(self):
        self.lock = threading.RLock()
        self.assets, self.sectors, self.macro, self.flags, self.issues = load_universe()
        self.history = {}
        self.quotes = {}
        self.quotes_at = 0.0
        self.quote_errors = 0
        self.history_ready = threading.Event()
        self.version = 0  # se incrementa en cada actualización de datos (invalida cachés)
        self.hist_version = 0
        self.symbols = [a["symbol"] for a in self.assets] + list(BENCHMARKS.keys())

    # --- históricos
    def _load_one_history(self, sym):
        path = CACHE_DIR / f"hist_{sym.replace('^', '_').replace('=', '_')}.json"
        cached = None
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                cached = None
        if cached and time.time() - cached.get("fetched_at", 0) < self.HISTORY_MAX_AGE:
            return sym, cached
        try:
            h = fetch_history(sym)
            if len(h["dates"]) > 30:
                path.write_text(json.dumps(h), encoding="utf-8")
                return sym, h
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            pass
        return sym, cached  # degradación: usa caché antigua si existe

    def refresh_history(self):
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(self._load_one_history, self.symbols))
        with self.lock:
            for sym, h in results:
                if h:
                    self.history[sym] = h
            self.version += 1
            self.hist_version += 1
        self.history_ready.set()

    # --- cotizaciones
    def refresh_quotes(self):
        new = {}
        chunks = [self.symbols[i:i + 20] for i in range(0, len(self.symbols), 20)]
        for chunk in chunks:
            url = ("https://query1.finance.yahoo.com/v7/finance/spark?symbols="
                   + urllib.parse.quote(",".join(chunk), safe=",") + "&range=1d&interval=1d")
            try:
                data = _get_json(url, timeout=8)
                for item in data.get("spark", {}).get("result", []):
                    resp = (item.get("response") or [{}])[0]
                    m = resp.get("meta", {})
                    if m.get("regularMarketPrice") is None:
                        continue
                    new[item["symbol"]] = {
                        "price": float(m["regularMarketPrice"]),
                        "change_pct": float(m.get("regularMarketChangePercent") or 0.0),
                        "market_time": m.get("regularMarketTime"),
                        "currency": m.get("currency"),
                    }
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
                self.quote_errors += 1
        with self.lock:
            if new:
                self.quotes.update(new)
                self.quotes_at = time.time()
                if "EURUSD=X" in new:
                    FX_TO_USD["EUR"] = new["EURUSD=X"]["price"]
                self.version += 1

    def reload_universe(self):
        with self.lock:
            self.assets, self.sectors, self.macro, self.flags, self.issues = load_universe()
            self.version += 1

    # --- bucles en segundo plano
    def start(self):
        def history_loop():
            while True:
                self.refresh_history()
                time.sleep(self.HISTORY_MAX_AGE)

        def quote_loop():
            while True:
                t0 = time.time()
                self.refresh_quotes()
                time.sleep(max(1.0, self.QUOTE_INTERVAL - (time.time() - t0)))

        threading.Thread(target=history_loop, daemon=True).start()
        threading.Thread(target=quote_loop, daemon=True).start()

    # --- series con último precio en vivo incorporado
    def series(self, sym):
        """Devuelve (dates, adj, close, volume) añadiendo/reemplazando la última barra con el precio en vivo."""
        with self.lock:
            h = self.history.get(sym)
            q = self.quotes.get(sym)
        if not h:
            return None
        dates, adj, close, vol = list(h["dates"]), list(h["adj"]), list(h["close"]), list(h["volume"])
        if q and q.get("price") and close:
            factor = adj[-1] / close[-1] if close[-1] else 1.0
            qdate = (datetime.fromtimestamp(q["market_time"], tz=timezone.utc).strftime("%Y-%m-%d")
                     if q.get("market_time") else dates[-1])
            if qdate == dates[-1]:
                close[-1], adj[-1] = q["price"], q["price"] * factor
            elif qdate > dates[-1]:
                dates.append(qdate)
                close.append(q["price"])
                adj.append(q["price"] * factor)
                vol.append(vol[-1])
        return dates, adj, close, vol
