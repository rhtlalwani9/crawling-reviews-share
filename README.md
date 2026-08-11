# Review Aggregation Crawler

Aggregates reviews from anti-bot-protected sites behind one HTTP API.

```
GET /reviews/aggregate?url=<profile url>&source_name=<g2>&filter_date=<YYYY-MM-DD>
```

A browser clears the challenge once (~25 s); an impersonating HTTP client then does the volume at
~0.7 s per page.

---

## Quick start

Requires **Python 3.11+** and **Google Chrome**. There is no install step — `PYTHONPATH=src` is the
whole mechanism.

### macOS / Linux

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m crawling_reviews.api      # → http://localhost:8080
```

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = "src"
.venv\Scripts\python -m crawling_reviews.api
```

### Docker

Portable, and the only option without Chrome installed. See [Limitations](#limitations) on Apple
Silicon.

```bash
docker compose --profile build build base   # OS, Chrome, fonts — slow, once
docker compose up --build
```

`make up`, `make test` and `make logs` wrap these on macOS/Linux; `make` is not required.

### Headless Linux server

Headless Chrome is detected by these targets, so run it against a virtual display:

```bash
sudo apt-get install -y xvfb
xvfb-run -a env PYTHONPATH=src .venv/bin/python -m crawling_reviews.api
```

### First request

```bash
curl -sG "http://localhost:8080/reviews/aggregate" \
  --data-urlencode "url=https://www.g2.com/products/asana/reviews" \
  --data-urlencode "max_pages=3" | jq
```

The first call takes 20–40 s while it mints a browser session; later calls reuse it (~1–2 s).

Without Google Chrome installed, run `.venv/bin/patchright install chromium` — it falls back to
Chromium, which has a weaker fingerprint but works. Port already in use? Set `PORT`, or `HOST_PORT`
for Docker.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /reviews/aggregate` | Aggregate reviews |
| `GET /health` | Liveness + registered sources |
| `GET /profiles` | Pool state, health scores, cooldowns |

**Parameters** — `url` (required), `source_name` (optional, inferred from the host), `filter_date`
(`YYYY-MM-DD`, keeps reviews on or after it), `max_pages` (safety cap).

```json
{
  "review_count": 13850,
  "aggregated_reviews": [
    { "rating": 5.0, "review_date": "2026-08-09", "reviewer_name": "Daniel C.", "comment": "…" }
  ],
  "review_aggregated_count": 100,
  "response_code": 200,
  "meta": { "source": "g2", "pages_fetched": 10, "avg_ms_per_page": 647,
            "stopped_early": false, "session_minted": true, "profile_attempts": 2 }
}
```

`review_count` is the total the **source advertises**; `review_aggregated_count` is what was returned.
They differ when `filter_date` applies or when the source caps pagination.

Errors return `{"response_code": N, "error": {"message": "…", "code": "…"}}` with codes
`VALIDATION_ERROR`, `NETWORK_ERROR` (retried), `BLOCKED` (retried on a *different profile*),
`MINT_FAILED`, `NO_PROFILE_AVAILABLE`, `PARSE_ERROR`.

**Ordering** — newest first. `order=most_recent` is applied to every request and results are sorted by
date descending; a `url` that already carries its own query is normalised, so ordering holds whatever
is passed in. G2 pins a review mid-page, so the source order is not strictly monotonic; the
`filter_date` early exit therefore requires *every* dated review on a page to precede the cutoff.

**Postman** — import [`docs/postman_collection.json`](docs/postman_collection.json). Eight requests
covering the success paths and each error case; retarget via the `targetUrl` collection variable. A
target URL carrying several query parameters must be URL-encoded, or its `&` separators are parsed as
API parameters.

---

## How it works

```
request → CrawlService ──leases── profiles/     (AVAILABLE → LEASED → COOLDOWN → RETIRED)
              │
              ├─ 1. mint once      session/minter (patchright + Chrome) → cookies + CSRF + UA
              └─ 2. replay cheaply http_fetcher (curl_cffi) ← sources/ (paginate + parse)
                                   wrapped by RateLimited → Retrying (backoff + full jitter)
```

These sites refuse plain HTTP clients *before* HTTP begins — at the TLS handshake. Chrome speaks
BoringSSL and emits GREASE, ALPS and a specific extension ordering that OpenSSL-based clients cannot
reproduce, because the handshake's shape is a property of the library build rather than a runtime
option. A browser is needed to *earn* credentials, but not to *use* them. Getting through needs
Chrome-shaped TLS, a cleared session, and a request shaped like in-page activity; any two still 403.

**The profile pool** is the core module. A *signature* is an immutable fingerprint template; a
*profile* is a live instance owning a Chrome user-data directory and a health record.

- **Sliding-window health** — block *timestamps*, not counters. Three blocks in ten minutes is a
  burned identity; three over two days is normal wear. A 429 is never a strike.
- **Cooldown before retirement** — a replacement is *worse* than what it replaces: no history, and
  each replacement costs a fresh challenge, which degrades the egress IP.
- **Heartbeat leases** — a legitimate crawl runs for half an hour, so lease *age* cannot identify a
  dead holder. Holders prove liveness; the reaper reclaims on silence.
- **Exclusive access** — Chrome enforces one holder per user-data directory via `SingletonLock`, so
  the pool models that constraint rather than fighting it. Selection is best-health-first.

Storage is scoped per platform (`_data/<macOS|Linux>/…`) because a Chrome profile is not portable
across operating systems, and neither is the clearance it holds.

---

## Limitations

**G2 caps anonymous pagination at 10 pages (100 reviews).** Verified: pages 11, 12, 20, 50, 139 and
200 all return HTTP 200 with byte-identical content — the site clamps silently rather than erroring,
so a crawler trusting "200 with rows" would loop forever collecting duplicates. De-duplication in
`sources/base.py` guards against that. Going beyond 100 requires partitioning the query space (per
star rating or sort order); not implemented.

**WebGL reports a software renderer under Docker on Apple Silicon.** No Chrome build exists for
Linux/ARM, so the image is `linux/amd64` and must be emulated; Chrome then falls back to SwiftShader.
Mesa (llvmpipe) is installed and selectable with `CHROME_GL=mesa` — it would report what an ordinary
Linux desktop without GPU drivers reports — but under Rosetta every Mesa path yields no WebGL context
at all, which is a worse signal. **Untested on a native x86_64 Linux host, where it is expected to
work**; I had no access to one. No rebuild needed, only the environment variable.

Renderer spoofing was deliberately not implemented: patching `getParameter` turns one check green
while `MAX_TEXTURE_SIZE`, the extension list and the rendered pixels still describe a software
rasteriser, and a browser contradicting itself is a stronger signal than one honestly reporting no
GPU. The same reasoning removed a `hardwareConcurrency` override — it reached the main thread but not
Web Workers, so the two disagreed.

**Multiple profiles behind one IP is weak.** Three identities from one address still look like one
machine running three sessions; the pool's value scales with distinct *exits*. Proxy support is
per-signature rather than global, because a cookie jar is only valid through the exit that earned it.

**IP reputation dominates.** During development, identical code went from working to hard-blocked
purely because the test address degraded. Priority: **IP ≫ profile age > fingerprint coherence.**

<details>
<summary>G2 specifics</summary>

- `/products/<slug>/reviews` is a **shell**. Reviews live in a lazily-loaded Turbo frame at
  `/products/<slug>/reviews_and_filters`, and the document's `?page=N` is never propagated into it —
  so every `?page=N` on `/reviews` returns page 1, in HTTP *and* in a real browser.
- Review cards carry bare `<meta content="…">` tags with no `name` or `itemprop`. Values are matched
  **by shape** (ISO date → date, decimal → rating), never by index.
</details>

---

## Layout

```
src/crawling_reviews/
├── config.py            env → typed config
├── core/                typed errors, structured logging
├── profiles/            signatures · models · store · manager   ← the pool
├── crawl/
│   ├── session/         minter · session cache · egress locale
│   ├── http_fetcher/    base · impersonate · httpx · classify · decorators
│   ├── rate_limit.py    per-host token bucket
│   └── service.py       orchestration + retry-with-a-different-profile
├── sources/             SourceAdapter + registry + g2/
└── api/                 FastAPI app, routes, schemas
```

```bash
PYTHONPATH=src python -m pytest -q      # 38 tests, <1 s, no network or browser
```
