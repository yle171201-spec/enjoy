#!/usr/bin/env python3
from app.db import init_db, SessionLocal
from app.services.data_update import sync_daily_public

init_db()
db = SessionLocal()
try:
    print(sync_daily_public(db))
finally:
    db.close()
