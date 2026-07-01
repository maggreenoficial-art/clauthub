"""Painel de engajamento das Páginas Clauth (50 posts + seguidores por perfil)."""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from engajamento import (
    MAX_POSTS_LABEL,
    TOP_POSTS_GLOBAL,
    _build_top_posts,
    _fmt_num,
    _mark_hot_pages,
    _posts_with_fmt,
    _render_post_card,
    collect_page_engagement,
)
from instagram_scraper import InstagramScraper, REQUEST_DELAY

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "paginas_clauth.json"
CACHE_PATH = ROOT / "config" / "paginas_clauth_cache.json"
DATA_PATH = ROOT / "data" / "paginas_clauth.json"
HTML_PATH = ROOT / "paginasclauth" / "index.html"

MIN_CACHE_POSTS = 100
_RELATORIO_LOOKUP: dict[str, int] | None = None


def _followers_from_relatorio() -> dict[str, int]:
    global _RELATORIO_LOOKUP
    if _RELATORIO_LOOKUP is not None:
        return _RELATORIO_LOOKUP
    path = ROOT / "config" / "relatorio_financeiro.json"
    lookup: dict[str, int] = {}
    if path.exists():
        try:
            for p in json.loads(path.read_text(encoding="utf-8")).get("paginas") or []:
                h = (p.get("instagram_handle") or "").lstrip("@")
                seg = p.get("seguidores_relatorio")
                if h and seg:
                    lookup[h] = int(seg)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    _RELATORIO_LOOKUP = lookup
    return lookup


def _fetch_followers_robust(
    handle: str,
    nome: str,
    api_key: str | None = None,
    model: str = "",
    cached: int | None = None,
) -> tuple[int, str, str]:
    """Busca seguidores com várias estratégias; preserva cache se tudo falhar."""
    handle = handle.lstrip("@")
    scraper = InstagramScraper(delay=0.4)
    followers = scraper.fetch_profile_followers(handle)
    source = "instagram"

    if followers is None:
        followers = _followers_from_relatorio().get(handle)
        source = "relatorio" if followers else ""

    if followers is None and api_key and model:
        try:
            from update_metrics import openrouter_extract

            ai = openrouter_extract(
                api_key,
                model,
                nome,
                None,
                f"https://www.instagram.com/{handle}/",
            )
            followers = ai.get("seguidores")
            if followers is not None:
                source = "openrouter"
        except Exception as exc:
            print(f"  [aviso] OpenRouter seguidores @{handle}: {exc}", file=sys.stderr)

    if followers is None and cached:
        followers = cached
        source = "cache"

    if followers:
        return int(followers), _fmt_num(int(followers)), source
    return 0, "—", ""


def _resolve_followers(
    scraper: InstagramScraper,
    handle: str,
    nome: str = "",
    api_key: str | None = None,
    model: str = "",
    cached: int | None = None,
) -> tuple[int, str]:
    del scraper  # sessão própria em _fetch_followers_robust
    seg, seg_fmt, _ = _fetch_followers_robust(handle, nome or handle, api_key, model, cached)
    return seg, seg_fmt


def refresh_followers(data: dict, api_key: str | None = None, model: str = "") -> dict:
    """Atualiza seguidores — uma sessão nova por perfil para evitar rate limit."""
    for row in data.get("paginas") or []:
        handle = row.get("handle", "").lstrip("@")
        if not handle:
            continue
        nome = row.get("nome", handle)
        cached = row.get("seguidores") or None
        seg, seg_fmt, source = _fetch_followers_robust(handle, nome, api_key, model, cached)
        row["seguidores"] = seg
        row["seguidores_fmt"] = seg_fmt
        if source:
            row["seguidores_source"] = source
        print(f"  @{handle}: {seg_fmt} ({source or 'sem dados'})", file=sys.stderr)
    total = sum(r.get("seguidores") or 0 for r in data.get("paginas") or [])
    resumo = data.setdefault("resumo", {})
    resumo["seguidores_total"] = total
    resumo["seguidores_fmt"] = _fmt_num(total) if total else "—"
    return data


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_pages() -> list[dict]:
    return load_config().get("paginas") or []


def _page_for_scrape(page: dict) -> dict:
    return {
        "name": page["nome"],
        "instagram_handle": page["instagram_handle"],
        "instagram_post": page.get("instagram_post"),
    }


def load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(data: dict) -> None:
    if (data.get("resumo") or {}).get("publicacoes_coletadas", 0) < MIN_CACHE_POSTS:
        return
    normalized = normalize_data(dict(data))
    payload = {
        "updated_at": datetime.now(timezone(timedelta(hours=-3))).isoformat(),
        **{k: v for k, v in normalized.items() if k not in ("updated_at", "updated_at_fmt", "model")},
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_page(page: dict, scraper: InstagramScraper, api_key: str | None, model: str) -> dict:
    handle = page["instagram_handle"]
    seg, seg_fmt = _resolve_followers(scraper, handle, page["nome"], api_key, model)
    row = collect_page_engagement(_page_for_scrape(page), scraper, api_key, model)
    row["seguidores"] = seg
    row["seguidores_fmt"] = seg_fmt
    return row


def compute(api_key: str | None, model: str) -> dict:
    pages = load_pages()
    scraper = InstagramScraper(delay=REQUEST_DELAY)
    rows = [collect_page(p, scraper, api_key, model) for p in pages]
    rows.sort(key=lambda x: x["engajamento_total"], reverse=True)
    _mark_hot_pages(rows)

    sum_views = sum(r["visualizacoes"] for r in rows)
    sum_likes = sum(r["curtidas"] for r in rows)
    sum_comments = sum(r["comentarios"] for r in rows)
    sum_posts = sum(r["publicacoes_coletadas"] for r in rows)
    sum_followers = sum(r.get("seguidores") or 0 for r in rows)
    total = sum_views + sum_likes + sum_comments
    media = total // len(rows) if rows else 0
    hot_count = sum(1 for r in rows if r.get("hot"))

    top5 = [r for r in rows if r["engajamento_total"] > 0][:5]
    max_eng = top5[0]["engajamento_total"] if top5 else 1
    top_posts = _build_top_posts(rows, limit=TOP_POSTS_GLOBAL)

    return {
        "titulo": load_config().get("titulo", "Páginas Clauth"),
        "max_posts_por_pagina": MAX_POSTS_LABEL,
        "resumo": {
            "visualizacoes_total": sum_views,
            "visualizacoes_fmt": _fmt_num(sum_views),
            "curtidas_total": sum_likes,
            "curtidas_fmt": _fmt_num(sum_likes),
            "comentarios_total": sum_comments,
            "comentarios_fmt": _fmt_num(sum_comments),
            "publicacoes_coletadas": sum_posts,
            "engajamento_total": total,
            "engajamento_fmt": _fmt_num(total),
            "seguidores_total": sum_followers,
            "seguidores_fmt": _fmt_num(sum_followers) if sum_followers else "—",
            "paginas": len(rows),
            "paginas_hot": hot_count,
            "media_por_pagina": media,
            "media_fmt": _fmt_num(media),
        },
        "paginas": rows,
        "top5": top5,
        "top_posts": top_posts,
        "max_engajamento": max_eng or 1,
    }


def normalize_data(data: dict) -> dict:
    pages = data.get("paginas") or []
    if pages and "hot" not in pages[0]:
        _mark_hot_pages(pages)
    for p in pages:
        if "seguidores_fmt" not in p:
            seg = p.get("seguidores")
            p["seguidores_fmt"] = _fmt_num(seg) if seg else "—"
    if not data.get("top5"):
        ranked = sorted(pages, key=lambda x: x.get("engajamento_total", 0), reverse=True)
        data["top5"] = [r for r in ranked if r.get("engajamento_total", 0) > 0][:5]
    if not data.get("max_engajamento"):
        top5 = data.get("top5") or []
        data["max_engajamento"] = top5[0]["engajamento_total"] if top5 else 1
    if not data.get("top_posts"):
        data["top_posts"] = _build_top_posts(pages)
    resumo = data.get("resumo") or {}
    if "seguidores_fmt" not in resumo:
        seg = sum(p.get("seguidores") or 0 for p in pages)
        resumo["seguidores_total"] = seg
        resumo["seguidores_fmt"] = _fmt_num(seg) if seg else "—"
        data["resumo"] = resumo
    return data


def prefer_cache(data: dict) -> dict:
    cached = load_cache()
    if not cached:
        return normalize_data(data)
    cur = (data.get("resumo") or {}).get("publicacoes_coletadas", 0)
    prev = (cached.get("resumo") or {}).get("publicacoes_coletadas", 0)
    if prev > cur:
        print(f"  [cache] Páginas Clauth: usando cache ({prev} pub.) em vez de {cur}", file=sys.stderr)
        merged = dict(cached)
        merged["max_posts_por_pagina"] = data.get("max_posts_por_pagina", MAX_POSTS_LABEL)
        return normalize_data(merged)
    return normalize_data(data)


def _render_page_block(page: dict, max_posts: int) -> str:
    posts = _posts_with_fmt(page.get("posts") or [])
    hot_badge = '<span class="hot-badge hot-badge-sm">🔥 Em tração</span>' if page.get("hot") else ""
    block_cls = "page-block page-block-hot" if page.get("hot") else "page-block"
    open_attr = " open" if page.get("hot") else ""
    seg = page.get("seguidores_fmt", "—")

    if not posts:
        return f"""
    <details class="{block_cls}">
      <summary>
        <span class="page-block-title">{html.escape(page["nome"])} {hot_badge}</span>
        <span class="page-block-meta">{html.escape(page["handle"])} · {seg} seguidores · sem publicações coletadas</span>
      </summary>
      <p class="empty-note">Nenhuma publicação disponível nesta coleta.</p>
    </details>"""

    max_eng = posts[0]["engajamento_total"] or 1
    rows = ""
    for i, p in enumerate(posts, 1):
        pct = min(100, int(p["engajamento_total"] / max_eng * 100)) if p["engajamento_total"] else 0
        row_cls = "post-row-top" if i <= 3 else ""
        link = html.escape(p["url"])
        rows += f"""
          <tr class="{row_cls}">
            <td>{i}</td>
            <td><a href="{link}" target="_blank" rel="noopener noreferrer" class="post-shortlink">/{html.escape(p["shortcode"])}</a></td>
            <td>{p["visualizacoes_fmt"]}</td>
            <td>{p["curtidas_fmt"]}</td>
            <td>{p["comentarios_fmt"]}</td>
            <td><strong>{p["engajamento_fmt"]}</strong></td>
            <td><div class="bar-wrap"><div class="bar" style="width:{pct}%"></div></div></td>
          </tr>"""

    return f"""
    <details class="{block_cls}"{open_attr}>
      <summary>
        <span class="page-block-title">{html.escape(page["nome"])} {hot_badge}</span>
        <span class="page-block-meta">{html.escape(page["handle"])} · <strong>{seg}</strong> seguidores · {len(posts)}/{max_posts} publicações · total {page["engajamento_fmt"]}</span>
      </summary>
      <p class="panel-desc">Ordenadas por engajamento (views + curtidas + comentários). As do topo indicam a vertente de conteúdo que está funcionando.</p>
      <table class="post-table">
        <thead><tr><th>#</th><th>Post</th><th>Views</th><th>Curtidas</th><th>Coment.</th><th>Total</th><th></th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </details>"""


def render_html(data: dict, updated_at: str) -> str:
    data = normalize_data(data)
    top_posts = data.get("top_posts") or _build_top_posts(data["paginas"], limit=TOP_POSTS_GLOBAL)
    max_posts = data.get("max_posts_por_pagina", MAX_POSTS_LABEL)
    hot_count = sum(1 for p in data["paginas"] if p.get("hot"))
    pages_posts_html = "".join(_render_page_block(p, max_posts) for p in data["paginas"])

    rows_html = ""
    for i, p in enumerate(data["paginas"], 1):
        pct = min(100, int(p["engajamento_total"] / data["max_engajamento"] * 100)) if p["engajamento_total"] else 0
        pub_note = f'{p["publicacoes_coletadas"]}/{max_posts} pub.' if p["publicacoes_coletadas"] else "—"
        hot_cls = "row-hot" if p.get("hot") else ""
        if p.get("hot"):
            momentum_suffix = f' · {p["momentum_fmt"]}' if p.get("momentum_fmt") else ""
            hot_badge = f'<span class="hot-badge" title="Em tração">🔥 HOT{momentum_suffix}</span>'
        else:
            hot_badge = ""
        tr_class = f' class="{hot_cls}"' if hot_cls else ""
        rows_html += f"""
        <tr{tr_class}>
          <td>{i}</td>
          <td><strong>{html.escape(p["nome"])}</strong>{hot_badge}<span class="td-handle">{html.escape(p["handle"])} · {pub_note}</span></td>
          <td><strong>{p.get("seguidores_fmt", "—")}</strong></td>
          <td>{p["visualizacoes_fmt"]}</td>
          <td>{p["curtidas_fmt"]}</td>
          <td>{p["comentarios_fmt"]}</td>
          <td><strong>{p["engajamento_fmt"]}</strong></td>
          <td><div class="bar-wrap"><div class="bar" style="width:{pct}%"></div></div></td>
        </tr>"""

    top_html = ""
    for p in data["top5"]:
        pct = min(100, int(p["engajamento_total"] / data["max_engajamento"] * 100))
        hot_cls = " top-item-hot" if p.get("hot") else ""
        hot_label = '<span class="hot-badge hot-badge-sm">🔥 Em tração</span>' if p.get("hot") else ""
        top_html += f"""
        <div class="top-item{hot_cls}">
          <div class="chart-label"><span>{html.escape(p["nome"])} {hot_label}</span><span>{p["engajamento_fmt"]}</span></div>
          <div class="chart-sub">{p.get("seguidores_fmt", "—")} seguidores · últimas {p["publicacoes_coletadas"]} publicações · {p["visualizacoes_fmt"]} views · {p["curtidas_fmt"]} curtidas</div>
          <div class="chart-bar-bg"><div class="chart-bar" style="width:{pct}%"></div></div>
        </div>"""

    if not top_html:
        top_html = '<p class="empty-note">Nenhuma publicação raspada nesta execução. Tente novamente ou verifique os perfis.</p>'

    if top_posts:
        top_posts_html = "".join(_render_post_card(p, i) for i, p in enumerate(top_posts, 1))
    else:
        top_posts_html = '<p class="empty-note">Nenhuma publicação com engajamento registrado.</p>'

    r = data["resumo"]
    titulo = data.get("titulo", "Páginas Clauth")
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Clauth Hub — {html.escape(titulo)}</title>
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
    @keyframes hot-glow {{
      0%,100% {{ box-shadow: 0 0 0 0 rgba(255,100,50,0.35); }}
      50% {{ box-shadow: 0 0 14px 3px rgba(255,100,50,0.55); }}
    }}
    @keyframes hot-shimmer {{
      0% {{ background-position: 200% center; }}
      100% {{ background-position: -200% center; }}
    }}
    .info-banner {{
      margin-top: 0.85rem; padding: 0.75rem 0.9rem; border-radius: 14px;
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1);
      font-size: 0.76rem; line-height: 1.55; color: rgba(255,255,255,0.65);
    }}
    .info-banner strong {{ color: rgba(255,255,255,0.9); }}
    .back-link {{ display: inline-flex; margin-top: 0.65rem; font-size: 0.78rem; color: rgba(255,255,255,0.55); }}
    main {{ max-width: 960px; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    .section-title {{ font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: rgba(255,255,255,0.45); margin: 1.5rem 0 0.75rem; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(2,1fr); gap: 0.65rem; }}
    @media(min-width:640px){{ .kpi-grid{{ grid-template-columns:repeat(3,1fr); }} }}
    @media(min-width:900px){{ .kpi-grid{{ grid-template-columns:repeat(6,1fr); }} }}
    .kpi-card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 1rem; }}
    .kpi-card.highlight {{ background: linear-gradient(135deg,rgba(240,148,51,0.15),rgba(188,24,136,0.15)); border-color: rgba(188,24,136,0.3); grid-column: span 2; }}
    @media(min-width:900px){{ .kpi-card.highlight{{ grid-column: span 1; }} }}
    .kpi-card.followers {{ background: linear-gradient(135deg,rgba(99,102,241,0.15),rgba(139,92,246,0.12)); border-color: rgba(139,92,246,0.3); }}
    .kpi-label {{ font-size: 0.68rem; text-transform: uppercase; color: rgba(255,255,255,0.45); }}
    .kpi-value {{ font-size: 1.35rem; font-weight: 800; margin-top: 0.25rem; }}
    .kpi-card.highlight .kpi-value {{ background: var(--ig-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    .kpi-card.followers .kpi-value {{ color: #a78bfa; }}
    .panel {{ background: var(--surface); border-radius: 18px; padding: 1.15rem; color: var(--text); margin-top: 0.65rem; overflow-x: auto; }}
    .panel h3 {{ font-size: 0.95rem; font-weight: 700; margin-bottom: 0.85rem; }}
    .panel-desc {{ font-size: 0.72rem; color: var(--muted); margin: -0.5rem 0 0.85rem; line-height: 1.45; }}
    .empty-note {{ font-size: 0.78rem; color: var(--muted); }}
    .chart-label {{ display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 0.15rem; }}
    .chart-sub {{ font-size: 0.68rem; color: var(--muted); margin-bottom: 0.35rem; }}
    .chart-bar-bg {{ height: 8px; background: #f0f0f0; border-radius: 4px; overflow: hidden; margin-bottom: 0.65rem; }}
    .chart-bar {{ height: 100%; background: var(--ig-gradient); border-radius: 4px; }}
    .top-item-hot {{
      padding: 0.55rem 0.65rem; margin: 0 -0.65rem 0.5rem; border-radius: 12px;
      background: linear-gradient(90deg, rgba(255,90,40,0.08), rgba(255,40,120,0.06), rgba(255,90,40,0.08));
      background-size: 200% auto; animation: hot-shimmer 4s linear infinite;
      border: 1px solid rgba(255,120,60,0.25);
    }}
    .hot-badge {{
      display: inline-flex; align-items: center; gap: 0.2rem;
      margin-left: 0.35rem; padding: 0.12rem 0.45rem; border-radius: 20px;
      font-size: 0.58rem; font-weight: 800; letter-spacing: 0.04em; text-transform: uppercase;
      color: #ff6b35; background: rgba(255,107,53,0.15); border: 1px solid rgba(255,107,53,0.35);
      animation: hot-glow 2s ease-in-out infinite; vertical-align: middle;
    }}
    .hot-badge-sm {{ font-size: 0.55rem; padding: 0.1rem 0.4rem; }}
    tr.row-hot td {{ background: linear-gradient(90deg, rgba(255,107,53,0.06), transparent); }}
    tr.row-hot td:first-child {{ border-left: 3px solid #ff6b35; }}
    .posts-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem; }}
    @media(min-width:640px){{ .posts-grid{{ grid-template-columns:repeat(3, minmax(0, 1fr)); }} }}
    @media(min-width:900px){{ .posts-grid{{ grid-template-columns:repeat(4, minmax(0, 1fr)); }} }}
    .post-card {{
      position: relative; border-radius: 14px; overflow: hidden;
      border: 1px solid var(--border); background: #fafafa;
      transition: transform 0.2s, box-shadow 0.2s;
    }}
    .post-card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.1); }}
    .post-rank {{
      position: absolute; top: 8px; left: 8px; z-index: 2;
      background: rgba(0,0,0,0.72); color: #fff; font-size: 0.62rem; font-weight: 800;
      padding: 0.2rem 0.45rem; border-radius: 20px;
    }}
    .post-thumb-link {{ display: block; aspect-ratio: 1; background: #efefef; }}
    .post-thumb {{ width: 100%; height: 100%; object-fit: cover; display: block; background: #111; }}
    .post-card-body {{ padding: 0.6rem 0.65rem 0.7rem; }}
    .post-page {{ font-size: 0.72rem; font-weight: 700; line-height: 1.3; }}
    .post-handle {{ font-size: 0.65rem; color: var(--muted); margin-top: 0.1rem; }}
    .post-metrics {{
      display: flex; flex-direction: column; gap: 0.15rem;
      font-size: 0.64rem; color: var(--muted); margin-top: 0.4rem; line-height: 1.35;
    }}
    .post-metrics strong {{ color: var(--text); font-size: 0.78rem; }}
    .post-link {{
      display: inline-block; margin-top: 0.45rem; font-size: 0.65rem; font-weight: 600;
      color: #dc2743 !important;
    }}
    .page-panel {{ display: flex; flex-direction: column; gap: 0.65rem; }}
    .page-block {{
      border: 1px solid var(--border); border-radius: 14px; overflow: hidden; background: #fafafa;
    }}
    .page-block-hot {{ border-color: rgba(255,107,53,0.35); }}
    .page-block summary {{
      cursor: pointer; list-style: none; padding: 0.85rem 1rem;
      display: flex; flex-direction: column; gap: 0.2rem;
      background: #fff; border-bottom: 1px solid transparent;
    }}
    .page-block[open] summary {{ border-bottom-color: var(--border); }}
    .page-block summary::-webkit-details-marker {{ display: none; }}
    .page-block-title {{ font-size: 0.82rem; font-weight: 700; }}
    .page-block-meta {{ font-size: 0.68rem; color: var(--muted); }}
    .page-block .panel-desc {{ padding: 0.65rem 1rem 0; }}
    .post-table {{ width: 100%; border-collapse: collapse; font-size: 0.72rem; min-width: 520px; }}
    .post-table th {{
      text-align: left; padding: 0.5rem 0.65rem; font-size: 0.62rem;
      text-transform: uppercase; color: var(--muted); border-bottom: 2px solid var(--border); background: #fff;
    }}
    .post-table td {{ padding: 0.45rem 0.65rem; border-bottom: 1px solid var(--border); vertical-align: middle; }}
    .post-table tr.post-row-top td {{ background: rgba(220,39,67,0.04); }}
    .post-table tr.post-row-top td:first-child {{ border-left: 3px solid #dc2743; }}
    .post-shortlink {{ font-weight: 600; color: #dc2743 !important; font-size: 0.7rem; }}
    .page-block .post-table {{ margin: 0.5rem 0 0.75rem; }}
    .page-block .empty-note {{ padding: 0.75rem 1rem 1rem; }}
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
      <span class="doc-badge">Páginas Clauth</span>
    </div>
    <h1 class="header-title">{html.escape(titulo)}</h1>
    <p class="header-meta">Engajamento das últimas publicações e seguidores de cada perfil Instagram.</p>
    <div class="info-banner">
      <strong>Como funciona:</strong> o sistema analisa as <strong>últimas {max_posts} publicações</strong> de cada página
      e exibe o total de <strong>seguidores</strong> atual de cada perfil.
      Atualização diária às 08:00 · {r["publicacoes_coletadas"]} publicações analisadas nesta coleta.
    </div>
    <div class="update-pill">{html.escape(updated_at)}</div>
    <a class="back-link" href="/">← Voltar ao acompanhamento</a>
  </header>
  <main>
    <p class="section-title">Resumo geral</p>
    <div class="kpi-grid">
      <div class="kpi-card highlight"><div class="kpi-label">Engajamento total</div><div class="kpi-value">{r["engajamento_fmt"]}</div></div>
      <div class="kpi-card followers"><div class="kpi-label">Seguidores (soma)</div><div class="kpi-value">{r.get("seguidores_fmt", "—")}</div></div>
      <div class="kpi-card"><div class="kpi-label">Publicações</div><div class="kpi-value">{r["publicacoes_coletadas"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Visualizações</div><div class="kpi-value">{r["visualizacoes_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Curtidas</div><div class="kpi-value">{r["curtidas_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Comentários</div><div class="kpi-value">{r["comentarios_fmt"]}</div></div>
    </div>

    <p class="section-title">Top 5 por engajamento</p>
    <div class="panel">{top_html}</div>

    <p class="section-title">Ranking completo · {r["paginas"]} páginas{f' · {hot_count} em tração 🔥' if hot_count else ''}</p>
    <div class="panel">
      <h3>Todas as páginas</h3>
      <p class="panel-desc">Ordenadas por engajamento total nas últimas {max_posts} publicações de cada perfil.</p>
      <table>
        <thead><tr><th>#</th><th>Página</th><th>Seguidores</th><th>Views</th><th>Curtidas</th><th>Coment.</th><th>Total</th><th></th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>

    <p class="section-title">Top {len(top_posts)} publicações (todas as páginas)</p>
    <div class="posts-grid">{top_posts_html}</div>

    <p class="section-title">Últimas {max_posts} publicações por página</p>
    <div class="panel page-panel">{pages_posts_html}</div>
  </main>
  <footer class="app-footer">Clauth Hub · {html.escape(titulo)} · {html.escape(updated_at)}</footer>
</body>
</html>"""


def persist(data: dict, updated_at: str, model: str) -> dict:
    data = normalize_data(data)
    payload = {
        **data,
        "updated_at": datetime.now(timezone(timedelta(hours=-3))).isoformat(),
        "updated_at_fmt": updated_at,
        "model": model,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    HTML_PATH.write_text(render_html(data, updated_at), encoding="utf-8")
    print(f"Publicações no painel: {data['resumo']['publicacoes_coletadas']}")
    print(f"Seguidores (soma): {data['resumo'].get('seguidores_fmt', '—')}")
    return data


def update_daily(model: str, updated_at: str, api_key: str | None = None) -> dict:
    """Atualização rápida diária via cache (sem scrape pesado no CI)."""
    cached = load_cache()
    if cached and (cached.get("resumo") or {}).get("publicacoes_coletadas", 0) >= MIN_CACHE_POSTS:
        data = normalize_data(dict(cached))
        print(
            f"  [diário] Páginas Clauth via cache: {data['resumo']['publicacoes_coletadas']} publicações",
            file=sys.stderr,
        )
        print("  [diário] Atualizando seguidores...", file=sys.stderr)
        data = refresh_followers(data, api_key=api_key, model=model)
        save_cache(data)
    else:
        print("  [diário] Cache insuficiente — coleta completa...", file=sys.stderr)
        return update_and_save(None, model, updated_at)
    return persist(data, updated_at, model)


def update_and_save(api_key: str | None, model: str, updated_at: str) -> dict:
    for p in load_pages():
        print(f"• {p['nome']} (@{p['instagram_handle']})")
    data = compute(api_key, model)
    data = prefer_cache(data)
    save_cache(data)
    return persist(data, updated_at, model)
