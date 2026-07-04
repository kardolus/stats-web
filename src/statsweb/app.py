"""stats.kardol.us — a cross-site visitor rollup over the self-hosted Umami DB.

Umami's OSS dashboard is strictly per-website; this page reads the same Postgres
read-only (role stats_ro) and shows the ONE number Umami won't: total visits and
pageviews across every tracked site, plus a per-site breakdown. Flightdeck styling,
shared with the other kardol.us apps.
"""

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

_CSS = (Path(__file__).parent / "static" / "app.css").read_text()
DATABASE_URL = os.environ["DATABASE_URL"]
CACHE_TTL = int(os.environ.get("CACHE_TTL", "120"))

# ───────────────────────── db (pool + tiny cache) ─────────────────────────
_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(1, 4, dsn=DATABASE_URL)
    return _pool


def _q(sql: str) -> list[dict]:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.rollback()  # read-only; never hold a txn open
        pool.putconn(conn)


def _cached(key: str, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


# ───────────────────────── queries ─────────────────────────
def totals() -> dict:
    def run():
        r = _q(
            """
            SELECT count(DISTINCT session_id)                               AS visits,
                   count(*) FILTER (WHERE event_type = 1)                   AS pageviews,
                   count(DISTINCT session_id)
                     FILTER (WHERE created_at > now() - interval '7 days')  AS visits_7d,
                   count(DISTINCT session_id)
                     FILTER (WHERE created_at > now() - interval '24 hours')AS visits_24h,
                   min(created_at)                                          AS since
            FROM website_event
            """
        )[0]
        return r
    return _cached("totals", run)


def per_site() -> list[dict]:
    def run():
        return _q(
            """
            SELECT w.name, w.domain,
                   count(DISTINCT e.session_id)                             AS visits,
                   count(e.*) FILTER (WHERE e.event_type = 1)               AS pageviews,
                   count(DISTINCT e.session_id)
                     FILTER (WHERE e.created_at > now() - interval '7 days')AS visits_7d,
                   min(e.created_at)                                        AS since
            FROM website w
            LEFT JOIN website_event e ON e.website_id = w.website_id
            GROUP BY w.name, w.domain
            ORDER BY visits DESC NULLS LAST, w.name
            """
        )
    return _cached("per_site", run)


# ───────────────────────── rendering ─────────────────────────
def _fmt(n) -> str:
    return f"{int(n or 0):,}"


def _ago(dt) -> str:
    if not dt:
        return "—"
    days = (datetime.now(timezone.utc) - dt).days
    return dt.strftime("%b %-d, %Y") + (f" · {days}d" if days else "")


FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="5" fill="#1da46c"/>'
    '<path fill="#fff" d="M4 20V4h2.5v13.5H20V20zM8.5 15V8.5H11V15zm4-9.5V15H15V5.5zm4 4.5V15H19v-5z"/>'
    '</svg>'
)
_LOGO = (
    '<svg class="logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M3 3v18h18"/><rect x="7" y="11" width="3" height="6"/>'
    '<rect x="12" y="7" width="3" height="10"/><rect x="17" y="13" width="3" height="4"/></svg>'
)


def render() -> str:
    t = totals()
    sites = per_site()
    active = [s for s in sites if (s["visits"] or 0) > 0]
    rows = ""
    for s in sites:
        dom = s["domain"] or ""
        name = (s["name"] or dom or "—")
        link = f'<a href="https://{dom}">{name}</a>' if dom else name
        dim = "" if (s["visits"] or 0) > 0 else ' style="opacity:.5"'
        rows += (
            f'<tr{dim}><td>{link}</td>'
            f'<td class="num">{_fmt(s["visits"])}</td>'
            f'<td class="num">{_fmt(s["visits_7d"])}</td>'
            f'<td class="num">{_fmt(s["pageviews"])}</td>'
            f'<td class="num meta">{_ago(s["since"])}</td></tr>'
        )
    body = f"""
    <div class="kpis">
      <div class="kpi"><div class="kpi-n">{_fmt(t["visits"])}</div><div class="kpi-l">total visits · all sites</div></div>
      <div class="kpi"><div class="kpi-n">{_fmt(t["pageviews"])}</div><div class="kpi-l">total pageviews</div></div>
      <div class="kpi"><div class="kpi-n">{_fmt(t["visits_7d"])}</div><div class="kpi-l">visits · last 7 days</div></div>
      <div class="kpi"><div class="kpi-n">{_fmt(t["visits_24h"])}</div><div class="kpi-l">visits · last 24 h</div></div>
      <div class="kpi"><div class="kpi-n">{len(active)}<span class="meta" style="font-size:.5em"> / {len(sites)}</span></div><div class="kpi-l">sites with traffic</div></div>
    </div>
    <p class="meta">Aggregated across every site tracked by <a href="https://analytics.kardol.us">Umami</a> · tracking since {_ago(t["since"])} · updates every {CACHE_TTL // 60 or 1} min.</p>
    <div class="card">
      <div class="card-head"><h2>By site</h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Site</th><th class="num">Visits</th><th class="num">7&nbsp;days</th><th class="num">Pageviews</th><th class="num">Since</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f8f9fa" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0d1117" media="(prefers-color-scheme: dark)">
<title>Analytics · all sites</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="description" content="Total visits and pageviews across every kardol.us site, aggregated from self-hosted Umami analytics.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_CSS}</style>
<script>
  var _t = localStorage.getItem('theme'); document.documentElement.classList.toggle('dark', _t ? _t === 'dark' : (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches));
  function toggleDark(){{ document.documentElement.classList.toggle('dark'); localStorage.setItem('theme', document.documentElement.classList.contains('dark') ? 'dark' : 'light'); }}
</script>
</head>
<body>
<div class="top-bar"><div class="top-utility">
  <div class="brand">{_LOGO} Analytics</div>
  <div class="top-actions">
    <a class="nbhd-select" href="https://analytics.kardol.us" style="text-decoration:none;line-height:32px">Open Umami →</a>
    <button class="dark-toggle" onclick="toggleDark()" title="Toggle dark mode">◐</button>
  </div>
</div></div>
<main class="wrap">{body}</main>
</body>
</html>"""


# ───────────────────────── routes ─────────────────────────
async def home(r):
    return HTMLResponse(render())


async def favicon(r):
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


async def healthz(r):
    return PlainTextResponse("ok")


async def ready(r):
    try:
        _q("SELECT 1")
        return PlainTextResponse("ready")
    except Exception as e:  # noqa: BLE001
        return PlainTextResponse(f"not ready: {e}", status_code=503)


app = Starlette(routes=[
    Route("/", home),
    Route("/favicon.svg", favicon),
    Route("/healthz", healthz),
    Route("/ready", ready),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
