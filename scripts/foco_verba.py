"""Painel Foco Verba — páginas prioritárias de investimento."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTION_TZ = timezone(timedelta(hours=-3))
CONFIG_PATH = ROOT / "config" / "foco_verba.json"
RELATORIO_CONFIG = ROOT / "config" / "relatorio_financeiro.json"
METRICS_CACHE = ROOT / "config" / "metrics_cache.json"
HTML_PATH = ROOT / "focoverba" / "index.html"
DATA_PATH = ROOT / "data" / "foco_verba.json"

IG_SVG = (
    '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 '
    "3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 "
    "4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-"
    "4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-"
    "3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C"
    "8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259"
    ".014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 "
    "3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-"
    "3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0z"
    "m0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 "
    '8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/></svg>'
)


def _fmt_brl(value: float | None) -> str:
    if value is None:
        return "—"
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_num(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}".replace(",", ".")


def _fmt_cost(value: float | None) -> str:
    if value is None:
        return "—"
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _load_relatorio_by_handle() -> dict[str, dict]:
    if not RELATORIO_CONFIG.exists():
        return {}
    data = json.loads(RELATORIO_CONFIG.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for p in data.get("paginas") or []:
        handle = (p.get("instagram_handle") or "").lstrip("@").lower()
        if handle:
            out[handle] = p
    return out


def _load_followers_by_handle() -> dict[str, int]:
    """Seguidores atuais do hub (metrics_cache) indexados por handle via pages.json."""
    pages_cfg = json.loads((ROOT / "config" / "pages.json").read_text(encoding="utf-8"))
    id_to_handle = {
        p["id"]: p["instagram_handle"].lstrip("@").lower()
        for p in pages_cfg
        if p.get("instagram_handle")
    }
    if not METRICS_CACHE.exists():
        return {}
    cache = json.loads(METRICS_CACHE.read_text(encoding="utf-8"))
    pages = cache.get("pages") or {}
    out: dict[str, int] = {}
    for pid_str, m in pages.items():
        try:
            pid = int(pid_str)
        except (TypeError, ValueError):
            continue
        handle = id_to_handle.get(pid)
        if not handle:
            continue
        seg = (m.get("instagram") or {}).get("seguidores")
        if seg is not None:
            out[handle] = int(seg)
    return out


def _load_relatorio_live_by_handle() -> dict[str, dict]:
    """Valores já calculados do relatório financeiro (investimento + custo)."""
    path = ROOT / "data" / "relatorio_financeiro.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict] = {}
    for p in data.get("paginas") or []:
        handle = (p.get("instagram_handle") or "").lstrip("@").lower()
        if handle:
            out[handle] = p
    return out


def compute(config: dict | None = None) -> dict:
    cfg = config or json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    relatorio_cfg = _load_relatorio_by_handle()
    relatorio_live = _load_relatorio_live_by_handle()
    followers = _load_followers_by_handle()

    paginas_out = []
    total_seguidores = 0

    for p in cfg["paginas"]:
        handle = p["instagram_handle"].lstrip("@").lower()
        rel_cfg = relatorio_cfg.get(handle) or {}
        rel_live = relatorio_live.get(handle) or {}

        seguidores = followers.get(handle)
        if seguidores is None:
            seguidores = rel_live.get("seguidores_atuais") or rel_cfg.get("seguidores_relatorio")
            if seguidores is not None:
                seguidores = int(seguidores)

        if seguidores:
            total_seguidores += seguidores

        paginas_out.append({
            "slug": p["slug"],
            "nome": p["nome"],
            "hub_name": p.get("hub_name") or p["nome"],
            "instagram": f"@{handle}",
            "instagram_handle": handle,
            "instagram_url": f"https://www.instagram.com/{handle}/",
            "segmentacao": p.get("segmentacao") or rel_cfg.get("segmentacao") or "",
            "seguidores": seguidores,
            "seguidores_fmt": _fmt_num(seguidores),
        })

    paginas_out.sort(key=lambda x: x["seguidores"] or 0, reverse=True)

    return {
        "titulo": cfg.get("titulo", "Foco Verba"),
        "descricao": cfg.get("descricao", ""),
        "resumo": {
            "paginas": len(paginas_out),
            "seguidores_total": total_seguidores,
            "seguidores_fmt": _fmt_num(total_seguidores),
        },
        "paginas": paginas_out,
    }


def render_html(data: dict, updated_at: str) -> str:
    resumo = data["resumo"]
    cards = ""
    for i, p in enumerate(data["paginas"], 1):
        cards += f"""
    <article class="page-card" id="{html.escape(p['slug'])}">
      <div class="page-card-head">
        <span class="rank">#{i}</span>
        <div>
          <h2>{html.escape(p['nome'])}</h2>
          <a class="handle" href="{html.escape(p['instagram_url'])}" target="_blank" rel="noopener">{html.escape(p['instagram'])}</a>
        </div>
      </div>
      <p class="region">{html.escape(p['segmentacao'] or '—')}</p>
      <div class="metrics">
        <div class="metric">
          <span class="metric-label">Seguidores atuais</span>
          <span class="metric-value accent">{html.escape(p['seguidores_fmt'])}</span>
        </div>
      </div>
      <a class="ig-link" href="{html.escape(p['instagram_url'])}" target="_blank" rel="noopener">Abrir no Instagram →</a>
    </article>"""

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Clauth Hub — Foco Verba</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --ig-gradient: linear-gradient(45deg, #f09433 0%, #e6683c 25%, #dc2743 50%, #cc2366 75%, #bc1888 100%);
      --bg: #000; --surface: #111; --panel: #1a1a1a; --text: #fff; --muted: rgba(255,255,255,0.55);
      --border: rgba(255,255,255,0.1); --accent: #34d399;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Inter, system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100dvh; }}
    a {{ color: #a5b4fc; text-decoration: none; }}
    .ig-header {{
      position: sticky; top: 0; z-index: 50; background: rgba(0,0,0,0.92);
      backdrop-filter: blur(12px); border-bottom: 1px solid var(--border);
      padding: 0.85rem 1.25rem 1rem;
    }}
    .header-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.45rem; }}
    .brand {{ display: flex; align-items: center; gap: 0.5rem; }}
    .brand-icon {{
      width: 28px; height: 28px; border-radius: 8px; background: var(--ig-gradient);
      display: flex; align-items: center; justify-content: center;
    }}
    .brand-icon svg {{ width: 16px; height: 16px; }}
    .brand-name {{
      font-size: 1.1rem; font-weight: 700; background: var(--ig-gradient);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }}
    .badge {{
      font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
      padding: 0.25rem 0.55rem; border-radius: 20px; background: rgba(52,211,153,0.15);
      border: 1px solid rgba(52,211,153,0.35); color: var(--accent);
    }}
    .header-title {{ font-size: 1.35rem; font-weight: 700; }}
    .header-sub {{ font-size: 0.85rem; color: var(--muted); margin-top: 0.35rem; line-height: 1.45; max-width: 42rem; }}
    .update-pill {{
      display: inline-block; margin-top: 0.75rem; font-size: 0.72rem; color: var(--muted);
      padding: 0.35rem 0.7rem; border-radius: 999px; border: 1px solid var(--border);
    }}
    main {{ max-width: 960px; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    .kpi-grid {{
      display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem; margin: 1rem 0 1.5rem;
    }}
    .kpi {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 1rem;
    }}
    .kpi-label {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
    .kpi-value {{ font-size: 1.25rem; font-weight: 700; margin-top: 0.35rem; }}
    .kpi.highlight .kpi-value {{ color: var(--accent); }}
    .section-title {{ font-size: 0.8rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 0.85rem; }}
    .pages {{ display: grid; gap: 1rem; }}
    .page-card {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 1.15rem 1.2rem;
    }}
    .page-card-head {{ display: flex; gap: 0.85rem; align-items: flex-start; }}
    .rank {{
      width: 2rem; height: 2rem; border-radius: 10px; display: grid; place-items: center;
      background: var(--ig-gradient); font-weight: 800; font-size: 0.85rem; flex-shrink: 0;
    }}
    .page-card h2 {{ font-size: 1.15rem; font-weight: 700; }}
    .handle {{ font-size: 0.85rem; color: var(--muted); display: inline-block; margin-top: 0.15rem; }}
    .badge-warn {{
      display: inline-block; margin-left: 0.4rem; font-size: 0.65rem; font-weight: 600;
      padding: 0.15rem 0.45rem; border-radius: 999px; background: rgba(251,191,36,0.15);
      color: #fbbf24; border: 1px solid rgba(251,191,36,0.35); vertical-align: middle;
    }}
    .region {{ font-size: 0.82rem; color: var(--muted); margin: 0.75rem 0 1rem; }}
    .metrics {{ display: grid; grid-template-columns: 1fr; gap: 0.65rem; }}
    @media (min-width: 640px) {{ .metrics {{ grid-template-columns: repeat(3, 1fr); }} }}
    .metric {{
      background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 12px; padding: 0.75rem;
    }}
    .metric-label {{ display: block; font-size: 0.7rem; color: var(--muted); margin-bottom: 0.25rem; }}
    .metric-value {{ font-size: 1.1rem; font-weight: 700; }}
    .metric-value.accent {{ color: var(--accent); }}
    .ig-link {{ display: inline-block; margin-top: 0.9rem; font-size: 0.85rem; font-weight: 600; }}
    .back-link {{ display: inline-block; margin-top: 1.75rem; color: var(--muted); font-size: 0.9rem; }}
    .back-link:hover {{ color: #fff; }}
    footer {{ text-align: center; color: var(--muted); font-size: 0.75rem; padding: 1rem 1rem 2rem; }}
  </style>
</head>
<body>
  <header class="ig-header">
    <div class="header-top">
      <div class="brand">
        <div class="brand-icon">{IG_SVG}</div>
        <span class="brand-name">Clauth Hub</span>
      </div>
      <span class="badge">Foco Verba</span>
    </div>
    <h1 class="header-title">{html.escape(data['titulo'])}</h1>
    <p class="header-sub">{html.escape(data.get('descricao') or '')}</p>
    <div class="update-pill">{html.escape(updated_at)}</div>
  </header>
  <main>
    <div class="kpi-grid">
      <div class="kpi highlight"><div class="kpi-label">Páginas foco</div><div class="kpi-value">{resumo['paginas']}</div></div>
      <div class="kpi"><div class="kpi-label">Seguidores</div><div class="kpi-value">{html.escape(resumo['seguidores_fmt'])}</div></div>
    </div>
    <p class="section-title">Páginas prioritárias</p>
    <div class="pages">{cards}</div>
    <a class="back-link" href="/">← Voltar ao acompanhamento</a>
  </main>
  <footer>Clauth Hub · Foco Verba · Atualização automática com o hub</footer>
</body>
</html>"""


def persist(data: dict, updated_at: str) -> dict:
    payload = {
        **data,
        "updated_at": datetime.now(COLLECTION_TZ).isoformat(),
        "updated_at_fmt": updated_at,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    HTML_PATH.write_text(render_html(data, updated_at), encoding="utf-8")
    return payload


def update_and_save(updated_at: str) -> dict:
    data = compute()
    return persist(data, updated_at)


if __name__ == "__main__":
    from relatorio_financeiro import collection_label

    label = collection_label(datetime.now(COLLECTION_TZ))
    out = update_and_save(label)
    print(f"Páginas: {out['resumo']['paginas']}")
    print(f"Seguidores: {out['resumo']['seguidores_fmt']}")
    print(f"HTML: {HTML_PATH}")
