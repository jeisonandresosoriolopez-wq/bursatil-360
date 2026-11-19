"""Motor de scoring multifactor determinista.

Flujo: métricas brutas -> limpieza (winsorización p5/p95) -> normalización relativa (z-score dentro del grupo
de pares; valoración y márgenes relativos a la mediana del sector) -> score por pilar (0-100) -> score compuesto
con pesos del perfil (ajustados por horizonte, objetivo y régimen de mercado) -> penalizaciones (red flags)
-> nivel de confianza -> etiqueta descriptiva.

NO se usa ningún LLM en el cálculo: mismo input => mismo output."""

import math
import statistics
from datetime import date

from metrics import percentile

# ------------------------------------------------------------------ definición de pilares
PILLARS = ["valoracion", "calidad", "crecimiento", "salud", "dividendos", "momentum",
           "riesgo", "sentimiento", "macro", "cualitativo", "esg"]
PILLAR_LABELS = {
    "valoracion": "Valoración", "calidad": "Calidad", "crecimiento": "Crecimiento",
    "salud": "Salud financiera", "dividendos": "Dividendos", "momentum": "Momentum / técnico",
    "riesgo": "Riesgo de mercado", "sentimiento": "Sentimiento", "macro": "Macro y sector",
    "cualitativo": "Cualitativo", "esg": "ESG",
}

# key: (pilar, etiqueta, unidad, dirección, peso, modo)
#   dirección: high = más es mejor, low = menos es mejor, mid = óptimo en rango
#   modo: z (z-score en grupo de pares), zlog (z-score de log), sector (log ratio vs mediana sectorial), abs (escala fija)
METRICS = {
    "pe":                ("valoracion", "PER", "x", "low", 1.0, "sector"),
    "pb":                ("valoracion", "Precio / valor contable", "x", "low", 0.6, "sector"),
    "ev_ebitda":         ("valoracion", "EV / EBITDA", "x", "low", 1.0, "sector"),
    "fcf_yield":         ("valoracion", "FCF yield", "%", "high", 1.0, "z"),
    "peg":               ("valoracion", "PEG", "x", "low", 0.7, "z"),
    "roe":               ("calidad", "ROE", "%", "high", 0.8, "z"),
    "roic":              ("calidad", "ROIC", "%", "high", 1.0, "z"),
    "op_margin":         ("calidad", "Margen operativo", "%", "high", 0.8, "sector"),
    "eps_stability":     ("calidad", "Estabilidad del BPA", "0-1", "high", 0.7, "abs"),
    "piotroski":         ("calidad", "Piotroski F-Score", "/9", "high", 0.7, "abs"),
    "ter":               ("calidad", "Coste (TER)", "%", "low", 1.0, "z"),
    "aum_bn":            ("calidad", "Patrimonio gestionado", "bn", "high", 0.5, "zlog"),
    "rev_cagr":          ("crecimiento", "CAGR ingresos (3a)", "%", "high", 1.0, "z"),
    "eps_cagr":          ("crecimiento", "CAGR BPA (3a)", "%", "high", 1.0, "z"),
    "est_revisions":     ("crecimiento", "Revisiones de estimaciones", "-1..1", "high", 0.6, "abs"),
    "debt_ebitda":       ("salud", "Deuda neta / EBITDA", "x", "low", 1.0, "z"),
    "interest_cov":      ("salud", "Cobertura de intereses", "x", "high", 0.8, "zlog"),
    "current_ratio":     ("salud", "Ratio de liquidez", "x", "high", 0.5, "z"),
    "altman_z":          ("salud", "Altman Z-Score", "", "high", 1.0, "abs"),
    "div_yield":         ("dividendos", "Rentabilidad por dividendo", "%", "high", 1.0, "z"),
    "payout":            ("dividendos", "Payout", "%", "mid", 0.7, "abs"),
    "div_growth_years":  ("dividendos", "Años de crecimiento del dividendo", "a", "high", 0.7, "z"),
    "fcf_div_cover":     ("dividendos", "Cobertura del dividendo por FCF", "x", "high", 0.7, "z"),
    "ret_3m":            ("momentum", "Rentabilidad 3 meses", "%", "high", 0.6, "z"),
    "ret_6m":            ("momentum", "Rentabilidad 6 meses", "%", "high", 0.8, "z"),
    "ret_12m_1":         ("momentum", "Rentabilidad 12-1 meses", "%", "high", 1.0, "z"),
    "px_ma200":          ("momentum", "Precio vs media 200 sesiones", "%", "high", 0.8, "z"),
    "ma50_ma200":        ("momentum", "Media 50 vs media 200", "%", "high", 0.5, "z"),
    "rsi14":             ("momentum", "RSI (14)", "", "mid", 0.4, "abs"),
    "vol_trend":         ("momentum", "Tendencia de volumen (20d/120d)", "x", "high", 0.3, "z"),
    "vol_1y":            ("riesgo", "Volatilidad anualizada", "%", "low", 1.0, "z"),
    "beta":              ("riesgo", "Beta vs índice", "", "low", 0.7, "z"),
    "max_dd_1y":         ("riesgo", "Máx. drawdown 1 año", "%", "high", 0.8, "z"),
    "var95":             ("riesgo", "VaR 95% diario", "%", "high", 0.7, "z"),
    "liquidity_usd_m":   ("riesgo", "Liquidez (vol. medio diario)", "M$", "high", 0.5, "zlog"),
    "analyst_rating":    ("sentimiento", "Consenso analistas (1=compra, 5=venta)", "", "low", 1.0, "abs"),
    "short_interest":    ("sentimiento", "Posiciones cortas", "% float", "low", 0.6, "z"),
    "insider_net":       ("sentimiento", "Compras netas de directivos", "-1..1", "high", 0.4, "abs"),
    "macro_fit":         ("macro", "Encaje con el régimen macro actual", "0-100", "high", 1.0, "abs"),
    "moat":              ("cualitativo", "Ventaja competitiva", "0-2", "high", 1.0, "abs"),
    "management":        ("cualitativo", "Calidad de la gestión", "1-3", "high", 0.7, "abs"),
    "reg_risk":          ("cualitativo", "Riesgo regulatorio", "1-3", "low", 0.7, "abs"),
    "cust_conc":         ("cualitativo", "Concentración de clientes", "1-3", "low", 0.5, "abs"),
    "esg_risk":          ("esg", "Riesgo ESG (menor = mejor)", "", "low", 1.0, "z"),
    "controversy":       ("esg", "Nivel de controversias", "0-5", "low", 0.7, "abs"),
}
PRICE_METRICS = {k for k, v in METRICS.items() if v[0] in ("momentum", "riesgo")}
FINANCIAL_SECTORS = {"Banca", "Seguros / Holding"}
NOT_FOR_FINANCIALS = {"ev_ebitda", "fcf_yield", "roic", "op_margin", "debt_ebitda", "interest_cov",
                      "current_ratio", "altman_z", "fcf_div_cover"}
VEHICLE_METRICS = {"pe", "pb", "ter", "aum_bn", "div_yield", "macro_fit"}

# ------------------------------------------------------------------ pesos
PROFILE_WEIGHTS = {  # tabla base (suma 100). Sentimiento/Macro se reparte 50/50.
    "conservador": {"valoracion": 20, "calidad": 20, "crecimiento": 5, "salud": 20, "dividendos": 15,
                    "momentum": 5, "riesgo": 10, "sentimiento": 2.5, "macro": 2.5, "cualitativo": 0, "esg": 0},
    "equilibrado": {"valoracion": 20, "calidad": 20, "crecimiento": 15, "salud": 10, "dividendos": 10,
                    "momentum": 10, "riesgo": 5, "sentimiento": 5, "macro": 5, "cualitativo": 0, "esg": 0},
    "agresivo":    {"valoracion": 10, "calidad": 15, "crecimiento": 25, "salud": 5, "dividendos": 0,
                    "momentum": 25, "riesgo": 5, "sentimiento": 7.5, "macro": 7.5, "cualitativo": 0, "esg": 0},
}
HORIZON_MULT = {
    "corto": {"momentum": 1.5, "riesgo": 1.2, "valoracion": 0.7, "calidad": 0.8, "crecimiento": 0.8},
    "medio": {},
    "largo": {"calidad": 1.25, "valoracion": 1.15, "salud": 1.1, "momentum": 0.6},
}
OBJECTIVE_MULT = {
    "ingresos": {"dividendos": 1.8, "salud": 1.1, "crecimiento": 0.8},
    "crecimiento": {"crecimiento": 1.5, "momentum": 1.1, "dividendos": 0.5},
    "preservacion": {"riesgo": 1.6, "salud": 1.3, "momentum": 0.7, "crecimiento": 0.7},
}
REGIME_MULT = {
    "high_rates": {"calidad": 1.2, "salud": 1.25, "crecimiento": 0.85},
    "low_rates": {"crecimiento": 1.15, "dividendos": 0.9},
    "high_vol": {"riesgo": 1.4, "momentum": 0.8, "salud": 1.1},
    "low_vol": {"momentum": 1.1},
    "bear": {"riesgo": 1.2, "calidad": 1.1, "momentum": 0.9},
    "bull": {"momentum": 1.1, "crecimiento": 1.05},
}
REGIME_LABELS = {"high_rates": "Tipos altos", "low_rates": "Tipos bajos", "high_vol": "Alta volatilidad",
                 "low_vol": "Baja volatilidad", "bear": "Tendencia bajista", "bull": "Tendencia alcista"}

FAV_THRESHOLD, DESF_THRESHOLD = 60.0, 40.0
PENALTY_CAP = 25.0
LABELS = {"fav": "Perfil favorable según el modelo", "neu": "Neutral según el modelo",
          "desf": "Perfil desfavorable según el modelo"}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


# ------------------------------------------------------------------ escalas absolutas
def _abs_score(key, v):
    if key == "eps_stability":
        return clamp(v * 100, 0, 100)
    if key == "piotroski":
        return clamp(v / 9 * 100, 0, 100)
    if key in ("est_revisions", "insider_net"):
        return clamp((v + 1) * 50, 0, 100)
    if key == "altman_z":
        return clamp((v - 1.0) / 5.0 * 100, 0, 100)
    if key == "payout":
        if v <= 0:
            return 30.0
        if v < 25:
            return 60 + v / 25 * 40
        if v <= 60:
            return 100.0
        if v <= 100:
            return 100 - (v - 60) * 1.5
        return clamp(40 - (v - 100), 0, 40)
    if key == "rsi14":
        if v < 30:
            return 55.0
        if v < 45:
            return 55 + (v - 30) * 3
        if v <= 65:
            return 100.0
        if v <= 80:
            return 100 - (v - 65) * 4
        return 25.0
    if key == "analyst_rating":
        return clamp((5 - v) / 4 * 100, 0, 100)
    if key == "moat":
        return clamp(v * 50, 0, 100)
    if key == "management":
        return clamp((v - 1) * 50, 0, 100)
    if key in ("reg_risk", "cust_conc"):
        return clamp((3 - v) * 50, 0, 100)
    if key == "controversy":
        return clamp(100 - 20 * v, 0, 100)
    if key == "macro_fit":
        return clamp(v, 0, 100)
    return None


# ------------------------------------------------------------------ aplicabilidad y grupos de pares
def asset_class(asset):
    t = asset["type"]
    return "accion" if t == "accion" else ("cripto" if t == "cripto" else "vehiculo")


def applicable(asset, key):
    cls = asset_class(asset)
    if key in PRICE_METRICS:
        if key in ("vol_trend", "liquidity_usd_m") and asset["type"] == "fondo":
            return False
        return True
    if key == "macro_fit":
        return True
    if cls == "cripto":
        return False
    if cls == "vehiculo":
        if key == "div_yield":  # vehículos de acumulación u oro: el dividendo no aplica (no es negativo)
            return (asset["f"].get("div_yield") or 0) > 0
        return key in VEHICLE_METRICS
    if key in ("ter", "aum_bn"):
        return False
    if asset["sector"] in FINANCIAL_SECTORS and key in NOT_FOR_FINANCIALS:
        return False
    return True


def peer_group(asset, key):
    return "all" if key in PRICE_METRICS else asset_class(asset)


def _transform(key, v, asset, sectors):
    mode = METRICS[key][5]
    if v is None:
        return None
    if mode == "z":
        return v
    if mode == "zlog":
        return math.log(v) if v > 0 else None
    if mode == "sector":
        ref = (sectors.get(asset["sector"]) or {}).get(key)
        if not ref or v <= 0:
            return None
        return math.log(v / ref)
    return v


def build_stats(assets, raw, sectors):
    """Parámetros de normalización por (métrica, grupo): límites de winsorización p5/p95, media y desviación."""
    buckets = {}
    for a in assets:
        r = raw[a["symbol"]]
        for key, spec in METRICS.items():
            if spec[5] == "abs" or not applicable(a, key):
                continue
            t = _transform(key, r.get(key), a, sectors)
            if t is None:
                continue
            buckets.setdefault((key, peer_group(a, key)), []).append(t)
    stats = {}
    for k, vals in buckets.items():
        if len(vals) < 3:
            continue
        lo, hi = percentile(vals, 0.05), percentile(vals, 0.95)
        w = [clamp(x, lo, hi) for x in vals]
        stats[k] = {"lo": lo, "hi": hi, "mean": statistics.fmean(w), "std": statistics.pstdev(w), "n": len(vals)}
    return stats


def metric_score(key, v, asset, stats, sectors):
    if v is None:
        return None
    spec = METRICS[key]
    if spec[5] == "abs":
        return _abs_score(key, v)
    t = _transform(key, v, asset, sectors)
    st = stats.get((key, peer_group(asset, key)))
    if t is None or not st:
        return None
    t = clamp(t, st["lo"], st["hi"])
    z = (t - st["mean"]) / st["std"] if st["std"] > 1e-12 else 0.0
    s = phi(clamp(z, -3, 3)) * 100
    return 100 - s if spec[3] == "low" else s


# ------------------------------------------------------------------ régimen de mercado
def regime_from_inputs(tnx, vix, bull, cpi_us, cpi_eu, pmi, source="vivo"):
    def sig_infl(c):
        return clamp((c - 2.5) / 1.5, -1, 1)
    flags = []
    if tnx is not None and tnx >= 4.5:
        flags.append("high_rates")
    if tnx is not None and tnx <= 3.0:
        flags.append("low_rates")
    if vix is not None and vix >= 25:
        flags.append("high_vol")
    if vix is not None and vix <= 14:
        flags.append("low_vol")
    if bull is True:
        flags.append("bull")
    elif bull is False:
        flags.append("bear")
    return {
        "tnx": tnx, "vix": vix, "bull": bull, "cpi_us": cpi_us, "cpi_eu": cpi_eu, "pmi": pmi, "source": source,
        "flags": flags, "flag_labels": [REGIME_LABELS[f] for f in flags],
        "signals": {
            "rates": clamp(((tnx if tnx is not None else 3.75) - 3.75) / 1.25, -1, 1),
            "infl_us": sig_infl(cpi_us), "infl_eu": sig_infl(cpi_eu),
            "cycle": clamp((0.5 if bull else -0.5 if bull is False else 0) + (pmi - 50) / 6, -1, 1),
            "vol": clamp(((vix if vix is not None else 18) - 18) / 10, -1, 1),
        },
    }


SCENARIO_REGIMES = {
    "favorable": dict(tnx=3.0, vix=13.0, bull=True, cpi_us=2.2, cpi_eu=1.9, pmi=53.0),
    "adverso": dict(tnx=5.25, vix=32.0, bull=False, cpi_us=4.0, cpi_eu=3.5, pmi=46.0),
}


def macro_fit(asset, regime, sectors):
    s = sectors.get(asset["sector"]) or {}
    sig = regime["signals"]
    infl = sig["infl_eu"] if asset["currency"] in ("EUR", "CHF", "DKK") else sig["infl_us"]
    total = (s.get("rate", 0) * sig["rates"] + s.get("infl", 0) * infl
             + s.get("cycle", 0) * sig["cycle"] + s.get("defensive", 0) * sig["vol"])
    return round(clamp(50 + 20 * total, 5, 95), 2)


def effective_weights(base, horizon, objective, regime, regime_on=True):
    w = {p: max(0.0, float(base.get(p, 0))) for p in PILLARS}
    applied = []
    layers = [("Horizonte", HORIZON_MULT.get(horizon, {})), ("Objetivo", OBJECTIVE_MULT.get(objective, {}))]
    if regime_on and regime:
        for f in regime["flags"]:
            layers.append((f"Régimen: {REGIME_LABELS[f]}", REGIME_MULT[f]))
    for src, mult in layers:
        for p, m in mult.items():
            if w[p] > 0:
                w[p] *= m
                applied.append({"source": src, "pillar": p, "mult": m})
    tot = sum(w.values()) or 1.0
    return {p: w[p] / tot for p in PILLARS}, applied


# ------------------------------------------------------------------ evaluación de un activo
def _months_since(as_of, today):
    try:
        y, m = (int(x) for x in as_of.split("-")[:2])
        return (today.year - y) * 12 + (today.month - m)
    except (ValueError, AttributeError):
        return 99


def fmt_value(key, v):
    if v is None:
        return "n/d"
    unit = METRICS[key][2]
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "x":
        return f"{v:.1f}x"
    if unit in ("bn",):
        return f"{v:,.0f} bn"
    if unit == "M$":
        return f"{v:,.1f} M$"
    if unit == "a":
        return f"{v:.0f} años"
    if unit == "/9":
        return f"{v:.0f}/9"
    if unit == "% float":
        return f"{v:.1f}%"
    return f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"


def auto_flags(asset, r, today):
    out = []
    fin = asset["sector"] in FINANCIAL_SECTORS
    if not fin and asset_class(asset) == "accion":
        if r.get("debt_ebitda") is not None and r["debt_ebitda"] > 4:
            out.append({"severity": "alta", "penalty": 8, "auto": True,
                        "text": f"Deuda neta/EBITDA de {r['debt_ebitda']:.1f}x (>4x): endeudamiento difícil de sostener."})
        if r.get("interest_cov") is not None and r["interest_cov"] < 2:
            out.append({"severity": "alta", "penalty": 6, "auto": True,
                        "text": f"Cobertura de intereses de {r['interest_cov']:.1f}x (<2x)."})
        if r.get("altman_z") is not None and r["altman_z"] < 1.8:
            out.append({"severity": "alta", "penalty": 8, "auto": True,
                        "text": f"Altman Z-Score {r['altman_z']:.1f} (<1,8): zona de riesgo financiero."})
    if r.get("payout") is not None and r["payout"] > 100:
        out.append({"severity": "media", "penalty": 4, "auto": True,
                    "text": f"Payout del {r['payout']:.0f}%: el dividendo supera el beneficio."})
    if r.get("piotroski") is not None and r["piotroski"] <= 3:
        out.append({"severity": "media", "penalty": 4, "auto": True,
                    "text": f"Piotroski F-Score {r['piotroski']:.0f}/9: deterioro fundamental."})
    if r.get("max_dd_1y") is not None and r["max_dd_1y"] < -50:
        out.append({"severity": "media", "penalty": 4, "auto": True,
                    "text": f"Caída máxima de {r['max_dd_1y']:.0f}% en el último año."})
    if r.get("liquidity_usd_m") is not None and r["liquidity_usd_m"] < 5:
        out.append({"severity": "media", "penalty": 6, "auto": True,
                    "text": f"Liquidez muy baja ({r['liquidity_usd_m']:.1f} M$ diarios): spreads y riesgo de ejecución."})
    if asset_class(asset) == "accion" and r.get("pe") is None and (r.get("roe") or 0) < 0:
        out.append({"severity": "info", "penalty": 0, "auto": True,
                    "text": "Beneficio negativo: el PER no es significativo."})
    months = _months_since(asset.get("as_of"), today)
    if asset_class(asset) != "cripto" and months > 12:
        out.append({"severity": "info", "penalty": 0, "auto": True,
                    "text": f"Fundamentales con {months} meses de antigüedad: actualiza fundamentals.csv."})
    return out


def evaluate(asset, r, stats, sectors, weights, manual_flags, today=None, detail=False):
    """r = métricas brutas (fundamentales + precio + macro_fit). weights = pesos efectivos (suman 1)."""
    today = today or date.today()
    pillars = {}
    metric_cov_num = metric_cov_den = 0.0
    for p in PILLARS:
        num = den = avail_w = app_w = 0.0
        mets = []
        for key, spec in METRICS.items():
            if spec[0] != p or not applicable(asset, key):
                continue
            app_w += spec[4]
            v = r.get(key)
            s = metric_score(key, v, asset, stats, sectors)
            if s is not None:
                num += s * spec[4]
                den += spec[4]
                avail_w += spec[4]
            if detail:
                ref = None
                if spec[5] == "sector":
                    ref = (sectors.get(asset["sector"]) or {}).get(key)
                mets.append({"key": key, "label": spec[1], "value": v, "display": fmt_value(key, v),
                             "score": None if s is None else round(s, 1),
                             "ref": None if ref is None else fmt_value(key, ref), "mode": spec[5],
                             "direction": spec[3]})
        if app_w == 0:
            continue  # pilar no aplicable a este tipo de activo
        score = num / den if den else None
        pillars[p] = {"score": None if score is None else round(score, 1),
                      "coverage": round(avail_w / app_w, 2), "weight": round(weights.get(p, 0), 4)}
        if detail:
            pillars[p]["metrics"] = mets
        if weights.get(p, 0) > 0:
            metric_cov_num += weights[p] * (avail_w / app_w)
            metric_cov_den += weights[p]

    scored = {p: d for p, d in pillars.items() if d["score"] is not None and weights.get(p, 0) > 0}
    wsum = sum(weights[p] for p in scored)
    raw_score = sum(weights[p] * d["score"] for p, d in scored.items()) / wsum if wsum else 50.0
    for p, d in pillars.items():
        d["contribution"] = round(weights.get(p, 0) / wsum * ((d["score"] or 50) - 50), 2) if wsum and p in scored else 0.0

    flags = auto_flags(asset, r, today) + [dict(f, auto=False) for f in manual_flags]
    penalty = min(PENALTY_CAP, sum(f.get("penalty", 0) for f in flags))
    score = clamp(raw_score - penalty, 0, 100)
    label_key = "fav" if score >= FAV_THRESHOLD else ("desf" if score <= DESF_THRESHOLD else "neu")

    # ---- confianza
    reasons, conf = [], 100.0
    weight_cov = wsum  # pesos efectivos suman 1
    if weight_cov < 0.999:
        loss = (1 - weight_cov) * 45
        conf -= loss
        reasons.append(f"{(1 - weight_cov) * 100:.0f}% del peso del perfil recae en pilares sin datos o no aplicables")
    mcov = metric_cov_num / metric_cov_den if metric_cov_den else 0
    if mcov < 0.999:
        conf -= (1 - mcov) * 20
        reasons.append(f"Cobertura de métricas dentro de los pilares: {mcov * 100:.0f}%")
    sc = [d["score"] for d in scored.values()]
    disp = statistics.pstdev(sc) if len(sc) > 1 else 0
    if disp > 18:
        conf -= min(20, disp - 18)
        reasons.append(f"Alta dispersión entre pilares (σ = {disp:.0f} pts): señales contradictorias")
    if r.get("analyst_dispersion") is not None and r["analyst_dispersion"] >= 0.3:
        conf -= r["analyst_dispersion"] * 20
        reasons.append("Discrepancia significativa entre analistas")
    months = _months_since(asset.get("as_of"), today)
    if asset_class(asset) != "cripto" and months > 3:
        conf -= min(20, months - 3)
        reasons.append(f"Fundamentales con {months} meses de antigüedad")
    if r.get("vol_1y") is None:
        conf -= 15
        reasons.append("Sin histórico de precios suficiente")
    conf = clamp(conf, 0, 100)
    conf_label = "Alta" if conf >= 70 else ("Media" if conf >= 45 else "Baja")

    out = {"score": round(score, 1), "raw_score": round(raw_score, 1), "penalty": round(penalty, 1),
           "label_key": label_key, "label": LABELS[label_key], "pillars": pillars, "flags": flags,
           "confidence": {"score": round(conf, 0), "label": conf_label, "reasons": reasons,
                          "pillar_dispersion": round(disp, 1)}}
    if detail:
        out["explanation"] = explain(pillars, score, label_key, penalty)
    return out


def explain(pillars, score, label_key, penalty):
    """Explicación por reglas (sin LLM): pilares que más suman y restan, con sus métricas extremas."""
    ranked = sorted([(p, d) for p, d in pillars.items() if d.get("contribution")], key=lambda x: x[1]["contribution"])
    pos = [x for x in reversed(ranked) if x[1]["contribution"] > 0.3][:2]
    neg = [x for x in ranked if x[1]["contribution"] < -0.3][:2]

    def detail(p, d, best=True):
        mets = [m for m in d.get("metrics", []) if m["score"] is not None]
        mets.sort(key=lambda m: m["score"], reverse=best)
        bits = []
        for m in mets[:2]:
            if (best and m["score"] < 55) or (not best and m["score"] > 45):
                continue
            s = f"{m['label']} {m['display']}"
            if m.get("ref"):
                s += f" (sector {m['ref']})"
            bits.append(s)
        return f"{PILLAR_LABELS[p]}" + (f": {', '.join(bits)}" if bits else "")

    strengths = [detail(p, d, True) for p, d in pos]
    weaknesses = [detail(p, d, False) for p, d in neg]
    parts = []
    if strengths:
        parts.append("Puntúa alto por " + " y ".join(strengths))
    if weaknesses:
        parts.append(("penaliza " if parts else "Penaliza ") + " y ".join(weaknesses))
    if penalty > 0:
        parts.append(f"se restan {penalty:.0f} pts por red flags")
    summary = "; ".join(parts) + "." if parts else "Perfil equilibrado sin factores dominantes."
    return {"summary": summary[0].upper() + summary[1:], "strengths": strengths, "weaknesses": weaknesses}


# ------------------------------------------------------------------ sensibilidad y escenarios
SHOCKS = [
    ("debt_ebitda", lambda v: 3.0 if v < 3 else v + 1.5, "Deuda neta/EBITDA"),
    ("pe", lambda v: v * 1.25, "PER (+25%, por subida de precio o caída de beneficios)"),
    ("eps_cagr", lambda v: v - 10, "Crecimiento del BPA (−10 pp)"),
    ("div_yield", lambda v: 0.0 if v > 0 else None, "Dividendo (supresión)"),
    ("ret_12m_1", lambda v: v - 20, "Rentabilidad 12-1 meses (−20 pp)"),
    ("vol_1y", lambda v: v * 1.5, "Volatilidad (+50%)"),
    ("analyst_rating", lambda v: min(5, v + 1), "Consenso de analistas (empeora 1 nivel)"),
    ("altman_z", lambda v: 1.7 if v >= 1.8 else None, "Altman Z-Score (cae a zona de riesgo)"),
    ("esg_risk", lambda v: v + 10, "Riesgo ESG (+10)"),
]


def sensitivity(asset, r, stats, sectors, weights, manual_flags, base_eval):
    out = []
    for key, fn, label in SHOCKS:
        v = r.get(key)
        if v is None or not applicable(asset, key):
            continue
        nv = fn(v)
        if nv is None:
            continue
        r2 = dict(r)
        r2[key] = nv
        e = evaluate(asset, r2, stats, sectors, weights, manual_flags)
        delta = e["score"] - base_eval["score"]
        if abs(delta) < 0.3:
            continue
        out.append({"key": key, "label": label, "from": fmt_value(key, v), "to": fmt_value(key, nv),
                    "score_from": base_eval["score"], "score_to": e["score"], "delta": round(delta, 1),
                    "label_change": e["label_key"] != base_eval["label_key"], "new_label": e["label"]})
    out.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return out[:5]


def price_shock(r, pct):
    """Aplica un shock de precio proporcional a la beta sobre las métricas de momentum/riesgo."""
    r2 = dict(r)
    beta = r.get("beta") if r.get("beta") is not None else 1.0
    move = pct * max(0.2, beta)
    for k in ("ret_3m", "ret_6m", "px_ma200", "ret_1y"):
        if r2.get(k) is not None:
            r2[k] = r2[k] + move
    if r2.get("vol_1y") is not None and pct < 0:
        r2["vol_1y"] *= 1.4
    if r2.get("max_dd_1y") is not None and pct < 0:
        r2["max_dd_1y"] = min(r2["max_dd_1y"], move)
    return r2
