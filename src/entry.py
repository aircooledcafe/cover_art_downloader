"""Cloudflare Python Worker entrypoint for Cover Art Downloader.

Implements the same JSON API as ``app.py`` using the native Cloudflare Workers
Python runtime SDK (``workers``), while static assets (``/``, ``/static/*``)
are served directly from ``./public`` via Cloudflare Workers Static Assets.
"""

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from workers import Request, Response, WorkerEntrypoint, fetch

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
COVER_ART_API = "https://coverartarchive.org"

DEFAULT_USER_AGENT = (
    "CoverArtDownloader/1.0 ( https://github.com/aircooledcafe/cover_art_downloader )"
)

# Only proxy downloads from hosts that actually serve Cover Art Archive images.
ALLOWED_DOWNLOAD_HOSTS = ("coverartarchive.org", "archive.org")

MBID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# Display order for art types; anything else is sorted alphabetically after these.
ART_TYPE_ORDER = [
    "Front", "Back", "Booklet", "Medium", "Tray", "Obi", "Spine", "Track",
    "Liner", "Sticker", "Poster", "Watermark", "Raw/Unedited", "Matrix/Runout",
    "Top", "Bottom", "Other", "Unknown",
]


# --------------------------------------------------------------------------- #
# Per-isolate rate limiting & caching
# --------------------------------------------------------------------------- #
_mb_last_call = 0.0

CAA_CACHE_TTL = 60 * 60  # 1 hour
CAA_CACHE_MAX = 2000
_caa_cache: Dict[str, Any] = {}


class CoverArtError(Exception):
    """Raised when the Cover Art Archive can't be reached or errors."""


def _user_agent(env: Any) -> str:
    if env is not None:
        val = getattr(env, "COVER_ART_USER_AGENT", None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return os.environ.get("COVER_ART_USER_AGENT", DEFAULT_USER_AGENT)


def _error(message: str, status: int) -> Response:
    return Response.json({"error": message}, status=status)


def _get_param(params: Dict[str, List[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    return values[0] if values else default


def _lucene_escape(value: str) -> str:
    """Escape a user value so it can be used inside a quoted Lucene phrase."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_query(album: str, artist: str, free_text: str) -> str:
    parts: List[str] = []
    if album:
        parts.append(f'release:"{_lucene_escape(album)}"')
    if artist:
        parts.append(f'artist:"{_lucene_escape(artist)}"')
    if free_text:
        parts.append(f"({free_text})")
    return " AND ".join(parts)


def _artist_credit(credits: Optional[List[Dict[str, Any]]]) -> str:
    if not credits:
        return ""
    return "".join(f"{c.get('name', '')}{c.get('joinphrase', '')}" for c in credits)


def _simplify_release(rel: Dict[str, Any]) -> Dict[str, Any]:
    media = rel.get("media") or []
    formats = sorted({m.get("format") for m in media if m.get("format")})
    labels = [
        li["label"]["name"]
        for li in (rel.get("label-info") or [])
        if li.get("label") and li["label"].get("name")
    ]
    rg = rel.get("release-group") or {}
    return {
        "id": rel.get("id"),
        "title": rel.get("title"),
        "artist": _artist_credit(rel.get("artist-credit")),
        "date": rel.get("date", ""),
        "country": rel.get("country", ""),
        "status": rel.get("status", ""),
        "disambiguation": rel.get("disambiguation", ""),
        "score": rel.get("score"),
        "track_count": rel.get("track-count"),
        "formats": formats,
        "labels": labels,
        "barcode": rel.get("barcode", ""),
        "primary_type": rg.get("primary-type", ""),
        "release_group_id": rg.get("id"),
        "thumbnail": f"{COVER_ART_API}/release/{rel.get('id')}/front-250",
    }


def _safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value).strip(" .")
    return value[:180] or "cover"


def _is_allowed_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and any(
        host == h or host.endswith("." + h) for h in ALLOWED_DOWNLOAD_HOSTS
    )


def _summarise_types(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for img in (payload or {}).get("images", []):
        for t in img.get("types") or ["Unknown"]:
            counts[t] = counts.get(t, 0) + 1
    rank = {t: i for i, t in enumerate(ART_TYPE_ORDER)}
    ordered = sorted(counts, key=lambda t: (rank.get(t, len(rank)), t))
    return [{"type": t, "count": counts[t]} for t in ordered]


async def _musicbrainz_get(
    path: str, params: Dict[str, Any], user_agent: str
) -> Response:
    global _mb_last_call
    query = urlencode({**params, "fmt": "json"})
    url = f"{MUSICBRAINZ_API}/{path}?{query}"
    headers = {"User-Agent": user_agent, "Accept": "application/json"}

    for attempt in range(2):
        wait = (1.0 if attempt == 0 else 1.5) - (time.monotonic() - _mb_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await fetch(url, headers=headers)
        finally:
            _mb_last_call = time.monotonic()
        # 503 is MusicBrainz's rate-limit response; back off and retry once.
        if resp.status != 503:
            break
    return resp


async def _fetch_caa(mbid: str, user_agent: str) -> Optional[Dict[str, Any]]:
    """Return the Cover Art Archive JSON for a release, or None if it has no art."""
    now = time.monotonic()
    hit = _caa_cache.get(mbid)
    if hit and now - hit[0] < CAA_CACHE_TTL:
        return hit[1]

    url = f"{COVER_ART_API}/release/{mbid}"
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    resp: Optional[Response] = None
    last_exc: Optional[Exception] = None

    for attempt in range(2):  # archive.org occasionally fails transiently; retry once.
        try:
            resp = await fetch(url, headers=headers)
            if resp.status < 500:
                break
        except Exception as exc:
            last_exc, resp = exc, None
        if attempt == 0:
            await asyncio.sleep(0.5)

    if resp is None:
        raise CoverArtError(f"Could not reach the Cover Art Archive: {last_exc}")

    if resp.status == 404:
        payload = None
    elif 200 <= resp.status < 300:
        payload = json.loads(await resp.text())
    else:
        raise CoverArtError(f"Cover Art Archive returned HTTP {resp.status}.")

    if len(_caa_cache) >= CAA_CACHE_MAX:
        oldest = min(_caa_cache, key=lambda k: _caa_cache[k][0])
        _caa_cache.pop(oldest, None)
    _caa_cache[mbid] = (now, payload)
    return payload


# --------------------------------------------------------------------------- #
# Route handlers
# --------------------------------------------------------------------------- #
async def _handle_search(params: Dict[str, List[str]], user_agent: str) -> Response:
    album = _get_param(params, "album").strip()
    artist = _get_param(params, "artist").strip()
    free_text = _get_param(params, "q").strip()
    query = _build_query(album, artist, free_text)
    if not query:
        return _error("Please provide an album, artist or search query.", 400)

    try:
        limit = max(1, min(int(_get_param(params, "limit", "25")), 100))
        offset = max(0, int(_get_param(params, "offset", "0")))
    except ValueError:
        return _error("limit and offset must be integers.", 400)

    try:
        resp = await _musicbrainz_get(
            "release", {"query": query, "limit": limit, "offset": offset}, user_agent
        )
    except Exception as exc:
        return _error(f"Could not reach MusicBrainz: {exc}", 502)

    if resp.status == 503:
        return _error(
            "MusicBrainz is rate limiting requests. Please try again shortly.", 503
        )
    if not (200 <= resp.status < 300):
        return _error(f"MusicBrainz returned HTTP {resp.status}.", 502)

    data = json.loads(await resp.text())
    return Response.json(
        {
            "query": query,
            "count": data.get("count", 0),
            "offset": data.get("offset", offset),
            "limit": limit,
            "releases": [_simplify_release(r) for r in data.get("releases", [])],
        }
    )


async def _handle_art_types(mbid: str, user_agent: str) -> Response:
    if not MBID_RE.match(mbid):
        return _error("Invalid MBID.", 400)
    try:
        payload = await _fetch_caa(mbid, user_agent)
    except CoverArtError as exc:
        return _error(str(exc), 502)

    types = _summarise_types(payload)
    return Response.json(
        {
            "id": mbid,
            "has_art": bool(payload and payload.get("images")),
            "image_count": len((payload or {}).get("images", [])),
            "types": types,
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _handle_release(mbid: str, user_agent: str) -> Response:
    if not MBID_RE.match(mbid):
        return _error("Invalid MBID.", 400)

    release: Dict[str, Any] = {"id": mbid}
    try:
        mb = await _musicbrainz_get(
            f"release/{mbid}",
            {"inc": "artist-credits+labels+release-groups"},
            user_agent,
        )
        if 200 <= mb.status < 300:
            release = _simplify_release(json.loads(await mb.text()))
    except Exception:
        pass  # Metadata is nice-to-have; artwork is the essential part.

    try:
        payload = await _fetch_caa(mbid, user_agent)
    except CoverArtError as exc:
        return _error(str(exc), 502)

    if payload is None:
        return Response.json({"release": release, "images": [], "caa_url": None})

    images = []
    for img in payload.get("images", []):
        thumbs = img.get("thumbnails") or {}
        sizes = []
        for key, label in (("250", "250px"), ("500", "500px"), ("1200", "1200px")):
            url = thumbs.get(key) or thumbs.get(
                {"250": "small", "500": "large"}.get(key, "")
            )
            if url:
                sizes.append({"key": key, "label": label, "url": url})
        if img.get("image"):
            sizes.append({"key": "original", "label": "Original", "url": img["image"]})

        images.append(
            {
                "id": img.get("id"),
                "types": img.get("types") or ["Unknown"],
                "front": img.get("front", False),
                "back": img.get("back", False),
                "comment": img.get("comment", ""),
                "approved": img.get("approved", False),
                "preview": thumbs.get("500")
                or thumbs.get("large")
                or img.get("image"),
                "sizes": sizes,
            }
        )

    images.sort(key=lambda i: (not i["front"], not i["back"]))
    return Response.json(
        {"release": release, "images": images, "caa_url": payload.get("release")}
    )


async def _handle_download(params: Dict[str, List[str]], user_agent: str) -> Response:
    url = _get_param(params, "url")
    name = _get_param(params, "name", "cover")
    if not _is_allowed_host(url):
        return _error(
            "Downloads are only permitted from the Cover Art Archive.", 400
        )

    try:
        upstream = await fetch(url, headers={"User-Agent": user_agent})
    except Exception as exc:
        return _error(f"Could not fetch image: {exc}", 502)

    final_url = upstream.js_object.url or url
    if not _is_allowed_host(final_url):
        return _error("Image redirected to an unexpected host.", 400)
    if not (200 <= upstream.status < 300):
        return _error(
            f"Image request failed with HTTP {upstream.status}.", 502
        )

    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    ext = os.path.splitext(urlparse(final_url).path)[1].lower()
    if not ext:
        ext = {
            "image/png": ".png",
            "image/gif": ".gif",
            "application/pdf": ".pdf",
        }.get(content_type.split(";")[0], ".jpg")
    filename = _safe_filename(name) + ext

    headers = {
        "Content-Type": content_type,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]

    return Response(upstream.body, status=200, headers=headers)


# --------------------------------------------------------------------------- #
# Worker Entrypoint
# --------------------------------------------------------------------------- #
class Default(WorkerEntrypoint):
    async def fetch(self, request: Request) -> Response:
        parsed = urlparse(request.url)
        path = parsed.path
        params = parse_qs(parsed.query, keep_blank_values=True)
        user_agent = _user_agent(self.env)

        if path.startswith("/api/"):
            method = getattr(request.method, "value", str(request.method)).upper()
            if method != "GET":
                return _error("Method not allowed.", 405)

            if path == "/api/search":
                return await _handle_search(params, user_agent)

            if path == "/api/download":
                return await _handle_download(params, user_agent)

            if path.startswith("/api/release/"):
                rest = path[len("/api/release/") :].strip("/")
                parts = [unquote(p) for p in rest.split("/") if p]
                if len(parts) == 2 and parts[1] == "art-types":
                    return await _handle_art_types(parts[0], user_agent)
                if len(parts) == 1:
                    return await _handle_release(parts[0], user_agent)

            return _error("Not found.", 404)

        # Static assets fallback (when not already served directly by the edge)
        if hasattr(self.env, "ASSETS"):
            asset_resp = await self.env.ASSETS.fetch(request)
            if asset_resp.status == 404:
                index_url = f"{parsed.scheme}://{parsed.netloc}/index.html"
                index_resp = await self.env.ASSETS.fetch(Request(index_url))
                if 200 <= index_resp.status < 300:
                    return Response(
                        index_resp.body,
                        status=404,
                        headers={"Content-Type": "text/html; charset=utf-8"},
                    )
            return asset_resp

        return _error("Not found.", 404)
