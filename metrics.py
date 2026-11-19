"""Cálculo determinista de métricas de precio (momentum y riesgo) a partir de series diarias."""

import math
import statistics


def _pct(a, b):
    return a / b - 1.0 if b else None


def rsi(prices, n=14):
    if len(prices) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = prices[i] - prices[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(prices)):
        d = prices[i] - prices[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def log_returns(p):
    return [math.log(p[i] / p[i - 1]) for i in range(1, len(p)) if p[i - 1] > 0 and p[i] > 0]


def max_drawdown(p):
    peak, mdd = p[0], 0.0
    for x in p:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1.0)
    return mdd


def percentile(vals, q):
    s = sorted(vals)
    if not s:
        return None
    k = (len(s) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def price_metrics(series, bench_series, is_crypto=False, fx=1.0, has_volume=True):
    """series = (dates, adj, close, volume). Devuelve dict de métricas o {} si no hay datos suficientes."""
    if not series:
        return {}
    dates, adj, close, vol = series
    ppy = 365 if is_crypto else 252  # barras por año
    n3, n6, n1m = ppy // 4, ppy // 2, ppy // 12
    if len(adj) < n6 + 5:
        return {}
    m = {}
    p = adj
    m["ret_3m"] = _pct(p[-1], p[-n3]) * 100
    m["ret_6m"] = _pct(p[-1], p[-n6]) * 100
    if len(p) > ppy:
        m["ret_12m_1"] = _pct(p[-n1m], p[-ppy]) * 100  # 12-1: excluye el último mes (reversión a corto)
        m["ret_1y"] = _pct(p[-1], p[-ppy]) * 100
    ma_long = 200 if not is_crypto else 290
    ma_short = 50 if not is_crypto else 72
    if len(p) >= ma_long:
        ma200 = sum(p[-ma_long:]) / ma_long
        ma50 = sum(p[-ma_short:]) / ma_short
        m["px_ma200"] = (p[-1] / ma200 - 1) * 100
        m["ma50_ma200"] = (ma50 / ma200 - 1) * 100
    m["rsi14"] = rsi(p[-120:])
    if has_volume and len(vol) >= 120 and sum(vol[-120:]) > 0:
        v20, v120 = sum(vol[-20:]) / 20, sum(vol[-120:]) / 120
        m["vol_trend"] = v20 / v120 if v120 else None
        liq = [close[i] * vol[i] for i in range(len(close) - 63, len(close))]
        m["liquidity_usd_m"] = statistics.fmean(liq) * fx / 1e6
    window = p[-(ppy + 1):]
    r = log_returns(window)
    if len(r) > 30:
        m["vol_1y"] = statistics.pstdev(r) * math.sqrt(ppy) * 100
        simple = [math.exp(x) - 1 for x in r]
        m["var95"] = percentile(simple, 0.05) * 100  # VaR histórico 1 día (negativo)
        m["max_dd_1y"] = max_drawdown(window) * 100
    # beta frente a su índice de referencia, alineando por fecha
    if bench_series:
        bd, badj = bench_series[0], bench_series[1]
        bmap = dict(zip(bd, badj))
        own = dict(zip(dates, p))
        common = [d for d in dates[-(ppy + 5):] if d in bmap]
        if len(common) > 60:
            ra, rb = [], []
            for i in range(1, len(common)):
                d0, d1 = common[i - 1], common[i]
                ra.append(own[d1] / own[d0] - 1)
                rb.append(bmap[d1] / bmap[d0] - 1)
            vb = statistics.pvariance(rb)
            if vb > 0:
                mb, ma = statistics.fmean(rb), statistics.fmean(ra)
                cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / len(ra)
                m["beta"] = cov / vb
    # serie compacta para sparkline (≈1 año, 80 puntos)
    tail = close[-ppy:]
    step = max(1, len(tail) // 80)
    m["_spark"] = [round(x, 4) for x in tail[::step]]
    m["_spark_dates"] = dates[-ppy:][::step]
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items() if v is not None}
