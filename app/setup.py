"""Einrichtung im Browser (seit 1.2).

Läuft statt der eigentlichen Oberfläche, wenn TeddyCloud-Adresse oder Quelle fehlen und TB_CONFIG_FILE gesetzt ist.
Zugang nur mit einem Einmal-Code aus dem Log (sonst könnte, wer zuerst kommt, die Instanz übernehmen).
Prüfen, Testen und Speichern: app/setupcore.py (dieselbe Logik wie die Einstellungsseite).
"""
import hmac
import logging
import os
import secrets
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__, i18n, setupcore
from .i18n import t

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("teddybridge")

STORE = setupcore.Store(os.environ.get("TB_CONFIG_FILE", ""))
STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "tb_setup"
CODE = setupcore.new_code()
_sessions = set()
_unlock_lock = threading.Lock()
_save_lock = threading.Lock()

log.warning("=" * 64)
log.warning("teddybridge %s ist noch nicht eingerichtet. Einrichtung im Browser öffnen", __version__)
log.warning("(http://<host>:<port>/setup) und diesen Einmal-Code eingeben:  %s", CODE)
log.warning("Gespeichert wird in %s", STORE.path)
log.warning("=" * 64)
if not STORE.writable():
    log.error("Einrichtung: %s ist nicht beschreibbar (Docker: Volume für /config einhängen)", STORE.path)
if STORE.fixed:
    log.info("Einrichtung: von der Umgebung gesetzt (Vorrang): %s", ", ".join(STORE.fixed))

app = FastAPI(title="teddybridge setup", docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


def _lang(request):
    fixed = i18n.norm(os.environ.get("UI_LANG", ""))
    return fixed or i18n.from_accept(request.headers.get("accept-language")) or "en"


@app.middleware("http")
async def guard(request: Request, call_next):
    tok = i18n.set(_lang(request))
    try:
        path = request.url.path
        if path.startswith("/api/setup/") and path not in ("/api/setup/unlock", "/api/setup/ping") \
                and request.cookies.get(COOKIE) not in _sessions:
            resp = JSONResponse({"error": t("Einmal-Code erforderlich")}, status_code=401)
        else:
            resp = await call_next(request)
    finally:
        i18n.reset(tok)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def page(request, mode="setup", api="/api/setup", auth=False):
    with open(os.path.join(STATIC, "setup.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace('<html lang="de">', f'<html lang="{_lang(request)}" data-mode="{mode}" data-api="{api}" '
                                            f'data-auth="{int(bool(auth))}">', 1)
    return HTMLResponse(html, headers={"Vary": "Accept-Language, Cookie", "Cache-Control": "no-store"})


@app.get("/setup")
def setup_page(request: Request):
    return page(request)


@app.get("/api/setup/ping")
def ping():
    return {"setup": True}


@app.post("/api/setup/unlock")
async def unlock(request: Request):
    try:
        code = (await request.json()).get("code", "")
    except ValueError:
        code = ""
    with _unlock_lock:  # Fehlversuche nacheinander und gebremst
        ok = hmac.compare_digest(setupcore.norm_code(code).encode(), setupcore.norm_code(CODE).encode())
        if not ok:
            time.sleep(1.0)
    if not ok:
        log.warning("Einrichtung: falscher Einmal-Code")
        return JSONResponse({"error": t("Einmal-Code falsch")}, status_code=403)
    tok = secrets.token_urlsafe(32)
    _sessions.add(tok)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="strict", path="/")
    return resp


@app.get("/api/setup/state")
def state():
    return {**STORE.state(), "version": __version__}


@app.post("/api/setup/test")
async def test(request: Request):
    try:
        d = await request.json()
    except ValueError:
        d = {}
    return await setupcore.run_test(STORE, d)


@app.post("/api/setup/save")
async def save(request: Request):
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    with _save_lock:
        status, body = STORE.save(payload)
    if status == 200:
        setupcore.restart_later()
    return JSONResponse(body, status_code=status)


@app.get("/{path:path}")
def everything_else(path: str):
    """Alles andere (auch /login für den Docker-Healthcheck) führt zur Einrichtung."""
    return RedirectResponse("/setup", status_code=303)
