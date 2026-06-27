"""Coleta e renderização do painel de engajamento das páginas (criativos FB/IG)."""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from relatorio_financeiro import collection_label

ROOT = Path(__file__).resolve().parent.parent
PAGES_CONFIG = ROOT / "config" / "pages.json"
ENGAJAMENTO_DATA = ROOT / "data" / "engajamento.json"
ENGAJAMENTO_HTML = ROOT / "engajamento" / "index.html"

BOT_UA = "facebookexternalhit/1.1"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _fmt_num(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 10_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return f"{value:,}".replace(",", ".")


def _parse_number(raw: str) -> int | None:
    raw = raw.strip().upper().replace(".", "").replace(",", "")
    mult = 1
    if raw.endswith("K"):
        mult = 1_000
        raw = raw[:-1]
    elif raw.endswith("M"):
        mult = 1_000_000
        raw = raw[:-1]
    try:
        return int(float(raw) * mult)
    except ValueError:
        return None


def _fetch_og_description(url: str) -> str | None:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": BOT_UA},
            timeout=25,
            allow_redirects=True,
        )
        r.raise_for_status()
        m = re.search(r'og:description" content="([^"]+)"', r.text)
        if m:
            return html.unescape(m.group(1))
    except requests.RequestException as exc:
        print(f"  [aviso] Falha ao buscar {url}: {exc}", file=sys.stderr)
    return None


def _parse_post_engagement(desc: str) -> int:
    total = 0
    patterns = [
        r"([\d.,]+[KkMm]?)\s*(?:reactions|reações|reaction|curtidas|likes|curtida)",
        r"([\d.,]+[KkMm]?)\s*(?:comments|comentários|comentarios|comment)",
        r"([\d.,]+[KkMm]?)\s*(?:shares|compartilhamentos|share)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, desc, re.I):
            n = _parse_number(m.group(1))
            if n:
                total += n
    return total


def _openrouter_engagement(
    api_key: str,
    model: str,
    page_name: str,
    fb_post: str | None,
    ig_post: str | None,
) -> dict:
    urls = [u for u in [fb_post, ig_post] if u]
    prompt = (
        f"Analise o engajamento total dos criativos/publicações da página '{page_name}'.\n"
        f"URLs: {json.dumps(urls)}\n\n"
        "Engajamento = soma de curtidas/reações + comentários + compartilhamentos visíveis "
        "nos posts/creativos (Facebook e Instagram separados).\n"
        "Retorne APENAS JSON válido, sem markdown:\n"
        '{"facebook_engajamento": number|null, "instagram_engajamento": number|null}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [
            {
                "type": "openrouter:web_fetch",
                "openrouter:web_fetch": {
                    "engine": "openrouter",
                    "allowed_domains": [
                        "facebook.com", "www.facebook.com", "fb.me",
                        "instagram.com", "www.instagram.com",
                    ],
                    "max_uses": 3,
                },
            }
        ],
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://clauthub.local",
            "X-Title": "Clauth Hub",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    fb = data.get("facebook_engajamento")
    ig = data.get("instagram_engajamento")
    return {
        "facebook": int(fb) if fb is not None else None,
        "instagram": int(ig) if ig is not None else None,
        "source": "openrouter",
    }


def collect_page_engagement(
    page: dict,
    metrics: dict,
    api_key: str | None,
    model: str,
) -> dict:
    fb_post = page.get("facebook_post")
    ig_post = page.get("instagram_post")
    fb_http = 0
    ig_http = 0

    if fb_post:
        desc = _fetch_og_description(fb_post)
        if desc:
            fb_http = _parse_post_engagement(desc)

    if ig_post:
        desc = _fetch_og_description(ig_post)
        if desc:
            ig_http = _parse_post_engagement(desc)

    fb_val, ig_val = fb_http or None, ig_http or None
    source = "http" if (fb_http or ig_http) else "metrics"

    if api_key and (fb_post or ig_post):
        try:
            print(f"  → OpenRouter engajamento ({page['name']})...")
            ai = _openrouter_engagement(api_key, model, page["name"], fb_post, ig_post)
            if ai.get("facebook") is not None:
                fb_val = ai["facebook"]
            if ai.get("instagram") is not None:
                ig_val = ai["instagram"]
            source = ai.get("source", "openrouter")
        except Exception as exc:
            print(f"  [aviso] OpenRouter engajamento ({page['name']}): {exc}", file=sys.stderr)

    m = metrics.get("facebook", {})
    fb_fallback = (m.get("curtidas") or 0) + (m.get("falando_sobre") or 0)
    if fb_val is None and fb_fallback:
        fb_val = fb_fallback
        if source == "metrics":
            source = "pagina"
    if ig_val is None:
        ig_val = 0

    fb_final = int(fb_val or 0)
    ig_final = int(ig_val or 0)
    total = fb_final + ig_final

    return {
        "nome": page["name"],
        "handle": f"@{page.get('instagram_handle', '')}",
        "facebook_engajamento": fb_final,
        "facebook_fmt": _fmt_num(fb_final),
        "instagram_engajamento": ig_final,
        "instagram_fmt": _fmt_num(ig_final),
        "engajamento_total": total,
        "engajamento_fmt": _fmt_num(total),
        "source": source,
    }


def compute_engajamento(
    pages: list,
    all_metrics: list,
    api_key: str | None,
    model: str,
) -> dict:
    rows = [
        collect_page_engagement(p, m, api_key, model)
        for p, m in zip(pages, all_metrics)
    ]
    rows.sort(key=lambda x: x["engajamento_total"], reverse=True)

    total_fb = sum(r["facebook_engajamento"] for r in rows)
    total_ig = sum(r["instagram_engajamento"] for r in rows)
    total = total_fb + total_ig
    media = total // len(rows) if rows else 0

    top5 = rows[:5]
    max_eng = top5[0]["engajamento_total"] if top5 else 1

    return {
        "titulo": "Engajamento das Páginas",
        "resumo": {
            "engajamento_total": total,
            "engajamento_fmt": _fmt_num(total),
            "facebook_total": total_fb,
            "facebook_fmt": _fmt_num(total_fb),
            "instagram_total": total_ig,
            "instagram_fmt": _fmt_num(total_ig),
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
        pct = min(100, int(p["engajamento_total"] / data["max_engajamento"] * 100))
        rows_html += f"""
        <tr>
          <td>{i}</td>
          <td><strong>{html.escape(p["nome"])}</strong><span class="td-handle">{html.escape(p["handle"])}</span></td>
          <td>{p["facebook_fmt"]}</td>
          <td>{p["instagram_fmt"]}</td>
          <td><strong>{p["engajamento_fmt"]}</strong></td>
          <td><div class="bar-wrap"><div class="bar" style="width:{pct}%"></div></div></td>
        </tr>"""

    top_html = ""
    for p in data["top5"]:
        pct = min(100, int(p["engajamento_total"] / data["max_engajamento"] * 100))
        top_html += f"""
        <div class="top-item">
          <div class="chart-label"><span>{html.escape(p["nome"])}</span><span>{p["engajamento_fmt"]}</span></div>
          <div class="chart-bar-bg"><div class="chart-bar" style="width:{pct}%"></div></div>
        </div>"""

    r = data["resumo"]
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Clauth Hub — Engajamento das Páginas</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --ig-gradient: linear-gradient(45deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888);
      --fb: #1877F2; --bg: #000; --surface: #fff; --text: #262626; --muted: #8e8e8e; --border: #efefef;
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
    @media(min-width:640px){{ .kpi-grid{{ grid-template-columns:repeat(4,1fr); }} }}
    .kpi-card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 1rem; }}
    .kpi-card.highlight {{ background: linear-gradient(135deg,rgba(240,148,51,0.15),rgba(188,24,136,0.15)); border-color: rgba(188,24,136,0.3); }}
    .kpi-label {{ font-size: 0.68rem; text-transform: uppercase; color: rgba(255,255,255,0.45); }}
    .kpi-value {{ font-size: 1.45rem; font-weight: 800; margin-top: 0.25rem; }}
    .kpi-card.highlight .kpi-value {{ background: var(--ig-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    .panel {{ background: var(--surface); border-radius: 18px; padding: 1.15rem; color: var(--text); margin-top: 0.65rem; }}
    .panel h3 {{ font-size: 0.95rem; font-weight: 700; margin-bottom: 0.85rem; }}
    .chart-label {{ display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 0.25rem; }}
    .chart-bar-bg {{ height: 8px; background: #f0f0f0; border-radius: 4px; overflow: hidden; margin-bottom: 0.65rem; }}
    .chart-bar {{ height: 100%; background: var(--ig-gradient); border-radius: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
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
      <span class="doc-badge">Engajamento</span>
    </div>
    <h1 class="header-title">Engajamento das Páginas</h1>
    <p class="header-meta"><strong>Atualização Automática</strong><br>Curtidas, comentários e compartilhamentos nos criativos · via IA + Meta</p>
    <div class="update-pill">{html.escape(updated_at)}</div>
    <a class="back-link" href="/">← Voltar ao acompanhamento</a>
  </header>
  <main>
    <p class="section-title">Resumo geral</p>
    <div class="kpi-grid">
      <div class="kpi-card highlight"><div class="kpi-label">Engajamento total</div><div class="kpi-value">{r["engajamento_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Facebook</div><div class="kpi-value">{r["facebook_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Instagram</div><div class="kpi-value">{r["instagram_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Média por página</div><div class="kpi-value">{r["media_fmt"]}</div></div>
    </div>
    <p class="section-title">Top 5 páginas</p>
    <div class="panel"><h3>Maior engajamento</h3>{top_html}</div>
    <p class="section-title">Todas as páginas ({r["paginas"]})</p>
    <div class="panel">
      <table>
        <thead><tr><th>#</th><th>Página</th><th>Facebook</th><th>Instagram</th><th>Total</th><th></th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </main>
  <footer class="app-footer">Clauth Hub · Engajamento dos criativos · atualização 1x por dia às 08:00</footer>
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
    return data
