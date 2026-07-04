# stats-web — stats.kardol.us

A cross-site visitor rollup over the self-hosted **Umami** database. Umami's OSS
dashboard is strictly per-website; this page reads the same Postgres **read-only**
(role `stats_ro`) and shows the one number Umami won't: **total visits + pageviews
across every tracked site**, plus a per-site breakdown. Flightdeck styling, shared
with the other kardol.us apps.

- Stack: Starlette (single `src/statsweb/app.py`) + psycopg2, flightdeck `app.css`.
- Reads `website`, `website_event`, `session` (SELECT only). `DATABASE_URL` = the
  `stats_ro` DSN to `umami-postgres` (host docker, in-cluster no-selector Service).
- Deploy: GHCR image + `ghcr-pull`; k8s Deployment/Service/Ingress in ns `analytics`
  (alongside umami). Host `stats.kardol.us` via the platform CF tunnel.
- "Visits" = distinct Umami sessions; "pageviews" = `website_event.event_type = 1`.

Build: `docker build -t ghcr.io/kardolus/stats-web:vN . && docker push …` on forge.
