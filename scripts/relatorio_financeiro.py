"""Atualização e renderização do relatório financeiro Meta Ads."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELATORIO_CONFIG = ROOT / "config" / "relatorio_financeiro.json"
RELATORIO_DATA = ROOT / "data" / "relatorio_financeiro.json"
RELATORIO_HTML = ROOT / "relatoriofinaceiro" / "index.html"

IG_SVG = '<svg viewBox="0 0 24 24"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/></svg>'


def _handle(raw: str) -> str:
    return raw.lstrip("@").strip()


def _fmt_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_num(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _fmt_cost(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def compute_relatorio(config: dict, fetch_followers) -> dict:
    paginas_out = []
    total_investimento = 0.0
    total_seguidores = 0

    paginas_cfg = config["paginas"]
    extra = float(config.get("investimento_extra", 0))
    raw_total = sum(float(p["investimento"]) for p in paginas_cfg)

    for p in paginas_cfg:
        handle = p.get("instagram_handle") or _handle(p.get("instagram", ""))
        investimento_relatorio = float(p["investimento"])
        share = investimento_relatorio / raw_total if raw_total > 0 else 0
        investimento_base = investimento_relatorio + extra * share
        seguidores_relatorio = int(p.get("seguidores_relatorio", p.get("seguidores", 0)))

        live = fetch_followers(handle, p["nome"])
        seguidores_live = live["seguidores"]
        seguidores_atuais = seguidores_live if seguidores_live is not None else seguidores_relatorio

        # Investimento sobe proporcionalmente aos seguidores; nunca reduz abaixo da base
        fator_bruto = seguidores_atuais / seguidores_relatorio if seguidores_relatorio > 0 else 1.0
        fator = max(fator_bruto, 1.0)
        investimento = investimento_base * fator
        custo = investimento / seguidores_atuais if seguidores_atuais > 0 else 0.0

        paginas_out.append({
            "nome": p["nome"],
            "instagram": f"@{handle}",
            "instagram_handle": handle,
            "investimento": round(investimento, 2),
            "investimento_fmt": _fmt_brl(investimento),
            "seguidores_atuais": seguidores_atuais,
            "seguidores_fmt": _fmt_num(seguidores_atuais),
            "resultado": seguidores_atuais,
            "custo_resultado": round(custo, 2),
            "custo_fmt": _fmt_cost(custo),
            "segmentacao": p.get("segmentacao", ""),
            "source": live.get("source", "relatorio"),
        })

        total_investimento += investimento
        total_seguidores += seguidores_atuais

    custo_medio = total_investimento / total_seguidores if total_seguidores > 0 else 0.0

    sorted_by_cost = sorted(paginas_out, key=lambda x: x["custo_resultado"])
    menores = [
        {"nome": x["nome"], "custo": x["custo_resultado"], "seguidores": x["seguidores_atuais"]}
        for x in sorted_by_cost[:5]
    ]
    maiores = [
        {"nome": x["nome"], "custo": x["custo_resultado"], "seguidores": x["seguidores_atuais"]}
        for x in sorted_by_cost[-5:][::-1]
    ]

    return {
        "titulo": config.get("titulo", "Relatório Financeiro Meta Ads"),
        "resumo": {
            "investimento_total": round(total_investimento, 2),
            "investimento_fmt": _fmt_brl(total_investimento),
            "paginas": len(paginas_out),
            "seguidores_atuais": total_seguidores,
            "seguidores_fmt": _fmt_num(total_seguidores),
            "custo_medio_seguidor": round(custo_medio, 2),
            "custo_medio_fmt": _fmt_cost(custo_medio),
            "resultado_total": total_seguidores,
            "custo_por_resultado": round(custo_medio, 2),
            "custo_resultado_fmt": _fmt_cost(custo_medio),
        },
        "paginas": paginas_out,
        "menores_custos": menores,
        "maiores_custos": maiores,
    }


def render_relatorio_html(data: dict, updated_at: str, model: str) -> str:
    data_json = json.dumps(data, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Clauth Hub — Relatório Financeiro Meta Ads</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --ig-gradient: linear-gradient(45deg, #f09433 0%, #e6683c 25%, #dc2743 50%, #cc2366 75%, #bc1888 100%);
      --fb: #1877F2; --bg: #000; --surface: #fff; --text: #262626; --muted: #8e8e8e; --border: #efefef;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: #fff; min-height: 100dvh; }}
    a {{ color: #a5b4fc; text-decoration: none; }}
    .ig-header {{
      position: sticky; top: 0; z-index: 100; background: rgba(0,0,0,0.9);
      backdrop-filter: blur(12px); border-bottom: 1px solid rgba(255,255,255,0.08); padding: 0.85rem 1.25rem 1rem;
    }}
    .header-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }}
    .brand {{ display: flex; align-items: center; gap: 0.5rem; }}
    .brand-icon {{ width: 28px; height: 28px; border-radius: 8px; background: var(--ig-gradient); display: flex; align-items: center; justify-content: center; }}
    .brand-icon svg {{ width: 16px; height: 16px; fill: #fff; }}
    .brand-name {{ font-size: 1.1rem; font-weight: 700; background: var(--ig-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    .doc-badge {{ font-size: 0.65rem; font-weight: 600; text-transform: uppercase; padding: 0.25rem 0.55rem; border-radius: 20px; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); color: rgba(255,255,255,0.7); }}
    .header-title {{ font-size: 1.25rem; font-weight: 700; }}
    .header-meta {{ font-size: 0.78rem; color: rgba(255,255,255,0.5); margin-top: 0.35rem; line-height: 1.5; }}
    .header-meta strong {{ color: rgba(255,255,255,0.8); }}
    .update-pill {{
      display: inline-flex; align-items: center; gap: 0.35rem; margin-top: 0.55rem;
      padding: 0.3rem 0.7rem; border-radius: 20px; background: rgba(34,197,94,0.15);
      border: 1px solid rgba(34,197,94,0.3); font-size: 0.72rem; color: #4ade80;
    }}
    .update-pill::before {{ content: ''; width: 6px; height: 6px; border-radius: 50%; background: #4ade80; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.4}} }}
    main {{ max-width: 960px; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    .section-title {{ font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: rgba(255,255,255,0.45); margin: 1.75rem 0 0.75rem; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(2,1fr); gap: 0.65rem; }}
    @media(min-width:640px){{ .kpi-grid{{ grid-template-columns:repeat(3,1fr); }} }}
    .kpi-card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 1rem; }}
    .kpi-card.highlight {{ background: linear-gradient(135deg,rgba(240,148,51,0.15),rgba(188,24,136,0.15)); border-color: rgba(188,24,136,0.3); }}
    .kpi-label {{ font-size: 0.68rem; text-transform: uppercase; color: rgba(255,255,255,0.45); }}
    .kpi-value {{ font-size: 1.5rem; font-weight: 800; margin-top: 0.25rem; color: #fff; }}
    .kpi-card.highlight .kpi-value {{ background: var(--ig-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }}
    .panel .kpi-card {{ background: #fafafa; border: 1px solid var(--border); }}
    .panel .kpi-card.highlight {{ background: linear-gradient(135deg, #fff8f0, #fff0f6); border-color: #fbcfe8; }}
    .panel .kpi-label {{ color: var(--muted); }}
    .panel .kpi-value {{ color: var(--text); }}
    .panel .kpi-card.highlight .kpi-value {{ background: var(--ig-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }}
    .kpi-sub {{ font-size: 0.72rem; color: #4ade80; margin-top: 0.2rem; }}
    .panel {{ background: var(--surface); border-radius: 18px; padding: 1.15rem; color: var(--text); margin-top: 0.65rem; }}
    .panel h3 {{ font-size: 0.95rem; font-weight: 700; margin-bottom: 0.85rem; }}
    .chart-label {{ display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 0.25rem; }}
    .chart-bar-bg {{ height: 8px; background: #f0f0f0; border-radius: 4px; overflow: hidden; margin-bottom: 0.65rem; }}
    .chart-bar {{ height: 100%; background: var(--ig-gradient); border-radius: 4px; }}
    .chart-bar.fb {{ background: var(--fb); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
    th {{ text-align: left; padding: 0.6rem 0.5rem; font-size: 0.65rem; text-transform: uppercase; color: var(--muted); border-bottom: 2px solid var(--border); }}
    td {{ padding: 0.65rem 0.5rem; border-bottom: 1px solid var(--border); }}
    .td-handle {{ color: var(--muted); font-size: 0.72rem; display: block; }}
    .growth {{ color: #059669; font-size: 0.68rem; font-weight: 600; }}
    .cost-badge {{ padding: 0.15rem 0.45rem; border-radius: 6px; font-weight: 700; font-size: 0.72rem; }}
    .cost-good {{ background: #ecfdf5; color: #059669; }}
    .cost-mid {{ background: #fffbeb; color: #d97706; }}
    .cost-bad {{ background: #fef2f2; color: #dc2626; }}
    .efficiency-grid {{ display: grid; gap: 0.75rem; }}
    @media(min-width:640px){{ .efficiency-grid{{ grid-template-columns:1fr 1fr; }} }}
    .eff-card.good {{ background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 14px; padding: 1rem; }}
    .eff-card.bad {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 14px; padding: 1rem; }}
    .eff-card h4 {{ font-size: 0.8rem; margin-bottom: 0.65rem; }}
    .eff-card.good h4 {{ color: #059669; }}
    .eff-card.bad h4 {{ color: #dc2626; }}
    .eff-item {{ display: flex; justify-content: space-between; padding: 0.45rem 0; border-bottom: 1px solid rgba(0,0,0,0.06); font-size: 0.78rem; }}
    .seg-item {{ padding: 0.65rem 0.75rem; background: #fafafa; border-radius: 10px; border: 1px solid var(--border); font-size: 0.78rem; margin-bottom: 0.5rem; }}
    .seg-region {{ color: var(--muted); font-size: 0.75rem; }}
    .app-footer {{ text-align: center; padding: 1.5rem; font-size: 0.72rem; color: rgba(255,255,255,0.3); border-top: 1px solid rgba(255,255,255,0.06); }}
    .back-link {{ display: inline-flex; margin-top: 1rem; font-size: 0.78rem; color: rgba(255,255,255,0.5); }}
    .header-actions {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-top: 0.65rem; }}
    .btn-print {{
      display: inline-flex; align-items: center; gap: 0.4rem;
      padding: 0.45rem 0.9rem; border: none; border-radius: 10px;
      background: var(--ig-gradient); color: #fff; font-size: 0.78rem;
      font-weight: 600; font-family: inherit; cursor: pointer;
      transition: opacity 0.15s, transform 0.15s;
    }}
    .btn-print:hover {{ opacity: 0.9; transform: translateY(-1px); }}
    .btn-print svg {{ width: 16px; height: 16px; fill: currentColor; }}
    .no-print {{ }}
    @media print {{
      body {{ background: #fff; color: #000; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
      .ig-header {{ position: static; background: #fff; border-bottom: 2px solid #eee; color: #000; }}
      .header-title, .header-meta, .header-meta strong {{ color: #000 !important; }}
      .brand-name {{ -webkit-text-fill-color: #c13584; color: #c13584; }}
      .doc-badge {{ border-color: #ddd; color: #666; background: #f5f5f5; }}
      .update-pill {{ color: #059669; border-color: #a7f3d0; background: #ecfdf5; }}
      .update-pill::before {{ background: #059669; animation: none; }}
      .section-title {{ color: #666; page-break-after: avoid; }}
      .panel {{ box-shadow: none; border: 1px solid #ddd; page-break-inside: avoid; margin-top: 0.5rem; }}
      .no-print {{ display: none !important; }}
      .chart-bar, .chart-bar.fb {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
      .eff-card.good, .eff-card.bad {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
      .app-footer {{ color: #999; border-color: #eee; }}
      main {{ padding: 0.5rem 0; }}
      @page {{ margin: 1.5cm; size: A4; }}
    }}
  </style>
</head>
<body>
  <header class="ig-header">
    <div class="header-top">
      <div class="brand"><div class="brand-icon">{IG_SVG}</div><span class="brand-name">Clauth Hub</span></div>
      <span class="doc-badge">Meta Ads</span>
    </div>
    <h1 class="header-title">Relatório Financeiro — Páginas e Campanhas</h1>
    <p class="header-meta"><strong>Atualização Automática</strong><br>Investimento Meta Ads · seguidores atualizados via Instagram</p>
    <div class="header-actions">
      <div class="update-pill">Atualização automática 1x por dia · {html.escape(updated_at)}</div>
      <button type="button" class="btn-print no-print" onclick="window.print()" aria-label="Imprimir relatório em PDF">
        <svg viewBox="0 0 24 24"><path d="M19 8H5c-1.66 0-3 1.34-3 3v6h4v4h12v-4h4v-6c0-1.66-1.34-3-3-3zm-3 11H8v-5h8v5zm3-7c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm-1-9H6v4h12V3z"/></svg>
        Salvar PDF
      </button>
    </div>
  </header>
  <main>
    <p class="section-title">1. Sumário executivo</p>
    <div class="panel"><div class="kpi-grid" id="kpiGrid"></div></div>
    <p class="section-title">2. Distribuição do investimento</p>
    <div class="panel"><h3>Investimento por página</h3><div id="investChart"></div></div>
    <p class="section-title">3. Resultado consolidado por página</p>
    <div class="panel"><div style="overflow-x:auto"><table>
      <thead><tr><th>Página</th><th>Investimento</th><th>Seguidores</th><th>Resultado</th><th>Custo/resultado</th></tr></thead>
      <tbody id="pagesTable"></tbody>
    </table></div></div>
    <p class="section-title">4. Distribuição de seguidores</p>
    <div class="panel"><h3>Seguidores atuais por página</h3><div id="followersChart"></div></div>
    <p class="section-title">5. Custo por resultado</p>
    <div class="efficiency-grid" id="efficiencyGrid"></div>
    <p class="section-title">6. Segmentações</p>
    <div class="panel"><div id="segList"></div></div>
    <a href="/" class="back-link no-print">← Voltar ao acompanhamento</a>
  </main>
  <footer class="app-footer">Clauth Hub · Atualização automática 1x por dia · seguidores do Instagram</footer>
  <script>
    const DATA = {data_json};
    const fmtBRL = n => n.toLocaleString('pt-BR', {{style:'currency',currency:'BRL'}});
    const fmtNum = n => n.toLocaleString('pt-BR');
    const fmtCost = n => 'R$ ' + n.toFixed(2).replace('.', ',');
    const costClass = c => c <= 0.15 ? 'cost-good' : c <= 0.35 ? 'cost-mid' : 'cost-bad';
    const r = DATA.resumo;
    document.getElementById('kpiGrid').innerHTML = [
      {{l:'Investimento total',v:r.investimento_fmt,h:1}},
      {{l:'Páginas analisadas',v:r.paginas}},
      {{l:'Seguidores atuais',v:r.seguidores_fmt}},
      {{l:'Custo médio/seguidor',v:r.custo_medio_fmt}},
      {{l:'Resultado total',v:r.seguidores_fmt}},
      {{l:'Custo por resultado',v:r.custo_resultado_fmt,h:1}},
    ].map(i=>`<div class="kpi-card${{i.h?' highlight':''}}"><div class="kpi-label">${{i.l}}</div><div class="kpi-value">${{i.v}}</div></div>`).join('');
    function bars(id,field,fmt,cls){{
      const max=Math.max(...DATA.paginas.map(p=>p[field]));
      document.getElementById(id).innerHTML=[...DATA.paginas].sort((a,b)=>b[field]-a[field]).map(p=>`
        <div class="chart-label"><span>${{p.nome}}</span><span>${{fmt(p[field])}}</span></div>
        <div class="chart-bar-bg"><div class="chart-bar ${{cls||''}}" style="width:${{(p[field]/max*100).toFixed(1)}}%"></div></div>`).join('');
    }}
    bars('investChart','investimento',fmtBRL,'fb');
    bars('followersChart','seguidores_atuais',fmtNum);
    document.getElementById('pagesTable').innerHTML=[...DATA.paginas].sort((a,b)=>b.investimento-a.investimento).map(p=>`
      <tr><td><strong>${{p.nome}}</strong><span class="td-handle">${{p.instagram}}</span></td>
      <td>${{p.investimento_fmt}}</td>
      <td>${{p.seguidores_fmt}}</td>
      <td>${{p.seguidores_fmt}}</td>
      <td><span class="cost-badge ${{costClass(p.custo_resultado)}}">${{p.custo_fmt}}</span></td></tr>`).join('');
    const eff=(arr)=>arr.map(i=>`<div class="eff-item"><span>${{i.nome}}</span><span><strong>${{fmtCost(i.custo)}}</strong> · ${{fmtNum(i.seguidores)}} seg.</span></div>`).join('');
    document.getElementById('efficiencyGrid').innerHTML=`
      <div class="panel eff-card good"><h4>Menores custos</h4>${{eff(DATA.menores_custos)}}</div>
      <div class="panel eff-card bad"><h4>Maiores custos</h4>${{eff(DATA.maiores_custos)}}</div>`;
    document.getElementById('segList').innerHTML=[...DATA.paginas].sort((a,b)=>a.nome.localeCompare(b.nome,'pt-BR')).map(p=>`
      <div class="seg-item"><strong>${{p.nome}}</strong> <span class="td-handle">${{p.instagram}}</span><br><span class="seg-region">${{p.segmentacao}}</span></div>`).join('');
  </script>
</body>
</html>"""


def update_and_save(fetch_followers, model: str, updated_at: str) -> dict:
    config = json.loads(RELATORIO_CONFIG.read_text(encoding="utf-8"))
    data = compute_relatorio(config, fetch_followers)
    payload = {
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "updated_at_fmt": updated_at,
        "model": model,
        **data,
    }
    RELATORIO_DATA.parent.mkdir(parents=True, exist_ok=True)
    RELATORIO_DATA.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    RELATORIO_HTML.parent.mkdir(parents=True, exist_ok=True)
    RELATORIO_HTML.write_text(render_relatorio_html(data, updated_at, model), encoding="utf-8")
    return payload
