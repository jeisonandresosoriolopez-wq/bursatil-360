/* Gráficos en Canvas puro (sin dependencias externas) */

function _prepCanvas(canvas, cssHeight) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(260, Math.round(rect.width || canvas.width || 480));
  const h = cssHeight || canvas.height || 260;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function drawRadar(canvas, pillars, refPillars, labels, refLabel) {
  const keys = Object.keys(pillars).filter((k) => pillars[k] && pillars[k].score !== null);
  const { ctx, w, h } = _prepCanvas(canvas, 300);
  if (keys.length < 3) {
    ctx.fillStyle = "#94a3b8";
    ctx.font = "12px Inter, sans-serif";
    ctx.fillText("Pocos pilares aplicables para gráfico radar.", 20, h / 2);
    return;
  }

  const cx = w / 2;
  const cy = h / 2 + 8;
  const r = Math.min(w, h) * 0.28;
  const n = keys.length;
  const angle = (i) => -Math.PI / 2 + (i * 2 * Math.PI) / n;

  // Rings
  ctx.strokeStyle = "rgba(148, 163, 184, 0.18)";
  ctx.lineWidth = 1;
  [25, 50, 75, 100].forEach((lvl) => {
    ctx.beginPath();
    keys.forEach((_, i) => {
      const rr = (r * lvl) / 100;
      const x = cx + rr * Math.cos(angle(i));
      const y = cy + rr * Math.sin(angle(i));
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.stroke();
  });

  // Spokes + labels
  ctx.font = "11px Inter, sans-serif";
  ctx.fillStyle = "#94a3b8";
  keys.forEach((k, i) => {
    const a = angle(i);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + r * Math.cos(a), cy + r * Math.sin(a));
    ctx.stroke();

    const lx = cx + (r + 16) * Math.cos(a);
    const ly = cy + (r + 14) * Math.sin(a);
    ctx.textAlign = Math.cos(a) > 0.2 ? "left" : Math.cos(a) < -0.2 ? "right" : "center";
    const text = (labels[k] || k).split(" ")[0];
    ctx.fillText(text, lx, ly + 4);
  });
  ctx.textAlign = "left";

  // Reference polygon
  if (refPillars) {
    ctx.beginPath();
    let started = false;
    keys.forEach((k, i) => {
      const val = refPillars[k] ?? 50;
      const rr = (r * val) / 100;
      const x = cx + rr * Math.cos(angle(i));
      const y = cy + rr * Math.sin(angle(i));
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.strokeStyle = "rgba(148, 163, 184, 0.7)";
    ctx.setLineDash([4, 3]);
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Asset polygon
  ctx.beginPath();
  keys.forEach((k, i) => {
    const val = pillars[k].score;
    const rr = (r * val) / 100;
    const x = cx + rr * Math.cos(angle(i));
    const y = cy + rr * Math.sin(angle(i));
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.closePath();
  ctx.fillStyle = "rgba(56, 189, 248, 0.24)";
  ctx.fill();
  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 2;
  ctx.stroke();

  // Legend
  ctx.font = "11px Inter, sans-serif";
  ctx.fillStyle = "#38bdf8";
  ctx.fillRect(10, 10, 10, 10);
  ctx.fillStyle = "#f1f5f9";
  ctx.fillText("Activo", 25, 19);
  if (refPillars) {
    ctx.fillStyle = "#94a3b8";
    ctx.fillRect(85, 10, 10, 10);
    ctx.fillText(refLabel || "Referencia", 100, 19);
  }
}

function drawSpark(canvas, values, dates) {
  const { ctx, w, h } = _prepCanvas(canvas, 110);
  if (!values || values.length < 2) return;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 8;

  const xFor = (i) => pad + (i / (values.length - 1)) * (w - pad * 2);
  const yFor = (v) => pad + (1 - (v - min) / span) * (h - pad * 2 - 14);

  const up = values[values.length - 1] >= values[0];
  const color = up ? "#10b981" : "#ef4444";

  ctx.beginPath();
  values.forEach((v, i) => {
    if (i === 0) ctx.moveTo(xFor(i), yFor(v));
    else ctx.lineTo(xFor(i), yFor(v));
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "#94a3b8";
  ctx.font = "10px Inter, sans-serif";
  if (dates && dates.length) {
    ctx.fillText(dates[0], pad, h - 2);
    ctx.textAlign = "right";
    ctx.fillText(dates[dates.length - 1], w - pad, h - 2);
    ctx.textAlign = "left";
  }
}

function drawLines(canvas, xLabels, seriesList, opts = {}) {
  const { ctx, w, h } = _prepCanvas(canvas, opts.height || 260);
  const pad = { top: 28, right: 38, bottom: 28, left: 64 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const allVals = seriesList.flatMap((s) => s.values);
  let minV = opts.minZero ? 0 : Math.min(...allVals);
  let maxV = Math.max(...allVals);
  if (maxV - minV < 1e-6) maxV = minV + 1;

  const n = Math.max(1, xLabels.length - 1);
  const xFor = (i) => pad.left + (i / n) * plotW;
  const yFor = (v) => pad.top + (1 - (v - minV) / (maxV - minV)) * plotH;

  // Grid
  ctx.strokeStyle = "rgba(148, 163, 184, 0.14)";
  ctx.lineWidth = 1;
  ctx.font = "11px Inter, sans-serif";
  ctx.fillStyle = "#94a3b8";
  for (let i = 0; i <= 4; i++) {
    const val = minV + ((maxV - minV) * i) / 4;
    const y = yFor(val);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();
    const lbl = opts.fmt ? opts.fmt(val) : val.toFixed(1);
    ctx.fillText(lbl, 6, y + 4);
  }

  // Lines
  seriesList.forEach((s) => {
    ctx.beginPath();
    s.values.forEach((v, i) => {
      if (i === 0) ctx.moveTo(xFor(i), yFor(v));
      else ctx.lineTo(xFor(i), yFor(v));
    });
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width || 2;
    if (s.dash) ctx.setLineDash(s.dash);
    else ctx.setLineDash([]);
    ctx.stroke();
  });
  ctx.setLineDash([]);

  // X labels
  const step = Math.max(1, Math.ceil(xLabels.length / 6));
  ctx.fillStyle = "#94a3b8";
  for (let i = 0; i < xLabels.length; i += step) {
    ctx.fillText(xLabels[i], xFor(i) - 14, h - 7);
  }

  // Legend
  let lx = pad.left;
  seriesList.forEach((s) => {
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, 7, 10, 10);
    ctx.fillStyle = "#f1f5f9";
    ctx.fillText(s.label, lx + 14, 16);
    lx += ctx.measureText(s.label).width + 32;
  });
}

function drawBars(canvas, labels, values) {
  const { ctx, w, h } = _prepCanvas(canvas, 220);
  const pad = { top: 22, right: 16, bottom: 28, left: 48 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const minV = Math.min(0, ...values);
  const maxV = Math.max(0, ...values, 1);
  const span = maxV - minV || 1;
  const y0 = pad.top + (maxV / span) * plotH;

  ctx.strokeStyle = "rgba(148, 163, 184, 0.25)";
  ctx.beginPath();
  ctx.moveTo(pad.left, y0);
  ctx.lineTo(w - pad.right, y0);
  ctx.stroke();

  const bw = (plotW / values.length) * 0.56;
  const gap = plotW / values.length;
  ctx.font = "11px Inter, sans-serif";

  values.forEach((v, i) => {
    const x = pad.left + i * gap + (gap - bw) / 2;
    const y = pad.top + ((maxV - v) / span) * plotH;
    const bh = Math.abs(y - y0);
    ctx.fillStyle = v >= 0 ? "#10b981" : "#ef4444";
    ctx.fillRect(x, Math.min(y, y0), bw, Math.max(2, bh));

    ctx.fillStyle = "#f1f5f9";
    ctx.textAlign = "center";
    ctx.fillText(`${v.toFixed(1)}%`, x + bw / 2, (v >= 0 ? y - 5 : y + 13));
    ctx.fillStyle = "#94a3b8";
    ctx.fillText(labels[i], x + bw / 2, h - 8);
  });
  ctx.textAlign = "left";
}
