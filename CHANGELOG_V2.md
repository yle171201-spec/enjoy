# V2 Changelog

Compared with V1:

- Added full NEXT_OPEN dynamic A/B/C exit replay.
- Added conservative open-limit skip and invalid-structure skip.
- Added gap bucket diagnostics.
- Added per-side commission / sell-tax parameters.
- Added K5/K7 mark-to-market portfolio engine.
- Added C satellite capacity and C-yields-to-A/B behavior.
- Added drawdown episode and underwater duration.
- Added Monte Carlo same-day capacity test.
- Added A/B/C structure overlays on individual K charts.
- Dashboard now shows only signals from the latest completed trading day.
- N-day screener now shows current price, return since signal and lifecycle state.
- Added data-update run logging and partial-failure handling.
- Added PostgreSQL URL normalization for psycopg3.
- Added GitHub CI, Docker Compose, Render test/production Blueprints.
- Added production deployment documentation.
- Added 6 automated tests covering sizing, previous-week alignment, next-open execution, limit skip, C yield, and MTM drawdown.
