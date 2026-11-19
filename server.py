"""Bursátil 360° — API + servidor estático (solo biblioteca estándar de Python).

Endpoints:
  GET  /api/meta            metodología, pesos por perfil, multiplicadores, umbrales, estado de datos
  POST /api/scores          ranking del universo según perfil, pesos y filtros
  POST /api/asset           ficha completa de un activo (radar, explicación, escenarios, sensibilidad, comparables)
  POST /api/portfolio       correlaciones, pesos (equiponderado / paridad de riesgo) y riesgo de cartera
  GET  /api/backtest        resultados del backtest walk-forward
  POST /api/reload          recarga los CSV/JSON de datos sin reiniciar
"""

import json
import math
import os
import statistics
import threading
import time
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import backtest
import scoring as sc
from data_sources import BENCHMARKS, FX_TO_USD, MarketData
from metrics import price_metrics

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MARKET_BENCH = {"IBEX": "^IBEX", "EEUU": "^GSPC", "GLOBAL": "^STOXX50E"}
BROAD_ALT = {"IBEX": "IWDA.AS", "EEUU": "VOO", "GLOBAL": "IWDA.AS"}


class Engine:
    def __init__(self):
        self.md = MarketData()
        self.lock = threading.Lock()
        self._raw_cache = (None, None, None)  # (version, raw, stats)
        self._bt_cache = (None, None)

    # ------------------------------------------------ métricas brutas + parámetros de normalización
    def bench_for(self, a):
        if a["type"] == "cripto":
            return "^GSPC"
        if a["market"] == "GLOBAL" and a["currency"] == "USD":
            return "^GSPC"
        return MARKET_BENCH.get(a["market"], "^GSPC")

    def raw_and_stats(self):
        v = self.md.version
        with self.lock:
            if self._raw_cache[0] == v:
                return self._raw_cache[1], self._raw_cache[2]
        raw = {}
        bench_cache = {}
        for a in self.md.assets:
            b = self.bench_for(a)
            if b not in bench_cache:
                bench_cache[b] = self.md.series(b)
            pm = price_metrics(self.md.series(a["symbol"]), bench_cache[b], is_crypto=a["type"] == "cripto",
                               fx=FX_TO_USD.get(a["currency"], 1.0), has_volume=a["type"] != "fondo")
            raw[a["symbol"]] = {**a["f"], **pm}
        stats = sc.build_stats(self.md.assets, raw, self.md.sectors)
        with self.lock:
            self._raw_cache = (v, raw, stats)
        return raw, stats

    def live_regime(self):
        q = self.md.quotes
        macro = self.md.macro

        def last(sym):
            if sym in q:
                return q[sym]["price"]
            h = self.md.history.get(sym)
            return h["close"][-1] if h else None

        bull = None
        s = self.md.series("^GSPC")
        if s and len(s[1]) >= 200:
            bull = s[1][-1] > sum(s[1][-200:]) / 200
        reg = sc.regime_from_inputs(last("^TNX"), last("^VIX"), bull, macro.get("cpi_us", 2.5),
                                    macro.get("cpi_eu", 2.0), macro.get("pmi_global", 50.0))
        reg["dxy"] = last("DX-Y.NYB")
        reg["eurusd"] = last("EURUSD=X")
        reg["spx"] = last("^GSPC")
        reg["macro_as_of"] = macro.get("as_of")
        return reg

    # ------------------------------------------------ parámetros de la petición
    @staticmethod
    def parse_params(body):
        profile = body.get("profile", "equilibrado")
        base = dict(sc.PROFILE_WEIGHTS.get(profile, sc.PROFILE_WEIGHTS["equilibrado"]))
        custom = body.get("weights")
        if isinstance(custom, dict):
            for p in sc.PILLARS:
                if p in custom:
                    try:
                        base[p] = max(0.0, float(custom[p]))
                    except (TypeError, ValueError):
                        pass
        return {
            "profile": profile, "base": base,
            "horizon": body.get("horizon", "medio"), "objective": body.get("objective", "crecimiento"),
            "regime_on": bool(body.get("regime_on", True)),
            "filters": body.get("filters") or {},
        }

    def evaluate_all(self, prm, regime=None, raw_override=None):
        raw, stats = self.raw_and_stats()
        regime = regime or self.live_regime()
        weights, applied = sc.effective_weights(prm["base"], prm["horizon"], prm["objective"], regime,
                                                prm["regime_on"])
        today = date.today()
        res = {}
        for a in self.md.assets:
            r = dict((raw_override or {}).get(a["symbol"]) or raw[a["symbol"]])
            r["macro_fit"] = sc.macro_fit(a, regime, self.md.sectors)
            res[a["symbol"]] = (a, r, sc.evaluate(a, r, stats, self.md.sectors, weights,
                                                  self.md.flags.get(a["symbol"], []), today))
        return res, weights, applied, regime, stats

    @staticmethod
    def passes(a, r, ev, f):
        if f.get("markets") and a["market"] not in f["markets"]:
            return False
        if f.get("types") and a["type"] not in f["types"]:
            return False
        if f.get("excluded_sectors") and a["sector"] in f["excluded_sectors"]:
            return False
        if f.get("currencies") and a["currency"] not in f["currencies"]:
            return False
        esg_max = f.get("esg_max")
        if esg_max not in (None, "") and a["type"] == "accion":
            if r.get("esg_risk") is None or r["esg_risk"] > float(esg_max):
                return False
        min_mcap = f.get("min_mcap_bn")
        if min_mcap not in (None, "", 0) and a["type"] == "accion":
            if (r.get("mcap_bn") or 0) < float(min_mcap):
                return False
        if f.get("min_confidence") and ev["confidence"]["score"] < float(f["min_confidence"]):
            return False
        return True

    def sector_ranks(self, res):
        by_sector = {}
        for sym, (a, r, ev) in res.items():
            by_sector.setdefault(a["sector"], []).append((ev["score"], sym))
        ranks = {}
        for sec, lst in by_sector.items():
            lst.sort(reverse=True)
            for i, (_, sym) in enumerate(lst):
                ranks[sym] = (i + 1, len(lst))
        return ranks

    def asset_row(self, a, r, ev, rank):
        q = self.md.quotes.get(a["symbol"], {})
        return {
            "symbol": a["symbol"], "name": a["name"], "market": a["market"], "type": a["type"],
            "sector": a["sector"], "currency": a["currency"], "as_of": a["as_of"],
            "price": q.get("price"), "change_pct": q.get("change_pct"),
            "score": ev["score"], "raw_score": ev["raw_score"], "penalty": ev["penalty"],
            "label": ev["label"], "label_key": ev["label_key"],
            "pillars": {p: d["score"] for p, d in ev["pillars"].items()},
            "confidence": ev["confidence"], "flags": ev["flags"],
            "sector_rank": rank[0], "sector_n": rank[1],
            "ret_1y": r.get("ret_1y"), "vol_1y": r.get("vol_1y"),
        }

    # ------------------------------------------------ endpoints
    def scores(self, body):
        prm = self.parse_params(body)
        res, weights, applied, regime, _ = self.evaluate_all(prm)
        ranks = self.sector_ranks(res)
        rows = [self.asset_row(a, r, ev, ranks[sym]) for sym, (a, r, ev) in res.items()
                if self.passes(a, r, ev, prm["filters"])]
        rows.sort(key=lambda x: x["score"], reverse=True)
        return {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": self.status(),
                "regime": regime, "weights": weights, "base_weights": prm["base"], "applied": applied,
                "assets": rows}

    def asset(self, body):
        sym = body.get("symbol")
        prm = self.parse_params(body)
        res, weights, applied, regime, stats = self.evaluate_all(prm)
        if sym not in res:
            return {"error": "Activo no encontrado"}
        a, r, ev_short = res[sym]
        manual = self.md.flags.get(sym, [])
        ev = sc.evaluate(a, r, stats, self.md.sectors, weights, manual, detail=True)
        ranks = self.sector_ranks(res)

        # escenarios: régimen macro + shock de precio proporcional a la beta (siempre con ajuste por régimen)
        scen = {}
        for name, shock in (("favorable", 10.0), ("adverso", -20.0)):
            reg = sc.regime_from_inputs(**sc.SCENARIO_REGIMES[name], source="escenario")
            w2, _ = sc.effective_weights(prm["base"], prm["horizon"], prm["objective"], reg, True)
            r2 = sc.price_shock(r, shock)
            r2["macro_fit"] = sc.macro_fit(a, reg, self.md.sectors)
            e2 = sc.evaluate(a, r2, stats, self.md.sectors, w2, manual)
            scen[name] = {"score": e2["score"], "label": e2["label"], "label_key": e2["label_key"],
                          "regime": reg["flag_labels"], "price_shock": shock}
        scen["base"] = {"score": ev["score"], "label": ev["label"], "label_key": ev["label_key"],
                        "regime": regime["flag_labels"], "price_shock": 0}
        price = (self.md.quotes.get(sym) or {}).get("price")
        rng = None
        if price and r.get("vol_1y"):
            s = r["vol_1y"] / 100
            rng = {"adverso": round(price * math.exp(-1.65 * s), 2), "base": round(price, 2),
                   "favorable": round(price * math.exp(s), 2), "vol": r["vol_1y"]}

        # comparables: mismo sector + índice de su mercado + alternativa amplia (ETF)
        peers = []
        for psym, (pa, pr, pe) in res.items():
            if pa["sector"] == a["sector"]:
                peers.append({"symbol": psym, "name": pa["name"], "score": pe["score"], "label_key": pe["label_key"],
                              "ret_1y": pr.get("ret_1y"), "vol_1y": pr.get("vol_1y"), "kind": "Mismo sector",
                              "self": psym == sym})
        alt = BROAD_ALT.get(a["market"])
        if alt and alt in res and alt != sym:
            pa, pr, pe = res[alt]
            peers.append({"symbol": alt, "name": pa["name"], "score": pe["score"], "label_key": pe["label_key"],
                          "ret_1y": pr.get("ret_1y"), "vol_1y": pr.get("vol_1y"), "kind": "Alternativa indexada"})
        bsym = self.bench_for(a)
        bpm = price_metrics(self.md.series(bsym), None)
        peers.append({"symbol": bsym, "name": BENCHMARKS.get(bsym, bsym), "score": None, "label_key": None,
                      "ret_1y": bpm.get("ret_1y"), "vol_1y": bpm.get("vol_1y"), "kind": "Índice de referencia"})

        # media del sector para el radar (sin el propio activo; si no hay pares, media de su clase)
        mates = [res[s][2] for s, (pa, _, _) in res.items() if pa["sector"] == a["sector"] and s != sym]
        ref_label = "Media del sector"
        if not mates:
            mates = [res[s][2] for s, (pa, _, _) in res.items()
                     if sc.asset_class(pa) == sc.asset_class(a) and s != sym]
            ref_label = "Media de su clase de activo"
        radar_ref = {}
        for p in ev["pillars"]:
            vals = [m["pillars"][p]["score"] for m in mates if p in m["pillars"] and m["pillars"][p]["score"] is not None]
            radar_ref[p] = round(statistics.fmean(vals), 1) if vals else None

        return {
            "asset": {k: a[k] for k in ("symbol", "name", "market", "type", "sector", "currency", "as_of")},
            "price": price, "change_pct": (self.md.quotes.get(sym) or {}).get("change_pct"),
            "eval": ev, "sector_rank": ranks[sym][0], "sector_n": ranks[sym][1],
            "weights": weights, "regime": regime,
            "scenarios": scen, "price_range_12m": rng,
            "sensitivity": sc.sensitivity(a, r, stats, self.md.sectors, weights, manual, ev),
            "peers": sorted(peers, key=lambda x: (x["score"] is None, -(x["score"] or 0))),
            "radar_ref": radar_ref, "radar_ref_label": ref_label,
            "spark": r.get("_spark"), "spark_dates": r.get("_spark_dates"),
            "price_metrics": {k: r.get(k) for k in sc.PRICE_METRICS},
            "quote_time": self.md.quotes_at,
        }

    def portfolio(self, body):
        syms = [s for s in (body.get("symbols") or []) if s in self.md.history][:15]
        if len(syms) < 2:
            return {"error": "Selecciona al menos 2 activos con histórico de precios."}
        series = {}
        for s in syms:
            se = self.md.series(s)
            if se:
                series[s] = dict(zip(se[0], se[1]))
        common = sorted(set.intersection(*(set(v.keys()) for v in series.values())))[-253:]
        if len(common) < 60:
            return {"error": "Pocas fechas comunes entre los activos seleccionados."}
        rets = {s: [series[s][common[i]] / series[s][common[i - 1]] - 1 for i in range(1, len(common))] for s in syms}
        n = len(common) - 1
        means = {s: statistics.fmean(rets[s]) for s in syms}
        cov = {(x, y): sum((a - means[x]) * (b - means[y]) for a, b in zip(rets[x], rets[y])) / n
               for x in syms for y in syms}
        vol = {s: math.sqrt(cov[(s, s)] * 252) for s in syms}
        corr = [[round(cov[(x, y)] / math.sqrt(cov[(x, x)] * cov[(y, y)]), 2) if cov[(x, x)] and cov[(y, y)] else 0
                 for y in syms] for x in syms]

        def stats_for(w):
            pv = math.sqrt(max(0.0, sum(w[x] * w[y] * cov[(x, y)] for x in syms for y in syms) * 252))
            dr = sum(w[s] * vol[s] for s in syms) / pv if pv else None
            path = [1.0]
            for i in range(n):
                path.append(path[-1] * (1 + sum(w[s] * rets[s][i] for s in syms)))
            peak, mdd = 1.0, 0.0
            for v in path:
                peak = max(peak, v)
                mdd = min(mdd, v / peak - 1)
            return {"vol": round(pv * 100, 2), "div_ratio": round(dr, 2) if dr else None,
                    "ret_hist": round((path[-1] - 1) * 100, 2), "max_dd": round(mdd * 100, 2),
                    "var95_1y": round(-1.65 * pv * 100, 2)}

        eq = {s: 1 / len(syms) for s in syms}
        inv = {s: (1 / vol[s]) if vol[s] > 0 else 0 for s in syms}
        tot = sum(inv.values()) or 1
        rp = {s: inv[s] / tot for s in syms}
        # tope por activo (evita que un activo casi sin volatilidad, p. ej. letras, absorba toda la cartera)
        cap = max(float(body.get("max_weight") or 0.4), 1 / len(syms))
        for _ in range(20):
            over = {s for s in syms if rp[s] > cap + 1e-9}
            if not over:
                break
            excess = sum(rp[s] - cap for s in over)
            free = [s for s in syms if s not in over and rp[s] < cap]
            ftot = sum(rp[s] for s in free) or 1
            for s in over:
                rp[s] = cap
            for s in free:
                rp[s] += excess * rp[s] / ftot
        names = {a["symbol"]: a["name"] for a in self.md.assets}
        return {
            "symbols": syms, "names": [names.get(s, s) for s in syms], "corr": corr,
            "vol": {s: round(vol[s] * 100, 2) for s in syms},
            "ret_1y": {s: round((series[s][common[-1]] / series[s][common[0]] - 1) * 100, 2) for s in syms},
            "methods": {"equiponderada": {"weights": eq, **stats_for(eq)},
                        "paridad_riesgo": {"weights": rp, **stats_for(rp)}},
            "window": {"from": common[0], "to": common[-1], "days": n}, "max_weight": cap,
        }

    def backtest(self):
        hv = self.md.hist_version
        with self.lock:
            if self._bt_cache[0] == hv and self._bt_cache[1]:
                return self._bt_cache[1]
        if not self.md.history_ready.is_set():
            return {"ok": False, "pending": True, "error": "Cargando históricos de precios..."}
        syms = [a["symbol"] for a in self.md.assets if a["type"] == "accion"]
        res = backtest.run_backtest(self.md.history, syms)
        with self.lock:
            self._bt_cache = (hv, res)
        return res

    def status(self):
        with_hist = sum(1 for a in self.md.assets if a["symbol"] in self.md.history)
        with_quote = sum(1 for a in self.md.assets if a["symbol"] in self.md.quotes)
        age = time.time() - self.md.quotes_at if self.md.quotes_at else None
        return {"history_ready": self.md.history_ready.is_set(), "assets": len(self.md.assets),
                "with_history": with_hist, "with_quote": with_quote,
                "quote_age_s": None if age is None else round(age), "quote_errors": self.md.quote_errors,
                "data_issues": self.md.issues}

    def meta(self):
        return {
            "pillars": sc.PILLARS, "pillar_labels": sc.PILLAR_LABELS,
            "metrics": {k: {"pillar": v[0], "label": v[1], "unit": v[2], "direction": v[3], "weight": v[4],
                            "mode": v[5]} for k, v in sc.METRICS.items()},
            "profiles": sc.PROFILE_WEIGHTS, "horizon_mult": sc.HORIZON_MULT, "objective_mult": sc.OBJECTIVE_MULT,
            "regime_mult": sc.REGIME_MULT, "regime_labels": sc.REGIME_LABELS,
            "thresholds": {"fav": sc.FAV_THRESHOLD, "desf": sc.DESF_THRESHOLD, "penalty_cap": sc.PENALTY_CAP},
            "scenario_regimes": sc.SCENARIO_REGIMES,
            "sectors": sorted({a["sector"] for a in self.md.assets}),
            "markets": sorted({a["market"] for a in self.md.assets}),
            "types": sorted({a["type"] for a in self.md.assets}),
            "currencies": sorted({a["currency"] for a in self.md.assets}),
            "macro": self.md.macro, "status": self.status(),
            "backtest_params": {"cost_bps": backtest.COST_BPS_PER_SIDE, "tax": backtest.TAX_RATE,
                                "calibration": backtest.CALIBRATION_MONTHS, "candidates": backtest.CANDIDATE_W},
        }


ENGINE = Engine()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/meta":
                return self._json(ENGINE.meta())
            if path == "/api/backtest":
                return self._json(ENGINE.backtest())
        except Exception as exc:  # noqa: BLE001 - devolver error legible al frontend
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/scores":
                return self._json(ENGINE.scores(body))
            if path == "/api/asset":
                return self._json(ENGINE.asset(body))
            if path == "/api/portfolio":
                return self._json(ENGINE.portfolio(body))
            if path == "/api/reload":
                ENGINE.md.reload_universe()
                return self._json({"ok": True, "issues": ENGINE.md.issues})
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": "Ruta no encontrada"}, 404)

    def log_message(self, *args):
        pass


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8050"))
    ENGINE.md.start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Bursátil 360° (scoring multifactor) en http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
