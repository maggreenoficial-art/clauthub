#!/usr/bin/env python3
"""Atualiza métricas das páginas (seguidores/curtidas) e regenera index.html."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from engajamento import update_and_save, update_engajamento_daily
from paginas_clauth import update_and_save as update_paginas_clauth, update_daily as update_paginas_clauth_daily
from relatorio_financeiro import COLLECTION_TZ, collection_label, update_and_save as update_relatorio
from foco_verba import update_and_save as update_foco_verba

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
CONFIG_PATH = ROOT / "config" / "pages.json"
METRICS_PATH = ROOT / "data" / "metrics.json"
METRICS_CACHE_PATH = ROOT / "config" / "metrics_cache.json"
INDEX_PATH = ROOT / "index.html"
ENV_PATH = ROOT / ".env"

BOT_UA = "facebookexternalhit/1.1"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
IG_APP_ID = "936619743392459"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PAGE_FETCH_DELAY = 2.5

FB_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>'
IG_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/></svg>'


def _clean_api_key(api_key: str | None) -> str | None:
    if not api_key or api_key.strip() == "sua-chave-aqui":
        return None
    # Remove newlines/espaços que quebram o header Authorization no requests
    cleaned = api_key.strip().replace("\r", "").replace("\n", "").strip()
    return cleaned or None


def load_metrics_cache() -> dict[int, dict]:
    if not METRICS_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(METRICS_CACHE_PATH.read_text(encoding="utf-8"))
        return {int(k): v for k, v in data.get("pages", {}).items()}
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return {}


def save_metrics_cache(all_metrics: list[dict]) -> None:
    payload = {
        "updated_at": datetime.now(COLLECTION_TZ).isoformat(),
        "pages": {str(m["id"]): m for m in all_metrics},
    }
    METRICS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_section(section: str, new: dict, old: dict | None) -> dict:
    merged = dict(new.get(section) or {})
    prev = (old or {}).get(section) or {}
    for key, value in merged.items():
        if value is None and prev.get(key) is not None:
            merged[key] = prev[key]
    if section == "facebook":
        merged["curtidas_fmt"] = format_count(merged.get("curtidas"))
        merged["falando_fmt"] = format_count(merged.get("falando_sobre"))
    elif section == "instagram":
        merged["seguidores_fmt"] = format_count(merged.get("seguidores"))
        merged["seguindo_fmt"] = format_count(merged.get("seguindo"))
        merged["posts_fmt"] = format_count(merged.get("posts"))
    return merged


def merge_with_cache(metrics: dict, page_id: int, cache: dict[int, dict]) -> dict:
    old = cache.get(page_id)
    if not old:
        return metrics
    merged = dict(metrics)
    merged["facebook"] = _merge_section("facebook", metrics, old)
    merged["instagram"] = _merge_section("instagram", metrics, old)
    if metrics.get("source") == "http" and old.get("source") not in (None, "http"):
        merged["source"] = f"{metrics.get('source', 'http')}+cache"
    return merged


def fetch_instagram_api_metrics(handle: str) -> dict:
    """Fallback via API interna quando og:description falha (429)."""
    handle = handle.lstrip("@")
    profile_url = f"https://www.instagram.com/{handle}/"
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    try:
        session.get(profile_url, timeout=25)
        csrf = session.cookies.get("csrftoken", "")
        r = session.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}",
            headers={
                "X-IG-App-ID": IG_APP_ID,
                "X-CSRFToken": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": profile_url,
            },
            timeout=25,
        )
        if r.status_code == 429:
            time.sleep(3)
            return {}
        if r.status_code != 200:
            return {}
        user = r.json().get("data", {}).get("user", {})
        return {
            "seguidores": user.get("edge_followed_by", {}).get("count"),
            "seguindo": user.get("edge_follow", {}).get("count"),
            "posts": user.get("edge_owner_to_timeline_media", {}).get("count"),
        }
    except (requests.RequestException, json.JSONDecodeError, AttributeError, TypeError) as exc:
        print(f"  [aviso] API Instagram @{handle}: {exc}", file=sys.stderr)
        return {}


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
    for key, patterns in (
        ("seguidores", (r"([\d.,]+[KkMm]?)\s*Followers", r"([\d.,]+[KkMm]?)\s*followers", r"([\d.,]+[KkMm]?)\s*seguidores")),
        ("seguindo", (r"([\d.,]+[KkMm]?)\s*Following", r"([\d.,]+[KkMm]?)\s*following", r"([\d.,]+[KkMm]?)\s*seguindo")),
        ("posts", (r"([\d.,]+[KkMm]?)\s*Posts", r"([\d.,]+[KkMm]?)\s*posts", r"([\d.,]+[KkMm]?)\s*publicações", r"([\d.,]+[KkMm]?)\s*publicacoes")),
    ):
        if result[key] is not None:
            continue
        for pat in patterns:
            m = re.search(pat, desc, re.I)
            if m:
                result[key] = parse_number(m.group(1))
                break
    return result


def format_count(n: int | None) -> str:
    """Formata contagens: exato até 999.999; a partir de 1M usa abreviação."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{n:,}".replace(",", ".")


def openrouter_extract(
    api_key: str,
    model: str,
    page_name: str,
    fb_url: str | None,
    ig_url: str,
) -> dict:
    """Usa OpenRouter + web_fetch quando o parse direto falha."""
    key = _clean_api_key(api_key)
    if not key:
        raise ValueError("OPENROUTER_API_KEY inválida ou vazia")
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
            "Authorization": f"Bearer {key}",
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


def _facebook_fetch_urls(page: dict) -> list[str]:
    urls: list[str] = []
    fb_id = page.get("facebook_id")
    if fb_id:
        urls.append(f"https://www.facebook.com/{fb_id}")
    post = page.get("facebook_post") or ""
    if "facebook.com/" in post and post not in urls:
        urls.append(post)
    return urls


def _fetch_facebook_metrics(page: dict) -> dict:
    fb = {"curtidas": None, "falando_sobre": None}
    for url in _facebook_fetch_urls(page):
        desc = fetch_og_description(url)
        if not desc:
            continue
        parsed = parse_facebook(desc)
        for key in ("curtidas", "falando_sobre"):
            if fb.get(key) is None and parsed.get(key) is not None:
                fb[key] = parsed[key]
        if fb.get("curtidas") is not None:
            break
    return fb


def collect_metrics(page: dict, api_key: str | None, model: str, cache: dict[int, dict] | None = None) -> dict:
    from instagram_scraper import InstagramScraper

    ig_handle = page["instagram_handle"]
    ig_url = f"https://www.instagram.com/{ig_handle}/"
    fb_urls = _facebook_fetch_urls(page)
    fb_url = fb_urls[0] if fb_urls else None

    ig_desc = fetch_og_description(ig_url)
    ig = parse_instagram(ig_desc) if ig_desc else {}
    fb = _fetch_facebook_metrics(page)
    source = "http"

    if ig.get("seguidores") is None:
        api_ig = fetch_instagram_api_metrics(ig_handle)
        for key in ("seguidores", "seguindo", "posts"):
            if ig.get(key) is None and api_ig.get(key) is not None:
                ig[key] = api_ig[key]
        if api_ig.get("seguidores") is not None:
            source = "api"

    if ig.get("seguidores") is None:
        followers = InstagramScraper(delay=0.3).fetch_profile_followers(ig_handle)
        if followers is not None:
            ig["seguidores"] = followers
            source = "scraper" if source == "http" else source

    needs_ai = api_key and (fb.get("curtidas") is None or ig.get("seguidores") is None)

    if needs_ai:
        print(f"  -> OpenRouter ({model}) para {page['name']}...")
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
            source = "http+openrouter" if source == "http" else f"{source}+openrouter"
        except Exception as exc:
            print(f"  [aviso] OpenRouter falhou: {exc}", file=sys.stderr)

    metrics = {
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
    if cache is not None:
        metrics = merge_with_cache(metrics, page["id"], cache)
    return metrics


def build_followers_lookup(
    relatorio_pages: list,
    hub_pages: list,
    cache: dict[int, dict],
) -> dict[str, int]:
    """Mapeia handle Instagram → seguidores do cache do hub principal."""
    hub_by_handle = {
        p["instagram_handle"].lstrip("@"): p["id"]
        for p in hub_pages
        if p.get("instagram_handle")
    }
    lookup: dict[str, int] = {}
    for p in relatorio_pages:
        handle = (p.get("instagram_handle") or "").lstrip("@")
        alt = (p.get("instagram_handle_alt") or "").lstrip("@")
        for h in (handle, alt):
            if not h:
                continue
            page_id = hub_by_handle.get(h)
            if page_id is None:
                continue
            seg = (cache.get(page_id) or {}).get("instagram", {}).get("seguidores")
            if seg is not None:
                lookup[handle] = seg
    return lookup


def fetch_ig_followers_for_relatorio(
    handle: str,
    name: str,
    api_key: str | None,
    model: str,
    alt_handle: str | None = None,
    cached_seguidores: int | None = None,
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

    if seguidores is None:
        api_ig = fetch_instagram_api_metrics(handle)
        seguidores = api_ig.get("seguidores")
        if seguidores is not None:
            source = "api"

    if seguidores is None and api_key:
        try:
            ai = openrouter_extract(api_key, model, name, None, ig_url)
            seguidores = ai.get("seguidores")
            source = "openrouter"
        except Exception as exc:
            print(f"  [aviso] OpenRouter relatório ({name}): {exc}", file=sys.stderr)

    if seguidores is None and cached_seguidores is not None:
        seguidores = cached_seguidores
        source = "cache"

    return {"seguidores": seguidores, "source": source}


def make_relatorio_fetcher(
    api_key: str | None,
    model: str,
    config_pages: list,
    followers_lookup: dict[str, int] | None = None,
) -> callable:
    alt_map = {
        p.get("instagram_handle", ""): p.get("instagram_handle_alt")
        for p in config_pages
        if p.get("instagram_handle_alt")
    }

    lookup = followers_lookup or {}

    def fetcher(handle: str, name: str) -> dict:
        h = handle.lstrip("@")
        return fetch_ig_followers_for_relatorio(
            handle,
            name,
            api_key,
            model,
            alt_map.get(handle),
            lookup.get(h),
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


def build_followers_summary(pages: list, all_metrics: list) -> dict:
    items: list[dict] = []
    total = 0
    com_dados = 0
    for p, m in zip(pages, all_metrics):
        seg = m.get("instagram", {}).get("seguidores")
        if seg is not None:
            seg = int(seg)
            total += seg
            com_dados += 1
        items.append({
            "nome": p["name"],
            "handle": f"@{p.get('instagram_handle', '')}",
            "seguidores": seg,
            "seguidores_fmt": format_count(seg) if seg is not None else "—",
        })
    items.sort(key=lambda x: x["seguidores"] or 0, reverse=True)
    return {
        "total": total,
        "total_fmt": format_count(total) if total else "—",
        "paginas_com_dados": com_dados,
        "paginas_total": len(pages),
        "items": items,
    }


def render_followers_section(summary: dict) -> str:
    items = summary["items"]
    if not items:
        return ""

    with_data = [i for i in items if i["seguidores"]]
    max_seg = with_data[0]["seguidores"] if with_data else 1
    rows = ""
    for i, item in enumerate(items, 1):
        seg = item["seguidores"] or 0
        pct = min(100, int(seg / max_seg * 100)) if seg and max_seg else 0
        rows += f"""
        <div class="followers-row">
          <div class="followers-row-head">
            <span class="followers-rank">{i}</span>
            <div class="followers-meta">
              <span class="followers-name">{html.escape(item["nome"])}</span>
              <span class="followers-handle">{html.escape(item["handle"])}</span>
            </div>
            <span class="followers-count">{item["seguidores_fmt"]}</span>
          </div>
          <div class="followers-bar-bg"><div class="followers-bar" style="width:{pct}%"></div></div>
        </div>"""

    return f"""
  <section class="followers-section" aria-label="Seguidores Instagram">
    <div class="followers-panel">
      <div class="followers-header">
        <div>
          <p class="followers-kicker">Instagram</p>
          <h2 class="followers-title">Seguidores por página</h2>
          <p class="followers-desc">Soma de todas as {summary["paginas_total"]} páginas monitoradas nesta coleta.</p>
        </div>
        <div class="followers-total-card">
          <span class="followers-total-label">Total de seguidores</span>
          <span class="followers-total-value">{summary["total_fmt"]}</span>
          <span class="followers-total-sub">{summary["paginas_com_dados"]}/{summary["paginas_total"]} com dados</span>
        </div>
      </div>
      <div class="followers-list">{rows}</div>
    </div>
  </section>"""


def sort_pages_for_index(pages: list, all_metrics: list) -> tuple[list, list]:
    """Ordena o carrossel da página inicial por seguidores no Instagram (maior → menor)."""
    paired = sorted(
        zip(pages, all_metrics),
        key=lambda pm: pm[1].get("instagram", {}).get("seguidores") or 0,
        reverse=True,
    )
    if not paired:
        return pages, all_metrics
    sorted_pages, sorted_metrics = zip(*paired)
    return list(sorted_pages), list(sorted_metrics)


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
    followers_html = render_followers_section(build_followers_summary(pages, all_metrics))

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

    .header-actions {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
      margin-top: 0.65rem;
    }}

    .btn-engajamento {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.45rem 0.9rem;
      border-radius: 10px;
      background: var(--ig-gradient);
      color: #fff;
      font-size: 0.75rem;
      font-weight: 600;
      text-decoration: none;
      border: none;
      transition: opacity 0.15s, transform 0.15s;
    }}

    .btn-engajamento:hover {{
      opacity: 0.92;
      transform: translateY(-1px);
    }}

    .btn-engajamento svg {{
      width: 14px;
      height: 14px;
      fill: currentColor;
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

    /* ── Seguidores ── */
    .followers-section {{
      padding: 0 1.25rem 1rem;
      max-width: 720px;
      margin: 0 auto;
    }}

    .followers-panel {{
      background: var(--surface);
      color: var(--text);
      border-radius: 18px;
      padding: 1.1rem 1.15rem 1.2rem;
      border: 1px solid rgba(255,255,255,0.08);
      box-shadow: 0 8px 32px rgba(0,0,0,0.35);
    }}

    .followers-header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;
      margin-bottom: 1rem;
    }}

    .followers-kicker {{
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 0.25rem;
    }}

    .followers-title {{
      font-size: 1rem;
      font-weight: 700;
      margin-bottom: 0.25rem;
    }}

    .followers-desc {{
      font-size: 0.72rem;
      color: var(--muted);
      line-height: 1.45;
      max-width: 22rem;
    }}

    .followers-total-card {{
      text-align: right;
      padding: 0.75rem 1rem;
      border-radius: 14px;
      background: linear-gradient(135deg, rgba(240,148,51,0.12), rgba(188,24,136,0.1));
      border: 1px solid rgba(188,24,136,0.2);
      min-width: 9rem;
    }}

    .followers-total-label {{
      display: block;
      font-size: 0.62rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
    }}

    .followers-total-value {{
      display: block;
      font-size: 1.65rem;
      font-weight: 800;
      line-height: 1.2;
      margin: 0.2rem 0;
      background: var(--ig-gradient);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}

    .followers-total-sub {{
      font-size: 0.65rem;
      color: var(--muted);
    }}

    .followers-list {{
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }}

    .followers-row-head {{
      display: flex;
      align-items: center;
      gap: 0.55rem;
      margin-bottom: 0.3rem;
    }}

    .followers-rank {{
      width: 1.35rem;
      font-size: 0.68rem;
      font-weight: 700;
      color: var(--muted);
      flex-shrink: 0;
    }}

    .followers-meta {{
      flex: 1;
      min-width: 0;
    }}

    .followers-name {{
      display: block;
      font-size: 0.78rem;
      font-weight: 600;
      line-height: 1.3;
    }}

    .followers-handle {{
      display: block;
      font-size: 0.65rem;
      color: var(--muted);
    }}

    .followers-count {{
      font-size: 0.82rem;
      font-weight: 700;
      flex-shrink: 0;
    }}

    .followers-bar-bg {{
      height: 7px;
      background: #f0f0f0;
      border-radius: 4px;
      overflow: hidden;
      margin-left: 1.9rem;
    }}

    .followers-bar {{
      height: 100%;
      background: var(--ig-gradient);
      border-radius: 4px;
      min-width: 2px;
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
    <div class="header-actions">
      <div class="update-pill">{updated_at}</div>
      <a href="/engajamento" class="btn-engajamento" aria-label="Ver engajamento das páginas">
        <svg viewBox="0 0 24 24"><path d="M16 6l2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></svg>
        Engajamento Instagram
      </a>
    </div>
  </header>

  <div class="story-bar-wrap">
    <div class="story-bar" id="storyBar">
      {stories}
    </div>
  </div>

  <div class="info-banner">
    <p>Deslize para o lado ou use as setas para navegar entre as <strong>{total} páginas</strong>. Toque nos stories acima para ir direto.</p>
  </div>

  {followers_html}

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
    pages, all_metrics = sort_pages_for_index(pages, all_metrics)
    updated_at = payload.get("updated_at_fmt")
    if payload.get("updated_at"):
        updated_at = collection_label(datetime.fromisoformat(payload["updated_at"]))
    INDEX_PATH.write_text(
        render_index(pages, all_metrics, updated_at, payload.get("model", "")),
        encoding="utf-8",
    )
    print(f"Página regenerada: {INDEX_PATH}")

    from relatorio_financeiro import RELATORIO_DATA, render_relatorio_html
    if RELATORIO_DATA.exists():
        rel = json.loads(RELATORIO_DATA.read_text(encoding="utf-8"))
        rel_body = {k: v for k, v in rel.items() if k not in ("updated_at", "updated_at_fmt", "model")}
        from relatorio_financeiro import RELATORIO_HTML
        rel_updated = rel.get("updated_at_fmt", "")
        if rel.get("updated_at"):
            rel_updated = collection_label(datetime.fromisoformat(rel["updated_at"]))
        RELATORIO_HTML.write_text(
            render_relatorio_html(rel_body, rel_updated, rel.get("model", "")),
            encoding="utf-8",
        )
        print(f"Relatório regenerado: {RELATORIO_HTML}")

    from engajamento import ENGAJAMENTO_DATA, ENGAJAMENTO_HTML, render_engajamento_html
    if ENGAJAMENTO_DATA.exists():
        eng = json.loads(ENGAJAMENTO_DATA.read_text(encoding="utf-8"))
        eng_body = {k: v for k, v in eng.items() if k not in ("updated_at", "updated_at_fmt", "model")}
        eng_updated = eng.get("updated_at_fmt", updated_at)
        if eng.get("updated_at"):
            eng_updated = collection_label(datetime.fromisoformat(eng["updated_at"]))
        ENGAJAMENTO_HTML.write_text(render_engajamento_html(eng_body, eng_updated), encoding="utf-8")
        print(f"Engajamento regenerado: {ENGAJAMENTO_HTML}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--html-only" in sys.argv:
        return regenerate_html_only()
    load_dotenv(ENV_PATH)
    api_key = _clean_api_key(os.getenv("OPENROUTER_API_KEY"))
    model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

    if not api_key:
        print("Aviso: OPENROUTER_API_KEY não configurada. Usando apenas busca HTTP/API.", file=sys.stderr)

    pages = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    metrics_cache = load_metrics_cache()
    print(f"Atualizando {len(pages)} páginas...")

    all_metrics = []
    for page in pages:
        print(f"• {page['name']}")
        all_metrics.append(collect_metrics(page, api_key, model, metrics_cache))
        time.sleep(PAGE_FETCH_DELAY)

    now = datetime.now(COLLECTION_TZ)
    updated_at = collection_label(now)

    payload = {
        "updated_at": now.isoformat(),
        "updated_at_fmt": updated_at,
        "model": model,
        "pages": [{**p, "metrics": m} for p, m in zip(pages, all_metrics)],
    }

    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    save_metrics_cache(all_metrics)
    index_pages, index_metrics = sort_pages_for_index(pages, all_metrics)
    INDEX_PATH.write_text(render_index(index_pages, index_metrics, updated_at, model), encoding="utf-8")

    print("\n--- Relatório financeiro ---")
    relatorio_config = json.loads((ROOT / "config" / "relatorio_financeiro.json").read_text(encoding="utf-8"))
    followers_lookup = build_followers_lookup(relatorio_config["paginas"], pages, metrics_cache)
    fetcher = make_relatorio_fetcher(api_key, model, relatorio_config["paginas"], followers_lookup)
    for p in relatorio_config["paginas"]:
        print(f"• {p['nome']} (@{p['instagram_handle']})")
    rel_payload = update_relatorio(fetcher, model, updated_at)
    print(f"Investimento total: {rel_payload['resumo']['investimento_fmt']}")
    print(f"Seguidores totais: {rel_payload['resumo']['seguidores_fmt']}")

    print("\n--- Foco Verba ---")
    try:
        foco_payload = update_foco_verba(updated_at)
        print(f"Páginas foco: {foco_payload['resumo']['paginas']}")
        print(f"Verba total: {foco_payload['resumo']['investimento_fmt']}")
    except Exception as exc:
        print(f"[erro] Foco Verba não atualizado: {exc}", file=sys.stderr)

    full_scrape = os.getenv("ENGAJAMENTO_SCRAPE_COMPLETO", "").strip().lower() in ("1", "true", "yes")

    print("\n--- Páginas Clauth ---")
    try:
        if os.getenv("GITHUB_ACTIONS") == "true" and not full_scrape:
            clauth_payload = update_paginas_clauth_daily(model, updated_at, api_key)
        else:
            clauth_payload = update_paginas_clauth(api_key, model, updated_at)
        print(f"Engajamento Clauth: {clauth_payload['resumo']['engajamento_fmt']}")
        print(f"Seguidores (soma): {clauth_payload['resumo'].get('seguidores_fmt', '—')}")
    except Exception as exc:
        print(f"[erro] Páginas Clauth não atualizadas: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        try:
            from paginas_clauth import load_cache, normalize_data, persist
            cached = load_cache()
            if cached:
                persist(normalize_data(dict(cached)), updated_at, model)
                print("[recuperação] Páginas Clauth restauradas do cache com data atualizada.")
        except Exception as exc2:
            print(f"[erro] Recuperação Páginas Clauth falhou: {exc2}", file=sys.stderr)

    print("\n--- Engajamento das páginas ---")
    for p in pages:
        print(f"• {p['name']}")
    try:
        if os.getenv("GITHUB_ACTIONS") == "true" and not full_scrape:
            eng_payload = update_engajamento_daily(pages, all_metrics, api_key, model, updated_at)
        else:
            eng_payload = update_and_save(pages, all_metrics, api_key, model, updated_at)
        print(f"Engajamento total: {eng_payload['resumo']['engajamento_fmt']}")
    except Exception as exc:
        print(f"[erro] Engajamento não atualizado: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        # Último recurso: só atualiza a data no HTML existente
        try:
            from engajamento import ENGAJAMENTO_HTML, load_engajamento_cache, _normalize_engajamento_data, _persist_engajamento
            cached = load_engajamento_cache()
            if cached:
                _persist_engajamento(_normalize_engajamento_data(dict(cached)), updated_at, model)
                print("[recuperação] Engajamento restaurado do cache com data atualizada.")
        except Exception as exc2:
            print(f"[erro] Recuperação engajamento falhou: {exc2}", file=sys.stderr)

    print(f"\nConcluído! Métricas salvas em {METRICS_PATH}")
    print(f"Página atualizada: {INDEX_PATH}")
    print(f"Relatório atualizado: relatoriofinaceiro/index.html")
    print(f"Engajamento atualizado: engajamento/index.html")
    print(f"Páginas Clauth atualizadas: paginasclauth/index.html")
    print(f"Foco Verba atualizado: focoverba/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
