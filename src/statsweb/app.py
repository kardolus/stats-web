"""stats.kardol.us — a cross-site visitor rollup over the self-hosted Umami DB.

Umami's OSS dashboard is strictly per-website; this reads the same Postgres
read-only (role stats_ro) and shows what Umami won't: totals + patterns ACROSS
every tracked site. Two tabs — Overview (totals + per-site table) and Patterns
(traffic over time, where visitors come from incl. cross-site links, top pages,
audience). Flightdeck styling, shared with the other kardol.us apps.

Design note: traffic here is small (hundreds of visits), so everything is framed
in ABSOLUTE counts — no pie charts, no percent-growth badges, low-volume rows
grouped into "other". Web Vitals are intentionally omitted (Umami isn't collecting
them). "visit" = distinct session_id; "pageview" = website_event.event_type = 1.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
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
_OG = Path(__file__).parent / "static" / "og.png"

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


def _q(sql: str, args=None) -> list[dict]:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # pass params ONLY when present — a bare execute(sql) skips %-interpolation,
            # so literal % in LIKE patterns (e.g. '%kardol.us') needs no escaping.
            cur.execute(sql, args) if args else cur.execute(sql)
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


# ───────────────────────── queries: overview ─────────────────────────
def totals() -> dict:
    return _cached("totals", lambda: _q(
        """
        SELECT count(DISTINCT session_id)                                AS visits,
               count(*) FILTER (WHERE event_type = 1)                    AS pageviews,
               count(DISTINCT session_id)
                 FILTER (WHERE created_at > now() - interval '7 days')   AS visits_7d,
               count(DISTINCT session_id)
                 FILTER (WHERE created_at > now() - interval '24 hours') AS visits_24h,
               min(created_at)                                          AS since
        FROM website_event
        """)[0])


def per_site() -> list[dict]:
    return _cached("per_site", lambda: _q(
        """
        SELECT w.name, w.domain,
               count(DISTINCT e.session_id)                              AS visits,
               count(e.*) FILTER (WHERE e.event_type = 1)                AS pageviews,
               count(DISTINCT e.session_id)
                 FILTER (WHERE e.created_at > now() - interval '7 days') AS visits_7d,
               min(e.created_at)                                        AS since
        FROM website w LEFT JOIN website_event e ON e.website_id = w.website_id
        GROUP BY w.name, w.domain
        ORDER BY visits DESC NULLS LAST, w.name
        """))


# ───────────────────────── queries: patterns ─────────────────────────
def traffic_by_day(days: int = 14) -> dict:
    """Stacked daily visits per site over the window (sites with traffic only)."""
    def run():
        rows = _q(
            """
            SELECT (e.created_at AT TIME ZONE 'America/New_York')::date AS d,
                   w.name, count(DISTINCT e.session_id) AS v
            FROM website_event e JOIN website w USING (website_id)
            WHERE e.created_at > now() - (%s || ' days')::interval
            GROUP BY 1, 2 ORDER BY 1
            """, (days,))
        # dense day axis
        end = datetime.now(timezone.utc).date()
        axis = [(end - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        labels = [d.strftime("%b %-d") for d in axis]
        sites = sorted({r["name"] for r in rows})
        idx = {d: i for i, d in enumerate(axis)}
        series = {s: [0] * days for s in sites}
        for r in rows:
            if r["d"] in idx:
                series[r["name"]][idx[r["d"]]] = int(r["v"])
        # keep only sites with any traffic; order by total desc
        series = {s: v for s, v in series.items() if sum(v) > 0}
        order = sorted(series, key=lambda s: -sum(series[s]))
        return {"labels": labels, "sites": order, "series": {s: series[s] for s in order}}
    return _cached(f"traffic:{days}", run)


def referrers() -> dict:
    """Direct / external / cross-site split + top external + cross-site links."""
    def run():
        buckets = _q(
            """
            SELECT CASE
                     WHEN referrer_domain IS NULL OR referrer_domain = '' THEN 'direct'
                     WHEN referrer_domain LIKE '%kardol.us' THEN 'internal'
                     ELSE 'external' END AS kind,
                   count(DISTINCT session_id) AS visits
            FROM website_event WHERE event_type = 1 GROUP BY 1
            """)
        ext = _q(
            """
            SELECT referrer_domain AS src, count(DISTINCT session_id) AS visits
            FROM website_event
            WHERE event_type = 1 AND referrer_domain IS NOT NULL AND referrer_domain <> ''
              AND referrer_domain NOT LIKE '%kardol.us'
            GROUP BY 1 ORDER BY visits DESC LIMIT 10
            """)
        internal = _q(
            """
            SELECT e.referrer_domain AS src, w.domain AS dst, count(*) AS hits
            FROM website_event e JOIN website w USING (website_id)
            WHERE e.event_type = 1 AND e.referrer_domain LIKE '%kardol.us'
              AND e.referrer_domain <> w.domain
            GROUP BY 1, 2 ORDER BY hits DESC LIMIT 8
            """)
        b = {r["kind"]: int(r["visits"]) for r in buckets}
        return {"direct": b.get("direct", 0), "external": b.get("external", 0),
                "internal": b.get("internal", 0), "top_ext": ext, "links": internal}
    return _cached("referrers", run)


def top_pages() -> list[dict]:
    return _cached("top_pages", lambda: _q(
        """
        SELECT hostname, url_path,
               count(*)                     AS pageviews,
               count(DISTINCT session_id)   AS visits
        FROM website_event
        WHERE event_type = 1 AND hostname IS NOT NULL
        GROUP BY hostname, url_path
        ORDER BY pageviews DESC LIMIT 15
        """))


def audience() -> dict:
    def run():
        def top(col):
            return _q(
                f"SELECT COALESCE(NULLIF({col}, ''), 'unknown') AS k, "
                f"count(*) AS n FROM session GROUP BY 1 ORDER BY n DESC LIMIT 8")
        return {"device": top("device"), "browser": top("browser"), "country": top("country")}
    return _cached("audience", run)


# ───────────────────────── rendering ─────────────────────────
def _fmt(n) -> str:
    return f"{int(n or 0):,}"


def _ago(dt) -> str:
    if not dt:
        return "—"
    days = (datetime.now(timezone.utc) - dt).days
    return dt.strftime("%b %-d, %Y") + (f" · {days}d" if days else "")


# device/browser icons kept simple; country codes shown as-is (uppercased).
def _ranklist(rows, key="k", count="n", top_label=None) -> str:
    rows = list(rows)
    mx = max((int(r[count]) for r in rows), default=1) or 1
    out = ""
    for r in rows:
        label = str(r[key] or "unknown")
        if top_label:
            label = top_label(label)
        w = round(100 * int(r[count]) / mx)
        out += (
            f'<div class="rank"><div class="rank-bar" style="width:{w}%"></div>'
            f'<span class="rank-lbl">{label}</span>'
            f'<span class="rank-n">{_fmt(r[count])}</span></div>'
        )
    return out or '<p class="note">No data yet.</p>'


_PALETTE = ["#1da46c", "#3b82f6", "#f59e0b", "#a855f7", "#ef4444",
            "#14b8a6", "#ec4899", "#84cc16", "#6366f1", "#f97316"]

_PATTERNS_CSS = """
.rank{position:relative;display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;margin:2px 0;overflow:hidden}
.rank-bar{position:absolute;left:0;top:0;bottom:0;background:var(--accent-soft);z-index:0}
.rank-lbl{position:relative;z-index:1;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.rank-n{position:relative;z-index:1;font-family:var(--font-mono);font-size:13px;color:var(--fg)}
.trio{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.trio h3{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--meta);font-weight:600}
.refkpi{display:flex;gap:14px;margin-bottom:10px}
.refkpi .b{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px}
.refkpi .b .n{font-family:var(--font-mono);font-size:20px;font-weight:600}
.refkpi .b .l{font-size:11px;color:var(--meta);text-transform:uppercase;letter-spacing:.04em}
@media(max-width:640px){.trio{grid-template-columns:1fr}}
"""


def _pill(host):
    return f'<span class="org">{host}</span>'


def render_patterns() -> str:
    tr = traffic_by_day(14)
    rf = referrers()
    pages = top_pages()
    aud = audience()

    # traffic chart datasets (Chart.js, embedded inline)
    datasets = [
        {"label": s, "data": tr["series"][s],
         "backgroundColor": _PALETTE[i % len(_PALETTE)]}
        for i, s in enumerate(tr["sites"])
    ]
    chart_json = json.dumps({"labels": tr["labels"], "datasets": datasets})

    # referrers
    links = "".join(
        f'<div class="rank"><span class="rank-lbl">{r["src"]} → {r["dst"]}</span>'
        f'<span class="rank-n">{_fmt(r["hits"])}</span></div>'
        for r in rf["links"]) or '<p class="note">No cross-site clicks yet.</p>'
    ref_body = f"""
    <div class="refkpi">
      <div class="b"><div class="n">{_fmt(rf["direct"])}</div><div class="l">direct</div></div>
      <div class="b"><div class="n">{_fmt(rf["external"])}</div><div class="l">external</div></div>
      <div class="b"><div class="n">{_fmt(rf["internal"])}</div><div class="l">cross-site</div></div>
    </div>
    <div class="trio" style="grid-template-columns:1fr 1fr">
      <div><h3>Top external sources</h3>{_ranklist(rf["top_ext"], "src", "visits")}</div>
      <div><h3>Between your sites</h3>{links}</div>
    </div>"""

    # top pages
    prows = "".join(
        f'<tr><td>{_pill(p["hostname"])}</td>'
        f'<td style="font-family:var(--font-mono);font-size:12px">{p["url_path"]}</td>'
        f'<td class="num">{_fmt(p["pageviews"])}</td>'
        f'<td class="num">{_fmt(p["visits"])}</td></tr>'
        for p in pages)

    aud_body = f"""
    <div class="trio">
      <div><h3>Device</h3>{_ranklist(aud["device"])}</div>
      <div><h3>Browser</h3>{_ranklist(aud["browser"])}</div>
      <div><h3>Country</h3>{_ranklist(aud["country"], top_label=lambda s: s.upper() if s != 'unknown' else s)}</div>
    </div>"""

    body = f"""
    <p class="meta">Patterns across every tracked site · small numbers still — everything is shown as raw counts, not percentages.</p>
    <div class="card"><div class="card-head"><h2>Visits by site · last 14 days</h2></div>
      <div class="chart-wrap" style="height:260px"><canvas id="traffic"></canvas></div></div>
    <div class="card"><div class="card-head"><h2>Where visitors come from</h2></div>{ref_body}</div>
    <div class="card"><div class="card-head"><h2>Top pages · all sites</h2></div>
      <div class="table-wrap"><table><thead><tr><th>Site</th><th>Path</th><th class="num">Views</th><th class="num">Visits</th></tr></thead>
      <tbody>{prows}</tbody></table></div></div>
    <div class="card"><div class="card-head"><h2>Audience</h2></div>{aud_body}</div>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" integrity="sha384-9nhczxUqK87bcKHh20fSQcTGD4qq5GhayNYSYWqwBkINBhOfQLg/P5HG5lF1urn4" crossorigin="anonymous"></script>
    <script>
      const css = getComputedStyle(document.documentElement);
      Chart.defaults.color = css.getPropertyValue('--meta').trim();
      Chart.defaults.font.family = "'DM Sans',sans-serif";
      new Chart('traffic', {{type:'bar', data:{chart_json},
        options:{{maintainAspectRatio:false,
          plugins:{{legend:{{position:'bottom',labels:{{boxWidth:10,padding:8}}}}}},
          scales:{{x:{{stacked:true,grid:{{display:false}}}},
                   y:{{stacked:true,beginAtZero:true,ticks:{{precision:0}}}}}}}}}});
    </script>"""
    return shell("Patterns", "/patterns", body)


def render_overview() -> str:
    t = totals()
    sites = per_site()
    active = [s for s in sites if (s["visits"] or 0) > 0]
    rows = ""
    for s in sites:
        dom = s["domain"] or ""
        name = s["name"] or dom or "—"
        link = f'<a href="https://{dom}" target="_blank" rel="noopener">{name}</a>' if dom else name
        dim = "" if (s["visits"] or 0) > 0 else ' style="opacity:.5"'
        rows += (
            f'<tr{dim}><td>{link}</td>'
            f'<td class="num">{_fmt(s["visits"])}</td>'
            f'<td class="num">{_fmt(s["visits_7d"])}</td>'
            f'<td class="num">{_fmt(s["pageviews"])}</td>'
            f'<td class="num meta col-since">{_ago(s["since"])}</td></tr>'
        )
    body = f"""
    <div class="kpis">
      <div class="kpi"><div class="kpi-n">{_fmt(t["visits"])}</div><div class="kpi-l">total visits · all sites</div></div>
      <div class="kpi"><div class="kpi-n">{_fmt(t["pageviews"])}</div><div class="kpi-l">total pageviews</div></div>
      <div class="kpi"><div class="kpi-n">{_fmt(t["visits_7d"])}</div><div class="kpi-l">visits · last 7 days</div></div>
      <div class="kpi"><div class="kpi-n">{_fmt(t["visits_24h"])}</div><div class="kpi-l">visits · last 24 h</div></div>
      <div class="kpi"><div class="kpi-n">{len(active)}<span class="meta" style="font-size:.5em"> / {len(sites)}</span></div><div class="kpi-l">sites with traffic</div></div>
    </div>
    <p class="meta">Aggregated across every site tracked by <a href="https://analytics.kardol.us" target="_blank" rel="noopener">Umami</a> · since {_ago(t["since"])} · updates every {CACHE_TTL // 60 or 1} min.</p>
    <div class="card"><div class="card-head"><h2>By site</h2></div>
      <div class="table-wrap"><table class="sites">
        <thead><tr><th>Site</th><th class="num">Visits</th><th class="num">7&nbsp;days</th><th class="num"><span class="lbl-full">Pageviews</span><span class="lbl-short">Views</span></th><th class="num col-since">Since</th></tr></thead>
        <tbody>{rows}</tbody></table></div></div>"""
    return shell("Overview", "/", body)


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
_NAV = [("/", "Overview"), ("/patterns", "Patterns")]


def shell(title: str, active: str, body: str) -> str:
    nav = "".join(
        f'<a href="{href}" class="{"active" if href == active else ""}">{label}</a>'
        for href, label in _NAV)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f8f9fa" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0d1117" media="(prefers-color-scheme: dark)">
<title>{title} · Analytics</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="description" content="Total visits and patterns across every kardol.us site, aggregated from self-hosted Umami analytics.">
<meta property="og:title" content="kardol.us · analytics">
<meta property="og:description" content="Visits, referrers and patterns across every kardol.us site — aggregated from self-hosted Umami.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://stats.kardol.us">
<meta property="og:image" content="https://stats.kardol.us/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="kardol.us · analytics">
<meta name="twitter:description" content="Visits, referrers and patterns across every kardol.us site.">
<meta name="twitter:image" content="https://stats.kardol.us/og.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_CSS}{_PATTERNS_CSS}</style>
<script>
  var _t = localStorage.getItem('theme'); document.documentElement.classList.toggle('dark', _t ? _t === 'dark' : (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches));
  function toggleDark(){{ document.documentElement.classList.toggle('dark'); localStorage.setItem('theme', document.documentElement.classList.contains('dark') ? 'dark' : 'light'); window.dispatchEvent(new Event('themechange')); }}
</script>
</head>
<body>
<div class="top-bar"><div class="top-utility">
  <div class="brand">{_LOGO} Analytics</div>
  <div class="top-actions">
    <a class="nbhd-select" href="https://analytics.kardol.us" target="_blank" rel="noopener" style="text-decoration:none">Open Umami →</a>
    <button class="dark-toggle" onclick="toggleDark()" title="Toggle dark mode">◐</button>
  </div>
</div><nav class="top-nav">{nav}</nav></div>
<main>{body}</main>
</body>
</html>"""


# ───────────────────────── routes ─────────────────────────
async def home(r):
    return HTMLResponse(render_overview())


async def patterns(r):
    return HTMLResponse(render_patterns())


async def favicon(r):
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


async def og(r):
    if _OG.exists():
        return Response(_OG.read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    return PlainTextResponse("no og", status_code=404)


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
    Route("/patterns", patterns),
    Route("/favicon.svg", favicon),
    Route("/og.png", og),
    Route("/healthz", healthz),
    Route("/ready", ready),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
