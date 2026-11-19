# Bursátil 360°

Sistema de **scoring y ranking multifactor determinista, transparente y explicable** para uso personal, con actualización de cotizaciones cada 30 segundos.

## Características
- **Universo multi-mercado y multi-activo:** IBEX 35, EE. UU. y global (acciones, ETFs, fondos y criptoactivos).
- **11 pilares configurables:** Valoración, Calidad, Crecimiento, Salud financiera, Dividendos, Momentum/técnico, Riesgo de mercado, Sentimiento, Macro y sector, Cualitativo y ESG.
- **Normalización y limpieza:** Winsorización (`p5`/`p95`), comparación relativa por grupo de pares y mediana sectorial, ajuste dinámico por régimen de mercado y penalizaciones por *red flags*.
- **Explicabilidad:** Gráfico radar, fortalezas/debilidades, escenarios (adverso, base, favorable), análisis de sensibilidad, comparables y nivel de confianza.
- **Cartera e interés compuesto:** Matriz de correlaciones, paridad de riesgo y proyector de interés compuesto por escenarios.
- **Validación *walk-forward*:** Backtest fuera de muestra sobre factores de precio (IC de Spearman, quintiles, Sharpe, drawdown, *turnover*, costes e impuestos).

## Ejecución local
Requiere Python 3.10+ (solo biblioteca estándar, sin dependencias externas):

```bash
python server.py
```

Luego abre `http://127.0.0.1:8050` en tu navegador.
