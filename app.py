"""Cover Art Downloader.

A small Flask web application for searching MusicBrainz releases and
downloading their album artwork from the Cover Art Archive.

APIs used:
  * MusicBrainz search:  https://musicbrainz.org/doc/MusicBrainz_API/Search
  * Cover Art Archive:   https://musicbrainz.org/doc/Cover_Art_Archive/API

All upstream requests ask for JSON (``fmt=json`` / ``Accept: application/json``)
and every endpoint exposed by this app returns JSON (apart from the image
download proxy, which streams the image itself).
"""

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
COVER_ART_API = "https://coverartarchive.org"

# MusicBrainz asks every client to identify itself with a meaningful User-Agent.
USER_AGENT = os.environ.get(
    "COVER_ART_USER_AGENT",
    "CoverArtDownloader/1.0 ( https://github.com/aircooledcafe/cover_art_downloader )",
)

REQUEST_TIMEOUT = 15  # seconds

# Only proxy downloads from hosts that actually serve Cover Art Archive images.
ALLOWED_DOWNLOAD_HOSTS = ("coverartarchive.org", "archive.org")

MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

app = Flask(
    __name__,
    static_folder="public/static",
    static_url_path="/static",
    template_folder="public",
)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


# --------------------------------------------------------------------------- #
# MusicBrainz rate limiting (max 1 request / second per their guidelines)
# --------------------------------------------------------------------------- #
_mb_lock = threading.Lock()
_mb_last_call = 0.0


def _musicbrainz_get(path: str, params: Dict[str, Any]) -> requests.Response:
    global _mb_last_call
    params = dict(params, fmt="json")
    with _mb_lock:
        for attempt in range(2):
            wait = (1.0 if attempt == 0 else 1.5) - (time.monotonic() - _mb_last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = session.get(f"{MUSICBRAINZ_API}/{path}", params=params, timeout=REQUEST_TIMEOUT)
            finally:
                _mb_last_call = time.monotonic()
            # 503 is MusicBrainz's rate-limit response; back off and retry once.
            if resp.status_code != 503:
                break
        return resp


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _error(message: str, status: int) -> Response:
    resp = jsonify({"error": message})
    resp.status_code = status
    return resp


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
    host = (urlparse(url).hostname or "").lower()
    return urlparse(url).scheme in ("http", "https") and any(
        host == h or host.endswith("." + h) for h in ALLOWED_DOWNLOAD_HOSTS
    )


# --------------------------------------------------------------------------- #
# Cover Art Archive fetching (with a small in-memory cache)
# --------------------------------------------------------------------------- #
class CoverArtError(Exception):
    """Raised when the Cover Art Archive can't be reached or errors."""


CAA_CACHE_TTL = 60 * 60  # 1 hour
CAA_CACHE_MAX = 2000
_caa_cache: Dict[str, Any] = {}
_caa_cache_lock = threading.Lock()


def _fetch_caa(mbid: str) -> Optional[Dict[str, Any]]:
    """Return the Cover Art Archive JSON for a release, or None if it has no art."""
    now = time.monotonic()
    with _caa_cache_lock:
        hit = _caa_cache.get(mbid)
        if hit and now - hit[0] < CAA_CACHE_TTL:
            return hit[1]

    resp = None
    last_exc: Optional[Exception] = None
    for attempt in range(2):  # archive.org occasionally fails transiently; retry once.
        try:
            resp = session.get(f"{COVER_ART_API}/release/{mbid}", timeout=REQUEST_TIMEOUT)
            if resp.status_code < 500:
                break
        except requests.RequestException as exc:
            last_exc, resp = exc, None
        if attempt == 0:
            time.sleep(0.5)

    if resp is None:
        raise CoverArtError(f"Could not reach the Cover Art Archive: {last_exc}")

    if resp.status_code == 404:
        payload = None
    elif resp.ok:
        payload = resp.json()
    else:
        raise CoverArtError(f"Cover Art Archive returned HTTP {resp.status_code}.")

    with _caa_cache_lock:
        if len(_caa_cache) >= CAA_CACHE_MAX:
            # Drop the oldest entry.
            oldest = min(_caa_cache, key=lambda k: _caa_cache[k][0])
            _caa_cache.pop(oldest, None)
        _caa_cache[mbid] = (now, payload)
    return payload


# Display order for art types; anything else is sorted alphabetically after these.
ART_TYPE_ORDER = [
    "Front", "Back", "Booklet", "Medium", "Tray", "Obi", "Spine", "Track",
    "Liner", "Sticker", "Poster", "Watermark", "Raw/Unedited", "Matrix/Runout",
    "Top", "Bottom", "Other", "Unknown",
]


def _summarise_types(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for img in (payload or {}).get("images", []):
        for t in img.get("types") or ["Unknown"]:
            counts[t] = counts.get(t, 0) + 1
    rank = {t: i for i, t in enumerate(ART_TYPE_ORDER)}
    ordered = sorted(counts, key=lambda t: (rank.get(t, len(rank)), t))
    return [{"type": t, "count": counts[t]} for t in ordered]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    """Search MusicBrainz releases.

    Query params: album, artist, q (raw Lucene / free text), limit, offset.
    """
    album = request.args.get("album", "").strip()
    artist = request.args.get("artist", "").strip()
    free_text = request.args.get("q", "").strip()
    query = _build_query(album, artist, free_text)
    if not query:
        return _error("Please provide an album, artist or search query.", 400)

    try:
        limit = max(1, min(int(request.args.get("limit", 25)), 100))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        return _error("limit and offset must be integers.", 400)

    try:
        resp = _musicbrainz_get("release", {"query": query, "limit": limit, "offset": offset})
    except requests.RequestException as exc:
        return _error(f"Could not reach MusicBrainz: {exc}", 502)

    if resp.status_code == 503:
        return _error("MusicBrainz is rate limiting requests. Please try again shortly.", 503)
    if not resp.ok:
        return _error(f"MusicBrainz returned HTTP {resp.status_code}.", 502)

    data = resp.json()
    return jsonify(
        {
            "query": query,
            "count": data.get("count", 0),
            "offset": data.get("offset", offset),
            "limit": limit,
            "releases": [_simplify_release(r) for r in data.get("releases", [])],
        }
    )


@app.route("/api/release/<mbid>/art-types")
def api_art_types(mbid: str):
    """Return a summary of which artwork types exist for a release.

    Used by the results list so users can see what's available before
    clicking through. Results are cached, so opening the release afterwards
    doesn't fetch from the Cover Art Archive again.
    """
    if not MBID_RE.match(mbid):
        return _error("Invalid MBID.", 400)
    try:
        payload = _fetch_caa(mbid)
    except CoverArtError as exc:
        return _error(str(exc), 502)

    types = _summarise_types(payload)
    return jsonify(
        {
            "id": mbid,
            "has_art": bool(payload and payload.get("images")),
            "image_count": len((payload or {}).get("images", [])),
            "types": types,
        }
    )


@app.route("/api/release/<mbid>")
def api_release(mbid: str):
    """Return release details plus all cover art available for it."""
    if not MBID_RE.match(mbid):
        return _error("Invalid MBID.", 400)

    # Release metadata (title / artist) from MusicBrainz.
    release: Dict[str, Any] = {"id": mbid}
    try:
        mb = _musicbrainz_get(f"release/{mbid}", {"inc": "artist-credits+labels+release-groups"})
        if mb.ok:
            release = _simplify_release(mb.json())
    except requests.RequestException:
        pass  # Metadata is nice-to-have; artwork is the essential part.

    # Cover art listing from the Cover Art Archive.
    try:
        payload = _fetch_caa(mbid)
    except CoverArtError as exc:
        return _error(str(exc), 502)

    if payload is None:
        return jsonify({"release": release, "images": [], "caa_url": None})
    images = []
    for img in payload.get("images", []):
        thumbs = img.get("thumbnails") or {}
        # Cover Art Archive provides 250/500/1200 thumbnails plus the original.
        sizes = []
        for key, label in (("250", "250px"), ("500", "500px"), ("1200", "1200px")):
            url = thumbs.get(key) or (thumbs.get({"250": "small", "500": "large"}.get(key, "")))
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
                "preview": thumbs.get("500") or thumbs.get("large") or img.get("image"),
                "sizes": sizes,
            }
        )

    # Show front covers first, then backs, then everything else.
    images.sort(key=lambda i: (not i["front"], not i["back"]))
    return jsonify({"release": release, "images": images, "caa_url": payload.get("release")})


@app.route("/api/download")
def api_download():
    """Proxy an image from the Cover Art Archive with a friendly filename.

    Browsers ignore the ``download`` attribute for cross-origin links, so we
    stream the file through Flask and set Content-Disposition ourselves.
    """
    url = request.args.get("url", "")
    name = request.args.get("name", "cover")
    if not _is_allowed_host(url):
        return _error("Downloads are only permitted from the Cover Art Archive.", 400)

    try:
        upstream = session.get(url, stream=True, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return _error(f"Could not fetch image: {exc}", 502)

    if not _is_allowed_host(upstream.url):
        upstream.close()
        return _error("Image redirected to an unexpected host.", 400)
    if not upstream.ok:
        upstream.close()
        return _error(f"Image request failed with HTTP {upstream.status_code}.", 502)

    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    ext = os.path.splitext(urlparse(upstream.url).path)[1].lower()
    if not ext:
        ext = {"image/png": ".png", "image/gif": ".gif", "application/pdf": ".pdf"}.get(
            content_type.split(";")[0], ".jpg"
        )
    filename = _safe_filename(name) + ext

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), content_type=content_type, headers=headers)


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return _error("Not found.", 404)
    return render_template("index.html"), 404


if __name__ == "__main__":
    # Port 5000 is used by AirPlay Receiver on macOS, so default to 5001.
    app.run(host="0.0.0.0", debug=False, port=int(os.environ.get("PORT", 5001)))
