"""Coleta e renderização do painel de engajamento Instagram (scraping de publicações)."""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from relatorio_financeiro import collection_label
from instagram_scraper import InstagramScraper, REQUEST_DELAY

ROOT = Path(__file__).resolve().parent.parent
ENGAJAMENTO_DATA = ROOT / "data" / "engajamento.json"
ENGAJAMENTO_HTML = ROOT / "engajamento" / "index.html"


def _fmt_num(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 10_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return f"{value:,}".replace(",", ".")


def collect_page_engagement(
    page: dict,
    scraper: InstagramScraper,
    api_key: str | None,
    model: str,
) -> dict:
    handle = page.get("instagram_handle", "")
    post_url = page.get("instagram_post")

    print(f"  → Scrape Instagram @{handle}...")
    profile = scraper.scrape_profile(
        handle=handle,
        fallback_post_url=post_url,
        api_key=api_key,
        model=model,
    )

    return {
        "nome": page["name"],
        "handle": f"@{handle}",
        "publicacoes_coletadas": len(profile.publicacoes),
        "visualizacoes": profile.visualizacoes,
        "curtidas": profile.curtidas,
        "comentarios": profile.comentarios,
        "compartilhamentos": profile.compartilhamentos,
        "visualizacoes_fmt": _fmt_num(profile.visualizacoes),
        "curtidas_fmt": _fmt_num(profile.curtidas),
        "comentarios_fmt": _fmt_num(profile.comentarios),
        "compartilhamentos_fmt": _fmt_num(profile.compartilhamentos),
        "engajamento_total": profile.total,
        "engajamento_fmt": _fmt_num(profile.total),
        "source": profile.source,
        "posts": [
            {
                "url": p.url,
                "shortcode": p.shortcode,
                "visualizacoes": p.visualizacoes,
                "curtidas": p.curtidas,
                "comentarios": p.comentarios,
                "compartilhamentos": p.compartilhamentos,
                "source": p.source,
            }
            for p in profile.publicacoes
        ],
    }


def compute_engajamento(
    pages: list,
    all_metrics: list,
    api_key: str | None,
    model: str,
) -> dict:
    scraper = InstagramScraper(delay=REQUEST_DELAY)
    rows = [collect_page_engagement(p, scraper, api_key, model) for p in pages]
    rows.sort(key=lambda x: x["engajamento_total"], reverse=True)

    sum_views = sum(r["visualizacoes"] for r in rows)
    sum_likes = sum(r["curtidas"] for r in rows)
    sum_comments = sum(r["comentarios"] for r in rows)
    sum_shares = sum(r["compartilhamentos"] for r in rows)
    sum_posts = sum(r["publicacoes_coletadas"] for r in rows)
    total = sum_views + sum_likes + sum_comments + sum_shares
    media = total // len(rows) if rows else 0

    top5 = [r for r in rows if r["engajamento_total"] > 0][:5]
    max_eng = top5[0]["engajamento_total"] if top5 else 1

    return {
        "titulo": "Engajamento Instagram",
        "resumo": {
            "visualizacoes_total": sum_views,
            "visualizacoes_fmt": _fmt_num(sum_views),
            "curtidas_total": sum_likes,
            "curtidas_fmt": _fmt_num(sum_likes),
            "comentarios_total": sum_comments,
            "comentarios_fmt": _fmt_num(sum_comments),
            "compartilhamentos_total": sum_shares,
            "compartilhamentos_fmt": _fmt_num(sum_shares),
            "publicacoes_coletadas": sum_posts,
            "engajamento_total": total,
            "engajamento_fmt": _fmt_num(total),
            "paginas": len(rows),
            "media_por_pagina": media,
            "media_fmt": _fmt_num(media),
        },
        "paginas": rows,
        "top5": top5,
        "max_engajamento": max_eng or 1,
    }


def render_engajamento_html(data: dict, updated_at: str) -> str:
    rows_html = ""
    for i, p in enumerate(data["paginas"], 1):
        pct = min(100, int(p["engajamento_total"] / data["max_engajamento"] * 100)) if p["engajamento_total"] else 0
        pub_note = f'{p["publicacoes_coletadas"]} pub.' if p["publicacoes_coletadas"] else "—"
        rows_html += f"""
        <tr>
          <td>{i}</td>
          <td><strong>{html.escape(p["nome"])}</strong><span class="td-handle">{html.escape(p["handle"])} · {pub_note}</span></td>
          <td>{p["visualizacoes_fmt"]}</td>
          <td>{p["curtidas_fmt"]}</td>
          <td>{p["comentarios_fmt"]}</td>
          <td>{p["compartilhamentos_fmt"]}</td>
          <td><strong>{p["engajamento_fmt"]}</strong></td>
          <td><div class="bar-wrap"><div class="bar" style="width:{pct}%"></div></div></td>
        </tr>"""

    top_html = ""
    for p in data["top5"]:
        pct = min(100, int(p["engajamento_total"] / data["max_engajamento"] * 100))
        top_html += f"""
        <div class="top-item">
          <div class="chart-label"><span>{html.escape(p["nome"])}</span><span>{p["engajamento_fmt"]}</span></div>
          <div class="chart-sub">{p["publicacoes_coletadas"]} publicações · {p["visualizacoes_fmt"]} views · {p["curtidas_fmt"]} curtidas · {p["comentarios_fmt"]} coment.</div>
          <div class="chart-bar-bg"><div class="chart-bar" style="width:{pct}%"></div></div>
        </div>"""

    if not top_html:
        top_html = '<p class="empty-note">Nenhuma publicação raspada nesta execução. Tente novamente ou verifique os perfis.</p>'

    r = data["resumo"]
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Clauth Hub — Engajamento Instagram</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --ig-gradient: linear-gradient(45deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888);
      --bg: #000; --surface: #fff; --text: #262626; --muted: #8e8e8e; --border: #efefef;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Inter, sans-serif; background: var(--bg); color: #fff; min-height: 100dvh; }}
    a {{ color: #a5b4fc; text-decoration: none; }}
    .ig-header {{
      position: sticky; top: 0; z-index: 100; background: rgba(0,0,0,0.92);
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
    .update-pill {{
      display: inline-flex; align-items: center; gap: 0.35rem; margin-top: 0.55rem;
      padding: 0.3rem 0.7rem; border-radius: 20px; background: rgba(34,197,94,0.15);
      border: 1px solid rgba(34,197,94,0.3); font-size: 0.72rem; color: #4ade80;
    }}
    .update-pill::before {{ content: ''; width: 6px; height: 6px; border-radius: 50%; background: #4ade80; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.4}} }}
    .back-link {{ display: inline-flex; margin-top: 0.65rem; font-size: 0.78rem; color: rgba(255,255,255,0.55); }}
    main {{ max-width: 960px; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    .section-title {{ font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: rgba(255,255,255,0.45); margin: 1.5rem 0 0.75rem; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(2,1fr); gap: 0.65rem; }}
    @media(min-width:640px){{ .kpi-grid{{ grid-template-columns:repeat(3,1fr); }} }}
    @media(min-width:900px){{ .kpi-grid{{ grid-template-columns:repeat(6,1fr); }} }}
    .kpi-card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 1rem; }}
    .kpi-card.highlight {{ background: linear-gradient(135deg,rgba(240,148,51,0.15),rgba(188,24,136,0.15)); border-color: rgba(188,24,136,0.3); grid-column: span 2; }}
    @media(min-width:900px){{ .kpi-card.highlight{{ grid-column: span 1; }} }}
    .kpi-label {{ font-size: 0.68rem; text-transform: uppercase; color: rgba(255,255,255,0.45); }}
    .kpi-value {{ font-size: 1.35rem; font-weight: 800; margin-top: 0.25rem; }}
    .kpi-card.highlight .kpi-value {{ background: var(--ig-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    .panel {{ background: var(--surface); border-radius: 18px; padding: 1.15rem; color: var(--text); margin-top: 0.65rem; overflow-x: auto; }}
    .panel h3 {{ font-size: 0.95rem; font-weight: 700; margin-bottom: 0.85rem; }}
    .empty-note {{ font-size: 0.78rem; color: var(--muted); }}
    .chart-label {{ display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 0.15rem; }}
    .chart-sub {{ font-size: 0.68rem; color: var(--muted); margin-bottom: 0.35rem; }}
    .chart-bar-bg {{ height: 8px; background: #f0f0f0; border-radius: 4px; overflow: hidden; margin-bottom: 0.65rem; }}
    .chart-bar {{ height: 100%; background: var(--ig-gradient); border-radius: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; min-width: 720px; }}
    th {{ text-align: left; padding: 0.6rem 0.5rem; font-size: 0.65rem; text-transform: uppercase; color: var(--muted); border-bottom: 2px solid var(--border); }}
    td {{ padding: 0.65rem 0.5rem; border-bottom: 1px solid var(--border); vertical-align: middle; }}
    .td-handle {{ color: var(--muted); font-size: 0.72rem; display: block; }}
    .bar-wrap {{ height: 6px; background: #f0f0f0; border-radius: 3px; overflow: hidden; min-width: 60px; }}
    .bar {{ height: 100%; background: var(--ig-gradient); border-radius: 3px; }}
    .app-footer {{ text-align: center; padding: 1.5rem; font-size: 0.72rem; color: rgba(255,255,255,0.3); border-top: 1px solid rgba(255,255,255,0.06); }}
  </style>
</head>
<body>
  <header class="ig-header">
    <div class="header-top">
      <div class="brand">
        <div class="brand-icon"><svg viewBox="0 0 24 24"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/></svg></div>
        <span class="brand-name">Clauth Hub</span>
      </div>
      <span class="doc-badge">Scraper IG</span>
    </div>
    <h1 class="header-title">Engajamento Instagram</h1>
    <p class="header-meta"><strong>Raspagem automática de publicações</strong><br>
    {r["publicacoes_coletadas"]} publicações analisadas · views, curtidas, comentários e compartilhamentos · 1x/dia às 08:00</p>
    <div class="update-pill">{html.escape(updated_at)}</div>
    <a class="back-link" href="/">← Voltar ao acompanhamento</a>
  </header>
  <main>
    <p class="section-title">Resumo geral</p>
    <div class="kpi-grid">
      <div class="kpi-card highlight"><div class="kpi-label">Engajamento total</div><div class="kpi-value">{r["engajamento_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Publicações</div><div class="kpi-value">{r["publicacoes_coletadas"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Visualizações</div><div class="kpi-value">{r["visualizacoes_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Curtidas</div><div class="kpi-value">{r["curtidas_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Comentários</div><div class="kpi-value">{r["comentarios_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Compartilhamentos</div><div class="kpi-value">{r["compartilhamentos_fmt"]}</div></div>
    </div>
    <p class="section-title">Top 5 páginas</p>
    <div class="panel"><h3>Maior engajamento raspado</h3>{top_html}</div>
    <p class="section-title">Todas as páginas ({r["paginas"]})</p>
    <div class="panel">
      <table>
        <thead><tr><th>#</th><th>Página</th><th>Views</th><th>Curtidas</th><th>Coment.</th><th>Shares</th><th>Total</th><th></th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </main>
  <footer class="app-footer">Clauth Hub · Scraper de publicações Instagram · API + embed + IA</footer>
</body>
</html>"""


def update_and_save(
    pages: list,
    all_metrics: list,
    api_key: str | None,
    model: str,
    updated_at: str,
) -> dict:
    data = compute_engajamento(pages, all_metrics, api_key, model)
    payload = {
        **data,
        "updated_at": datetime.now(timezone(timedelta(hours=-3))).isoformat(),
        "updated_at_fmt": updated_at,
        "model": model,
    }
    ENGAJAMENTO_DATA.parent.mkdir(parents=True, exist_ok=True)
    ENGAJAMENTO_HTML.parent.mkdir(parents=True, exist_ok=True)
    ENGAJAMENTO_DATA.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ENGAJAMENTO_HTML.write_text(render_engajamento_html(data, updated_at), encoding="utf-8")
    print(f"Publicações raspadas: {data['resumo']['publicacoes_coletadas']}")
    return data
