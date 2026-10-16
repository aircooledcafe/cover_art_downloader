# Cover Art Downloader

A Flask web app for searching [MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API/Search) releases and downloading their artwork from the [Cover Art Archive](https://musicbrainz.org/doc/Cover_Art_Archive/API).

- Search by album and/or artist
- Pick the release you want from the results
- See all the artwork for that release (Front, Back, Booklet, Medium, …), filter it by type, and download it at 250px, 500px, 1200px or the original size

All API calls use JSON (`fmt=json`). The UI uses only plain HTML, CSS and JavaScript, with no external CSS frameworks.

## Setup

```bash
python3 -m venv cover_art_downloader_venv
source cover_art_downloader_venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5001.

Set `PORT` to use a different port. MusicBrainz asks clients to send a User-Agent that identifies them, so set `COVER_ART_USER_AGENT` to something like `MyCoverApp/1.0 ( you@example.com )`.

## JSON API

| Endpoint | Description |
|---|---|
| `GET /api/search?album=&artist=&q=&limit=&offset=` | Search MusicBrainz releases |
| `GET /api/release/<mbid>/art-types` | Which artwork types a release has, with counts (used by the results list) |
| `GET /api/release/<mbid>` | Release details plus the available artwork and sizes |
| `GET /api/download?url=&name=` | Fetches an image through the app with a readable filename (only allows Cover Art Archive and archive.org hosts) |

MusicBrainz requests are limited to 1 per second, as MusicBrainz requires.
