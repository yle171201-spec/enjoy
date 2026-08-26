#!/usr/bin/env python3
from app.db import init_db
from app.services.maintenance_service import (
    run_smart_maintenance,
    smart_progress_snapshot,
)


def main():
    init_db()
    run_smart_maintenance()
    state = smart_progress_snapshot()
    print(
        "daily maintenance:",
        state.get("status"),
        "|",
        state.get("stage"),
        "|",
        state.get("detail"),
    )
    if state.get("status") not in {"ok", "idle"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
