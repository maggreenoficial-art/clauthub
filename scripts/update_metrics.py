#!/usr/bin/env python3
"""Atualiza métricas das páginas (seguidores/curtidas) e regenera index.html."""

from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from relatorio_financeiro import update_and_save as update_relatorio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
CONFIG_PATH = ROOT / "config" / "pages.json"
METRICS_PATH = ROOT / "data" / "metrics.json"
INDEX_PATH = ROOT / "index.html"
ENV_PATH = ROOT / ".env"

BOT_UA = "facebookexternalhit/1.1"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

FB_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>'
IG_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/></svg>'


def fetch_og_description(url: str) -> str | None:
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


def parse_number(raw: str) -> int | None:
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


def parse_facebook(desc: str) -> dict:
    result = {"curtidas": None, "falando_sobre": None, "raw": desc}
    m = re.search(r"([\d.,]+[KkMm]?)\s*curtidas", desc, re.I)
    if m:
        result["curtidas"] = parse_number(m.group(1))
    m = re.search(r"([\d.,]+[KkMm]?)\s*falando sobre", desc, re.I)
    if m:
        result["falando_sobre"] = parse_number(m.group(1))
    return result


def parse_instagram(desc: str) -> dict:
    result = {"seguidores": None, "seguindo": None, "posts": None, "raw": desc}
    m = re.search(r"([\d.,]+[KkMm]?)\s*Followers", desc, re.I)
    if m:
        result["seguidores"] = parse_number(m.group(1))
    m = re.search(r"([\d.,]+[KkMm]?)\s*Following", desc, re.I)
    if m:
        result["seguindo"] = parse_number(m.group(1))
    m = re.search(r"([\d.,]+[KkMm]?)\s*Posts", desc, re.I)
    if m:
        result["posts"] = parse_number(m.group(1))
    return result


def format_count(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return f"{n:,}".replace(",", ".")


def openrouter_extract(
    api_key: str,
    model: str,
    page_name: str,
    fb_url: str | None,
    ig_url: str,
) -> dict:
    """Usa OpenRouter + web_fetch quando o parse direto falha."""
    urls = [u for u in [fb_url, ig_url] if u]
    prompt = (
        f"Extraia as métricas públicas da página '{page_name}'.\n"
        f"URLs: {json.dumps(urls)}\n\n"
        "Retorne APENAS JSON válido, sem markdown:\n"
        '{"facebook_curtidas": number|null, "facebook_falando_sobre": number|null, '
        '"instagram_seguidores": number|null, "instagram_seguindo": number|null, '
        '"instagram_posts": number|null}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [
            {
                "type": "openrouter:web_fetch",
                "openrouter:web_fetch": {
                    "engine": "openrouter",
                    "allowed_domains": ["facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com"],
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
            "HTTP-Referer": "https://visucliente.local",
            "X-Title": "Clauth Hub",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    return {
        "curtidas": data.get("facebook_curtidas"),
        "falando_sobre": data.get("facebook_falando_sobre"),
        "seguidores": data.get("instagram_seguidores"),
        "seguindo": data.get("instagram_seguindo"),
        "posts": data.get("instagram_posts"),
        "source": "openrouter",
    }


def collect_metrics(page: dict, api_key: str | None, model: str) -> dict:
    fb_id = page.get("facebook_id")
    ig_handle = page["instagram_handle"]

    fb_url = f"https://www.facebook.com/{fb_id}" if fb_id else None
    ig_url = f"https://www.instagram.com/{ig_handle}/"

    fb_desc = fetch_og_description(fb_url) if fb_url else None
    ig_desc = fetch_og_description(ig_url)

    fb = parse_facebook(fb_desc) if fb_desc else {}
    ig = parse_instagram(ig_desc) if ig_desc else {}

    needs_ai = (
        api_key
        and (
            (fb_url and fb.get("curtidas") is None)
            or ig.get("seguidores") is None
        )
    )

    if needs_ai:
        print(f"  → OpenRouter ({model}) para {page['name']}...")
        try:
            ai = openrouter_extract(api_key, model, page["name"], fb_url, ig_url)
            if fb.get("curtidas") is None:
                fb["curtidas"] = ai.get("curtidas")
            if fb.get("falando_sobre") is None:
                fb["falando_sobre"] = ai.get("falando_sobre")
            if ig.get("seguidores") is None:
                ig["seguidores"] = ai.get("seguidores")
            if ig.get("seguindo") is None:
                ig["seguindo"] = ai.get("seguindo")
            if ig.get("posts") is None:
                ig["posts"] = ai.get("posts")
            source = "http+openrouter"
        except Exception as exc:
            print(f"  [aviso] OpenRouter falhou: {exc}", file=sys.stderr)
            source = "http"
    else:
        source = "http"

    return {
        "id": page["id"],
        "name": page["name"],
        "facebook": {
            "curtidas": fb.get("curtidas"),
            "falando_sobre": fb.get("falando_sobre"),
            "curtidas_fmt": format_count(fb.get("curtidas")),
            "falando_fmt": format_count(fb.get("falando_sobre")),
        },
        "instagram": {
            "seguidores": ig.get("seguidores"),
            "seguindo": ig.get("seguindo"),
            "posts": ig.get("posts"),
            "seguidores_fmt": format_count(ig.get("seguidores")),
            "seguindo_fmt": format_count(ig.get("seguindo")),
            "posts_fmt": format_count(ig.get("posts")),
        },
        "source": source,
    }


def fetch_ig_followers_for_relatorio(
    handle: str,
    name: str,
    api_key: str | None,
    model: str,
    alt_handle: str | None = None,
) -> dict:
    """Busca seguidores Instagram para o relatório financeiro."""
    ig_url = f"https://www.instagram.com/{handle}/"
    ig_desc = fetch_og_description(ig_url)
    ig = parse_instagram(ig_desc) if ig_desc else {}
    seguidores = ig.get("seguidores")
    source = "http"

    if seguidores is None and alt_handle:
        alt_url = f"https://www.instagram.com/{alt_handle}/"
        alt_desc = fetch_og_description(alt_url)
        if alt_desc:
            alt_ig = parse_instagram(alt_desc)
            seguidores = alt_ig.get("seguidores")
            if seguidores is not None:
                source = "http-alt"

    if seguidores is None and api_key:
        try:
            ai = openrouter_extract(api_key, model, name, None, ig_url)
            seguidores = ai.get("seguidores")
            source = "openrouter"
        except Exception as exc:
            print(f"  [aviso] OpenRouter relatório ({name}): {exc}", file=sys.stderr)

    return {"seguidores": seguidores, "source": source}


def make_relatorio_fetcher(api_key: str | None, model: str, config_pages: list) -> callable:
    alt_map = {
        p.get("instagram_handle", ""): p.get("instagram_handle_alt")
        for p in config_pages
        if p.get("instagram_handle_alt")
    }

    def fetcher(handle: str, name: str) -> dict:
        return fetch_ig_followers_for_relatorio(
            handle, name, api_key, model, alt_map.get(handle)
        )

    return fetcher


def render_card(page: dict, metrics: dict, index: int, total: int) -> str:
    m = metrics
    handle = page.get("instagram_handle", "")
    display_handle = f"@{handle}" if handle else (page.get("subtitle") or "")
    initials = "".join(w[0] for w in page["name"].split()[:2]).upper()

    note = ""
    if page.get("note"):
        note = f'<p class="card-note">{html.escape(page["note"])}</p>'

    fb_block = ""
    if page.get("facebook_post"):
        fb_block = f"""
          <div class="platform-block fb-block">
            <div class="platform-label">
              <span class="platform-icon fb-icon">{FB_SVG}</span>
              Facebook
            </div>
            <div class="stat-grid">
              <div class="stat-item">
                <span class="stat-value">{m["facebook"]["curtidas_fmt"]}</span>
                <span class="stat-label">curtidas</span>
              </div>
              <div class="stat-item">
                <span class="stat-value">{m["facebook"]["falando_fmt"]}</span>
                <span class="stat-label">falando sobre</span>
              </div>
            </div>
            <a class="action-btn fb-btn" href="{page["facebook_post"]}" target="_blank" rel="noopener noreferrer">Ver criativo</a>
          </div>"""

    ig_block = f"""
          <div class="platform-block ig-block">
            <div class="platform-label">
              <span class="platform-icon ig-icon">{IG_SVG}</span>
              Instagram
            </div>
            <div class="stat-grid">
              <div class="stat-item">
                <span class="stat-value">{m["instagram"]["seguidores_fmt"]}</span>
                <span class="stat-label">seguidores</span>
              </div>
              <div class="stat-item">
                <span class="stat-value">{m["instagram"]["posts_fmt"]}</span>
                <span class="stat-label">publicações</span>
              </div>
            </div>
            <a class="action-btn ig-btn" href="{page["instagram_post"]}" target="_blank" rel="noopener noreferrer">Ver criativo</a>
          </div>"""

    return f"""
        <article class="store-card" data-index="{index}" aria-label="{html.escape(page["name"])}">
          <div class="card-ring">
            <div class="card-inner">
              <div class="card-top">
                <div class="avatar" aria-hidden="true">{initials}</div>
                <div class="card-identity">
                  <h2>{html.escape(page["name"])}</h2>
                  <span class="handle">{html.escape(display_handle)}</span>
                </div>
                <span class="card-badge">{index + 1}/{total}</span>
              </div>
              {note}
              <div class="platforms">
                {fb_block}
                {ig_block}
              </div>
            </div>
          </div>
        </article>"""


def render_story_item(page: dict, index: int) -> str:
    handle = page.get("instagram_handle", "")
    initials = "".join(w[0] for w in page["name"].split()[:2]).upper()
    short = page["name"].split()[0][:10]
    return f"""
        <button class="story-item" data-goto="{index}" aria-label="{html.escape(page["name"])}">
          <span class="story-ring"><span class="story-avatar">{initials}</span></span>
          <span class="story-name">{html.escape(short)}</span>
        </button>"""


def render_index(pages: list, all_metrics: list, updated_at: str, model: str) -> str:
    total = len(pages)
    cards = "".join(render_card(p, m, i, total) for i, (p, m) in enumerate(zip(pages, all_metrics)))
    stories = "".join(render_story_item(p, i) for i, p in enumerate(pages))
    dots = "".join(f'<span class="dot{" active" if i == 0 else ""}" data-goto="{i}"></span>' for i in range(total))

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Clauth Hub — Acompanhamento Automatizado</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --ig-gradient: linear-gradient(45deg, #f09433 0%, #e6683c 25%, #dc2743 50%, #cc2366 75%, #bc1888 100%);
      --ig-gradient-soft: linear-gradient(135deg, #fdf497 0%, #fd5949 50%, #d6249f 75%, #285AEB 100%);
      --fb: #1877F2;
      --bg: #000000;
      --surface: #ffffff;
      --text: #262626;
      --text-light: #ffffff;
      --muted: #8e8e8e;
      --border: #dbdbdb;
      --ring-size: 3px;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    html {{ scroll-behavior: smooth; }}

    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: var(--bg);
      color: var(--text-light);
      min-height: 100dvh;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }}

    /* ── Header estilo Instagram ── */
    .ig-header {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(0, 0, 0, 0.85);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border-bottom: 1px solid rgba(255,255,255,0.08);
      padding: 0.85rem 1.25rem 1rem;
    }}

    .header-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.65rem;
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    .brand-icon {{
      width: 28px;
      height: 28px;
      border-radius: 8px;
      background: var(--ig-gradient);
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    .brand-icon svg {{ width: 16px; height: 16px; fill: #fff; }}

    .brand-name {{
      font-size: 1.15rem;
      font-weight: 700;
      background: var(--ig-gradient);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      letter-spacing: -0.03em;
    }}

    .live-badge {{
      font-size: 0.65rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 0.25rem 0.55rem;
      border-radius: 20px;
      background: rgba(255,255,255,0.1);
      color: rgba(255,255,255,0.7);
      border: 1px solid rgba(255,255,255,0.12);
    }}

    .header-title {{
      font-size: 1.35rem;
      font-weight: 700;
      color: #fff;
      letter-spacing: -0.02em;
      line-height: 1.25;
    }}

    .header-sub {{
      font-size: 0.82rem;
      color: rgba(255,255,255,0.55);
      margin-top: 0.3rem;
      line-height: 1.4;
    }}

    .header-sub strong {{ color: rgba(255,255,255,0.85); font-weight: 500; }}

    .update-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      margin-top: 0.55rem;
      padding: 0.3rem 0.7rem;
      border-radius: 20px;
      background: rgba(34, 197, 94, 0.15);
      border: 1px solid rgba(34, 197, 94, 0.3);
      font-size: 0.72rem;
      color: #4ade80;
    }}

    .update-pill::before {{
      content: '';
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #4ade80;
      animation: pulse 2s infinite;
    }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.4; }}
    }}

    /* ── Story bar ── */
    .story-bar-wrap {{
      padding: 1rem 0 0.5rem;
      overflow: hidden;
    }}

    .story-bar {{
      display: flex;
      gap: 0.85rem;
      padding: 0 1.25rem;
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      scrollbar-width: none;
      -ms-overflow-style: none;
    }}

    .story-bar::-webkit-scrollbar {{ display: none; }}

    .story-item {{
      flex: 0 0 auto;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.35rem;
      background: none;
      border: none;
      cursor: pointer;
      padding: 0;
      width: 72px;
      transition: transform 0.2s;
    }}

    .story-item:hover {{ transform: scale(1.05); }}
    .story-item.active .story-ring {{ opacity: 1; transform: scale(1.08); }}

    .story-ring {{
      width: 68px;
      height: 68px;
      border-radius: 50%;
      padding: 3px;
      background: var(--ig-gradient);
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0.55;
      transition: opacity 0.25s, transform 0.25s;
    }}

    .story-item.active .story-ring,
    .story-item:hover .story-ring {{ opacity: 1; }}

    .story-avatar {{
      width: 100%;
      height: 100%;
      border-radius: 50%;
      background: #1a1a1a;
      border: 2.5px solid #000;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.75rem;
      font-weight: 700;
      color: #fff;
    }}

    .story-name {{
      font-size: 0.68rem;
      color: rgba(255,255,255,0.75);
      text-align: center;
      max-width: 72px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    /* ── Carousel ── */
    .carousel-section {{
      padding: 0.75rem 0 1.5rem;
      position: relative;
    }}

    .carousel-hint {{
      text-align: center;
      font-size: 0.72rem;
      color: rgba(255,255,255,0.35);
      margin-bottom: 0.85rem;
      letter-spacing: 0.02em;
    }}

    .carousel-viewport {{
      position: relative;
      max-width: 420px;
      margin: 0 auto;
      padding: 0 3rem;
    }}

    .carousel-track {{
      display: flex;
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      scroll-behavior: smooth;
      scrollbar-width: none;
      -ms-overflow-style: none;
      gap: 0;
      border-radius: 20px;
    }}

    .carousel-track::-webkit-scrollbar {{ display: none; }}

    .store-card {{
      flex: 0 0 100%;
      scroll-snap-align: center;
      scroll-snap-stop: always;
      padding: 0.25rem;
    }}

    .card-ring {{
      border-radius: 22px;
      padding: 3px;
      background: var(--ig-gradient);
      box-shadow: 0 8px 32px rgba(188, 24, 136, 0.25), 0 2px 8px rgba(0,0,0,0.4);
    }}

    .card-inner {{
      background: var(--surface);
      border-radius: 19px;
      padding: 1.25rem 1.15rem 1.15rem;
      color: var(--text);
      min-height: 380px;
      display: flex;
      flex-direction: column;
    }}

    .card-top {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 0.85rem;
    }}

    .avatar {{
      width: 48px;
      height: 48px;
      border-radius: 50%;
      background: var(--ig-gradient);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.85rem;
      font-weight: 700;
      color: #fff;
      flex-shrink: 0;
    }}

    .card-identity {{ flex: 1; min-width: 0; }}

    .card-identity h2 {{
      font-size: 1rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .handle {{
      font-size: 0.78rem;
      color: var(--muted);
      display: block;
      margin-top: 0.1rem;
    }}

    .card-badge {{
      font-size: 0.7rem;
      font-weight: 600;
      color: var(--muted);
      background: #f5f5f5;
      padding: 0.2rem 0.5rem;
      border-radius: 10px;
      flex-shrink: 0;
    }}

    .card-note {{
      font-size: 0.72rem;
      color: #b45309;
      background: #fffbeb;
      border: 1px solid #fde68a;
      border-radius: 8px;
      padding: 0.4rem 0.6rem;
      margin-bottom: 0.75rem;
      line-height: 1.35;
    }}

    .platforms {{
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
      flex: 1;
    }}

    .platform-block {{
      border-radius: 14px;
      padding: 0.85rem;
      border: 1px solid var(--border);
    }}

    .fb-block {{ background: #f0f6ff; border-color: #bfdbfe; }}
    .ig-block {{ background: #fff5f8; border-color: #fbcfe8; }}

    .platform-label {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.78rem;
      font-weight: 600;
      margin-bottom: 0.65rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}

    .fb-block .platform-label {{ color: var(--fb); }}
    .ig-block .platform-label {{ color: #c13584; }}

    .platform-icon {{ display: flex; }}
    .platform-icon svg {{ width: 16px; height: 16px; }}
    .fb-icon svg {{ fill: var(--fb); }}
    .ig-icon svg {{ fill: #c13584; }}

    .stat-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }}

    .stat-item {{ text-align: center; }}

    .stat-value {{
      display: block;
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      line-height: 1.2;
    }}

    .stat-label {{
      font-size: 0.68rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}

    .action-btn {{
      display: block;
      text-align: center;
      padding: 0.55rem;
      border-radius: 10px;
      font-size: 0.82rem;
      font-weight: 600;
      text-decoration: none;
      transition: opacity 0.15s, transform 0.15s;
    }}

    .action-btn:hover {{ opacity: 0.9; transform: scale(1.02); }}

    .fb-btn {{ background: var(--fb); color: #fff; }}
    .ig-btn {{
      background: var(--ig-gradient);
      color: #fff;
    }}

    /* ── Navigation ── */
    .nav-btn {{
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      width: 36px;
      height: 36px;
      border-radius: 50%;
      border: 1px solid rgba(255,255,255,0.15);
      background: rgba(255,255,255,0.1);
      backdrop-filter: blur(8px);
      color: #fff;
      font-size: 1.25rem;
      line-height: 1;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background 0.2s, transform 0.2s;
      z-index: 10;
    }}

    .nav-btn:hover {{
      background: rgba(255,255,255,0.2);
      transform: translateY(-50%) scale(1.08);
    }}

    .nav-btn:disabled {{
      opacity: 0.25;
      cursor: default;
      transform: translateY(-50%);
    }}

    .nav-prev {{ left: 0.25rem; }}
    .nav-next {{ right: 0.25rem; }}

    .carousel-footer {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.6rem;
      margin-top: 1rem;
      padding: 0 1.25rem;
    }}

    .dots {{
      display: flex;
      gap: 0.35rem;
      align-items: center;
    }}

    .dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: rgba(255,255,255,0.25);
      border: none;
      cursor: pointer;
      padding: 0;
      transition: all 0.25s;
    }}

    .dot.active {{
      width: 20px;
      border-radius: 3px;
      background: var(--ig-gradient);
    }}

    .page-counter {{
      font-size: 0.75rem;
      color: rgba(255,255,255,0.45);
      font-weight: 500;
    }}

    /* ── Info banner ── */
    .info-banner {{
      margin: 0 1.25rem 0.5rem;
      padding: 0.85rem 1rem;
      border-radius: 14px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
    }}

    .info-banner p {{
      font-size: 0.78rem;
      color: rgba(255,255,255,0.55);
      line-height: 1.5;
    }}

    .info-banner strong {{ color: rgba(255,255,255,0.85); }}

    /* ── Footer ── */
    .app-footer {{
      text-align: center;
      padding: 1.25rem 1.5rem 2rem;
      border-top: 1px solid rgba(255,255,255,0.06);
      margin-top: 0.5rem;
    }}

    .app-footer p {{
      font-size: 0.72rem;
      color: rgba(255,255,255,0.3);
      line-height: 1.6;
    }}

    .meta-logos {{
      display: flex;
      justify-content: center;
      gap: 1rem;
      margin-bottom: 0.5rem;
      opacity: 0.4;
    }}

    .meta-logos svg {{ height: 18px; }}

    @media (max-width: 480px) {{
      .carousel-viewport {{ padding: 0 2.5rem; }}
      .nav-btn {{ width: 32px; height: 32px; font-size: 1.1rem; }}
      .card-inner {{ min-height: 360px; }}
    }}

    @media (min-width: 768px) {{
      .carousel-viewport {{ max-width: 400px; }}
    }}
  </style>
</head>
<body>
  <header class="ig-header">
    <div class="header-top">
      <div class="brand">
        <div class="brand-icon">{IG_SVG}</div>
        <span class="brand-name">Clauth Hub</span>
      </div>
      <span class="live-badge">Auto</span>
    </div>
    <h1 class="header-title">Acompanhamento de Páginas Automatizado</h1>
    <p class="header-sub">
      Métricas de <strong>Facebook</strong> e <strong>Instagram</strong> reunidas em um só lugar.
      Atualização automática <strong>1x por dia</strong>.
    </p>
    <div class="update-pill">Atualização automática 1x por dia · {updated_at}</div>
  </header>

  <div class="story-bar-wrap">
    <div class="story-bar" id="storyBar">
      {stories}
    </div>
  </div>

  <div class="info-banner">
    <p>Deslize para o lado ou use as setas para navegar entre as <strong>{total} páginas</strong>. Toque nos stories acima para ir direto.</p>
  </div>

  <section class="carousel-section" aria-label="Páginas monitoradas">
    <p class="carousel-hint">← deslize para explorar →</p>
    <div class="carousel-viewport">
      <button class="nav-btn nav-prev" id="prevBtn" aria-label="Página anterior">‹</button>
      <div class="carousel-track" id="carousel">
{cards}
      </div>
      <button class="nav-btn nav-next" id="nextBtn" aria-label="Próxima página">›</button>
    </div>
    <div class="carousel-footer">
      <div class="dots" id="dots">{dots}</div>
      <span class="page-counter" id="counter">1 / {total}</span>
    </div>
  </section>

  <footer class="app-footer">
    <div class="meta-logos">
      <svg viewBox="0 0 24 24" fill="#fff"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/></svg>
      <svg viewBox="0 0 24 24" fill="#fff"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>
    </div>
    <p>Clauth Hub · Acompanhamento automatizado de redes sociais<br>
    Dados coletados diariamente às 08:00 · Powered by Meta platforms</p>
  </footer>

  <script>
    (function() {{
      const carousel = document.getElementById('carousel');
      const prevBtn = document.getElementById('prevBtn');
      const nextBtn = document.getElementById('nextBtn');
      const counter = document.getElementById('counter');
      const dots = document.querySelectorAll('.dot');
      const storyItems = document.querySelectorAll('.story-item');
      const total = {total};
      let current = 0;

      function getCardWidth() {{
        return carousel.querySelector('.store-card').offsetWidth;
      }}

      function goTo(index) {{
        current = Math.max(0, Math.min(index, total - 1));
        carousel.scrollTo({{ left: current * getCardWidth(), behavior: 'smooth' }});
        updateUI();
      }}

      function updateUI() {{
        counter.textContent = (current + 1) + ' / ' + total;
        prevBtn.disabled = current === 0;
        nextBtn.disabled = current === total - 1;
        dots.forEach((d, i) => d.classList.toggle('active', i === current));
        storyItems.forEach((s, i) => s.classList.toggle('active', i === current));
        const activeStory = storyItems[current];
        if (activeStory) {{
          activeStory.scrollIntoView({{ behavior: 'smooth', block: 'nearest', inline: 'center' }});
        }}
      }}

      prevBtn.addEventListener('click', () => goTo(current - 1));
      nextBtn.addEventListener('click', () => goTo(current + 1));

      dots.forEach(d => d.addEventListener('click', () => goTo(+d.dataset.goto)));
      storyItems.forEach(s => s.addEventListener('click', () => goTo(+s.dataset.goto)));

      carousel.addEventListener('scroll', () => {{
        const w = getCardWidth();
        if (!w) return;
        const idx = Math.round(carousel.scrollLeft / w);
        if (idx !== current) {{
          current = idx;
          updateUI();
        }}
      }}, {{ passive: true }});

      document.addEventListener('keydown', (e) => {{
        if (e.key === 'ArrowLeft') goTo(current - 1);
        if (e.key === 'ArrowRight') goTo(current + 1);
      }});

      let touchStartX = 0;
      carousel.addEventListener('touchstart', (e) => {{
        touchStartX = e.touches[0].clientX;
      }}, {{ passive: true }});

      carousel.addEventListener('touchend', (e) => {{
        const diff = touchStartX - e.changedTouches[0].clientX;
        if (Math.abs(diff) > 50) {{
          goTo(diff > 0 ? current + 1 : current - 1);
        }}
      }}, {{ passive: true }});

      window.addEventListener('resize', () => goTo(current));
      updateUI();
    }})();
  </script>
</body>
</html>
"""


def regenerate_html_only() -> int:
    if not METRICS_PATH.exists():
        print("metrics.json não encontrado. Rode a atualização completa primeiro.", file=sys.stderr)
        return 1
    payload = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    pages = []
    all_metrics = []
    for entry in payload["pages"]:
        m = entry["metrics"]
        pages.append({k: v for k, v in entry.items() if k != "metrics"})
        all_metrics.append(m)
    INDEX_PATH.write_text(
        render_index(pages, all_metrics, payload["updated_at_fmt"], payload.get("model", "")),
        encoding="utf-8",
    )
    print(f"Página regenerada: {INDEX_PATH}")

    from relatorio_financeiro import RELATORIO_DATA, render_relatorio_html
    if RELATORIO_DATA.exists():
        rel = json.loads(RELATORIO_DATA.read_text(encoding="utf-8"))
        rel_body = {k: v for k, v in rel.items() if k not in ("updated_at", "updated_at_fmt", "model")}
        from relatorio_financeiro import RELATORIO_HTML
        RELATORIO_HTML.write_text(
            render_relatorio_html(rel_body, rel["updated_at_fmt"], rel.get("model", "")),
            encoding="utf-8",
        )
        print(f"Relatório regenerado: {RELATORIO_HTML}")
    return 0


def main() -> int:
    if "--html-only" in sys.argv:
        return regenerate_html_only()
    load_dotenv(ENV_PATH)
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

    if not api_key or api_key == "sua-chave-aqui":
        print("Aviso: OPENROUTER_API_KEY não configurada. Usando apenas busca HTTP.", file=sys.stderr)

    pages = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    print(f"Atualizando {len(pages)} páginas...")

    all_metrics = []
    for page in pages:
        print(f"• {page['name']}")
        all_metrics.append(collect_metrics(page, api_key, model))

    now = datetime.now(timezone.utc).astimezone()
    updated_at = now.strftime("%d/%m/%Y às %H:%M")

    payload = {
        "updated_at": now.isoformat(),
        "updated_at_fmt": updated_at,
        "model": model,
        "pages": [{**p, "metrics": m} for p, m in zip(pages, all_metrics)],
    }

    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    INDEX_PATH.write_text(render_index(pages, all_metrics, updated_at, model), encoding="utf-8")

    print("\n--- Relatório financeiro ---")
    relatorio_config = json.loads((ROOT / "config" / "relatorio_financeiro.json").read_text(encoding="utf-8"))
    fetcher = make_relatorio_fetcher(api_key, model, relatorio_config["paginas"])
    for p in relatorio_config["paginas"]:
        print(f"• {p['nome']} (@{p['instagram_handle']})")
    rel_payload = update_relatorio(fetcher, model, updated_at)
    print(f"Investimento total: {rel_payload['resumo']['investimento_fmt']}")
    print(f"Seguidores totais: {rel_payload['resumo']['seguidores_fmt']}")

    print(f"\nConcluído! Métricas salvas em {METRICS_PATH}")
    print(f"Página atualizada: {INDEX_PATH}")
    print(f"Relatório atualizado: relatoriofinaceiro/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
