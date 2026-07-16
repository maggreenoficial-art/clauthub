"""Coleta e renderização do painel de engajamento Instagram (scraping de publicações)."""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from relatorio_financeiro import collection_label
from instagram_scraper import BROWSER_UA, InstagramScraper, REQUEST_DELAY, DEFAULT_MAX_POSTS, OPENROUTER_URL

ROOT = Path(__file__).resolve().parent.parent
ENGAJAMENTO_DATA = ROOT / "data" / "engajamento.json"
ENGAJAMENTO_CACHE_PATH = ROOT / "config" / "engajamento_cache.json"
ENGAJAMENTO_HTML = ROOT / "engajamento" / "index.html"
THUMBS_DIR = ROOT / "engajamento" / "thumbs"
DEFAULT_THUMB = "/engajamento/thumbs/clauth-default.svg"
MAX_POSTS_LABEL = DEFAULT_MAX_POSTS
TOP_POSTS_GLOBAL = 24


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


def _build_top_posts(rows: list, limit: int = TOP_POSTS_GLOBAL) -> list:
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
        if len(posts) >= 10:
            recent = posts[:5]
            older = posts[-5:]
        elif len(posts) >= 6:
            recent = posts[:3]
            older = posts[-3:]
        else:
            recent = []
            older = []
        if recent and older:
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


def _parse_fmt_num(raw: str) -> int:
    raw = (raw or "").strip().replace(".", "").replace(",", ".")
    if not raw:
        return 0
    mult = 1
    if raw.upper().endswith("K"):
        mult = 1_000
        raw = raw[:-1]
    elif raw.upper().endswith("M"):
        mult = 1_000_000
        raw = raw[:-1]
    try:
        return int(float(raw) * mult)
    except ValueError:
        return 0


def load_engajamento_cache() -> dict | None:
    if not ENGAJAMENTO_CACHE_PATH.exists():
        return None
    try:
        return json.loads(ENGAJAMENTO_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_engajamento_cache(data: dict) -> None:
    if (data.get("resumo") or {}).get("publicacoes_coletadas", 0) < 50:
        return
    normalized = _normalize_engajamento_data(dict(data))
    payload = {
        "updated_at": datetime.now(timezone(timedelta(hours=-3))).isoformat(),
        **{k: v for k, v in normalized.items() if k not in ("updated_at", "updated_at_fmt", "model", "analise_ia")},
    }
    ENGAJAMENTO_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENGAJAMENTO_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_ia_context_from_html(html_content: str) -> dict:
    """Reconstrói contexto para IA a partir do HTML publicado (quando o JSON local está desatualizado)."""
    pub_m = re.search(r"Publicações</div><div class=\"kpi-value\">(\d+)", html_content)
    eng_m = re.search(r"Engajamento total</div><div class=\"kpi-value\">([^<]+)", html_content)
    views_m = re.search(r"Visualizações</div><div class=\"kpi-value\">([^<]+)", html_content)
    likes_m = re.search(r"Curtidas</div><div class=\"kpi-value\">([^<]+)", html_content)
    comm_m = re.search(r"Comentários</div><div class=\"kpi-value\">([^<]+)", html_content)
    pag_m = re.search(r"Todas as páginas \((\d+)\)", html_content)

    resumo = {
        "publicacoes_coletadas": int(pub_m.group(1)) if pub_m else 0,
        "engajamento_fmt": eng_m.group(1).strip() if eng_m else "0",
        "visualizacoes_fmt": views_m.group(1).strip() if views_m else "0",
        "curtidas_fmt": likes_m.group(1).strip() if likes_m else "0",
        "comentarios_fmt": comm_m.group(1).strip() if comm_m else "0",
        "paginas": int(pag_m.group(1)) if pag_m else 0,
    }

    pages_out: list[dict] = []
    block_pat = re.compile(
        r'<details class="page-block[^"]*"(?:\s+open)?>\s*<summary>\s*'
        r'<span class="page-block-title">([^<]+)(?:<span[^>]*>.*?</span>)?\s*</span>\s*'
        r'<span class="page-block-meta">(@[^\s·]+)\s*·\s*(\d+)/(\d+)\s*publicações\s*·\s*total\s*([^<]+)</span>',
        re.S,
    )
    row_pat = re.compile(
        r'<tr[^>]*>\s*<td>(\d+)</td>\s*'
        r'<td><a href="([^"]+)"[^>]*class="post-shortlink">/([^<]+)</a></td>\s*'
        r"<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*"
        r"<td><strong>([^<]*)</strong></td>",
        re.S,
    )

    for bm in block_pat.finditer(html_content):
        nome = re.sub(r"\s+", " ", bm.group(1)).strip()
        handle = bm.group(2).strip()
        coletadas = int(bm.group(3))
        max_posts = int(bm.group(4))
        total_fmt = bm.group(5).strip()
        start = bm.end()
        next_block = html_content.find('<details class="page-block', start)
        chunk = html_content[start:next_block if next_block != -1 else len(html_content)]
        posts = []
        for rm in row_pat.finditer(chunk):
            posts.append({
                "shortcode": rm.group(3).strip(),
                "url": rm.group(2).strip(),
                "visualizacoes": _parse_fmt_num(rm.group(4)),
                "curtidas": _parse_fmt_num(rm.group(5)),
                "comentarios": _parse_fmt_num(rm.group(6)),
                "engajamento": _parse_fmt_num(rm.group(7)),
            })
        eng_total = _parse_fmt_num(total_fmt)
        pages_out.append({
            "nome": nome,
            "handle": handle,
            "hot": "page-block-hot" in bm.group(0),
            "publicacoes_coletadas": coletadas,
            "engajamento_total": eng_total,
            "visualizacoes": sum(p["visualizacoes"] for p in posts),
            "curtidas": sum(p["curtidas"] for p in posts),
            "comentarios": sum(p["comentarios"] for p in posts),
            "media_por_post": round(eng_total / coletadas) if coletadas else 0,
            "top_posts": posts[:5],
        })

    top_global: list[dict] = []
    card_pat = re.compile(
        r'<article class="post-card">.*?<div class="post-page">([^<]+)</div>\s*'
        r'<div class="post-handle">([^<]+)</div>.*?'
        r'<strong>([^<]+)</strong> total.*?'
        r'([^<]*) views · ([^<]*) ❤ · ([^<]*) 💬.*?'
        r'href="([^"]+)"',
        re.S,
    )
    for cm in card_pat.finditer(html_content):
        top_global.append({
            "pagina": cm.group(1).strip(),
            "handle": cm.group(2).strip(),
            "engajamento": _parse_fmt_num(cm.group(3)),
            "visualizacoes": _parse_fmt_num(cm.group(4)),
            "curtidas": _parse_fmt_num(cm.group(5)),
            "comentarios": _parse_fmt_num(cm.group(6)),
            "url": cm.group(7).strip(),
            "shortcode": (re.search(r"/p/([^/]+)/", cm.group(7)) or [None, ""])[1],
        })

    return {
        "resumo": resumo,
        "max_posts_por_pagina": max_posts if pages_out else MAX_POSTS_LABEL,
        "paginas": pages_out,
        "top_posts_global": top_global[:12],
    }


def _ia_panel_css() -> str:
    return """
    .header-actions {
      display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-top: 0.65rem;
    }
    .btn-ia-analise {
      display: inline-flex; align-items: center; gap: 0.4rem;
      padding: 0.5rem 1rem; border: none; border-radius: 12px;
      background: var(--ig-gradient); color: #fff; font-size: 0.78rem;
      font-weight: 700; font-family: inherit; cursor: pointer;
      transition: opacity 0.15s, transform 0.15s;
      box-shadow: 0 4px 20px rgba(220,39,67,0.35);
    }
    .btn-ia-analise:hover { opacity: 0.92; transform: translateY(-1px); }
    .btn-ia-analise:disabled { opacity: 0.55; cursor: wait; transform: none; }
    .btn-ia-analise svg { width: 16px; height: 16px; fill: currentColor; }
    .btn-ia-refresh {
      display: inline-flex; align-items: center; gap: 0.35rem;
      padding: 0.4rem 0.75rem; border-radius: 10px; border: 1px solid var(--border);
      background: #fff; color: var(--text); font-size: 0.72rem; font-weight: 600;
      font-family: inherit; cursor: pointer;
    }
    .btn-ia-refresh:disabled { opacity: 0.5; cursor: wait; }
    .ia-report-panel {
      background: var(--surface); border-radius: 18px; padding: 1.15rem 1.15rem 1.25rem;
      color: var(--text); margin-top: 0.65rem; border: 1px solid rgba(188,24,136,0.25);
      box-shadow: 0 8px 32px rgba(188,24,136,0.12);
    }
    .ia-report-panel[hidden] { display: none !important; }
    .ia-report-panel.is-loading .ia-report-body { opacity: 0.45; pointer-events: none; }
    .ia-report-head {
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start;
      gap: 0.5rem; margin-bottom: 0.85rem;
    }
    .ia-report-head p { font-size: 0.72rem; color: var(--muted); line-height: 1.45; max-width: 36rem; }
    .ia-report-title { font-size: 1rem; font-weight: 800; margin-bottom: 0.75rem; }
    .ia-block { margin-bottom: 1rem; }
    .ia-block h4 {
      font-size: 0.78rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.04em; color: var(--muted); margin-bottom: 0.5rem;
    }
    .ia-block p { font-size: 0.8rem; line-height: 1.55; margin-bottom: 0.45rem; }
    .ia-block-highlight {
      padding: 0.85rem 1rem; border-radius: 14px;
      background: linear-gradient(135deg, #fff8f0, #fff5f8); border: 1px solid #fbcfe8;
    }
    .ia-block-plan {
      padding: 0.85rem 1rem; border-radius: 14px;
      background: #f0fdf4; border: 1px solid #bbf7d0;
    }
    .ia-block-plan h4 { color: #059669; }
    .ia-cards { display: grid; gap: 0.65rem; }
    @media(min-width:640px){ .ia-cards{ grid-template-columns:1fr 1fr; } }
    .ia-card {
      padding: 0.75rem 0.85rem; border-radius: 12px;
      background: #fafafa; border: 1px solid var(--border); font-size: 0.78rem; line-height: 1.45;
    }
    .ia-card-warn { background: #fffbeb; border-color: #fde68a; }
    .ia-card-head { display: flex; justify-content: space-between; align-items: center; gap: 0.35rem; margin-bottom: 0.2rem; }
    .ia-handle { font-size: 0.68rem; color: var(--muted); display: block; margin-bottom: 0.35rem; }
    .ia-status {
      font-size: 0.58rem; font-weight: 700; text-transform: uppercase;
      padding: 0.12rem 0.4rem; border-radius: 20px; white-space: nowrap;
    }
    .ia-status-hot { background: rgba(255,107,53,0.15); color: #ff6b35; }
    .ia-status-ok { background: #ecfdf5; color: #059669; }
    .ia-card ul, .ia-block ul { margin: 0.35rem 0 0 1rem; font-size: 0.76rem; }
    .ia-steps { margin: 0.35rem 0 0 1.15rem; font-size: 0.78rem; line-height: 1.55; }
    .ia-meta { font-size: 0.65rem; color: var(--muted); margin-top: 0.5rem; }
    .ia-empty { font-size: 0.78rem; color: var(--muted); line-height: 1.5; }
    .ia-spinner {
      display: inline-block; width: 14px; height: 14px;
      border: 2px solid rgba(220,39,67,0.25); border-top-color: #dc2743;
      border-radius: 50%; animation: ia-spin 0.7s linear infinite; vertical-align: middle;
    }
    @keyframes ia-spin { to { transform: rotate(360deg); } }"""


def _ia_panel_script(ia_context_json: str) -> str:
    return f"""
  <script>
    const IA_CONTEXT = {ia_context_json};

    function esc(s) {{
      const d = document.createElement('div');
      d.textContent = s || '';
      return d.innerHTML;
    }}

    function renderAnalise(a) {{
      if (!a) return '<p class="ia-empty">Não foi possível gerar a análise. Tente novamente.</p>';
      let h = '<h3 class="ia-report-title">' + esc(a.titulo || 'Relatório estratégico de engajamento') + '</h3>';
      if (a.resumo_executivo) {{
        h += '<div class="ia-block ia-block-highlight"><h4>Resumo executivo</h4>';
        a.resumo_executivo.split('\\n\\n').filter(Boolean).forEach(p => {{ h += '<p>' + esc(p) + '</p>'; }});
        h += '</div>';
      }}
      if (a.paginas_destaque && a.paginas_destaque.length) {{
        h += '<div class="ia-block"><h4>Páginas que estão dando certo</h4><div class="ia-cards">';
        a.paginas_destaque.forEach(item => {{
          const st = (item.status || '').toLowerCase();
          const cls = st.includes('alta') ? 'ia-status-hot' : 'ia-status-ok';
          h += '<article class="ia-card"><div class="ia-card-head"><strong>' + esc(item.pagina) +
            '</strong><span class="ia-status ' + cls + '">' + esc(item.status) + '</span></div>' +
            '<span class="ia-handle">' + esc(item.handle) + '</span>' +
            '<p><strong>Por que funciona:</strong> ' + esc(item.porque_funciona) + '</p>' +
            '<p><strong>Vertente de conteúdo:</strong> ' + esc(item.vertente_conteudo) + '</p></article>';
        }});
        h += '</div></div>';
      }}
      if (a.padroes_vencedores) {{
        h += '<div class="ia-block"><h4>Padrões dos posts vencedores</h4><p>' + esc(a.padroes_vencedores) + '</p></div>';
      }}
      const plano = a.plano_conteudo || {{}};
      if (plano.vertente_principal || (plano.replicar && plano.replicar.length)) {{
        h += '<div class="ia-block ia-block-plan"><h4>Plano de conteúdo · trilhar a vertente certa</h4>';
        if (plano.vertente_principal) h += '<p><strong>Vertente principal:</strong> ' + esc(plano.vertente_principal) + '</p>';
        if (plano.replicar && plano.replicar.length) {{
          h += '<p><strong>O que replicar:</strong></p><ul>' + plano.replicar.map(x => '<li>' + esc(x) + '</li>').join('') + '</ul>';
        }}
        if (plano.evitar && plano.evitar.length) {{
          h += '<p><strong>O que evitar:</strong></p><ul>' + plano.evitar.map(x => '<li>' + esc(x) + '</li>').join('') + '</ul>';
        }}
        if (plano.frequencia_sugerida) h += '<p><strong>Frequência sugerida:</strong> ' + esc(plano.frequencia_sugerida) + '</p>';
        h += '</div>';
      }}
      if (a.paginas_melhorar && a.paginas_melhorar.length) {{
        h += '<div class="ia-block"><h4>Como melhorar</h4><div class="ia-cards">';
        a.paginas_melhorar.forEach(item => {{
          const acoes = (item.acoes || []).map(x => '<li>' + esc(x) + '</li>').join('');
          h += '<article class="ia-card ia-card-warn"><strong>' + esc(item.pagina) + '</strong><p>' +
            esc(item.diagnostico) + '</p><ul>' + acoes + '</ul></article>';
        }});
        h += '</div></div>';
      }}
      if (a.proximos_passos && a.proximos_passos.length) {{
        h += '<div class="ia-block"><h4>Próximos passos</h4><ol class="ia-steps">' +
          a.proximos_passos.map(p => '<li>' + esc(p) + '</li>').join('') + '</ol></div>';
      }}
      if (a.gerado_em) h += '<p class="ia-meta">Gerado em ' + esc(a.gerado_em.slice(0, 16).replace('T', ' ')) + '</p>';
      return h;
    }}

    async function fetchAnaliseIA() {{
      const panel = document.getElementById('ia-report-panel');
      const body = document.getElementById('ia-report-body');
      const btnMain = document.getElementById('btn-ia-analise');
      const btnRefresh = document.getElementById('btn-ia-refresh');
      panel.hidden = false;
      panel.classList.add('is-loading');
      btnMain.disabled = true;
      btnRefresh.disabled = true;
      body.innerHTML = '<p class="ia-empty"><span class="ia-spinner"></span> A Clauth IA está analisando ' +
        IA_CONTEXT.resumo.publicacoes_coletadas + ' publicações…</p>';
      panel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      try {{
        const res = await fetch('/api/analise-engajamento', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ context: IA_CONTEXT }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Falha na análise');
        body.innerHTML = renderAnalise(data.analise);
      }} catch (err) {{
        body.innerHTML = '<p class="ia-empty">Erro ao gerar análise: ' + esc(err.message) +
          '. Verifique se OPENROUTER_API_KEY está configurada na Vercel.</p>';
      }} finally {{
        panel.classList.remove('is-loading');
        btnMain.disabled = false;
        btnRefresh.disabled = false;
      }}
    }}

    function openAnalisePanel() {{
      const panel = document.getElementById('ia-report-panel');
      const body = document.getElementById('ia-report-body');
      panel.hidden = false;
      panel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      if (body.querySelector('.ia-report-title')) return;
      fetchAnaliseIA();
    }}

    document.getElementById('btn-ia-analise').addEventListener('click', openAnalisePanel);
    document.getElementById('btn-ia-refresh').addEventListener('click', fetchAnaliseIA);
  </script>"""


def inject_ia_into_html(html_content: str, ia_context: dict, analise: dict | None) -> str:
    """Insere botão e painel de análise IA em um HTML de engajamento existente."""
    if "btn-ia-analise" in html_content:
        html_content = re.sub(
            r'<script>\s*const IA_CONTEXT = [\s\S]*?</script>\s*(?=</body>)',
            "",
            html_content,
        )
        html_content = re.sub(
            r'<div class="ia-report-panel"[\s\S]*?</div>\s*(?=<p class="section-title">Resumo geral)',
            "",
            html_content,
        )
        html_content = re.sub(
            r'<div class="header-actions">[\s\S]*?</div>\s*<a class="back-link"',
            '<a class="back-link"',
            html_content,
        )
        html_content = re.sub(r"\.header-actions[\s\S]*?@keyframes ia-spin[\s\S]*?\}", "", html_content)

    css = _ia_panel_css()
    if ".btn-ia-analise" not in html_content:
        html_content = html_content.replace("</style>", css + "\n  </style>", 1)

    header_actions = """
    <div class="header-actions">
      <div class="update-pill">""" + (
        re.search(r'<div class="update-pill">([^<]+)</div>', html_content).group(1)
        if re.search(r'<div class="update-pill">([^<]+)</div>', html_content)
        else ""
    ) + """</div>
      <button type="button" class="btn-ia-analise" id="btn-ia-analise" aria-controls="ia-report-panel">
        <svg viewBox="0 0 24 24"><path d="M12 2a2 2 0 012 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 017 7h1a1 1 0 011 1v3a1 1 0 01-1 1h-1v1a2 2 0 01-2 2H9a2 2 0 01-2-2v-1H6a1 1 0 01-1-1v-3a1 1 0 011-1h1a7 7 0 017-7h1V5.73c-.6-.34-1-.99-1-1.73a2 2 0 012-2M7.5 13A2.5 2.5 0 005 15.5 2.5 2.5 0 007.5 18a2.5 2.5 0 002.5-2.5 2.5 2.5 0 00-2.5-2.5m9 0a2.5 2.5 0 00-2.5 2.5 2.5 2.5 0 002.5 2.5 2.5 2.5 0 002.5-2.5 2.5 2.5 0 00-2.5-2.5z"/></svg>
        Gerar análise com IA
      </button>
    </div>"""

    html_content = re.sub(
        r'<div class="update-pill">[^<]+</div>\s*<a class="back-link"',
        header_actions + '\n    <a class="back-link"',
        html_content,
        count=1,
    )

    panel = f"""
    <div class="ia-report-panel" hidden id="ia-report-panel">
      <div class="ia-report-head">
        <p>A Clauth IA analisa as últimas publicações, identifica o que está funcionando e sugere como trilhar a mesma vertente de conteúdo.</p>
        <button type="button" class="btn-ia-refresh" id="btn-ia-refresh">↻ Atualizar análise</button>
      </div>
      <div class="ia-report-body" id="ia-report-body">{_render_analise_ia_body(analise)}</div>
    </div>"""

    html_content = html_content.replace(
        '  <main>\n    <p class="section-title">Resumo geral</p>',
        f"  <main>{panel}\n    <p class=\"section-title\">Resumo geral</p>",
        1,
    )

    script = _ia_panel_script(json.dumps(ia_context, ensure_ascii=False))
    return html_content.replace("</body>", script + "\n</body>", 1)


def _parse_ia_json(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return json.loads(content)


def _build_ia_context(data: dict) -> dict:
    """Resumo compacto dos dados para prompt da IA (sem inflar o token)."""
    pages_out = []
    for p in data.get("paginas") or []:
        posts = sorted(
            p.get("posts") or [],
            key=lambda x: x.get("engajamento_total") or _post_engagement(x),
            reverse=True,
        )[:5]
        pages_out.append({
            "nome": p["nome"],
            "handle": p["handle"],
            "hot": bool(p.get("hot")),
            "momentum": p.get("momentum_fmt") or "",
            "publicacoes_coletadas": p.get("publicacoes_coletadas", 0),
            "engajamento_total": p.get("engajamento_total", 0),
            "visualizacoes": p.get("visualizacoes", 0),
            "curtidas": p.get("curtidas", 0),
            "comentarios": p.get("comentarios", 0),
            "media_por_post": round(
                p["engajamento_total"] / p["publicacoes_coletadas"]
                if p.get("publicacoes_coletadas")
                else 0
            ),
            "top_posts": [
                {
                    "shortcode": t.get("shortcode"),
                    "url": t.get("url"),
                    "visualizacoes": t.get("visualizacoes", 0),
                    "curtidas": t.get("curtidas", 0),
                    "comentarios": t.get("comentarios", 0),
                    "engajamento": t.get("engajamento_total") or _post_engagement(t),
                }
                for t in posts
            ],
        })
    top_global = []
    for t in (data.get("top_posts") or [])[:12]:
        top_global.append({
            "pagina": t.get("pagina"),
            "handle": t.get("handle"),
            "shortcode": t.get("shortcode"),
            "url": t.get("url"),
            "engajamento": t.get("engajamento_total", 0),
            "visualizacoes": t.get("visualizacoes", 0),
            "curtidas": t.get("curtidas", 0),
            "comentarios": t.get("comentarios", 0),
        })
    return {
        "resumo": data.get("resumo", {}),
        "max_posts_por_pagina": data.get("max_posts_por_pagina", MAX_POSTS_LABEL),
        "paginas": pages_out,
        "top_posts_global": top_global,
    }


IA_ANALISE_SCHEMA = """{
  "titulo": "Relatório estratégico de engajamento",
  "resumo_executivo": "2-4 parágrafos em linguagem clara para a cliente",
  "paginas_destaque": [
    {"pagina": "nome", "handle": "@...", "status": "em alta|estável|precisa atenção", "porque_funciona": "...", "vertente_conteudo": "..."}
  ],
  "padroes_vencedores": "O que os posts de maior engajamento têm em comum",
  "paginas_melhorar": [
    {"pagina": "nome", "diagnostico": "...", "acoes": ["ação 1", "ação 2"]}
  ],
  "plano_conteudo": {
    "vertente_principal": "linha editorial que está performando",
    "replicar": ["recomendação 1", "recomendação 2"],
    "evitar": ["o que não está funcionando"],
    "frequencia_sugerida": "sugestão prática"
  },
  "proximos_passos": ["passo 1", "passo 2", "passo 3"]
}"""


def generate_ia_analise(context: dict, api_key: str, model: str) -> dict | None:
    """Gera relatório estratégico via OpenRouter a partir das métricas coletadas."""
    prompt = (
        "Você é analista de conteúdo Instagram para uma rede de páginas de notícias regionais do Rio de Janeiro.\n"
        "Analise os dados de engajamento (últimas publicações por perfil) e produza um relatório estratégico para a cliente.\n"
        "Explique: quais páginas dão certo, POR QUE dão certo, como melhorar as que estão fracas, "
        "e como trilhar novos posts na mesma vertente do que performa.\n"
        "Cite páginas, handles e números reais dos dados. Tom profissional e acessível. Português do Brasil.\n\n"
        f"DADOS:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        f"Retorne APENAS JSON válido com esta estrutura:\n{IA_ANALISE_SCHEMA}"
    )
    try:
        r = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://clauthub.digital",
                "X-Title": "Clauth Hub Engajamento",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=180,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content") or ""
        analise = _parse_ia_json(content)
        analise["gerado_em"] = datetime.now(timezone(timedelta(hours=-3))).isoformat()
        analise["modelo"] = model
        return analise
    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"  [aviso] Análise IA engajamento: {exc}", file=sys.stderr)
        return None


def _render_analise_ia_body(analise: dict | None) -> str:
    if not analise:
        return (
            '<p class="ia-empty">A análise estratégica ainda não foi gerada. '
            'Clique em <strong>Gerar análise com IA</strong> para criar o relatório com os dados atuais.</p>'
        )

    parts: list[str] = []
    titulo = html.escape(analise.get("titulo") or "Relatório estratégico de engajamento")
    parts.append(f'<h3 class="ia-report-title">{titulo}</h3>')

    resumo = analise.get("resumo_executivo") or ""
    if resumo:
        paras = [p.strip() for p in resumo.split("\n\n") if p.strip()]
        parts.append('<div class="ia-block ia-block-highlight">')
        parts.append('<h4>Resumo executivo</h4>')
        for p in paras:
            parts.append(f"<p>{html.escape(p)}</p>")
        parts.append("</div>")

    destaque = analise.get("paginas_destaque") or []
    if destaque:
        parts.append('<div class="ia-block"><h4>Páginas que estão dando certo</h4><div class="ia-cards">')
        for item in destaque:
            status = html.escape(item.get("status") or "")
            status_cls = "ia-status-hot" if "alta" in status.lower() else "ia-status-ok"
            parts.append(f"""
            <article class="ia-card">
              <div class="ia-card-head">
                <strong>{html.escape(item.get("pagina") or "")}</strong>
                <span class="ia-status {status_cls}">{status}</span>
              </div>
              <span class="ia-handle">{html.escape(item.get("handle") or "")}</span>
              <p><strong>Por que funciona:</strong> {html.escape(item.get("porque_funciona") or "")}</p>
              <p><strong>Vertente de conteúdo:</strong> {html.escape(item.get("vertente_conteudo") or "")}</p>
            </article>""")
        parts.append("</div></div>")

    padroes = analise.get("padroes_vencedores") or ""
    if padroes:
        parts.append(
            f'<div class="ia-block"><h4>Padrões dos posts vencedores</h4><p>{html.escape(padroes)}</p></div>'
        )

    plano = analise.get("plano_conteudo") or {}
    if plano:
        parts.append('<div class="ia-block ia-block-plan"><h4>Plano de conteúdo · trilhar a vertente certa</h4>')
        if plano.get("vertente_principal"):
            parts.append(f'<p><strong>Vertente principal:</strong> {html.escape(plano["vertente_principal"])}</p>')
        if plano.get("replicar"):
            parts.append("<p><strong>O que replicar:</strong></p><ul>")
            parts.extend(f"<li>{html.escape(x)}</li>" for x in plano["replicar"])
            parts.append("</ul>")
        if plano.get("evitar"):
            parts.append("<p><strong>O que evitar:</strong></p><ul>")
            parts.extend(f"<li>{html.escape(x)}</li>" for x in plano["evitar"])
            parts.append("</ul>")
        if plano.get("frequencia_sugerida"):
            parts.append(f'<p><strong>Frequência sugerida:</strong> {html.escape(plano["frequencia_sugerida"])}</p>')
        parts.append("</div>")

    melhorar = analise.get("paginas_melhorar") or []
    if melhorar:
        parts.append('<div class="ia-block"><h4>Como melhorar</h4><div class="ia-cards">')
        for item in melhorar:
            acoes = "".join(f"<li>{html.escape(a)}</li>" for a in (item.get("acoes") or []))
            parts.append(f"""
            <article class="ia-card ia-card-warn">
              <strong>{html.escape(item.get("pagina") or "")}</strong>
              <p>{html.escape(item.get("diagnostico") or "")}</p>
              <ul>{acoes}</ul>
            </article>""")
        parts.append("</div></div>")

    passos = analise.get("proximos_passos") or []
    if passos:
        parts.append('<div class="ia-block"><h4>Próximos passos</h4><ol class="ia-steps">')
        parts.extend(f"<li>{html.escape(p)}</li>" for p in passos)
        parts.append("</ol></div>")

    if analise.get("gerado_em"):
        parts.append(f'<p class="ia-meta">Gerado em {html.escape(analise["gerado_em"][:16].replace("T", " "))}</p>')

    return "".join(parts)


def collect_page_engagement(
    page: dict,
    scraper: InstagramScraper,
    api_key: str | None,
    model: str,
) -> dict:
    handle = page.get("instagram_handle", "")
    post_url = page.get("instagram_post")

    print(f"  -> Scrape Instagram @{handle}...")
    profile = scraper.scrape_profile(
        handle=handle,
        fallback_post_url=post_url,
        max_posts=DEFAULT_MAX_POSTS,
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
    top_posts = _build_top_posts(rows, limit=TOP_POSTS_GLOBAL)

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


def _normalize_engajamento_data(data: dict) -> dict:
    """Garante campos derivados exigidos pelo render (ex.: cache antigo sem top5)."""
    pages = data.get("paginas") or []
    if pages and "hot" not in pages[0]:
        _mark_hot_pages(pages)
    if not data.get("top5"):
        ranked = sorted(pages, key=lambda x: x.get("engajamento_total", 0), reverse=True)
        data["top5"] = [r for r in ranked if r.get("engajamento_total", 0) > 0][:5]
    if not data.get("max_engajamento"):
        top5 = data.get("top5") or []
        data["max_engajamento"] = top5[0]["engajamento_total"] if top5 else 1
    if not data.get("top_posts"):
        data["top_posts"] = _build_top_posts(pages)
    return data


def _prefer_engajamento_cache(data: dict) -> dict:
    """Usa cache quando a coleta atual falhou parcialmente."""
    cached = load_engajamento_cache()
    if not cached:
        return _normalize_engajamento_data(data)
    cur = (data.get("resumo") or {}).get("publicacoes_coletadas", 0)
    prev = (cached.get("resumo") or {}).get("publicacoes_coletadas", 0)
    if prev > cur:
        print(f"  [cache] Engajamento: usando cache ({prev} pub.) em vez de {cur}", file=sys.stderr)
        merged = dict(cached)
        merged["max_posts_por_pagina"] = data.get("max_posts_por_pagina", MAX_POSTS_LABEL)
        return _normalize_engajamento_data(merged)
    return _normalize_engajamento_data(data)


def _posts_with_fmt(posts: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for p in posts:
        eng = p.get("engajamento_total") or _post_engagement(p)
        enriched.append({
            **p,
            "engajamento_total": eng,
            "visualizacoes_fmt": _fmt_num(p.get("visualizacoes", 0)),
            "curtidas_fmt": _fmt_num(p.get("curtidas", 0)),
            "comentarios_fmt": _fmt_num(p.get("comentarios", 0)),
            "engajamento_fmt": _fmt_num(eng),
        })
    enriched.sort(key=lambda x: x["engajamento_total"], reverse=True)
    return enriched


def _render_page_posts_block(page: dict, max_posts: int) -> str:
    posts = _posts_with_fmt(page.get("posts") or [])
    hot_badge = '<span class="hot-badge hot-badge-sm">🔥 Em tração</span>' if page.get("hot") else ""
    block_cls = "page-block page-block-hot" if page.get("hot") else "page-block"
    open_attr = " open" if page.get("hot") else ""

    if not posts:
        return f"""
    <details class="{block_cls}">
      <summary>
        <span class="page-block-title">{html.escape(page["nome"])} {hot_badge}</span>
        <span class="page-block-meta">{html.escape(page["handle"])} · sem publicações coletadas</span>
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
        <span class="page-block-meta">{html.escape(page["handle"])} · {len(posts)}/{max_posts} publicações · total {page["engajamento_fmt"]}</span>
      </summary>
      <p class="panel-desc">Ordenadas por engajamento (views + curtidas + comentários). As do topo indicam a vertente de conteúdo que está funcionando.</p>
      <table class="post-table">
        <thead><tr><th>#</th><th>Post</th><th>Views</th><th>Curtidas</th><th>Coment.</th><th>Total</th><th></th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </details>"""


def _render_post_card(post: dict, rank: int | None = None) -> str:
    link = html.escape(post["url"])
    rank_badge = f'<span class="post-rank">#{rank}</span>' if rank else ""
    thumb_html = (
        f'<a href="{link}" target="_blank" rel="noopener noreferrer" class="post-thumb-link">'
        f'<img class="post-thumb" src="{DEFAULT_THUMB}" '
        f'alt="Hot Clauth IA — {html.escape(post["pagina"])}" loading="lazy">'
        f"</a>"
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
    data = _normalize_engajamento_data(data)

    top_posts = data.get("top_posts") or _build_top_posts(data["paginas"], limit=TOP_POSTS_GLOBAL)

    max_posts = data.get("max_posts_por_pagina", MAX_POSTS_LABEL)
    hot_count = sum(1 for p in data["paginas"] if p.get("hot"))
    pages_posts_html = "".join(_render_page_posts_block(p, max_posts) for p in data["paginas"])

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

    analise = data.get("analise_ia")
    ia_context_json = json.dumps(_build_ia_context(data), ensure_ascii=False)
    ia_body_html = _render_analise_ia_body(analise)
    panel_hidden = " hidden"

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
    .header-actions {{
      display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-top: 0.65rem;
    }}
    .btn-ia-analise {{
      display: inline-flex; align-items: center; gap: 0.4rem;
      padding: 0.5rem 1rem; border: none; border-radius: 12px;
      background: var(--ig-gradient); color: #fff; font-size: 0.78rem;
      font-weight: 700; font-family: inherit; cursor: pointer;
      transition: opacity 0.15s, transform 0.15s;
      box-shadow: 0 4px 20px rgba(220,39,67,0.35);
    }}
    .btn-ia-analise:hover {{ opacity: 0.92; transform: translateY(-1px); }}
    .btn-ia-analise:disabled {{ opacity: 0.55; cursor: wait; transform: none; }}
    .btn-ia-analise svg {{ width: 16px; height: 16px; fill: currentColor; }}
    .btn-ia-refresh {{
      display: inline-flex; align-items: center; gap: 0.35rem;
      padding: 0.4rem 0.75rem; border-radius: 10px; border: 1px solid var(--border);
      background: #fff; color: var(--text); font-size: 0.72rem; font-weight: 600;
      font-family: inherit; cursor: pointer;
    }}
    .btn-ia-refresh:disabled {{ opacity: 0.5; cursor: wait; }}
    .ia-report-panel {{
      background: var(--surface); border-radius: 18px; padding: 1.15rem 1.15rem 1.25rem;
      color: var(--text); margin-top: 0.65rem; border: 1px solid rgba(188,24,136,0.25);
      box-shadow: 0 8px 32px rgba(188,24,136,0.12);
    }}
    .ia-report-panel[hidden] {{ display: none !important; }}
    .ia-report-panel.is-loading .ia-report-body {{ opacity: 0.45; pointer-events: none; }}
    .ia-report-head {{
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start;
      gap: 0.5rem; margin-bottom: 0.85rem;
    }}
    .ia-report-head p {{ font-size: 0.72rem; color: var(--muted); line-height: 1.45; max-width: 36rem; }}
    .ia-report-title {{ font-size: 1rem; font-weight: 800; margin-bottom: 0.75rem; }}
    .ia-block {{ margin-bottom: 1rem; }}
    .ia-block h4 {{
      font-size: 0.78rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.04em; color: var(--muted); margin-bottom: 0.5rem;
    }}
    .ia-block p {{ font-size: 0.8rem; line-height: 1.55; margin-bottom: 0.45rem; }}
    .ia-block-highlight {{
      padding: 0.85rem 1rem; border-radius: 14px;
      background: linear-gradient(135deg, #fff8f0, #fff5f8); border: 1px solid #fbcfe8;
    }}
    .ia-block-plan {{
      padding: 0.85rem 1rem; border-radius: 14px;
      background: #f0fdf4; border: 1px solid #bbf7d0;
    }}
    .ia-block-plan h4 {{ color: #059669; }}
    .ia-cards {{ display: grid; gap: 0.65rem; }}
    @media(min-width:640px){{ .ia-cards{{ grid-template-columns:1fr 1fr; }} }}
    .ia-card {{
      padding: 0.75rem 0.85rem; border-radius: 12px;
      background: #fafafa; border: 1px solid var(--border); font-size: 0.78rem; line-height: 1.45;
    }}
    .ia-card-warn {{ background: #fffbeb; border-color: #fde68a; }}
    .ia-card-head {{ display: flex; justify-content: space-between; align-items: center; gap: 0.35rem; margin-bottom: 0.2rem; }}
    .ia-handle {{ font-size: 0.68rem; color: var(--muted); display: block; margin-bottom: 0.35rem; }}
    .ia-status {{
      font-size: 0.58rem; font-weight: 700; text-transform: uppercase;
      padding: 0.12rem 0.4rem; border-radius: 20px; white-space: nowrap;
    }}
    .ia-status-hot {{ background: rgba(255,107,53,0.15); color: #ff6b35; }}
    .ia-status-ok {{ background: #ecfdf5; color: #059669; }}
    .ia-card ul, .ia-block ul {{ margin: 0.35rem 0 0 1rem; font-size: 0.76rem; }}
    .ia-steps {{ margin: 0.35rem 0 0 1.15rem; font-size: 0.78rem; line-height: 1.55; }}
    .ia-meta {{ font-size: 0.65rem; color: var(--muted); margin-top: 0.5rem; }}
    .ia-empty {{ font-size: 0.78rem; color: var(--muted); line-height: 1.5; }}
    .ia-spinner {{
      display: inline-block; width: 14px; height: 14px;
      border: 2px solid rgba(220,39,67,0.25); border-top-color: #dc2743;
      border-radius: 50%; animation: ia-spin 0.7s linear infinite; vertical-align: middle;
    }}
    @keyframes ia-spin {{ to {{ transform: rotate(360deg); }} }}
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
      border: 1px solid var(--border); border-radius: 14px; overflow: hidden;
      background: #fafafa;
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
    .post-table {{
      width: 100%; border-collapse: collapse; font-size: 0.72rem; min-width: 520px;
    }}
    .post-table th {{
      text-align: left; padding: 0.5rem 0.65rem; font-size: 0.62rem;
      text-transform: uppercase; color: var(--muted); border-bottom: 2px solid var(--border);
      background: #fff;
    }}
    .post-table td {{ padding: 0.45rem 0.65rem; border-bottom: 1px solid var(--border); vertical-align: middle; }}
    .post-table tr.post-row-top td {{ background: rgba(220,39,67,0.04); }}
    .post-table tr.post-row-top td:first-child {{ border-left: 3px solid #dc2743; }}
    .post-shortlink {{ font-weight: 600; color: #dc2743 !important; font-size: 0.7rem; }}
    .page-block .post-table {{ margin: 0.5rem 0 0.75rem; }}
    .page-block .empty-note {{ padding: 0.75rem 1rem 1rem; }}
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
      <strong>(excluindo as fixadas)</strong>, para medir o engajamento real sem posts que já estouraram no topo do perfil.
      Atualização diária às 08:00 · {r["publicacoes_coletadas"]} publicações analisadas nesta coleta.
    </div>
    <div class="header-actions">
      <div class="update-pill">{html.escape(updated_at)}</div>
      <button type="button" class="btn-ia-analise" id="btn-ia-analise" aria-controls="ia-report-panel">
        <svg viewBox="0 0 24 24"><path d="M12 2a2 2 0 012 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 017 7h1a1 1 0 011 1v3a1 1 0 01-1 1h-1v1a2 2 0 01-2 2H9a2 2 0 01-2-2v-1H6a1 1 0 01-1-1v-3a1 1 0 011-1h1a7 7 0 017-7h1V5.73c-.6-.34-1-.99-1-1.73a2 2 0 012-2M7.5 13A2.5 2.5 0 005 15.5 2.5 2.5 0 007.5 18a2.5 2.5 0 002.5-2.5 2.5 2.5 0 00-2.5-2.5m9 0a2.5 2.5 0 00-2.5 2.5 2.5 2.5 0 002.5 2.5 2.5 2.5 0 002.5-2.5 2.5 2.5 0 00-2.5-2.5z"/></svg>
        Gerar análise com IA
      </button>
    </div>
    <a class="back-link" href="/">← Voltar ao acompanhamento</a>
  </header>
  <main>
    <div class="ia-report-panel"{panel_hidden} id="ia-report-panel">
      <div class="ia-report-head">
        <p>A Clauth IA analisa as últimas publicações, identifica o que está funcionando e sugere como trilhar a mesma vertente de conteúdo.</p>
        <button type="button" class="btn-ia-refresh" id="btn-ia-refresh">↻ Atualizar análise</button>
      </div>
      <div class="ia-report-body" id="ia-report-body">{ia_body_html}</div>
    </div>
    <p class="section-title">Resumo geral</p>
    <div class="kpi-grid">
      <div class="kpi-card highlight"><div class="kpi-label">Engajamento total</div><div class="kpi-value">{r["engajamento_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Publicações</div><div class="kpi-value">{r["publicacoes_coletadas"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Visualizações</div><div class="kpi-value">{r["visualizacoes_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Curtidas</div><div class="kpi-value">{r["curtidas_fmt"]}</div></div>
      <div class="kpi-card"><div class="kpi-label">Comentários</div><div class="kpi-value">{r["comentarios_fmt"]}</div></div>
    </div>
    <p class="section-title">Top 5 páginas{f' · {hot_count} em tração 🔥' if hot_count else ''}</p>
    <div class="panel"><h3>Maior engajamento nas últimas {max_posts} publicações (sem fixadas)</h3>{top_html}</div>
    <p class="section-title">O que está funcionando · top publicações</p>
    <div class="panel">
      <h3>Publicações com maior engajamento para análise de conteúdo</h3>
      <p class="panel-desc">Clique na miniatura ou no link para abrir no Instagram e ver o que performou melhor.</p>
      <div class="posts-grid">{top_posts_html}</div>
    </div>
    <p class="section-title">Análise por página · últimas {max_posts} publicações (sem fixadas)</p>
    <div class="panel page-panel">
      <h3>Verifique o que está funcionando em cada perfil</h3>
      <p class="panel-desc">Expanda cada página para ver todas as publicações analisadas, da que mais engajou à que menos. Use os links para abrir no Instagram e identificar a vertente de conteúdo.</p>
      {pages_posts_html}
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
  <script>
    const IA_CONTEXT = {ia_context_json};

    function esc(s) {{
      const d = document.createElement('div');
      d.textContent = s || '';
      return d.innerHTML;
    }}

    function renderAnalise(a) {{
      if (!a) return '<p class="ia-empty">Não foi possível gerar a análise. Tente novamente.</p>';
      let h = '<h3 class="ia-report-title">' + esc(a.titulo || 'Relatório estratégico de engajamento') + '</h3>';
      if (a.resumo_executivo) {{
        h += '<div class="ia-block ia-block-highlight"><h4>Resumo executivo</h4>';
        a.resumo_executivo.split('\\n\\n').filter(Boolean).forEach(p => {{ h += '<p>' + esc(p) + '</p>'; }});
        h += '</div>';
      }}
      if (a.paginas_destaque && a.paginas_destaque.length) {{
        h += '<div class="ia-block"><h4>Páginas que estão dando certo</h4><div class="ia-cards">';
        a.paginas_destaque.forEach(item => {{
          const st = (item.status || '').toLowerCase();
          const cls = st.includes('alta') ? 'ia-status-hot' : 'ia-status-ok';
          h += '<article class="ia-card"><div class="ia-card-head"><strong>' + esc(item.pagina) +
            '</strong><span class="ia-status ' + cls + '">' + esc(item.status) + '</span></div>' +
            '<span class="ia-handle">' + esc(item.handle) + '</span>' +
            '<p><strong>Por que funciona:</strong> ' + esc(item.porque_funciona) + '</p>' +
            '<p><strong>Vertente de conteúdo:</strong> ' + esc(item.vertente_conteudo) + '</p></article>';
        }});
        h += '</div></div>';
      }}
      if (a.padroes_vencedores) {{
        h += '<div class="ia-block"><h4>Padrões dos posts vencedores</h4><p>' + esc(a.padroes_vencedores) + '</p></div>';
      }}
      const plano = a.plano_conteudo || {{}};
      if (plano.vertente_principal || (plano.replicar && plano.replicar.length)) {{
        h += '<div class="ia-block ia-block-plan"><h4>Plano de conteúdo · trilhar a vertente certa</h4>';
        if (plano.vertente_principal) h += '<p><strong>Vertente principal:</strong> ' + esc(plano.vertente_principal) + '</p>';
        if (plano.replicar && plano.replicar.length) {{
          h += '<p><strong>O que replicar:</strong></p><ul>' + plano.replicar.map(x => '<li>' + esc(x) + '</li>').join('') + '</ul>';
        }}
        if (plano.evitar && plano.evitar.length) {{
          h += '<p><strong>O que evitar:</strong></p><ul>' + plano.evitar.map(x => '<li>' + esc(x) + '</li>').join('') + '</ul>';
        }}
        if (plano.frequencia_sugerida) h += '<p><strong>Frequência sugerida:</strong> ' + esc(plano.frequencia_sugerida) + '</p>';
        h += '</div>';
      }}
      if (a.paginas_melhorar && a.paginas_melhorar.length) {{
        h += '<div class="ia-block"><h4>Como melhorar</h4><div class="ia-cards">';
        a.paginas_melhorar.forEach(item => {{
          const acoes = (item.acoes || []).map(x => '<li>' + esc(x) + '</li>').join('');
          h += '<article class="ia-card ia-card-warn"><strong>' + esc(item.pagina) + '</strong><p>' +
            esc(item.diagnostico) + '</p><ul>' + acoes + '</ul></article>';
        }});
        h += '</div></div>';
      }}
      if (a.proximos_passos && a.proximos_passos.length) {{
        h += '<div class="ia-block"><h4>Próximos passos</h4><ol class="ia-steps">' +
          a.proximos_passos.map(p => '<li>' + esc(p) + '</li>').join('') + '</ol></div>';
      }}
      if (a.gerado_em) h += '<p class="ia-meta">Gerado em ' + esc(a.gerado_em.slice(0, 16).replace('T', ' ')) + '</p>';
      return h;
    }}

    async function fetchAnaliseIA() {{
      const panel = document.getElementById('ia-report-panel');
      const body = document.getElementById('ia-report-body');
      const btnMain = document.getElementById('btn-ia-analise');
      const btnRefresh = document.getElementById('btn-ia-refresh');
      panel.hidden = false;
      panel.classList.add('is-loading');
      btnMain.disabled = true;
      btnRefresh.disabled = true;
      body.innerHTML = '<p class="ia-empty"><span class="ia-spinner"></span> A Clauth IA está analisando ' +
        IA_CONTEXT.resumo.publicacoes_coletadas + ' publicações…</p>';
      panel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      try {{
        const res = await fetch('/api/analise-engajamento', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ context: IA_CONTEXT }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Falha na análise');
        body.innerHTML = renderAnalise(data.analise);
      }} catch (err) {{
        body.innerHTML = '<p class="ia-empty">Erro ao gerar análise: ' + esc(err.message) +
          '. Verifique se OPENROUTER_API_KEY está configurada na Vercel.</p>';
      }} finally {{
        panel.classList.remove('is-loading');
        btnMain.disabled = false;
        btnRefresh.disabled = false;
      }}
    }}

    function openAnalisePanel() {{
      const panel = document.getElementById('ia-report-panel');
      const body = document.getElementById('ia-report-body');
      panel.hidden = false;
      panel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      if (body.querySelector('.ia-report-title')) return;
      fetchAnaliseIA();
    }}

    document.getElementById('btn-ia-analise').addEventListener('click', openAnalisePanel);
    document.getElementById('btn-ia-refresh').addEventListener('click', fetchAnaliseIA);
  </script>
</body>
</html>"""


def _persist_engajamento(data: dict, updated_at: str, model: str) -> dict:
    data = _normalize_engajamento_data(data)
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
    print(f"Publicações no painel: {data['resumo']['publicacoes_coletadas']}")
    return data


def _recompute_engajamento_resumo(data: dict) -> dict:
    pages = data.get("paginas") or []
    pages.sort(key=lambda x: x.get("engajamento_total", 0), reverse=True)
    _mark_hot_pages(pages)
    sum_views = sum(r.get("visualizacoes", 0) for r in pages)
    sum_likes = sum(r.get("curtidas", 0) for r in pages)
    sum_comments = sum(r.get("comentarios", 0) for r in pages)
    sum_posts = sum(r.get("publicacoes_coletadas", 0) for r in pages)
    total = sum_views + sum_likes + sum_comments
    media = total // len(pages) if pages else 0
    hot_count = sum(1 for r in pages if r.get("hot"))
    top5 = [r for r in pages if r.get("engajamento_total", 0) > 0][:5]
    data["paginas"] = pages
    data["top5"] = top5
    data["top_posts"] = _build_top_posts(pages, limit=TOP_POSTS_GLOBAL)
    data["max_engajamento"] = top5[0]["engajamento_total"] if top5 else 1
    data["resumo"] = {
        **(data.get("resumo") or {}),
        "visualizacoes_total": sum_views,
        "visualizacoes_fmt": _fmt_num(sum_views),
        "curtidas_total": sum_likes,
        "curtidas_fmt": _fmt_num(sum_likes),
        "comentarios_total": sum_comments,
        "comentarios_fmt": _fmt_num(sum_comments),
        "publicacoes_coletadas": sum_posts,
        "engajamento_total": total,
        "engajamento_fmt": _fmt_num(total),
        "paginas": len(pages),
        "paginas_hot": hot_count,
        "media_por_pagina": media,
        "media_fmt": _fmt_num(media),
    }
    return data


def _refresh_stale_engajamento_pages(
    data: dict,
    pages: list,
    api_key: str | None,
    model: str,
) -> dict:
    """Re-coleta páginas com handle errado ou sem publicações no cache."""
    hub_by_name = {p["name"]: p for p in pages}
    scraper = InstagramScraper(delay=REQUEST_DELAY)
    rescraped = False
    for row in data.get("paginas") or []:
        hub = hub_by_name.get(row["nome"])
        if not hub:
            continue
        expected = hub["instagram_handle"].lstrip("@")
        current = row.get("handle", "").lstrip("@")
        if current == expected and row.get("publicacoes_coletadas", 0) > 0:
            continue
        print(
            f"  [re-scrape] {row['nome']} (@{expected}) — handle ou cache desatualizado",
            file=sys.stderr,
        )
        fresh = collect_page_engagement(hub, scraper, api_key, model)
        row.clear()
        row.update(fresh)
        rescraped = True
    return _recompute_engajamento_resumo(data) if rescraped else data


def update_engajamento_daily(
    pages: list,
    all_metrics: list,
    api_key: str | None,
    model: str,
    updated_at: str,
) -> dict:
    """Atualização rápida diária: reaproveita cache e atualiza data/HTML (sem scrape pesado)."""
    cached = load_engajamento_cache()
    if cached and (cached.get("resumo") or {}).get("publicacoes_coletadas", 0) >= 50:
        data = _normalize_engajamento_data(dict(cached))
        data = _refresh_stale_engajamento_pages(data, pages, api_key, model)
        print(
            f"  [diário] Engajamento via cache: {data['resumo']['publicacoes_coletadas']} publicações",
            file=sys.stderr,
        )
    else:
        print("  [diário] Cache insuficiente — coleta completa...", file=sys.stderr)
        return update_and_save(pages, all_metrics, api_key, model, updated_at)

    if api_key:
        try:
            print("Gerando análise estratégica Clauth IA...")
            analise = generate_ia_analise(_build_ia_context(data), api_key, model)
            if analise:
                data["analise_ia"] = analise
                print("  ✓ Análise IA gerada")
        except Exception as exc:
            print(f"  [aviso] Análise IA: {exc}", file=sys.stderr)

    return _persist_engajamento(data, updated_at, model)


def update_and_save(
    pages: list,
    all_metrics: list,
    api_key: str | None,
    model: str,
    updated_at: str,
) -> dict:
    data = compute_engajamento(pages, all_metrics, api_key, model)
    data = _prefer_engajamento_cache(data)
    data = _refresh_stale_engajamento_pages(data, pages, api_key, model)
    save_engajamento_cache(data)

    if api_key:
        try:
            print("Gerando análise estratégica Clauth IA...")
            analise = generate_ia_analise(_build_ia_context(data), api_key, model)
            if analise:
                data["analise_ia"] = analise
                print("  ✓ Análise IA gerada")
            else:
                print("  [aviso] Análise IA não gerada", file=sys.stderr)
        except Exception as exc:
            print(f"  [aviso] Análise IA: {exc}", file=sys.stderr)

    return _persist_engajamento(data, updated_at, model)
