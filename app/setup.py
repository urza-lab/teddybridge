"""Einrichtung im Browser (seit 1.2).

Läuft statt der eigentlichen Oberfläche, wenn TeddyCloud-Adresse oder Quelle fehlen und TB_CONFIG_FILE gesetzt ist.
Zugang nur mit einem Einmal-Code aus dem Log (sonst könnte, wer zuerst kommt, die Instanz übernehmen).
Gespeichert wird in TB_CONFIG_FILE (Rechte 600); gesetzte Umgebungsvariablen haben Vorrang und werden nicht
überschrieben. Danach startet sich der Prozess neu (os.execv) und läuft normal mit der neuen Konfiguration.
Geheimnisse (Passwörter, Token, Webhook) werden nie an den Browser zurückgegeben, nur „gesetzt“.
"""
import asyncio
import contextvars
import hmac
import logging
import os
import secrets
import sys
import threading
import time
import types
import urllib.parse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__, config, fs, i18n, pwhash
from .clients import Abs, ApiError, Nd, Tc
from .i18n import t

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("teddybridge")

PATH = os.environ.get("TB_CONFIG_FILE", "")
STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "tb_setup"
SECRET_KEYS = ("TC_AUTH", "ABS_TOKEN", "ND_PASS", "UI_PASS_HASH", "DISCORD_WEBHOOK")
SHOWN_KEYS = ("TC_URL", "TC_BASE", "ABS_URL", "ND_URL", "ND_USER", "FS_ROOTS", "UI_USER", "UI_LANG",
              "FRAME_ANCESTORS", "TRUSTED_PROXIES")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O, 1/I: gut abzutippen; 12 Zeichen = 60 Bit
CODE = "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))
_sessions = set()
_unlock_lock = threading.Lock()
_save_lock = threading.Lock()
_FILE = config.read_config_file(PATH) if PATH else {}
# Von der Umgebung gesetzt (nicht aus der Datei): in der Einrichtung nicht änderbar
FIXED = tuple(k for k in config.SETUP_KEYS
              if os.environ.get(k, "").strip() and os.environ.get(k) != _FILE.get(k))


def writable():
    d = os.path.dirname(os.path.abspath(PATH)) if PATH else ""
    if not d or not os.path.isdir(d) or not os.access(d, os.W_OK):
        return False
    return not os.path.exists(PATH) or os.access(PATH, os.W_OK)


def current(k):
    """Aktueller Wert: Umgebung (falls fest) oder Datei."""
    return os.environ.get(k, "").strip() if k in FIXED else _FILE.get(k, "")


log.warning("=" * 64)
log.warning("teddybridge %s ist noch nicht eingerichtet. Einrichtung im Browser öffnen", __version__)
log.warning("(http://<host>:<port>/setup) und diesen Einmal-Code eingeben:  %s", CODE)
log.warning("Gespeichert wird in %s", PATH)
log.warning("=" * 64)
if not writable():
    log.error("Einrichtung: %s ist nicht beschreibbar (Docker: Volume für /config einhängen)", PATH)
if FIXED:
    log.info("Einrichtung: von der Umgebung gesetzt (Vorrang): %s", ", ".join(FIXED))

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


@app.get("/setup")
def setup_page(request: Request):
    with open(os.path.join(STATIC, "setup.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace('<html lang="de">', f'<html lang="{_lang(request)}">', 1)
    return HTMLResponse(html, headers={"Vary": "Accept-Language, Cookie"})


@app.get("/api/setup/ping")
def ping():
    return {"setup": True}


@app.post("/api/setup/unlock")
async def unlock(request: Request):
    try:
        code = str((await request.json()).get("code", ""))
    except ValueError:
        code = ""
    norm = lambda s: "".join(ch for ch in s.upper() if ch.isalnum())
    with _unlock_lock:  # Fehlversuche nacheinander und gebremst
        ok = hmac.compare_digest(norm(code).encode(), norm(CODE).encode())
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
    return {
        "version": __version__,
        "path": PATH,
        "writable": writable(),
        "fixed": list(FIXED),
        "values": {k: current(k) for k in SHOWN_KEYS},
        "secrets": {k: bool(current(k)) for k in SECRET_KEYS},
        "tcUser": current("TC_AUTH").split(":", 1)[0] if ":" in current("TC_AUTH") else "",
        "minPass": pwhash.MIN_LEN,
    }


def _s(v, maxlen=2000):
    v = "" if v is None else str(v).strip()
    if len(v) > maxlen or any(ch in v for ch in "\r\n\0"):
        raise ValueError(t("ungültige Eingabe"))
    return v


def _url(v):
    v = _s(v).rstrip("/")
    u = urllib.parse.urlsplit(v)
    if v and (u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment or u.username):
        raise ValueError(t("Adresse bitte mit http:// oder https:// angeben, z. B. http://teddycloud"))
    return v


def resolve(d):
    """Eingaben aus dem Browser -> Werte für die Datei. Leere Geheimnisse = bisherigen Wert behalten."""
    out = {}
    out["TC_URL"] = _url(d.get("TC_URL"))
    tc_user, tc_pass = _s(d.get("tcUser"), 200), _s(d.get("tcPass"), 500)
    if ":" in tc_user:
        raise ValueError(t("TeddyCloud-Benutzer darf keinen Doppelpunkt enthalten"))
    if tc_user:
        old = current("TC_AUTH")
        if not tc_pass and old.startswith(tc_user + ":"):
            tc_pass = old.split(":", 1)[1]
        if not tc_pass:
            raise ValueError(t("TeddyCloud: Passwort fehlt"))
        out["TC_AUTH"] = f"{tc_user}:{tc_pass}"
    else:
        out["TC_AUTH"] = ""
    out["TC_BASE"] = _s(d.get("TC_BASE"), 200)
    if d.get("useAbs"):
        out["ABS_URL"] = _url(d.get("ABS_URL"))
        out["ABS_TOKEN"] = _s(d.get("ABS_TOKEN"), 4000) or current("ABS_TOKEN")
    else:
        out["ABS_URL"] = out["ABS_TOKEN"] = ""
    if d.get("useNd"):
        out["ND_URL"] = _url(d.get("ND_URL"))
        out["ND_USER"] = _s(d.get("ND_USER"), 200)
        out["ND_PASS"] = _s(d.get("ND_PASS"), 500) or (current("ND_PASS") if out["ND_USER"] == current("ND_USER") else "")
    else:
        out["ND_URL"] = out["ND_USER"] = out["ND_PASS"] = ""
    out["FS_ROOTS"] = _s(d.get("FS_ROOTS"), 4000) if d.get("useFs") else ""
    if d.get("noLogin"):
        out["UI_USER"] = out["UI_PASS_HASH"] = ""
    else:
        user, pw = _s(d.get("UI_USER"), 200), _s(d.get("uiPass"), 500)
        if not user:
            raise ValueError(t("Login: Benutzer fehlt"))
        if pw:
            if len(pw) < pwhash.MIN_LEN:
                raise ValueError(t("Login: Passwort mindestens {n} Zeichen", n=pwhash.MIN_LEN))
            if pw != _s(d.get("uiPass2"), 500):
                raise ValueError(t("Login: Passwörter stimmen nicht überein"))
            out["UI_PASS_HASH"] = pwhash.make(pw)
        elif current("UI_PASS_HASH") and user == current("UI_USER"):
            out["UI_PASS_HASH"] = current("UI_PASS_HASH")
        else:
            raise ValueError(t("Login: Passwort fehlt"))
        out["UI_USER"] = user
    out["UI_LANG"] = _s(d.get("UI_LANG"), 10)
    out["FRAME_ANCESTORS"] = _s(d.get("FRAME_ANCESTORS"), 1000)
    out["TRUSTED_PROXIES"] = _s(d.get("TRUSTED_PROXIES"), 1000)
    out["DISCORD_WEBHOOK"] = _s(d.get("DISCORD_WEBHOOK"), 1000) or current("DISCORD_WEBHOOK")
    for k in FIXED:  # Umgebung hat Vorrang
        out.pop(k, None)
    return out


def _effective(values):
    """Wie beim Start: Umgebung (fest) + neue Werte; für config.load(env=...)."""
    env = {k: v for k, v in os.environ.items() if k not in config.SETUP_KEYS or k in FIXED}
    env.update({k: v for k, v in values.items() if k not in FIXED})
    return env


def _short(e):
    return (str(e) or type(e).__name__)[:300]


def test_tc(v):
    url, auth = v.get("TC_URL", ""), v.get("TC_AUTH", "")
    if not url:
        raise ValueError(t("TeddyCloud-Adresse fehlt"))
    tc = Tc(types.SimpleNamespace(tc_url=url, tc_auth=tuple(auth.split(":", 1)) if ":" in auth else None))
    tc.c.timeout = httpx.Timeout(10.0)
    try:
        r = tc._request("GET", "/api/fileIndex", "fileIndex", retry=False, params={"path": "", **Tc.LIB})
    finally:
        tc.c.close()
    if r.status_code == 200:
        try:
            files = r.json().get("files")
        except ValueError:
            files = None
        if isinstance(files, list):
            return t("TeddyCloud erreichbar, Bibliothek lesbar")
        return t("Antwort ist keine TeddyCloud-Bibliothek – Adresse prüfen")
    if r.status_code in (401, 403):
        if not auth:
            return None, t("TeddyCloud verlangt eine Anmeldung: Benutzer und Passwort eintragen")
        return None, t("TeddyCloud: Zugriff verweigert – Benutzer und Passwort prüfen")
    return None, t("TeddyCloud antwortet mit HTTP {status} – Adresse prüfen", status=r.status_code)


def test_abs(v):
    if not (v.get("ABS_URL") and v.get("ABS_TOKEN")):
        raise ValueError(t("Audiobookshelf: Adresse und API-Token eintragen"))
    a = Abs(types.SimpleNamespace(abs_url=v["ABS_URL"], abs_token=v["ABS_TOKEN"]))
    a.c.timeout = httpx.Timeout(10.0)
    try:
        libs = a.libraries()
    finally:
        a.c.close()
    return t("Audiobookshelf erreichbar: {n} Bibliotheken ({names})", n=len(libs),
             names=", ".join(str(x.get("name")) for x in libs[:5]) or "–")


def test_nd(v):
    if not (v.get("ND_URL") and v.get("ND_USER") and v.get("ND_PASS")):
        raise ValueError(t("Navidrome: Adresse, Benutzer und Passwort eintragen"))
    n = Nd(types.SimpleNamespace(nd_url=v["ND_URL"], nd_user=v["ND_USER"], nd_pass=v["ND_PASS"]))
    n.c.timeout = httpx.Timeout(10.0)
    try:
        folders = n.folders()
    finally:
        n.c.close()
    return t("Navidrome erreichbar: {n} Musikordner", n=len(folders))


def test_fs(v):
    roots = fs.parse_roots(v.get("FS_ROOTS", ""))
    if not roots:
        raise ValueError(t("Dateiquelle: mindestens einen Ordner als Name=/pfad eintragen"))
    bad = [f"{n} ({p})" for n, p in roots if not (os.path.isdir(p) and os.access(p, os.R_OK | os.X_OK))]
    if bad:
        return None, t("nicht lesbar oder nicht vorhanden: {list}", list=", ".join(bad))
    return t("{n} Ordner lesbar", n=len(roots))


TESTS = {"tc": test_tc, "abs": test_abs, "nd": test_nd, "fs": test_fs}


@app.post("/api/setup/test")
async def test(request: Request):
    try:
        d = await request.json()
        kind = d.get("kind")
        if kind not in TESTS:
            return JSONResponse({"error": "kind"}, status_code=400)
        data = dict(d.get("values") or {})
        data.update(useAbs=kind == "abs" or data.get("useAbs"), useNd=kind == "nd" or data.get("useNd"),
                    useFs=kind == "fs" or data.get("useFs"), noLogin=True)
        v = resolve(data)
        for k in FIXED:
            v[k] = os.environ.get(k, "").strip()
        res = await _in_thread(TESTS[kind], v)
    except (ValueError, ApiError) as e:
        return {"ok": False, "message": _short(e)}
    except Exception as e:  # Test darf die Einrichtung nie abbrechen
        log.warning("Einrichtung: Test %s: %s", kind, type(e).__name__)
        return {"ok": False, "message": t("Fehler: {name}", name=type(e).__name__)}
    if isinstance(res, tuple):
        return {"ok": False, "message": res[1]}
    return {"ok": True, "message": res}


async def _in_thread(fn, *a):
    """Blockierende Tests im Thread; Kontext (Sprache der Anfrage) mitnehmen."""
    ctx = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(None, ctx.run, fn, *a)


@app.post("/api/setup/save")
async def save(request: Request):
    if not writable():
        return JSONResponse({"error": t("{path} ist nicht beschreibbar (Docker: Volume für /config einhängen)", path=PATH)},
                            status_code=409)
    try:
        values = resolve(await request.json())
        config.load(env=_effective(values))  # dieselben Prüfungen wie beim Start
    except ValueError as e:
        return JSONResponse({"error": _short(e)}, status_code=400)
    except SystemExit as e:
        return JSONResponse({"error": _short(e)}, status_code=400)
    with _save_lock:
        try:
            config.write_config_file(PATH, values)
        except (OSError, ValueError) as e:
            log.error("Einrichtung: Speichern fehlgeschlagen (%s)", type(e).__name__)
            return JSONResponse({"error": t("Speichern fehlgeschlagen ({name})", name=type(e).__name__)}, status_code=500)
    log.info("Einrichtung gespeichert (%s), starte neu", ", ".join(k for k, v in values.items() if v))
    threading.Thread(target=_restart, daemon=True).start()
    return {"ok": True}


def _restart():
    time.sleep(1.0)  # Antwort noch ausliefern
    for h in logging.getLogger().handlers:
        h.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "app"])


@app.get("/{path:path}")
def everything_else(path: str):
    """Alles andere (auch /login für den Docker-Healthcheck) führt zur Einrichtung."""
    return RedirectResponse("/setup", status_code=303)
