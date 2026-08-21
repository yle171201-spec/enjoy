from __future__ import annotations
import secrets
from fastapi import Request
from itsdangerous import URLSafeSerializer,BadSignature
from .config import settings
_serializer=URLSafeSerializer(settings.session_secret,salt="abc-strategy"); COOKIE="abc_session"
def valid_password(value:str)->bool:return secrets.compare_digest(value or "",settings.app_password)
def make_cookie()->str:return _serializer.dumps({"ok":True})
def is_logged_in(request:Request)->bool:
    token=request.cookies.get(COOKIE)
    if not token:return False
    try:return bool(_serializer.loads(token).get("ok"))
    except BadSignature:return False
