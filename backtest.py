"""Backtest walk-forward de la parte del modelo que puede validarse sin look-ahead bias.

Solo se backtestean factores de PRECIO (momentum + riesgo), porque los fundamentales del CSV son una foto
actual: usarlos en el pasado introduciría look-ahead bias. Rebalanceo mensual, quintiles, IC de Spearman,
costes de transacción y calibración walk-forward (ventana expansiva) del peso momentum/baja volatilidad."""

import bisect
import math
import statistics

from metrics import percentile

CANDIDATE_W = [0.0, 0.25, 0.5, 0.75, 1.0]  # peso de momentum (resto = baja volatilidad)
CALIBRATION_MONTHS = 12
COST_BPS_PER_SIDE = 15  # comisión + medio spread, por lado
TAX_RATE = 0.19  # supuesto: tramo inicial base del ahorro (España)


def _rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0
        i = j + 1
    return ranks


def spearman(a, b):
    if len(a) < 5:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


def _zscores(vals):
    lo, hi = percentile(vals, 0.05), percentile(vals, 0.95)
    w = [min(hi, max(lo, v)) for v in vals]
    m, s = statistics.fmean(w), statistics.pstdev(w)
    return [(x - m) / s if s > 0 else 0.0 for x in w]


def _max_dd(curve):
    peak, mdd = curve[0], 0.0
    for x in curve:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    return mdd


def _perf(monthly):
    if not monthly:
        return {}
    curve = [1.0]
    for r in monthly:
        curve.append(curve[-1] * (1 + r))
    n = len(monthly)
    cagr = curve[-1] ** (12 / n) - 1
    vol = statistics.pstdev(monthly) * math.sqrt(12) if n > 1 else 0
    return {"cagr": round(cagr * 100, 2), "vol": round(vol * 100, 2),
            "sharpe": round(cagr / vol, 2) if vol > 0 else None,
            "max_dd": round(_max_dd(curve) * 100, 2), "total": round((curve[-1] - 1) * 100, 2), "curve": curve}


def run_backtest(history, symbols, calendar_symbol="^GSPC"):
    cal = history.get(calendar_symbol)
    if not cal:
        return {"ok": False, "error": "Sin calendario de referencia (histórico del S&P 500 no disponible)."}
    # fin de mes según el calendario de referencia
    month_ends = []
    for i, d in enumerate(cal["dates"]):
        if i + 1 == len(cal["dates"]) or cal["dates"][i + 1][:7] != d[:7]:
            month_ends.append(d)
    data = {s: history[s] for s in symbols if s in history and len(history[s]["dates"]) > 300}

    def px_at(h, d):
        j = bisect.bisect_right(h["dates"], d) - 1
        return j

    months = []  # cada elemento: {date, syms, feats, fwd}
    for mi in range(len(month_ends) - 1):
        t, t1 = month_ends[mi], month_ends[mi + 1]
        rows = []
        for s, h in data.items():
            j = px_at(h, t)
            j1 = px_at(h, t1)
            if j < 260 or j1 <= j:
                continue
            p = h["adj"]
            window = p[j - 252:j + 1]
            lr = [math.log(window[k] / window[k - 1]) for k in range(1, len(window)) if window[k - 1] > 0]
            if len(lr) < 200:
                continue
            ma200 = sum(p[j - 199:j + 1]) / 200
            rows.append({
                "s": s,
                "mom12_1": p[j - 21] / p[j - 252] - 1,
                "mom6": p[j] / p[j - 126] - 1,
                "trend": p[j] / ma200 - 1,
                "vol": statistics.pstdev(lr) * math.sqrt(252),
                "fwd": p[j1] / p[j] - 1,
            })
        if len(rows) >= 10:
            months.append({"date": t, "rows": rows})

    if len(months) < CALIBRATION_MONTHS + 6:
        return {"ok": False, "error": f"Histórico insuficiente ({len(months)} meses válidos)."}

    # pre-cálculo del score para cada candidato
    for m in months:
        rows = m["rows"]
        zm = _zscores([r["mom12_1"] for r in rows])
        z6 = _zscores([r["mom6"] for r in rows])
        zt = _zscores([r["trend"] for r in rows])
        zv = _zscores([r["vol"] for r in rows])
        mom = [(a + b + c) / 3 for a, b, c in zip(zm, z6, zt)]
        fwd = [r["fwd"] for r in rows]
        m["fwd"] = fwd
        m["scores"] = {w: [w * mo - (1 - w) * v for mo, v in zip(mom, zv)] for w in CANDIDATE_W}
        m["ic"] = {w: spearman(m["scores"][w], fwd) for w in CANDIDATE_W}

    # in-sample (todo el periodo): el mejor candidato "a posteriori" (sesgo de sobreajuste, solo comparativo)
    def mean_ic(ms, w):
        v = [m["ic"][w] for m in ms if m["ic"][w] is not None]
        return statistics.fmean(v) if v else -9

    best_is = max(CANDIDATE_W, key=lambda w: mean_ic(months, w))

    # walk-forward: en cada mes se elige el peso con mejor IC en los meses ANTERIORES y se aplica al siguiente
    oos = []
    prev_top, prev_bot = set(), set()
    for k in range(CALIBRATION_MONTHS, len(months)):
        w = max(CANDIDATE_W, key=lambda c: mean_ic(months[:k], c))
        m = months[k]
        sc, fwd, syms = m["scores"][w], m["fwd"], [r["s"] for r in m["rows"]]
        order = sorted(range(len(sc)), key=lambda i: sc[i])
        n = len(order)
        q = [order[int(i * n / 5):int((i + 1) * n / 5)] for i in range(5)]
        qret = [statistics.fmean([fwd[i] for i in g]) if g else 0 for g in q]
        top, bot = {syms[i] for i in q[4]}, {syms[i] for i in q[0]}
        turn_top = 1 - len(top & prev_top) / len(top) if prev_top else 1.0
        turn_bot = 1 - len(bot & prev_bot) / len(bot) if prev_bot else 1.0
        cost = COST_BPS_PER_SIDE / 1e4 * 2
        oos.append({"date": m["date"], "w": w, "ic": m["ic"][w], "q": qret, "n": n,
                    "top_net": qret[4] - turn_top * cost, "bot_net": qret[0] - turn_bot * cost,
                    "universe": statistics.fmean(fwd), "turnover": turn_top,
                    "top_names": sorted(top)})
        prev_top, prev_bot = top, bot

    ics = [o["ic"] for o in oos if o["ic"] is not None]
    ic_mean = statistics.fmean(ics)
    ic_std = statistics.pstdev(ics)
    top = _perf([o["top_net"] for o in oos])
    top_gross = _perf([o["q"][4] for o in oos])
    bot = _perf([o["bot_net"] for o in oos])
    uni = _perf([o["universe"] for o in oos])
    ls = _perf([o["q"][4] - o["q"][0] for o in oos])
    quint_ann = [round(((1 + statistics.fmean([o["q"][i] for o in oos])) ** 12 - 1) * 100, 2) for i in range(5)]
    after_tax = None
    if top.get("curve"):
        gain = top["curve"][-1] - 1
        after_tax = round((gain * (1 - TAX_RATE) if gain > 0 else gain) * 100, 2)

    def strip(p):
        return {k: v for k, v in p.items() if k != "curve"}

    return {
        "ok": True,
        "period": {"from": oos[0]["date"], "to": oos[-1]["date"], "months_oos": len(oos),
                   "calibration_months": CALIBRATION_MONTHS, "universe_size": len(data)},
        "ic": {"mean": round(ic_mean, 4), "std": round(ic_std, 4),
               "t_stat": round(ic_mean / ic_std * math.sqrt(len(ics)), 2) if ic_std > 0 else None,
               "hit_rate": round(sum(1 for x in ics if x > 0) / len(ics) * 100, 1),
               "in_sample_best_w": best_is, "in_sample_ic": round(mean_ic(months, best_is), 4),
               "oos_ic": round(ic_mean, 4)},
        "quintiles_annualized": quint_ann,
        "top_net": strip(top), "top_gross": strip(top_gross), "bottom_net": strip(bot),
        "universe": strip(uni), "long_short": strip(ls),
        "avg_turnover": round(statistics.fmean([o["turnover"] for o in oos[1:]]) * 100, 1),
        "after_tax_total_top": after_tax, "tax_rate": TAX_RATE, "cost_bps_per_side": COST_BPS_PER_SIDE,
        "curves": {"dates": [months[CALIBRATION_MONTHS - 1]["date"]] + [o["date"] for o in oos],
                   "top": top["curve"], "bottom": bot["curve"], "universe": uni["curve"]},
        "monthly": [{"date": o["date"], "ic": None if o["ic"] is None else round(o["ic"], 3), "w": o["w"],
                     "top_names": o["top_names"]} for o in oos],
    }
