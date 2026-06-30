"""Scraper de publicações Instagram — views, curtidas, comentários e compartilhamentos."""

from __future__ import annotations

import html as htmlmod
import json
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

BOT_UA = "facebookexternalhit/1.1"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
IG_APP_ID = "936619743392459"
POST_GRAPHQL_DOC_ID = "8845758582119845"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MAX_POSTS = 50
REQUEST_DELAY = 2.0
ENRICH_VIEWS_CAP = 15


@dataclass
class PostMetrics:
    shortcode: str
    url: str
    visualizacoes: int = 0
    curtidas: int = 0
    comentarios: int = 0
    compartilhamentos: int = 0
    thumbnail: str = ""
    source: str = "http"

    @property
    def total(self) -> int:
        return self.visualizacoes + self.curtidas + self.comentarios + self.compartilhamentos


@dataclass
class ProfileEngagement:
    handle: str
    publicacoes: list[PostMetrics] = field(default_factory=list)
    source: str = "http"

    @property
    def visualizacoes(self) -> int:
        return sum(p.visualizacoes for p in self.publicacoes)

    @property
    def curtidas(self) -> int:
        return sum(p.curtidas for p in self.publicacoes)

    @property
    def comentarios(self) -> int:
        return sum(p.comentarios for p in self.publicacoes)

    @property
    def compartilhamentos(self) -> int:
        return sum(p.compartilhamentos for p in self.publicacoes)

    @property
    def total(self) -> int:
        return self.visualizacoes + self.curtidas + self.comentarios + self.compartilhamentos


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


def _parse_metrics_text(text: str) -> dict:
    """Extrai métricas de texto livre (og:description, embed, etc.)."""
    result = {
        "visualizacoes": 0,
        "curtidas": 0,
        "comentarios": 0,
        "compartilhamentos": 0,
    }
    patterns = {
        "curtidas": r"([\d.,]+[KkMm]?)\s*(?:likes?|curtidas?|reactions?|reações?)",
        "comentarios": r"([\d.,]+[KkMm]?)\s*(?:comments?|comentários?|comentarios?)",
        "visualizacoes": r"([\d.,]+[KkMm]?)\s*(?:views?|visualizações?|visualizacoes?|plays?|reproduções?)",
        "compartilhamentos": r"([\d.,]+[KkMm]?)\s*(?:shares?|compartilhamentos?|shared?)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.I)
        if m:
            n = _parse_number(m.group(1))
            if n is not None:
                result[key] = n
    return result


def _shortcode_from_url(url: str) -> str | None:
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _post_url(shortcode: str) -> str:
    return f"https://www.instagram.com/p/{shortcode}/"


def _embed_url(shortcode: str) -> str:
    return f"https://www.instagram.com/p/{shortcode}/embed/captioned/"


def _thumbnail_from_feed_item(item: dict) -> str:
    candidates = (item.get("image_versions2") or {}).get("candidates") or []
    if candidates:
        return candidates[0].get("url") or ""
    return item.get("thumbnail_url") or item.get("display_uri") or ""


def _thumbnail_from_graphql_media(media: dict) -> str:
    return media.get("thumbnail_src") or media.get("display_url") or ""


def _thumbnail_from_html(html_page: str) -> str:
    m = re.search(r'og:image" content="([^"]+)"', html_page)
    return htmlmod.unescape(m.group(1)) if m else ""


class InstagramScraper:
    def __init__(self, delay: float = REQUEST_DELAY):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "pt-BR,pt;q=0.9"})

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    def _fetch(self, url: str, ua: str = BOT_UA) -> str | None:
        try:
            r = self.session.get(url, headers={"User-Agent": ua}, timeout=25, allow_redirects=True)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            print(f"  [aviso] Falha ao buscar {url}: {exc}", file=sys.stderr)
            return None

    def _ensure_csrf(self, referer: str) -> str:
        if not self.session.cookies.get("csrftoken"):
            self.session.get(referer, timeout=25)
        return self.session.cookies.get("csrftoken", "")

    def _metrics_from_graphql_media(self, media: dict) -> dict:
        likes = (
            media.get("edge_media_preview_like", {}).get("count")
            or media.get("edge_liked_by", {}).get("count")
            or 0
        )
        comments = (
            media.get("edge_media_to_parent_comment", {}).get("count")
            or media.get("edge_media_to_comment", {}).get("count")
            or media.get("edge_media_preview_comment", {}).get("count")
            or 0
        )
        views = int(media.get("video_view_count") or media.get("video_play_count") or 0)
        return {
            "visualizacoes": views,
            "curtidas": int(likes),
            "comentarios": int(comments),
            "compartilhamentos": 0,
            "thumbnail": _thumbnail_from_graphql_media(media),
        }

    def fetch_post_graphql(self, shortcode: str, referer: str | None = None) -> dict | None:
        """Métricas completas via GraphQL interno (inclui video_view_count)."""
        referer = referer or _post_url(shortcode)
        variables = quote(
            json.dumps(
                {
                    "shortcode": shortcode,
                    "fetch_tagged_user_count": None,
                    "hoisted_comment_id": None,
                    "hoisted_reply_id": None,
                },
                separators=(",", ":"),
            )
        )
        body = f"variables={variables}&doc_id={POST_GRAPHQL_DOC_ID}"

        for attempt in range(3):
            csrf = self._ensure_csrf(referer)
            try:
                r = self.session.post(
                    "https://www.instagram.com/graphql/query/",
                    data=body,
                    headers={
                        "X-IG-App-ID": IG_APP_ID,
                        "X-CSRFToken": csrf,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": referer,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=25,
                )
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    return None
                payload = r.json()
                if not isinstance(payload, dict):
                    return None
                media = (payload.get("data") or {}).get("xdt_shortcode_media")
                if not media:
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        continue
                    return None
                return self._metrics_from_graphql_media(media)
            except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                print(f"  [aviso] GraphQL post {shortcode} (tentativa {attempt + 1}): {exc}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
        return None

    def scrape_post(self, url: str) -> PostMetrics | None:
        """Raspagem de um post/reel via GraphQL + og:description."""
        url = url.split("#")[0].strip()
        if not url.endswith("/"):
            url += "/"
        shortcode = _shortcode_from_url(url)
        if not shortcode:
            return None

        metrics = {"visualizacoes": 0, "curtidas": 0, "comentarios": 0, "compartilhamentos": 0}
        thumbnail = ""
        source = "http"

        gql = self.fetch_post_graphql(shortcode, referer=url)
        if gql and any(gql.values()):
            metrics = {k: max(metrics[k], gql[k]) for k in metrics}
            thumbnail = gql.get("thumbnail") or ""
            source = "graphql"

        html_page = None
        if not metrics["curtidas"] or not metrics["comentarios"] or not thumbnail:
            html_page = self._fetch(url, BOT_UA)
            if html_page:
                if not thumbnail:
                    thumbnail = _thumbnail_from_html(html_page)
                m = re.search(r'og:description" content="([^"]+)"', html_page)
                if m:
                    parsed = _parse_metrics_text(htmlmod.unescape(m.group(1)))
                    metrics = {k: max(metrics[k], parsed[k]) for k in metrics}
                    if source == "http" and any(parsed.values()):
                        source = "http"

        if not any(metrics.values()):
            return None

        return PostMetrics(
            shortcode=shortcode,
            url=url,
            visualizacoes=metrics["visualizacoes"],
            curtidas=metrics["curtidas"],
            comentarios=metrics["comentarios"],
            compartilhamentos=metrics["compartilhamentos"],
            thumbnail=thumbnail,
            source=source,
        )

    def _enrich_post_views(self, post: PostMetrics) -> PostMetrics:
        gql = self.fetch_post_graphql(post.shortcode, referer=post.url)
        if not gql:
            return post
        if not post.visualizacoes:
            post.visualizacoes = max(post.visualizacoes, gql["visualizacoes"])
        post.curtidas = max(post.curtidas, gql["curtidas"])
        post.comentarios = max(post.comentarios, gql["comentarios"])
        if not post.thumbnail:
            post.thumbnail = gql.get("thumbnail") or ""
        if gql["visualizacoes"]:
            post.source = "graphql" if post.source in ("api", "feed") else post.source
        self._sleep()
        return post

    def _metrics_from_feed_item(self, item: dict) -> dict:
        views = int(item.get("play_count") or item.get("ig_play_count") or item.get("view_count") or 0)
        return {
            "visualizacoes": views,
            "curtidas": int(item.get("like_count") or 0),
            "comentarios": int(item.get("comment_count") or 0),
            "compartilhamentos": int(item.get("share_count") or 0),
        }

    def resolve_user_id(self, handle: str) -> str | None:
        """Obtém o ID numérico do perfil (necessário para listar o feed)."""
        profile_url = f"https://www.instagram.com/{handle}/"
        self._sleep()
        self.session.get(profile_url, timeout=25)
        csrf = self.session.cookies.get("csrftoken", "")

        for attempt in range(2):
            try:
                r = self.session.get(
                    f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}",
                    headers={
                        "X-IG-App-ID": IG_APP_ID,
                        "X-Requested-With": "XMLHttpRequest",
                        "X-CSRFToken": csrf,
                        "Referer": profile_url,
                    },
                    timeout=25,
                )
                if r.status_code == 429:
                    break
                if r.status_code == 200:
                    user_id = r.json().get("data", {}).get("user", {}).get("id")
                    if user_id:
                        return str(user_id)
            except (requests.RequestException, json.JSONDecodeError, AttributeError):
                pass
            time.sleep(2 * (attempt + 1))

        html_page = self._fetch(profile_url, BOT_UA)
        if html_page:
            for pat in (r'"user_id":"(\d+)"', r'"profilePage_(\d+)"', r'"owner_id":"(\d+)"'):
                m = re.search(pat, html_page)
                if m:
                    return m.group(1)

        html_page = self._fetch(profile_url, BROWSER_UA)
        if html_page:
            for pat in (r'"user_id":"(\d+)"', r'"profilePage_(\d+)"'):
                m = re.search(pat, html_page)
                if m:
                    return m.group(1)
        return None

    def fetch_posts_feed(self, handle: str, max_posts: int = DEFAULT_MAX_POSTS) -> list[PostMetrics]:
        """Lista publicações recentes via /api/v1/feed/user/ (paginado, até max_posts)."""
        handle = handle.lstrip("@")
        profile_url = f"https://www.instagram.com/{handle}/"
        user_id = self.resolve_user_id(handle)
        if not user_id:
            print(f"  [aviso] Não foi possível obter user_id de @{handle}", file=sys.stderr)
            return []

        self._sleep()
        csrf = self._ensure_csrf(profile_url)
        posts: list[PostMetrics] = []
        next_max_id: str | None = None
        enriched = 0

        while len(posts) < max_posts:
            params = {"count": min(12, max_posts - len(posts))}
            if next_max_id:
                params["max_id"] = next_max_id
            try:
                r = self.session.get(
                    f"https://www.instagram.com/api/v1/feed/user/{user_id}/",
                    params=params,
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
                    continue
                if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                    break
                payload = r.json()
                items = payload.get("items") or []
                if not items:
                    break
                for item in items:
                    code = item.get("code") or item.get("shortcode")
                    if not code:
                        continue
                    metrics = self._metrics_from_feed_item(item)
                    post = PostMetrics(
                        shortcode=code,
                        url=_post_url(code),
                        visualizacoes=metrics["visualizacoes"],
                        curtidas=metrics["curtidas"],
                        comentarios=metrics["comentarios"],
                        compartilhamentos=metrics["compartilhamentos"],
                        thumbnail=_thumbnail_from_feed_item(item),
                        source="feed",
                    )
                    if enriched < ENRICH_VIEWS_CAP and (not post.visualizacoes or not post.thumbnail):
                        post = self._enrich_post_views(post)
                        enriched += 1
                    posts.append(post)
                    if len(posts) >= max_posts:
                        break
                if len(posts) >= max_posts or not payload.get("more_available"):
                    break
                next_max_id = payload.get("next_max_id")
                if not next_max_id:
                    break
                self._sleep()
            except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                print(f"  [aviso] Feed Instagram @{handle}: {exc}", file=sys.stderr)
                break
        return posts

    def _extract_user_id(self, handle: str) -> str | None:
        return self.resolve_user_id(handle)

    def fetch_posts_api(self, handle: str, max_posts: int = DEFAULT_MAX_POSTS) -> list[PostMetrics]:
        """Lista publicações recentes via API interna do Instagram (quando disponível)."""
        self._sleep()
        profile_url = f"https://www.instagram.com/{handle}/"
        self.session.get(profile_url, timeout=25)
        csrf = self.session.cookies.get("csrftoken", "")

        for attempt in range(3):
            try:
                r = self.session.get(
                    f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}",
                    headers={
                        "X-IG-App-ID": IG_APP_ID,
                        "X-Requested-With": "XMLHttpRequest",
                        "X-CSRFToken": csrf,
                        "Referer": profile_url,
                    },
                    timeout=25,
                )
                if r.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                edges = (
                    r.json()
                    .get("data", {})
                    .get("user", {})
                    .get("edge_owner_to_timeline_media", {})
                    .get("edges", [])
                )
                posts: list[PostMetrics] = []
                for edge in edges[:max_posts]:
                    node = edge.get("node", {})
                    code = node.get("shortcode")
                    if not code:
                        continue
                    post = PostMetrics(
                        shortcode=code,
                        url=_post_url(code),
                        visualizacoes=int(node.get("video_view_count") or node.get("video_play_count") or 0),
                        curtidas=int(node.get("edge_liked_by", {}).get("count") or 0),
                        comentarios=int(node.get("edge_media_to_comment", {}).get("count") or 0),
                        compartilhamentos=0,
                        thumbnail=node.get("thumbnail_src") or node.get("display_url") or "",
                        source="api",
                    )
                    if not post.visualizacoes or not post.thumbnail:
                        post = self._enrich_post_views(post)
                    posts.append(post)
                    self._sleep()
                return posts
            except (requests.RequestException, json.JSONDecodeError, KeyError) as exc:
                print(f"  [aviso] API Instagram @{handle} (tentativa {attempt + 1}): {exc}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
        return []

    def fetch_posts_openrouter(
        self,
        api_key: str,
        model: str,
        handle: str,
        profile_url: str,
        max_posts: int = DEFAULT_MAX_POSTS,
    ) -> list[PostMetrics]:
        """IA raspa publicações recentes do perfil quando a API bloqueia."""
        prompt = (
            f"Raspagem do perfil Instagram @{handle} ({profile_url}).\n"
            f"Liste até {max_posts} publicações/reels recentes VISÍVEIS publicamente.\n"
            "Para cada uma, extraia: shortcode, visualizacoes (views/plays), curtidas, comentarios, compartilhamentos.\n"
            "Retorne APENAS JSON:\n"
            '{"publicacoes":[{"shortcode":"...","visualizacoes":0,"curtidas":0,"comentarios":0,"compartilhamentos":0}]}'
        )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "type": "openrouter:web_fetch",
                    "openrouter:web_fetch": {
                        "engine": "openrouter",
                        "allowed_domains": ["instagram.com", "www.instagram.com"],
                        "max_uses": 5,
                    },
                }
            ],
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
        content = r.json()["choices"][0]["message"].get("content") or ""
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
        posts: list[PostMetrics] = []
        for item in data.get("publicacoes", [])[:max_posts]:
            code = item.get("shortcode") or _shortcode_from_url(item.get("url", ""))
            if not code:
                continue
            posts.append(
                PostMetrics(
                    shortcode=code,
                    url=_post_url(code),
                    visualizacoes=int(item.get("visualizacoes") or 0),
                    curtidas=int(item.get("curtidas") or 0),
                    comentarios=int(item.get("comentarios") or 0),
                    compartilhamentos=int(item.get("compartilhamentos") or 0),
                    source="openrouter",
                )
            )
        return posts

    def scrape_profile(
        self,
        handle: str,
        fallback_post_url: str | None = None,
        max_posts: int = DEFAULT_MAX_POSTS,
        api_key: str | None = None,
        model: str = "google/gemini-2.5-flash",
    ) -> ProfileEngagement:
        """
        Raspa engajamento das publicações recentes do perfil.
        1) Feed paginado (50 posts) → 2) API web_profile_info → 3) post configurado → 4) IA.
        """
        handle = handle.lstrip("@")
        profile_url = f"https://www.instagram.com/{handle}/"
        result = ProfileEngagement(handle=handle)

        if max_posts > 12:
            posts = self.fetch_posts_feed(handle, max_posts)
            if posts:
                result.publicacoes = posts
                result.source = "feed"
                return result

        posts = self.fetch_posts_api(handle, min(max_posts, 12))
        if posts and len(posts) >= max_posts:
            result.publicacoes = posts[:max_posts]
            result.source = "api"
            return result

        if len(posts) < max_posts:
            feed_posts = self.fetch_posts_feed(handle, max_posts)
            if feed_posts:
                seen = {p.shortcode for p in posts}
                for p in feed_posts:
                    if p.shortcode not in seen:
                        posts.append(p)
                        seen.add(p.shortcode)
                    if len(posts) >= max_posts:
                        break

        if posts:
            result.publicacoes = posts[:max_posts]
            sources = {p.source for p in posts}
            result.source = "feed" if "feed" in sources else posts[0].source
            return result

        seen: set[str] = set()
        collected: list[PostMetrics] = []

        if fallback_post_url and _shortcode_from_url(fallback_post_url):
            post = self.scrape_post(fallback_post_url)
            if post:
                collected.append(post)
                seen.add(post.shortcode)
            self._sleep()

        if collected:
            result.publicacoes = collected
            result.source = collected[0].source
            return result

        if api_key:
            try:
                ai_posts = self.fetch_posts_openrouter(api_key, model, handle, profile_url, max_posts)
                for p in ai_posts:
                    if p.shortcode not in seen:
                        detailed = self.scrape_post(p.url)
                        if detailed:
                            p.visualizacoes = max(p.visualizacoes, detailed.visualizacoes)
                            p.curtidas = max(p.curtidas, detailed.curtidas)
                            p.comentarios = max(p.comentarios, detailed.comentarios)
                            p.compartilhamentos = max(p.compartilhamentos, detailed.compartilhamentos)
                            p.source = detailed.source
                        collected.append(p)
                        seen.add(p.shortcode)
                        self._sleep()
                if collected:
                    result.publicacoes = collected
                    result.source = "openrouter+scrape"
                    return result
            except Exception as exc:
                print(f"  [aviso] OpenRouter scrape @{handle}: {exc}", file=sys.stderr)

        return result
