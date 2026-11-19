/* Bursátil 360° — Frontend multifactor */

const REFRESH_INTERVAL = 30;
const PROFILE_ASSUMPTIONS = {
  conservador: { adv: 1.5, base: 4.5, fav: 7.5 },
  equilibrado: { adv: 2.5, base: 7.0, fav: 10.5 },
  agresivo:    { adv: 3.0, base: 9.0, fav: 13.5 },
};

const state = {
  meta: null,
  scores: null,
  profile: "equilibrado",
  horizon: "medio",
  objective: "crecimiento",
  regimeOn: true,
  customWeights: null,
  selectedSymbol: "MSFT",
  portfolio: new Set(["MSFT", "SAN.MC", "VOO", "GLD", "SGOV"]),
  autoRefresh: true,
  countdown: REFRESH_INTERVAL,
  activeTab: "ranking",
  btLoaded: false,
  page: 1,
  pageSize: 50,
};

const $ = (id) => document.getElementById(id);

function fmtMoney(n) {
  return new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", maximumFractionDigits: 0 }).format(n);
}

function badgeClass(key) {
  if (key === "fav") return "badge-fav";
  if (key === "desf") return "badge-desf";
  return "badge-neu";
}

function shortLabel(key) {
  if (key === "fav") return "Perfil favorable";
  if (key === "desf") return "Perfil desfavorable";
  return "Neutral";
}

function currentParams() {
  const markets = [...document.querySelectorAll("#market-checks input:checked")].map((i) => i.value);
  const types = [...document.querySelectorAll("#type-checks input:checked")].map((i) => i.value);
  const currencies = [...document.querySelectorAll("#currency-checks input:checked")].map((i) => i.value);
  const excludedSectors = [...document.querySelectorAll("#sector-checks input:checked")].map((i) => i.value);

  return {
    profile: state.profile,
    horizon: state.horizon,
    objective: state.objective,
    regime_on: state.regimeOn,
    weights: state.customWeights,
    filters: {
      sp500_only: $("sp500-only").checked,
      markets,
      types,
      currencies,
      excluded_sectors: excludedSectors,
      esg_max: $("esg-max").value || null,
      min_mcap_bn: $("min-mcap").value || null,
      min_confidence: Number($("min-conf").value) || 0,
    },
  };
}

async function apiGet(url) {
  const r = await fetch(url, { cache: "no-store" });
  return r.json();
}

async function apiPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ------------------------------------------------ Init & Sidebar
async function boot() {
  state.meta = await apiGet("/api/meta");
  buildSidebar();
  setCompoundDefaults();
  renderMethodology();
  await refreshScores();
  startTimer();
}

function buildSidebar() {
  const m = state.meta;
  // Markets, types, currencies, sectors
  $("market-checks").innerHTML = m.markets
    .map((x) => `<label class="chip-check"><input type="checkbox" value="${x}" checked /> ${x}</label>`)
    .join("");
  $("type-checks").innerHTML = m.types
    .map((x) => `<label class="chip-check"><input type="checkbox" value="${x}" checked /> ${x}</label>`)
    .join("");
  $("currency-checks").innerHTML = m.currencies
    .map((x) => `<label class="chip-check"><input type="checkbox" value="${x}" checked /> ${x}</label>`)
    .join("");
  $("sector-checks").innerHTML = m.sectors
    .map((x) => `<label><input type="checkbox" value="${x}" /> ${x}</label>`)
    .join("");
  $("sector-filter").innerHTML = `<option value="">Todos los sectores</option>` + m.sectors
    .map((x) => `<option value="${x}">${x}</option>`)
    .join("");

  renderWeightsSliders();

  // Listeners
  ["market-checks", "type-checks", "currency-checks", "sector-checks"].forEach((id) => {
    $(id).addEventListener("change", () => {
      const exc = document.querySelectorAll("#sector-checks input:checked").length;
      $("excluded-count").textContent = exc ? `(${exc} excluidos)` : "";
      refreshScores();
    });
  });

  $("esg-max").addEventListener("input", refreshScores);
  $("min-mcap").addEventListener("input", refreshScores);
  $("min-conf").addEventListener("input", () => {
    $("min-conf-label").textContent = $("min-conf").value;
    refreshScores();
  });

  $("profile-select").addEventListener("change", (e) => {
    state.profile = e.target.value;
    state.customWeights = null;
    renderWeightsSliders();
    setCompoundDefaults();
    refreshScores();
  });
  $("horizon-select").addEventListener("change", (e) => {
    state.horizon = e.target.value;
    refreshScores();
  });
  $("objective-select").addEventListener("change", (e) => {
    state.objective = e.target.value;
    refreshScores();
  });
  $("regime-toggle").addEventListener("change", (e) => {
    state.regimeOn = e.target.checked;
    refreshScores();
  });
  $("reset-weights").addEventListener("click", () => {
    state.customWeights = null;
    renderWeightsSliders();
    refreshScores();
  });
}

function renderWeightsSliders() {
  const m = state.meta;
  const base = state.customWeights || m.profiles[state.profile];
  $("weights-box").innerHTML = m.pillars
    .map((p) => {
      const val = Number(base[p] || 0);
      return `
      <div class="weight-row" data-pillar="${p}">
        <span>${m.pillar_labels[p]}</span>
        <span class="weight-base">${val.toFixed(0)}%</span>
        <span class="weight-eff" id="eff-${p}">--</span>
        <input type="range" min="0" max="40" step="1" value="${val}" data-p="${p}" />
      </div>`;
    })
    .join("");

  $("weights-box").querySelectorAll("input[type='range']").forEach((inp) => {
    inp.addEventListener("input", () => {
      if (!state.customWeights) {
        state.customWeights = { ...m.profiles[state.profile] };
      }
      state.customWeights[inp.dataset.p] = Number(inp.value);
      inp.closest(".weight-row").querySelector(".weight-base").textContent = `${inp.value}%`;
    });
    inp.addEventListener("change", refreshScores);
  });
}

// ------------------------------------------------ Refresh & Status
async function refreshScores() {
  const data = await apiPost("/api/scores", currentParams());
  state.scores = data;
  renderTopbar(data);
  renderEffectiveWeights(data);
  renderRanking(data);
  populateAssetSelect(data.assets);
  if (state.activeTab === "ficha") loadAsset(state.selectedSymbol);
  if (state.activeTab === "cartera") refreshPortfolio();
}

function renderTopbar(data) {
  const st = data.status;
  const histTxt = st.history_ready ? "histórico OK" : `${st.with_history}/${st.assets} hist.`;
  $("status-text").textContent = `${st.with_quote}/${st.assets} en vivo · ${histTxt}`;
  $("last-updated").textContent = data.updated_at.split(" ")[1] || data.updated_at;

  const r = data.regime;
  const chips = [];
  if (r.tnx != null) chips.push({ t: `Bono 10Y: ${r.tnx.toFixed(2)}%`, cls: r.tnx >= 4.5 ? "warn" : "" });
  if (r.vix != null) chips.push({ t: `VIX: ${r.vix.toFixed(1)}`, cls: r.vix >= 25 ? "warn" : "ok" });
  if (r.bull !== null) chips.push({ t: r.bull ? "S&P 500 > MM200 (Alcista)" : "S&P 500 < MM200 (Bajista)", cls: r.bull ? "ok" : "warn" });
  chips.push({ t: `CPI EE.UU.: ${r.cpi_us}% · EU: ${r.cpi_eu}%`, cls: "" });
  $("regime-chips").innerHTML = chips.map((c) => `<span class="regime-chip ${c.cls}">${c.t}</span>`).join("");
}

function renderEffectiveWeights(data) {
  Object.entries(data.weights).forEach(([p, w]) => {
    const el = $(`eff-${p}`);
    if (el) el.textContent = `${(w * 100).toFixed(1)}%`;
  });
  if (data.applied && data.applied.length) {
    const uniq = [...new Set(data.applied.map((a) => a.source))];
    $("applied-box").textContent = `Ajustes activos: ${uniq.join(" · ")}`;
  } else {
    $("applied-box").textContent = "Sin multiplicadores adicionales activos.";
  }
}

// ------------------------------------------------ Ranking Tab
function renderRanking(data) {
  const q = ($("search-box").value || "").trim().toLowerCase();
  const sec = $("sector-filter").value;
  const list = data.assets.filter(
    (a) =>
      (!sec || a.sector === sec) &&
      (!q || a.symbol.toLowerCase().includes(q) || a.name.toLowerCase().includes(q) || a.sector.toLowerCase().includes(q))
  );

  const favN = list.filter((a) => a.label_key === "fav").length;
  const neuN = list.filter((a) => a.label_key === "neu").length;
  const desfN = list.filter((a) => a.label_key === "desf").length;
  const avgConf = list.length ? Math.round(list.reduce((s, a) => s + a.confidence.score, 0) / list.length) : 0;

  $("summary-row").innerHTML = `
    <div class="kpi-card"><span>Activos mostrados</span><strong>${list.length}</strong><small>de ${data.status.assets} en universo</small></div>
    <div class="kpi-card"><span>Perfil favorable (≥60)</span><strong class="up">${favN}</strong><small>según pesos actuales</small></div>
    <div class="kpi-card"><span>Neutral (40–60)</span><strong>${neuN}</strong><small>equilibrio de factores</small></div>
    <div class="kpi-card"><span>Perfil desfavorable (≤40)</span><strong class="down">${desfN}</strong><small>o penalizados</small></div>
    <div class="kpi-card"><span>Confianza media</span><strong>${avgConf}%</strong><small>cobertura y consistencia</small></div>
  `;

  const totalPages = Math.max(1, Math.ceil(list.length / state.pageSize));
  if (state.page > totalPages) state.page = totalPages;
  const startIdx = (state.page - 1) * state.pageSize;
  const pageList = list.slice(startIdx, startIdx + state.pageSize);

  const activePillars = Object.entries(data.weights)
    .filter(([, w]) => w > 0.01)
    .map(([p]) => p);

  $("rank-tbody").innerHTML = pageList
    .map((a, idx) => {
      const rankNum = startIdx + idx + 1;
      const chgCls = (a.change_pct || 0) >= 0 ? "up" : "down";
      const chgTxt = a.change_pct == null ? "--" : `${a.change_pct >= 0 ? "+" : ""}${a.change_pct.toFixed(2)}%`;
      const pxTxt = a.price == null ? "--" : `${a.price.toLocaleString("es-ES", { maximumFractionDigits: 2 })} ${a.currency}`;
      const bars = activePillars
        .map((p) => {
          const s = a.pillars[p];
          if (s == null) return `<div class="mini-bar" style="height:2px;opacity:.25" title="${state.meta.pillar_labels[p]}: n/d"></div>`;
          const col = s >= 60 ? "#10b981" : s <= 40 ? "#ef4444" : "#38bdf8";
          return `<div class="mini-bar" style="height:${Math.max(3, Math.round((s / 100) * 22))}px;background:${col}" title="${state.meta.pillar_labels[p]}: ${s}"></div>`;
        })
        .join("");

      const flagCount = a.flags.filter((f) => f.severity !== "info").length;
      const flagBadge = flagCount ? `<span class="badge badge-desf">${flagCount}</span>` : `<span class="muted">0</span>`;

      return `
      <tr data-sym="${a.symbol}">
        <td onclick="event.stopPropagation()"><input type="checkbox" class="port-chk" data-sym="${a.symbol}" ${state.portfolio.has(a.symbol) ? "checked" : ""} /></td>
        <td class="muted">${rankNum}</td>
        <td class="asset-name"><strong>${a.symbol}</strong><span>${a.name}</span></td>
        <td>${a.market} · ${a.type}</td>
        <td>${a.sector} <span class="muted">(${a.sector_rank}/${a.sector_n})</span></td>
        <td>${pxTxt}</td>
        <td class="${chgCls}">${chgTxt}</td>
        <td><span class="score-pill">${a.score.toFixed(1)}</span>${a.penalty > 0 ? ` <small class="down">(-${a.penalty})</small>` : ""}</td>
        <td><span class="badge ${badgeClass(a.label_key)}">${shortLabel(a.label_key)}</span></td>
        <td>${a.confidence.score}% <small class="muted">(${a.confidence.label})</small></td>
        <td><div class="mini-bars">${bars}</div></td>
        <td>${flagBadge}</td>
      </tr>`;
    })
    .join("");

  $("rank-tbody").querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => {
      state.selectedSymbol = tr.dataset.sym;
      $("asset-select").value = tr.dataset.sym;
      switchTab("ficha");
    });
  });

  $("rank-tbody").querySelectorAll(".port-chk").forEach((chk) => {
    chk.addEventListener("change", () => {
      if (chk.checked) state.portfolio.add(chk.dataset.sym);
      else state.portfolio.delete(chk.dataset.sym);
      renderPortfolioChips();
    });
  });

  $("pagination-bar").innerHTML = `
    <span class="muted">Mostrando ${list.length ? startIdx + 1 : 0}–${Math.min(startIdx + state.pageSize, list.length)} de ${list.length} activos</span>
    <div class="row-gap">
      <label class="inline-field">Filas:
        <select id="page-size-sel">
          <option value="25" ${state.pageSize === 25 ? "selected" : ""}>25</option>
          <option value="50" ${state.pageSize === 50 ? "selected" : ""}>50</option>
          <option value="100" ${state.pageSize === 100 ? "selected" : ""}>100</option>
        </select>
      </label>
      <button class="btn btn-secondary" id="prev-page-btn" ${state.page <= 1 ? "disabled" : ""}>← Anterior</button>
      <span>Página <strong>${state.page}</strong> de <strong>${totalPages}</strong></span>
      <button class="btn btn-secondary" id="next-page-btn" ${state.page >= totalPages ? "disabled" : ""}>Siguiente →</button>
    </div>
  `;
  $("page-size-sel").addEventListener("change", (e) => {
    state.pageSize = Number(e.target.value);
    state.page = 1;
    renderRanking(state.scores);
  });
  $("prev-page-btn").addEventListener("click", () => {
    if (state.page > 1) { state.page -= 1; renderRanking(state.scores); }
  });
  $("next-page-btn").addEventListener("click", () => {
    if (state.page < totalPages) { state.page += 1; renderRanking(state.scores); }
  });
}

function populateAssetSelect(assets) {
  const sel = $("asset-select");
  const cur = state.selectedSymbol;
  sel.innerHTML = assets.map((a) => `<option value="${a.symbol}">${a.symbol} — ${a.name} (${a.score.toFixed(1)})</option>`).join("");
  if (assets.some((a) => a.symbol === cur)) sel.value = cur;
  else if (assets.length) state.selectedSymbol = sel.value = assets[0].symbol;
}

// ------------------------------------------------ Ficha Tab
async function loadAsset(sym) {
  if (!sym) return;
  const body = { ...currentParams(), symbol: sym };
  const d = await apiPost("/api/asset", body);
  if (d.error) {
    $("ficha-content").innerHTML = `<p class="muted pad">${d.error}</p>`;
    return;
  }

  const ev = d.eval;
  const a = d.asset;
  const px = d.price == null ? "--" : `${d.price.toLocaleString("es-ES", { maximumFractionDigits: 2 })} ${a.currency}`;

  const flagsHtml = ev.flags.length
    ? ev.flags.map((f) => `<div class="flag-item flag-${f.severity}"><strong>[${f.severity.toUpperCase()}]</strong> ${f.text} ${f.penalty ? `<strong>(-${f.penalty} pts)</strong>` : ""}</div>`).join("")
    : `<p class="muted">Sin red flags detectados para este activo.</p>`;

  const sensHtml = d.sensitivity.length
    ? `<table><thead><tr><th>Factor</th><th>Cambio</th><th>Impacto</th><th>Nuevo score</th></tr></thead><tbody>` +
      d.sensitivity
        .map(
          (s) => `<tr><td>${s.label}</td><td>${s.from} → ${s.to}</td><td class="${s.delta >= 0 ? "up" : "down"}">${s.delta >= 0 ? "+" : ""}${s.delta} pts</td><td>${s.score_to}${s.label_change ? ` <span class="badge badge-info">Cambia etiqueta</span>` : ""}</td></tr>`
        )
        .join("") +
      `</tbody></table>`
    : `<p class="muted">Sin sensibilidades relevantes.</p>`;

  const peersHtml = `<table><thead><tr><th>Activo</th><th>Relación</th><th>Score</th><th>Rent. 1a</th><th>Vol. 1a</th></tr></thead><tbody>` +
    d.peers
      .map(
        (p) => `<tr ${p.self ? 'style="background:rgba(56,189,248,.08)"' : ""}>
          <td><strong>${p.symbol}</strong> <span class="muted">${p.name}</span></td>
          <td>${p.kind}</td>
          <td>${p.score == null ? "--" : `<strong>${p.score}</strong>`}</td>
          <td class="${(p.ret_1y || 0) >= 0 ? "up" : "down"}">${p.ret_1y == null ? "--" : `${p.ret_1y.toFixed(1)}%`}</td>
          <td>${p.vol_1y == null ? "--" : `${p.vol_1y.toFixed(1)}%`}</td>
        </tr>`
      )
      .join("") +
    `</tbody></table>`;

  const pillarsHtml = Object.entries(ev.pillars)
    .map(([p, info]) => {
      const mets = (info.metrics || [])
        .map(
          (m) => `<tr><td>${m.label}</td><td>${m.display}</td><td>${m.ref || "—"}</td><td>${m.score == null ? "n/d" : m.score}</td></tr>`
        )
        .join("");
      return `<details class="pillar-block">
        <summary>
          <span>${state.meta.pillar_labels[p]} <small class="muted">(peso ${(info.weight * 100).toFixed(1)}%, cob. ${(info.coverage * 100).toFixed(0)}%)</small></span>
          <span>${info.score == null ? "n/d" : `${info.score} / 100`} <small class="${info.contribution >= 0 ? "up" : "down"}">(${info.contribution >= 0 ? "+" : ""}${info.contribution})</small></span>
        </summary>
        <table><thead><tr><th>Métrica</th><th>Valor</th><th>Ref. sector</th><th>Score</th></tr></thead><tbody>${mets}</tbody></table>
      </details>`;
    })
    .join("");

  $("ficha-content").innerHTML = `
    <div class="ficha-hero">
      <div>
        <h2>${a.symbol} — ${a.name}</h2>
        <p class="muted">${a.market} · ${a.type} · ${a.sector} (puesto ${d.sector_rank} de ${d.sector_n}) · Fundamentales: ${a.as_of}</p>
        <p style="margin-top:6px">Precio actual: <strong>${px}</strong></p>
      </div>
      <div class="hero-score">
        <div class="big">${ev.score.toFixed(1)} <span class="muted" style="font-size:1rem">/ 100</span></div>
        <span class="badge ${badgeClass(ev.label_key)}">${ev.label}</span>
        <div class="muted" style="font-size:.76rem;margin-top:4px">Confianza: <strong>${ev.confidence.score}% (${ev.confidence.label})</strong></div>
      </div>
    </div>

    <div class="ficha-grid">
      <div class="card">
        <h3>Por qué puntúa así</h3>
        <div class="explain-box">${ev.explanation.summary}</div>
        <h4 style="margin-bottom:6px;font-size:.85rem">Riesgos y red flags</h4>
        ${flagsHtml}
        ${ev.confidence.reasons.length ? `<p class="muted" style="font-size:.76rem;margin-top:8px">Motivos de confianza: ${ev.confidence.reasons.join("; ")}.</p>` : ""}
      </div>
      <div class="card">
        <h3>Gráfico radar de pilares</h3>
        <canvas id="radar-canvas" height="300"></canvas>
      </div>
    </div>

    <div class="ficha-grid">
      <div class="card">
        <h3>Escenarios (régimen macro + shock de mercado)</h3>
        <div class="scen-grid" style="margin-top:10px">
          ${["adverso", "base", "favorable"]
            .map((k) => {
              const sc = d.scenarios[k];
              return `<div class="scen-card">
                <h4>${k.toUpperCase()}</h4>
                <strong style="font-size:1.3rem">${sc.score}</strong>
                <div><span class="badge ${badgeClass(sc.label_key)}">${shortLabel(sc.label_key)}</span></div>
                <small class="muted">${(sc.regime || []).join(", ") || "Estable"}</small>
              </div>`;
            })
            .join("")}
        </div>
        ${d.price_range_12m ? `<p class="muted" style="font-size:.78rem;margin-top:10px">Rango estadístico 12m (vol. ${d.price_range_12m.vol}%): adverso ${d.price_range_12m.adverso} · base ${d.price_range_12m.base} · favorable ${d.price_range_12m.favorable} ${a.currency} (no es una previsión).</p>` : ""}
        <h3 style="margin-top:14px">Qué cambiaría la valoración</h3>
        ${sensHtml}
      </div>
      <div class="card">
        <h3>Precio 1 año y comparables</h3>
        <canvas id="spark-canvas" height="110"></canvas>
        <h4 style="margin:12px 0 6px;font-size:.85rem">Comparación con sector y alternativas</h4>
        ${peersHtml}
      </div>
    </div>

    <div class="card">
      <h3>Desglose completo de pilares y métricas</h3>
      ${pillarsHtml}
    </div>
  `;

  drawRadar($("radar-canvas"), ev.pillars, d.radar_ref, state.meta.pillar_labels, d.radar_ref_label);
  if (d.spark) drawSpark($("spark-canvas"), d.spark, d.spark_dates);
}

// ------------------------------------------------ Cartera e Interés Compuesto
function renderPortfolioChips() {
  $("portfolio-chips").innerHTML = [...state.portfolio]
    .map((s) => `<span class="chip-rem">${s} <button data-rem="${s}">×</button></span>`)
    .join("");
  $("portfolio-chips").querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      state.portfolio.delete(b.dataset.rem);
      renderPortfolioChips();
      if (state.scores) renderRanking(state.scores);
      refreshPortfolio();
    });
  });
}

async function refreshPortfolio() {
  renderPortfolioChips();
  const syms = [...state.portfolio];
  if (syms.length < 2) {
    $("portfolio-content").innerHTML = `<p class="muted pad">Selecciona al menos 2 activos para calcular correlaciones y paridad de riesgo.</p>`;
    return;
  }
  const res = await apiPost("/api/portfolio", { symbols: syms, max_weight: Number($("max-weight").value) });
  if (res.error) {
    $("portfolio-content").innerHTML = `<p class="muted pad">${res.error}</p>`;
    return;
  }

  const eq = res.methods.equiponderada;
  const rp = res.methods.paridad_riesgo;
  const weightsRows = res.symbols
    .map(
      (s, i) => `<tr>
        <td><strong>${s}</strong> <span class="muted">${res.names[i]}</span></td>
        <td>${res.vol[s]}%</td>
        <td class="${res.ret_1y[s] >= 0 ? "up" : "down"}">${res.ret_1y[s]}%</td>
        <td>${(eq.weights[s] * 100).toFixed(1)}%</td>
        <td><strong>${(rp.weights[s] * 100).toFixed(1)}%</strong></td>
      </tr>`
    )
    .join("");

  const corrHead = `<tr><th></th>${res.symbols.map((s) => `<th>${s}</th>`).join("")}</tr>`;
  const corrBody = res.corr
    .map(
      (row, i) => `<tr><th>${res.symbols[i]}</th>${row
        .map((v) => {
          const bg = v > 0.6 && i !== res.corr[i].indexOf(v) ? "rgba(239,68,68,.2)" : v < 0.2 ? "rgba(16,185,129,.18)" : "transparent";
          return `<td style="background:${bg}">${v.toFixed(2)}</td>`;
        })
        .join("")}</tr>`
    )
    .join("");

  $("portfolio-content").innerHTML = `
    <div class="kpi-row" style="margin-bottom:14px">
      <div class="kpi-card"><span>Equiponderada · Volatilidad</span><strong>${eq.vol}%</strong><small>Max DD: ${eq.max_dd}% · Div. ratio: ${eq.div_ratio}</small></div>
      <div class="kpi-card"><span>Equiponderada · Rent. 1a</span><strong>${eq.ret_hist}%</strong><small>VaR 95% anual: ${eq.var95_1y}%</small></div>
      <div class="kpi-card"><span>Paridad de riesgo · Volatilidad</span><strong class="up">${rp.vol}%</strong><small>Max DD: ${rp.max_dd}% · Div. ratio: ${rp.div_ratio}</small></div>
      <div class="kpi-card"><span>Paridad de riesgo · Rent. 1a</span><strong>${rp.ret_hist}%</strong><small>Ventana: ${res.window.from} → ${res.window.to}</small></div>
    </div>
    <div class="ficha-grid">
      <div class="table-wrap">
        <table><thead><tr><th>Activo</th><th>Vol. 1a</th><th>Rent. 1a</th><th>Equipond.</th><th>Paridad riesgo</th></tr></thead><tbody>${weightsRows}</tbody></table>
      </div>
      <div class="table-wrap">
        <table class="corr-table"><thead>${corrHead}</thead><tbody>${corrBody}</tbody></table>
      </div>
    </div>
  `;
}

function setCompoundDefaults() {
  const d = PROFILE_ASSUMPTIONS[state.profile] || PROFILE_ASSUMPTIONS.equilibrado;
  $("ci-adv").value = d.adv;
  $("ci-base").value = d.base;
  $("ci-fav").value = d.fav;
  updateCompound();
}

function simulateCompound(initial, monthly, years, annualPct, inflPct, feePct) {
  const realNet = ((1 + (annualPct - feePct) / 100) / (1 + inflPct / 100)) - 1;
  const mr = Math.pow(1 + realNet, 1 / 12) - 1;
  let bal = initial;
  const arr = [bal];
  for (let y = 1; y <= years; y++) {
    for (let m = 0; m < 12; m++) bal = (bal + monthly) * (1 + mr);
    arr.push(bal);
  }
  return arr;
}

function updateCompound() {
  const initial = Math.max(0, Number($("ci-initial").value) || 0);
  const monthly = Math.max(0, Number($("ci-monthly").value) || 0);
  const years = Math.max(1, Number($("ci-years").value) || 15);
  $("ci-years-label").textContent = years;

  const adv = Number($("ci-adv").value) || 0;
  const base = Number($("ci-base").value) || 0;
  const fav = Number($("ci-fav").value) || 0;
  const infl = Number($("ci-infl").value) || 0;
  const fee = Number($("ci-fee").value) || 0;

  const sAdv = simulateCompound(initial, monthly, years, adv, infl, fee);
  const sBase = simulateCompound(initial, monthly, years, base, infl, fee);
  const sFav = simulateCompound(initial, monthly, years, fav, infl, fee);
  const sContrib = Array.from({ length: years + 1 }, (_, y) => initial + monthly * 12 * y);

  $("ci-kpis").innerHTML = `
    <div class="kpi-card"><span>Capital aportado</span><strong>${fmtMoney(sContrib[years])}</strong><small>sin rendimiento</small></div>
    <div class="kpi-card"><span>Escenario adverso (real neto)</span><strong>${fmtMoney(sAdv[years])}</strong><small>${adv}% bruto anual</small></div>
    <div class="kpi-card"><span>Escenario base (real neto)</span><strong class="up">${fmtMoney(sBase[years])}</strong><small>${base}% bruto anual</small></div>
    <div class="kpi-card"><span>Escenario favorable (real neto)</span><strong class="up">${fmtMoney(sFav[years])}</strong><small>${fav}% bruto anual</small></div>
  `;

  const xLabels = Array.from({ length: years + 1 }, (_, y) => `A${y}`);
  drawLines(
    $("ci-chart"),
    xLabels,
    [
      { label: "Favorable", values: sFav, color: "#10b981", width: 2 },
      { label: "Base", values: sBase, color: "#38bdf8", width: 2.5 },
      { label: "Adverso", values: sAdv, color: "#f59e0b", width: 2 },
      { label: "Aportado", values: sContrib, color: "#94a3b8", width: 1.5, dash: [5, 4] },
    ],
    { minZero: true, fmt: (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : `${Math.round(v / 1000)}k`) }
  );
}

// ------------------------------------------------ Backtest Tab
async function loadBacktest() {
  if (state.btLoaded) return;
  const b = await apiGet("/api/backtest");
  if (!b.ok) {
    $("backtest-content").innerHTML = `<div class="card"><p class="muted">${b.error}</p></div>`;
    return;
  }
  state.btLoaded = true;
  $("backtest-content").innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <h2>Validación walk-forward (fuera de muestra)</h2>
          <p class="card-desc">Solo evalúa factores de precio (momentum + baja volatilidad) en ${b.period.universe_size} acciones (${b.period.from} → ${b.period.to}, ${b.period.months_oos} meses OOS) para evitar look-ahead bias. Incluye ${b.cost_bps_per_side} bps por lado y ${Math.round(b.tax_rate * 100)}% fiscal.</p>
        </div>
      </div>
      <div class="kpi-row" style="margin-bottom:14px">
        <div class="kpi-card"><span>IC medio (Spearman OOS)</span><strong>${b.ic.mean}</strong><small>t-stat: ${b.ic.t_stat} · hit rate: ${b.ic.hit_rate}%</small></div>
        <div class="kpi-card"><span>Top quintil neto (Q5)</span><strong class="up">${b.top_net.cagr}% / año</strong><small>Sharpe ${b.top_net.sharpe} · Max DD ${b.top_net.max_dd}%</small></div>
        <div class="kpi-card"><span>Universo equiponderado</span><strong>${b.universe.cagr}% / año</strong><small>Sharpe ${b.universe.sharpe} · Max DD ${b.universe.max_dd}%</small></div>
        <div class="kpi-card"><span>Bottom quintil neto (Q1)</span><strong>${b.bottom_net.cagr}% / año</strong><small>Turnover Q5: ${b.avg_turnover}%/mes</small></div>
        <div class="kpi-card"><span>Total Q5 neto tras impuestos</span><strong>${b.after_tax_total_top}%</strong><small>bruto ${b.top_gross.total}% vs neto ${b.top_net.total}%</small></div>
      </div>
      <div class="ficha-grid">
        <div class="chart-box"><h4 style="margin-bottom:6px;font-size:.84rem">Curvas de capital acumuladas (base = 1.0)</h4><canvas id="bt-curves" height="250"></canvas></div>
        <div class="chart-box"><h4 style="margin-bottom:6px;font-size:.84rem">Rentabilidad anualizada por quintil</h4><canvas id="bt-quintiles" height="250"></canvas></div>
      </div>
      <p class="muted" style="font-size:.78rem;margin-top:10px">Nota sobre sobreajuste: el mejor peso in-sample (a posteriori) dio IC ${b.ic.in_sample_ic}, frente a ${b.ic.oos_ic} fuera de muestra (walk-forward).</p>
    </div>
  `;

  drawLines(
    $("bt-curves"),
    b.curves.dates,
    [
      { label: "Top quintil (Q5 neto)", values: b.curves.top, color: "#10b981", width: 2.2 },
      { label: "Universo", values: b.curves.universe, color: "#38bdf8", width: 2 },
      { label: "Bottom quintil (Q1 neto)", values: b.curves.bottom, color: "#ef4444", width: 1.8 },
    ],
    { fmt: (v) => v.toFixed(2) }
  );
  drawBars($("bt-quintiles"), ["Q1 (peor)", "Q2", "Q3", "Q4", "Q5 (mejor)"], b.quintiles_annualized);
}

// ------------------------------------------------ Metodología Tab
function renderMethodology() {
  $("method-content").innerHTML = `
    <div class="method-grid">
      <div class="card">
        <h3>1. Cálculo determinista y auditable</h3>
        <ul>
          <li><strong>Limpieza:</strong> winsorización en percentiles p5 y p95 por grupo de pares; barras de precio incompletas descartadas.</li>
          <li><strong>Normalización relativa:</strong> valoración y márgenes frente a la mediana del sector; resto por z-score en su grupo de pares (acciones vs vehículos) convertido a escala 0-100.</li>
          <li><strong>Ajuste por régimen:</strong> multiplicadores automáticos según bono 10Y, VIX, tendencia S&amp;P 500 e inflación.</li>
          <li><strong>Penalizaciones:</strong> red flags automáticos (deuda/EBITDA &gt; 4x, Altman Z &lt; 1,8, payout &gt; 100%, baja liquidez) y manuales, con tope de -25 pts.</li>
          <li><strong>Sin LLM en el score:</strong> las reglas numéricas son 100% deterministas y reproducibles.</li>
        </ul>
      </div>
      <div class="card">
        <h3>2. Nivel de confianza y etiquetas</h3>
        <ul>
          <li>La confianza penaliza pilares sin datos, alta dispersión entre pilares, discrepancia entre analistas y antigüedad de fundamentales (&gt;3 meses).</li>
          <li><strong>Etiquetas neutrales:</strong> <em>Perfil favorable según el modelo</em> (≥60), <em>Neutral</em> (40–60), <em>Perfil desfavorable</em> (≤40). No constituyen asesoramiento de compra o venta (MiFID II).</li>
        </ul>
      </div>
      <div class="card">
        <h3>3. Limitaciones que debes tener presentes</h3>
        <ul>
          <li>Los modelos describen el pasado y relaciones históricas; no predicen el futuro.</li>
          <li>Eventos imprevisibles (fraudes, guerras, cambios regulatorios abruptos) no están en las cifras.</li>
          <li>Los fundamentales locales en <code>data/fundamentals.csv</code> son una foto manual y requieren actualización periódica.</li>
          <li>El backtest cubre solo factores de precio para evitar <em>look-ahead bias</em>, y sufre <em>survivorship bias</em> al usar el universo actual.</li>
          <li>No contempla tu situación fiscal, deudas ni necesidades de liquidez personales.</li>
        </ul>
      </div>
    </div>
  `;
}

// ------------------------------------------------ Navigation & Timer
function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${tab}`));
  if (tab === "ficha") loadAsset(state.selectedSymbol);
  if (tab === "cartera") { refreshPortfolio(); updateCompound(); }
  if (tab === "backtest") loadBacktest();
}

function startTimer() {
  setInterval(() => {
    if (!state.autoRefresh) return;
    state.countdown -= 1;
    if (state.countdown <= 0) {
      state.countdown = REFRESH_INTERVAL;
      refreshScores();
    }
    $("countdown-text").textContent = `${state.countdown}s`;
    $("countdown-bar").style.width = `${(state.countdown / REFRESH_INTERVAL) * 100}%`;
  }, 1000);
}

// Event bindings
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (btn) switchTab(btn.dataset.tab);
});
document.querySelector("[data-goto='metodologia']").addEventListener("click", (e) => {
  e.preventDefault();
  switchTab("metodologia");
});
$("toggle-auto-btn").addEventListener("click", () => {
  state.autoRefresh = !state.autoRefresh;
  $("toggle-auto-btn").textContent = state.autoRefresh ? "Pausar" : "Reanudar";
  if (!state.autoRefresh) $("countdown-text").textContent = "Pausado";
});
$("refresh-now-btn").addEventListener("click", () => {
  state.countdown = REFRESH_INTERVAL;
  $("countdown-bar").style.width = "100%";
  refreshScores();
});
$("search-box").addEventListener("input", () => { state.page = 1; if (state.scores) renderRanking(state.scores); });
$("sector-filter").addEventListener("change", () => { state.page = 1; if (state.scores) renderRanking(state.scores); });
$("sp500-only").addEventListener("change", () => { state.page = 1; refreshScores(); });
$("asset-select").addEventListener("change", (e) => {
  state.selectedSymbol = e.target.value;
  loadAsset(state.selectedSymbol);
});
$("add-to-portfolio-btn").addEventListener("click", () => {
  state.portfolio.add(state.selectedSymbol);
  renderPortfolioChips();
  if (state.scores) renderRanking(state.scores);
});
$("add-top-btn").addEventListener("click", () => {
  if (!state.scores) return;
  state.scores.assets.filter((a) => a.label_key === "fav").slice(0, 8).forEach((a) => state.portfolio.add(a.symbol));
  if (state.scores) renderRanking(state.scores);
  refreshPortfolio();
});
$("clear-portfolio-btn").addEventListener("click", () => {
  state.portfolio.clear();
  if (state.scores) renderRanking(state.scores);
  refreshPortfolio();
});
$("max-weight").addEventListener("change", refreshPortfolio);

["ci-initial", "ci-monthly", "ci-years", "ci-adv", "ci-base", "ci-fav", "ci-infl", "ci-fee"].forEach((id) => {
  $(id).addEventListener("input", updateCompound);
});

boot();
