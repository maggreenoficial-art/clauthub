"""Coleta e renderização do painel de engajamento Instagram (scraping de publicações)."""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from relatorio_financeiro import collection_label
from instagram_scraper import BROWSER_UA, InstagramScraper, REQUEST_DELAY, DEFAULT_MAX_POSTS

ROOT = Path(__file__).resolve().parent.parent
ENGAJAMENTO_DATA = ROOT / "data" / "engajamento.json"
ENGAJAMENTO_HTML = ROOT / "engajamento" / "index.html"
THUMBS_DIR = ROOT / "engajamento" / "thumbs"
MAX_POSTS_LABEL = DEFAULT_MAX_POSTS


def _post_engagement(post: dict) -> int:
    return post["visualizacoes"] + post["curtidas"] + post["comentarios"]


def _serialize_post(post: dict, pagina: str, handle: str) -> dict:
    eng = _post_engagement(post)
    return {
        "url": post["url"],
        "shortcode": post["shortcode"],
        "thumbnail": post.get("thumbnail") or "",
        "visualizacoes": post["visualizacoes"],
        "curtidas": post["curtidas"],
        "comentarios": post["comentarios"],
        "visualizacoes_fmt": _fmt_num(post["visualizacoes"]),
        "curtidas_fmt": _fmt_num(post["curtidas"]),
        "comentarios_fmt": _fmt_num(post["comentarios"]),
        "engajamento_total": eng,
        "engajamento_fmt": _fmt_num(eng),
        "pagina": pagina,
        "handle": handle,
        "source": post.get("source", ""),
    }


def _build_top_posts(rows: list, limit: int = 12) -> list:
    posts: list[dict] = []
    for row in rows:
        for post in row.get("posts") or []:
            if _post_engagement(post) <= 0:
                continue
            posts.append(_serialize_post(post, row["nome"], row["handle"]))
    posts.sort(key=lambda x: x["engajamento_total"], reverse=True)
    return posts[:limit]


def _backfill_missing_thumbnails(top_posts: list, max_fetch: int = 12) -> None:
    missing = [p for p in top_posts if not p.get("thumbnail")][:max_fetch]
    if not missing:
        return
    scraper = InstagramScraper(delay=0.6)
    for post in missing:
        gql = scraper.fetch_post_graphql(post["shortcode"], referer=post["url"])
        if gql and gql.get("thumbnail"):
            post["thumbnail"] = gql["thumbnail"]


def _local_thumb_path(shortcode: str) -> str:
    return f"/engajamento/thumbs/{shortcode}.jpg"


def _cache_thumbnail(remote_url: str, shortcode: str) -> str:
    """Baixa miniatura do CDN do Instagram para servir localmente (URLs expiram)."""
    if not remote_url or not shortcode:
        return ""
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    dest = THUMBS_DIR / f"{shortcode}.jpg"
    try:
        r = requests.get(
            remote_url,
            headers={"User-Agent": BROWSER_UA, "Referer": "https://www.instagram.com/"},
            timeout=30,
        )
        r.raise_for_status()
        dest.write_bytes(r.content)
        return _local_thumb_path(shortcode)
    except requests.RequestException as exc:
        print(f"  [aviso] Miniatura {shortcode}: {exc}", file=sys.stderr)
        if dest.exists():
            return _local_thumb_path(shortcode)
        return ""


def _cache_post_thumbnails(posts: list) -> None:
    for post in posts:
        shortcode = post.get("shortcode")
        if not shortcode:
            continue
        local = _local_thumb_path(shortcode)
        local_file = THUMBS_DIR / f"{shortcode}.jpg"
        if local_file.exists() and local_file.stat().st_size > 0:
            post["thumbnail_local"] = local
            continue
        remote = post.get("thumbnail") or ""
        if remote.startswith("http"):
            cached = _cache_thumbnail(remote, shortcode)
            if cached:
                post["thumbnail_local"] = cached


def _mark_hot_pages(rows: list) -> None:
    """Marca páginas em tração: top 3, momentum recente ou acima da média."""
    active = [r for r in rows if r["publicacoes_coletadas"] > 0 and r["engajamento_total"] > 0]
    if not active:
        for r in rows:
            r["hot"] = False
            r["momentum_fmt"] = ""
        return

    avg_per_post = sum(r["engajamento_total"] / r["publicacoes_coletadas"] for r in active) / len(active)
    top3_names = {r["nome"] for r in rows[:3]}

    for r in rows:
        if r["publicacoes_coletadas"] == 0:
            r["hot"] = False
            r["momentum_fmt"] = ""
            continue

        posts = r.get("posts") or []
        momentum = 0.0
        if len(posts) >= 6:
            recent = posts[:3]
            older = posts[-3:]
            recent_avg = sum(_post_engagement(p) for p in recent) / len(recent)
            older_avg = sum(_post_engagement(p) for p in older) / len(older)
            if older_avg > 0:
                momentum = recent_avg / older_avg
            elif recent_avg > 0:
                momentum = 2.0

        per_post = r["engajamento_total"] / r["publicacoes_coletadas"]
        r["hot"] = (
            r["nome"] in top3_names
            or momentum >= 1.35
            or per_post >= avg_per_post * 1.4
        )
        r["momentum_fmt"] = f"{momentum:.1f}x" if momentum >= 1.1 else ""


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

    row = {
        "nome": page["name"],
        "handle": f"@{handle}",
        "publicacoes_coletadas": len(profile.publicacoes),
        "visualizacoes": profile.visualizacoes,
        "curtidas": profile.curtidas,
        "comentarios": profile.comentarios,
        "visualizacoes_fmt": _fmt_num(profile.visualizacoes),
        "curtidas_fmt": _fmt_num(profile.curtidas),
        "comentarios_fmt": _fmt_num(profile.comentarios),
        "engajamento_total": profile.visualizacoes + profile.curtidas + profile.comentarios,
        "engajamento_fmt": _fmt_num(profile.visualizacoes + profile.curtidas + profile.comentarios),
        "source": profile.source,
        "posts": [
            {
                "url": p.url,
                "shortcode": p.shortcode,
                "thumbnail": p.thumbnail,
                "visualizacoes": p.visualizacoes,
                "curtidas": p.curtidas,
                "comentarios": p.comentarios,
                "engajamento_total": p.visualizacoes + p.curtidas + p.comentarios,
                "source": p.source,
            }
            for p in profile.publicacoes
        ],
        "melhor_publicacao": None,
    }
    if profile.publicacoes:
        best = max(profile.publicacoes, key=lambda p: p.visualizacoes + p.curtidas + p.comentarios)
        row["melhor_publicacao"] = _serialize_post(
            {
                "url": best.url,
                "shortcode": best.shortcode,
                "thumbnail": best.thumbnail,
                "visualizacoes": best.visualizacoes,
                "curtidas": best.curtidas,
                "comentarios": best.comentarios,
                "source": best.source,
            },
            row["nome"],
            row["handle"],
        )
    return row


def compute_engajamento(
    pages: list,
    all_metrics: list,
    api_key: str | None,
    model: str,
) -> dict:
    scraper = InstagramScraper(delay=REQUEST_DELAY)
    rows = [collect_page_engagement(p, scraper, api_key, model) for p in pages]
    rows.sort(key=lambda x: x["engajamento_total"], reverse=True)
    _mark_hot_pages(rows)

    sum_views = sum(r["visualizacoes"] for r in rows)
    sum_likes = sum(r["curtidas"] for r in rows)
    sum_comments = sum(r["comentarios"] for r in rows)
    sum_posts = sum(r["publicacoes_coletadas"] for r in rows)
    total = sum_views + sum_likes + sum_comments
    media = total // len(rows) if rows else 0
    hot_count = sum(1 for r in rows if r.get("hot"))

    top5 = [r for r in rows if r["engajamento_total"] > 0][:5]
    max_eng = top5[0]["engajamento_total"] if top5 else 1
    top_posts = _build_top_posts(rows, limit=12)
    _backfill_missing_thumbnails(top_posts)
    _cache_post_thumbnails(top_posts)

    return {
        "titulo": "Engajamento Instagram",
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


def _render_post_card(post: dict, rank: int | None = None) -> str:
    link = html.escape(post["url"])
    thumb_src = post.get("thumbnail_local") or post.get("thumbnail") or ""
    rank_badge = f'<span class="post-rank">#{rank}</span>' if rank else ""
    if thumb_src:
        thumb_html = (
            f'<a href="{link}" target="_blank" rel="noopener noreferrer" class="post-thumb-link">'
            f'<img class="post-thumb" src="{html.escape(thumb_src)}" '
            f'referrerpolicy="no-referrer" '
            f'alt="Publicação {html.escape(post["pagina"])}" loading="lazy">'
            f"</a>"
        )
    else:
        thumb_html = (
            f'<a href="{link}" target="_blank" rel="noopener noreferrer" class="post-thumb-link post-thumb-empty">'
            f'<span>📷</span></a>'
        )
    return f"""
    <article class="post-card">
      {rank_badge}
      {thumb_html}
      <div class="post-card-body">
        <div class="post-page">{html.escape(post["pagina"])}</div>
        <div class="post-handle">{html.escape(post["handle"])}</div>
        <div class="post-metrics">
          <span><strong>{post["engajamento_fmt"]}</strong> total</span>
          <span>{post["visualizacoes_fmt"]} views · {post["curtidas_fmt"]} ❤ · {post["comentarios_fmt"]} 💬</span>
        </div>
        <a class="post-link" href="{link}" target="_blank" rel="noopener noreferrer">Ver no Instagram →</a>
      </div>
    </article>"""


def render_engajamento_html(data: dict, updated_at: str) -> str:
    if data["paginas"] and "hot" not in data["paginas"][0]:
        _mark_hot_pages(data["paginas"])

    top_posts = data.get("top_posts") or _build_top_posts(data["paginas"], limit=12)
    _backfill_missing_thumbnails(top_posts)
    _cache_post_thumbnails(top_posts)

    max_posts = data.get("max_posts_por_pagina", MAX_POSTS_LABEL)
    hot_count = sum(1 for p in data["paginas"] if p.get("hot"))

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
          <div class="chart-sub">últimas {p["publicacoes_coletadas"]} publicações · {p["visualizacoes_fmt"]} views · {p["curtidas_fmt"]} curtidas · {p["comentarios_fmt"]} coment.</div>
          <div class="chart-bar-bg"><div class="chart-bar" style="width:{pct}%"></div></div>
        </div>"""

    if not top_html:
        top_html = '<p class="empty-note">Nenhuma publicação raspada nesta execução. Tente novamente ou verifique os perfis.</p>'

    top_posts_html = ""
    if top_posts:
        top_posts_html = "".join(_render_post_card(p, i) for i, p in enumerate(top_posts, 1))
    else:
        top_posts_html = '<p class="empty-note">Nenhuma publicação com engajamento registrado.</p>'

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
    @media(min-width:900px){{ .kpi-grid{{ grid-template-columns:repeat(5,1fr); }} }}
    .kpi-card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 1rem; }}
    .kpi-card.highlight {{ background: linear-gradient(135deg,rgba(240,148,51,0.15),rgba(188,24,136,0.15)); border-color: rgba(188,24,136,0.3); grid-column: span 2; }}
    @media(min-width:900px){{ .kpi-card.highlight{{ grid-column: span 1; }} }}
    .kpi-label {{ font-size: 0.68rem; text-transform: uppercase; color: rgba(255,255,255,0.45); }}
    .kpi-value {{ font-size: 1.35rem; font-weight: 800; margin-top: 0.25rem; }}
    .kpi-card.highlight .kpi-value {{ background: var(--ig-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
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
      background-size: 200% auto;
      animation: hot-shimmer 4s linear infinite;
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
    tr.row-hot td {{
      background: linear-gradient(90deg, rgba(255,107,53,0.06), transparent);
    }}
    tr.row-hot td:first-child {{ border-left: 3px solid #ff6b35; }}
    .posts-grid {{
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem;
    }}
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
    .post-thumb {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .post-thumb-empty {{
      display: flex; align-items: center; justify-content: center;
      height: 100%; min-height: 120px; font-size: 2rem; color: var(--muted);
    }}
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
    table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; min-width: 640px; }}
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
    <p class="header-meta">Métricas consolidadas das publicações recentes de cada perfil.</p>
    <div class="info-banner">
      <strong>Como funciona:</strong> o sistema analisa automaticamente as <strong>últimas {max_posts} publicações</strong> de cada página no Instagram
      e soma visualizações, curtidas e comentários para medir o engajamento real.
      Atualização diária às 08:00 · {r["publicacoes_coletadas"]} publicações analisadas nesta coleta.
    </div>
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
    </div>
    <p class="section-title">Top 5 páginas{f' · {hot_count} em tração 🔥' if hot_count else ''}</p>
    <div class="panel"><h3>Maior engajamento nas últimas {max_posts} publicações</h3>{top_html}</div>
    <p class="section-title">O que está funcionando · top publicações</p>
    <div class="panel">
      <h3>Publicações com maior engajamento para análise de conteúdo</h3>
      <p class="panel-desc">Clique na miniatura ou no link para abrir no Instagram e ver o que performou melhor.</p>
      <div class="posts-grid">{top_posts_html}</div>
    </div>
    <p class="section-title">Todas as páginas ({r["paginas"]})</p>
    <div class="panel">
      <table>
        <thead><tr><th>#</th><th>Página</th><th>Views</th><th>Curtidas</th><th>Coment.</th><th>Total</th><th></th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </main>
  <footer class="app-footer">Clauth Hub · Últimas {max_posts} publicações por perfil · views, curtidas e comentários</footer>
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
