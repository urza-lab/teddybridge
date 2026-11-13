"""FastAPI-Backend: ABS und Navidrome stöbern, Status in TeddyCloud, Warteschlange."""
import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import os
import threading
import time
import urllib.parse

import httpx

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import __version__, config, i18n, pwhash, setupcore
from .i18n import t
from .clients import Abs, ApiError, Nd, Tc, check_id
from .fs import Fs
from .worker import FINAL, MAX_FILES, MARKER, Queue, Reserved, clean_folder, id8, scan_paths, series_list, series_seq

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # loggt sonst volle URLs
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("teddybridge")

CFG = config.load()
log.info("teddybridge %s", __version__)
i18n.DEFAULT = CFG.ui_lang or "en"  # Rückfall ohne Cookie/Accept-Language, z. B. für Skripte
ABS = Abs(CFG) if CFG.abs_enabled else None
TC = Tc(CFG)
_libs = {"t": 0, "names": {}}


def lib_name(lib_id):
    if lib_id not in _libs["names"] or time.time() - _libs["t"] > 600:
        _libs["names"] = {l["id"]: l["name"] for l in abs_().libraries()}
        _libs["t"] = time.time()
    return _libs["names"].get(lib_id) or t("Bibliothek")


ND = Nd(CFG) if CFG.nd_enabled else None
_ndf = {"t": 0, "names": {}}


def nd_folder_name(fid):
    if fid is None:
        return None
    if str(fid) not in _ndf["names"] or time.time() - _ndf["t"] > 600:
        _ndf["names"] = {str(f["id"]): f["name"] for f in ND.folders()}
        _ndf["t"] = time.time()
    return _ndf["names"].get(str(fid))


FS = Fs(list(CFG.fs_roots)) if CFG.fs_roots else None
for _n, _p in CFG.fs_roots:
    if not os.path.isdir(_p):
        log.warning("Dateiquelle „%s“: Ordner nicht vorhanden oder nicht lesbar (nicht eingehängt?)", _n)

QUEUE = Queue(CFG, Abs(CFG) if CFG.abs_enabled else None, Tc(CFG), lib_name,  # eigene Verbindungen für den Worker-Thread
              nd=Nd(CFG) if CFG.nd_enabled else None, nd_folder_name=nd_folder_name, fs=FS)
READONLY = os.environ.get("TB_READONLY") == "1"
if READONLY:
    log.warning("Nur-Lese-Modus aktiv: Übertragungen sind deaktiviert")
STATIC = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="teddybridge", docs_url=None, redoc_url=None, openapi_url=None)

if CFG.trusted_proxies:
    log.info("Reverse Proxy vertraut: %s", ", ".join(CFG.trusted_proxies))
if CFG.ui_pass_hash:
    log.info("Login für die Oberfläche aktiv (Passwort-Hash)")
elif CFG.ui_auth:
    log.warning("Login aktiv, aber Passwort im Klartext (UI_PASS). Besser: UI_PASS_HASH, erzeugen mit "
                "venv/bin/python -m app.pwhash")
else:
    log.info("Kein Login konfiguriert (UI_USER/UI_PASS_HASH leer): Oberfläche ohne Login")


SESSION_COOKIE = "tb_session"
SESSION_TTL = 30 * 86400
SESSIONS = {}  # sha256(token) -> Ablaufzeit; nur im Speicher, Neustart = neu anmelden


def _creds_ok(user, pw):
    """Benutzer und Passwort prüfen. Das Passwort wird immer geprüft (auch bei falschem Benutzer),
    damit die Antwortzeit nichts verrät. Mit Hash dauert das ca. 0,1 s, darum im Thread aufrufen."""
    u_ok = hmac.compare_digest(user.encode(), CFG.ui_user.encode())
    if CFG.ui_pass_hash:
        p_ok = pwhash.verify(pw, CFG.ui_pass_hash)
    else:
        p_ok = hmac.compare_digest(pw.encode(), CFG.ui_pass.encode())
    return u_ok and p_ok


_BASIC_KEY = secrets.token_bytes(32)
_basic_cache = {}  # HMAC(Header) -> Ablaufzeit; erspart Skripten pro Aufruf einen scrypt-Durchlauf


def _basic_ok(header):
    """Basic-Auth-Header (für Skripte/Tests; der Browser nutzt das Formular)."""
    scheme, _, b64 = (header or "").partition(" ")
    if scheme.lower() != "basic" or not b64:
        return False
    key = hmac.new(_BASIC_KEY, header.encode(), "sha256").digest()
    if _basic_cache.get(key, 0) > time.time():
        return True
    try:
        user, sep, pw = base64.b64decode(b64.strip(), validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    if not (sep and _creds_ok(user, pw)):
        return False
    if len(_basic_cache) > 50:
        _basic_cache.clear()
    _basic_cache[key] = time.time() + 300
    return True


def _sid(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _session_ok(token):
    exp = SESSIONS.get(_sid(token)) if token else None
    return bool(exp and exp > time.time())


def _new_session():
    now = time.time()
    for k in [k for k, exp in SESSIONS.items() if exp <= now]:
        del SESSIONS[k]
    while len(SESSIONS) >= 200:  # Obergrenze: älteste Sitzung fliegt raus
        del SESSIONS[min(SESSIONS, key=SESSIONS.get)]
    token = secrets.token_urlsafe(32)
    SESSIONS[_sid(token)] = now + SESSION_TTL
    return token


def _client(request):
    return request.client.host if request.client else "?"


@app.middleware("http")
async def require_login(request: Request, call_next):
    if (not CFG.ui_auth or request.url.path in ("/login", "/logout")
            or _session_ok(request.cookies.get(SESSION_COOKIE))):
        return await call_next(request)
    if request.headers.get("authorization"):
        if await asyncio.to_thread(_basic_ok, request.headers["authorization"]):
            return await call_next(request)
        log.warning("Anmeldung: falsche Zugangsdaten (Basic) von %s", _client(request))
        await asyncio.sleep(1)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": t("Anmeldung erforderlich")}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# Einbetten in einen Rahmen nur, wenn FRAME_ANCESTORS gesetzt ist (z. B. TeddyCloud-Plugin), sonst nie (Clickjacking).
FRAMES = " ".join(CFG.frame_ancestors) or "'none'"
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       f"connect-src 'self'; form-action 'self'; frame-ancestors {FRAMES}; base-uri 'none'; object-src 'none'")
if CFG.frame_ancestors:
    log.info("Einbetten erlaubt für: %s", ", ".join(CFG.frame_ancestors))


@app.middleware("http")  # nach require_login registriert = äussere Schicht, gilt also auch für 401/Weiterleitungen
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    if not CFG.frame_ancestors:  # X-Frame-Options kennt keine Liste; mit FRAME_ANCESTORS gilt nur die CSP
        resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


LANG_COOKIE = "tb_lang"


def req_lang(request):
    """Sprache einer Anfrage: UI_LANG (fest) > ?lang= (vom TeddyCloud-Plugin: Sprache von TeddyCloud) > Cookie
    (Auswahl in der Oberfläche) > Accept-Language > Standard."""
    return (CFG.ui_lang or i18n.norm(request.query_params.get("lang")) or i18n.norm(request.cookies.get(LANG_COOKIE))
            or i18n.from_accept(request.headers.get("accept-language")) or i18n.DEFAULT)


@app.middleware("http")  # zuletzt registriert = äusserste Schicht: gilt auch für 401 aus require_login
async def language(request: Request, call_next):
    lang = req_lang(request)
    token = i18n.set(lang)
    try:
        resp = await call_next(request)
    finally:
        i18n.reset(token)
    # ?lang= merken, damit auch die API-Aufrufe der Seite (Meldungen, Aufträge) diese Sprache nutzen
    if not CFG.ui_lang and i18n.norm(request.query_params.get("lang")) and request.cookies.get(LANG_COOKIE) != lang:
        resp.set_cookie(LANG_COOKIE, lang, max_age=31536000, samesite="strict", path="/",
                        secure=request.url.scheme == "https")
    return resp


_PAGES = {}


def page(name, request):
    """HTML-Seite mit der Sprache der Anfrage im <html lang>; data-lang-fixed=1, wenn UI_LANG gesetzt ist."""
    if name not in _PAGES:
        with open(os.path.join(STATIC, name), encoding="utf-8") as f:
            _PAGES[name] = f.read()
    html = _PAGES[name].replace('<html lang="de">', f'<html lang="{req_lang(request)}" '
                                f'data-lang-fixed="{1 if CFG.ui_lang else 0}">', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache", "Vary": "Cookie, Accept-Language"})


@app.get("/login")
def login_page(request: Request):
    if not CFG.ui_auth or _session_ok(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return page("login.html", request)


@app.post("/login")
async def login(request: Request):
    body = await request.body()
    if len(body) > 4096:
        return Response(status_code=413)
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    user = (form.get("username") or [""])[0]
    pw = (form.get("password") or [""])[0]
    if not CFG.ui_auth:
        return RedirectResponse("/", status_code=303)
    if not await asyncio.to_thread(_creds_ok, user, pw):
        log.warning("Anmeldung: falsche Zugangsdaten (Formular) von %s", _client(request))
        await asyncio.sleep(1)  # bremst Durchprobieren
        return RedirectResponse("/login?fehler=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, _new_session(), max_age=SESSION_TTL, httponly=True, samesite="strict", path="/",
                    secure=request.url.scheme == "https")  # über Caddy mit HTTPS: Cookie nur noch verschlüsselt
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        SESSIONS.pop(_sid(token), None)
    resp = RedirectResponse("/login" if CFG.ui_auth else "/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return resp


@app.exception_handler(ApiError)
def api_error(_req, exc):
    msg = str(exc)
    for s in CFG.secrets():
        msg = msg.replace(s, "***")
    return JSONResponse({"error": msg}, status_code=502)


def simplify(it):
    m = it.get("media") or {}
    md = m.get("metadata") or {}
    author = md.get("authorName") or ", ".join(a.get("name", "") for a in md.get("authors") or [])
    series = md.get("seriesName") or ", ".join(
        (s.get("name", "") + (f" #{s['sequence']}" if s.get("sequence") else "")) for s in series_list(md))
    if isinstance(m.get("audioFiles"), list):
        nfiles = len([a for a in m["audioFiles"] if not a.get("exclude")])
    else:
        nfiles = m.get("numAudioFiles")
    iid = it.get("id") or ""
    return {"id": iid, "source": "abs", "mark": id8(iid), "title": md.get("title") or "", "author": author,
            "series": series, "duration": m.get("duration") or 0, "files": nfiles,
            "hasCover": bool(m.get("coverPath")), "cover": f"/api/cover/{iid}" if m.get("coverPath") else None,
            "seq": series_seq(md)}


def simplify_nd(a):
    aid = a.get("id") or ""
    cov = a.get("coverArt")
    return {"id": aid, "source": "nd", "mark": id8(aid, "nd"), "title": a.get("name") or "",
            "author": a.get("artist") or a.get("displayArtist") or "", "series": str(a.get("year") or ""),
            "duration": a.get("duration") or 0, "files": a.get("songCount"), "hasCover": bool(cov),
            "cover": f"/api/nd/cover/{cov}" if cov else None, "seq": None}


@app.get("/")
def index(request: Request):
    return page("index.html", request)


@app.get("/api/config")
def get_config():
    return {"tcBase": CFG.tc_base, "maxFiles": MAX_FILES, "discord": bool(CFG.discord_webhook), "readonly": READONLY, "auth": CFG.ui_auth,
            "lang": i18n.get(), "langFixed": bool(CFG.ui_lang), "version": __version__,
            "settings": bool(os.environ.get("TB_CONFIG_FILE")),
            "sources": (["abs"] if ABS else []) + (["nd"] if ND else []) + (["fs"] if FS else [])}


# ---------- Einstellungen ändern (seit 1.2; gleiche Logik wie die Einrichtung, app/setupcore.py)
# Mit Login: nur angemeldet, Speichern zusätzlich mit dem aktuellen Passwort. Ohne Login: Einmal-Code aus dem Log
# (auf Knopfdruck erzeugt, 10 min gültig). Speichern startet teddybridge neu, darum nicht während Übertragungen.
BOOT = secrets.token_hex(8)
SETTINGS_COOKIE = "tb_settings"
_set = {"code": None, "until": 0.0, "sessions": set()}
_set_lock = threading.Lock()


def _settings_store():
    path = os.environ.get("TB_CONFIG_FILE", "")
    if not path:
        raise HTTPException(404, t("Einstellungen im Browser sind nicht aktiv (TB_CONFIG_FILE fehlt)"))
    return setupcore.Store(path)


def _settings_unlocked(request):
    return CFG.ui_auth or request.cookies.get(SETTINGS_COOKIE) in _set["sessions"]


def _settings_guard(request):
    if not _settings_unlocked(request):
        raise HTTPException(401, t("Einmal-Code erforderlich"))


@app.get("/settings")
def settings_page(request: Request):
    _settings_store()
    with open(os.path.join(STATIC, "setup.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace('<html lang="de">', f'<html lang="{req_lang(request)}" data-mode="settings" '
                                            f'data-api="/api/settings" data-auth="{int(CFG.ui_auth)}">', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Vary": "Cookie, Accept-Language"})


@app.get("/api/settings/ping")
def settings_ping():
    return {"boot": BOOT}


@app.post("/api/settings/code")
def settings_code():
    """Ohne Login: neuen Einmal-Code ins Log schreiben."""
    _settings_store()
    if CFG.ui_auth:
        return {"ok": True}
    with _set_lock:
        if not _set["code"] or time.time() > _set["until"]:
            _set["code"] = setupcore.new_code()
        _set["until"] = time.time() + 600
        log.warning("Einstellungen: Einmal-Code (10 min gültig):  %s", _set["code"])
    return {"ok": True}


@app.post("/api/settings/unlock")
async def settings_unlock(request: Request):
    _settings_store()
    try:
        code = (await request.json()).get("code", "")
    except ValueError:
        code = ""
    with _set_lock:
        want = _set["code"] if time.time() <= _set["until"] else None
        ok = bool(want) and hmac.compare_digest(setupcore.norm_code(code).encode(), setupcore.norm_code(want).encode())
    if not ok:
        log.warning("Einstellungen: falscher oder abgelaufener Einmal-Code von %s", _client(request))
        await asyncio.sleep(1)
        return JSONResponse({"error": t("Einmal-Code falsch oder abgelaufen")}, status_code=403)
    tok = secrets.token_urlsafe(32)
    with _set_lock:
        _set["sessions"].add(tok)
        _set["code"] = None  # nur einmal verwendbar
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SETTINGS_COOKIE, tok, httponly=True, samesite="strict", path="/", secure=request.url.scheme == "https")
    return resp


@app.get("/api/settings/state")
def settings_state(request: Request):
    store = _settings_store()
    _settings_guard(request)
    return {**store.state(), "version": __version__, "boot": BOOT, "auth": CFG.ui_auth}


@app.post("/api/settings/test")
async def settings_test(request: Request):
    store = _settings_store()
    _settings_guard(request)
    try:
        d = await request.json()
    except ValueError:
        d = {}
    return await setupcore.run_test(store, d)


@app.post("/api/settings/save")
async def settings_save(request: Request):
    store = _settings_store()
    _settings_guard(request)
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if CFG.ui_auth:
        pw = str(payload.get("currentPass") or "")
        if not (pw and await asyncio.to_thread(_creds_ok, CFG.ui_user, pw)):
            log.warning("Einstellungen: falsches aktuelles Passwort von %s", _client(request))
            await asyncio.sleep(1)
            return JSONResponse({"error": t("Aktuelles Passwort falsch")}, status_code=403)
    if any(j["state"] not in FINAL for j in QUEUE.snapshot()):
        return JSONResponse({"error": t("Es läuft noch eine Übertragung – bitte warten, bis die Warteschlange fertig ist")},
                            status_code=409)
    with _set_lock:
        status, body = store.save(payload)
    if status == 200:
        setupcore.restart_later()
    return JSONResponse(body, status_code=status)


def abs_():
    if not ABS:
        raise HTTPException(404, t("Audiobookshelf ist nicht konfiguriert"))
    return ABS


@app.get("/api/libraries")
def libraries():
    return [{"id": l["id"], "name": l["name"]}
            for l in abs_().libraries() if l.get("mediaType", "book") == "book"]


@app.get("/api/libraries/{lib}/items")
def items(lib: str, page: int = Query(0, ge=0), limit: int = Query(48, ge=1, le=200),
          sort: str = Query("media.metadata.title"), desc: bool = False):
    if sort not in ("media.metadata.title", "addedAt", "media.duration", "media.metadata.authorName"):
        sort = "media.metadata.title"
    d = abs_().items(lib, limit, page, sort, desc)
    return {"items": [simplify(i) for i in d.get("results", [])], "total": d.get("total", 0),
            "page": page, "limit": limit}


@app.get("/api/libraries/{lib}/search")
def search(lib: str, q: str = Query(..., min_length=1, max_length=200)):
    d = abs_().search(lib, q)
    return {"items": [simplify(b["libraryItem"]) for b in d.get("book", []) if b.get("libraryItem")]}


@app.get("/api/libraries/{lib}/collections")
def collections(lib: str):
    return [{"id": c["id"], "name": c["name"], "count": len(c.get("books") or [])}
            for c in abs_().collections(lib)]


@app.get("/api/libraries/{lib}/collections/{cid}")
def collection_items(lib: str, cid: str):
    for c in abs_().collections(lib):
        if c.get("id") == cid:
            return {"id": cid, "name": c["name"], "items": [simplify(b) for b in c.get("books") or []]}
    raise HTTPException(404, t("Sammlung nicht gefunden"))


@app.get("/api/libraries/{lib}/series")
def series(lib: str):
    out = [{"id": s["id"], "name": s["name"], "count": len(s.get("books") or []),
            "coverId": (s.get("books") or [{}])[0].get("id")} for s in abs_().series(lib)]
    return sorted(out, key=lambda s: s["name"].lower())


@app.get("/api/libraries/{lib}/series/{sid}/items")
def series_items(lib: str, sid: str, page: int = Query(0, ge=0), limit: int = Query(48, ge=1, le=200)):
    check_id(sid)
    flt = "series." + base64.b64encode(sid.encode()).decode()
    d = abs_().items(lib, limit, page, "sequence", False, filter=flt)
    return {"items": [simplify(i) for i in d.get("results", [])], "total": d.get("total", 0),
            "page": page, "limit": limit}


_cover_cache = {}  # id -> (zeit, bytes, ctype); klein gehalten


@app.get("/api/cover/{item_id}")
def cover(item_id: str):
    check_id(item_id)
    hit = _cover_cache.get(item_id)
    if not hit or time.time() - hit[0] > 3600:
        res = abs_().cover(item_id)
        if not res:
            return Response(status_code=404)
        if len(_cover_cache) > 500:
            _cover_cache.clear()
        hit = (time.time(), *res)
        _cover_cache[item_id] = hit
    return Response(hit[1], media_type=hit[2], headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- Navidrome (nur lesend)

ND_SORT = {"addedAt:1": "newest", "media.metadata.title:0": "alphabeticalByName",
           "media.metadata.authorName:0": "alphabeticalByArtist"}


def nd():
    if not ND:
        raise HTTPException(404, t("Navidrome ist nicht konfiguriert"))
    return ND


def nd_lib(lib):
    if not lib.isdigit():
        raise HTTPException(422, t("ungültiger Musikordner"))
    return int(lib)


@app.get("/api/nd/libraries")
def nd_libraries():
    return [{"id": str(f["id"]), "name": f["name"]} for f in nd().folders()]


@app.get("/api/nd/libraries/{lib}/items")
def nd_items(lib: str, page: int = Query(0, ge=0), limit: int = Query(48, ge=1, le=200),
             sort: str = Query("media.metadata.title"), desc: bool = False):
    typ = ND_SORT.get(f"{sort}:{int(desc)}", "alphabeticalByName")
    al = nd().albums(nd_lib(lib), typ, limit, page * limit)
    # Subsonic liefert keine Gesamtzahl: bei voller Seite gibt es evtl. mehr
    total = page * limit + len(al) + (1 if len(al) == limit else 0)
    return {"items": [simplify_nd(a) for a in al], "total": total, "page": page, "limit": limit}


def simplify_song(s):
    cov = s.get("coverArt")
    return {"id": s.get("id"), "title": s.get("title") or "", "artist": s.get("artist") or "",
            "album": s.get("album") or "", "duration": s.get("duration") or 0,
            "cover": f"/api/nd/cover/{cov}" if cov else None}


def simplify_playlist(p):
    pid = p.get("id") or ""
    cov = p.get("coverArt")
    return {"id": pid, "source": "nd", "kind": "playlist", "mark": id8(pid, "nd", "playlist"),
            "title": p.get("name") or "", "author": p.get("owner") or "", "series": "Playlist",
            "duration": p.get("duration") or 0, "files": p.get("songCount"), "hasCover": bool(cov),
            "cover": f"/api/nd/cover/{cov}" if cov else None, "seq": None}


@app.get("/api/nd/libraries/{lib}/search")
def nd_search(lib: str, q: str = Query(..., min_length=1, max_length=200)):
    albums, songs = nd().search(nd_lib(lib), q, songs=50)
    return {"items": [simplify_nd(a) for a in albums], "songs": [simplify_song(s) for s in songs]}


@app.get("/api/nd/albums/{aid}/songs")
def nd_album_songs(aid: str):
    from .worker import nd_tracks
    a = nd().album(aid)
    return {"id": aid, "name": a.get("name") or "", "songs": [simplify_song({**s, "coverArt": s.get("coverArt") or a.get("coverArt")})
                                                              for s in nd_tracks(a)]}


@app.get("/api/nd/playlists")
def nd_playlists():
    return sorted((simplify_playlist(p) for p in nd().playlists()), key=lambda p: p["title"].lower())


@app.get("/api/nd/libraries/{lib}/series")
def nd_artists(lib: str):
    out = [{"id": a["id"], "name": a.get("name") or "", "count": a.get("albumCount") or 0,
            "cover": f"/api/nd/cover/{a['coverArt']}" if a.get("coverArt") else None}
           for a in nd().artists(nd_lib(lib))]
    return sorted(out, key=lambda a: a["name"].lower())


@app.get("/api/nd/libraries/{lib}/series/{sid}/items")
def nd_artist_albums(lib: str, sid: str, page: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
    al = nd().artist(sid).get("album") or []
    al.sort(key=lambda a: (a.get("year") or 0, (a.get("name") or "").lower()))
    return {"items": [simplify_nd(a) for a in al[page * limit:(page + 1) * limit]], "total": len(al),
            "page": page, "limit": limit}


ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_nd_cover_cache = {}


@app.get("/api/nd/cover/{cover_id}")
def nd_cover(cover_id: str):
    check_id(cover_id)
    hit = _nd_cover_cache.get(cover_id)
    if not hit or time.time() - hit[0] > 3600:
        res = nd().cover(cover_id)
        if not res:
            return Response(status_code=404)
        if len(_nd_cover_cache) > 500:
            _nd_cover_cache.clear()
        hit = (time.time(), *res)
        _nd_cover_cache[cover_id] = hit
    return Response(hit[1], media_type=hit[2], headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- Dateiquelle (NAS per NFS/SMB, nur lesend)

FSID = r"^(-|[A-Za-z0-9_-]{1,1400})$"


def fs():
    if not FS:
        raise HTTPException(404, t("Dateiquelle ist nicht konfiguriert"))
    return FS


@app.get("/api/fs/libraries")
def fs_libraries():
    return [{"id": str(i), "name": n} for i, (n, _) in enumerate(fs().roots)]


@app.get("/api/fs/libraries/{lib}/browse")
def fs_browse(lib: str, path: str = Query("-", pattern=FSID)):
    return fs().browse(lib, path)


@app.get("/api/fs/libraries/{lib}/search")
def fs_search(lib: str, q: str = Query(..., min_length=1, max_length=200)):
    return {"items": fs().search(lib, q)}


@app.get("/api/fs/libraries/{lib}/cover/{item_id}")
def fs_cover(lib: str, item_id: str):
    if not re.match(FSID, item_id):
        raise HTTPException(422, t("ungültige Ordner-ID"))
    res = fs().cover(lib, item_id)
    if not res:
        return Response(status_code=404)
    return Response(res[0], media_type=res[1], headers={"Cache-Control": "private, max-age=3600"})


_present = {"t": 0, "ids": [], "tafs": {}}


@app.get("/api/present")
def present():
    """id8-Kennzeichen aller TAFs unterhalb des Basisordners (kurz gecacht), dazu je Kennzeichen die Pfade
    (relativ zur Bibliothek; ganze TAF zuerst, Teile nach Nummer) für „Tonie zuweisen“ an der Kachel."""
    if time.time() - _present["t"] > 20 or QUEUE.changed > _present["t"]:
        hits = {}
        for b in CFG.tc_scan_bases:
            for d, n in scan_paths(TC, b):
                m = MARKER.search(n)
                if m:
                    hits.setdefault(m.group(1), []).append((b != CFG.tc_base, int(m.group(2) or 0), f"{d}/{n}"))
        tafs = {k: [p for *_, p in sorted(v)] for k, v in hits.items()}
        _present.update(t=time.time(), ids=sorted(tafs), tafs=tafs)
    return {"base": CFG.tc_base, "ids": _present["ids"], "tafs": _present["tafs"]}


class ReserveRequest(BaseModel):
    ruid: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")  # None = Vormerkung entfernen
    expect: str = Field("", max_length=2000)


class JobItem(BaseModel):
    id: str = Field(pattern=FSID)  # Dateiquelle: base64url des relativen Pfads, sonst max. 64 Zeichen (unten geprüft)
    title: str = Field("", max_length=500)
    split: bool = False
    merge: bool = False  # >99 Dateien: kürzeste Nachbarn zu einer TAF zusammenfügen (statt aufteilen)
    parts_min: int = Field(0, ge=0, le=600)  # >0: in Teile von etwa so vielen Minuten aufteilen (Oberfläche: 60)
    source: str = Field("abs", pattern=r"^(abs|nd|fs)$")
    lib: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    kind: str = Field("album", pattern=r"^(album|playlist|songs)$")  # songs = eigene Playlist (nur Navidrome)
    songs: list[str] = Field(default_factory=list, max_length=500)


class JobRequest(BaseModel):
    subfolder: str | None = Field(None, max_length=500)  # None/leer = automatische Ablage
    items: list[JobItem] = Field(min_length=1, max_length=500)
    bundle: str | None = Field(None, max_length=120)  # Name einer Sammel-TAF: alle items in *eine* TAF
    chapter_min: int = Field(0, ge=0, le=120)  # Dateien ohne Kapitelmarken: etwa alle n min ein Kapitel (0 = aus)
    tonie: ReserveRequest | None = None  # gleich beim Einreihen vormerken (nur bei genau einem Auftrag)


@app.post("/api/jobs")
def submit(req: JobRequest):
    if READONLY:
        raise HTTPException(403, t("Nur-Lese-Modus: Übertragen ist deaktiviert"))
    if any(i.source == "abs" for i in req.items) and not ABS:
        raise HTTPException(400, t("Audiobookshelf ist nicht konfiguriert"))
    if any(i.source == "nd" for i in req.items) and not ND:
        raise HTTPException(400, t("Navidrome ist nicht konfiguriert"))
    if any(i.source == "fs" for i in req.items) and not FS:
        raise HTTPException(400, t("Dateiquelle ist nicht konfiguriert"))
    for i in req.items:
        if i.source != "fs" and not ID.match(i.id):
            raise HTTPException(422, t("ungültige ID"))
        if i.source == "fs":
            if not (i.lib or "").isdigit():
                raise HTTPException(422, t("Dateiquelle: Freigabe fehlt"))
            FS.resolve(i.lib, i.id)  # wirft bei ungültigem Pfad / ausserhalb der Freigabe
        if i.split and i.merge:
            raise HTTPException(422, t("entweder Aufteilen oder Zusammenfügen"))
        if i.parts_min and (i.split or i.merge):
            raise HTTPException(422, t("entweder Teile nach Dauer oder Aufteilen/Zusammenfügen"))
        if i.parts_min and req.bundle is not None:
            raise HTTPException(422, t("eine Sammel-TAF kann nicht nach Dauer aufgeteilt werden"))
        if 0 < i.parts_min < 10:
            raise HTTPException(422, t("Teile nach Dauer: mindestens 10 min"))
        if i.kind != "album" and i.source != "nd":
            raise HTTPException(422, t("Playlists gibt es nur bei Navidrome"))
        if i.kind == "songs":
            if not i.songs or not all(ID.match(x) for x in i.songs):
                raise HTTPException(422, t("eigene Playlist: ungültige oder leere Titelliste"))
            if not i.title.strip():
                raise HTTPException(422, t("eigene Playlist: Name fehlt"))
    bundle = None
    if req.bundle is not None:
        bundle = req.bundle.strip()
        if not bundle:
            raise HTTPException(422, t("Sammel-TAF: Name fehlt"))
        if len(req.items) < 2:
            raise HTTPException(422, t("Sammel-TAF: mindestens zwei Einträge auswählen"))
        if len({(i.source, i.lib, i.id, i.kind) for i in req.items}) != len(req.items):
            raise HTTPException(422, t("Sammel-TAF: ein Eintrag ist doppelt ausgewählt"))
    if 0 < req.chapter_min < 3:
        raise HTTPException(422, t("Kapitel nach Dauer: mindestens 3 min"))
    sub = (req.subfolder or "").strip() or None
    if sub:
        clean_folder(sub)
    tonie = None
    if req.tonie and req.tonie.ruid:
        if len(req.items) != 1 and bundle is None:
            raise HTTPException(422, t("Tonie vormerken geht nur bei einem einzelnen Auftrag"))
        try:
            cur = _current(req.tonie.ruid, req.tonie.expect)
        except AssignError as e:
            raise HTTPException(e.status, str(e)) from None
        tonie = {"ruid": req.tonie.ruid, "name": cur["name"], "expect": cur["source"]}
    try:
        bid = QUEUE.submit([i.model_dump() for i in req.items], sub, bundle=bundle, chapter_min=req.chapter_min,
                           tonie=tonie)
    except Reserved as e:
        raise HTTPException(409, t("„{name}“ ist schon für „{title}“ vorgemerkt", name=tonie["name"], title=str(e))) from None
    if tonie:
        log.info("Stapel %s: Tonie %s (%s) vorgemerkt", bid, tonie["ruid"], tonie["name"])
    return {"batch": bid}


# ---------- Tonie zuweisen (seit 1.5)
# Einzige Änderung an bestehenden TeddyCloud-Einstellungen: die Quelle einer vorhandenen Figur, nur auf Knopfdruck
# mit Bestätigung, nur eigene TAFs. Wie die TeddyCloud-Oberfläche: source=lib://…, dazu nocloud=true und live=false.

RUID = re.compile(r"^[0-9a-f]{16}$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_tonies = {"t": 0.0, "list": [], "pics": {}}
_pic_cache = {}
_assign_lock = threading.Lock()


def _tonie_list(fresh=False):
    if fresh or time.time() - _tonies["t"] > 30:
        out, pics = [], {}
        for tg in TC.tags():
            ruid = str(tg.get("ruid") or "").lower()
            if not RUID.match(ruid) or tg.get("type") not in (None, "tag"):
                continue
            info = tg.get("tonieInfo") if isinstance(tg.get("tonieInfo"), dict) else {}
            name = " – ".join(str(x) for x in (info.get("series"), info.get("episode")) if x) or ruid
            if info.get("picture"):
                pics[ruid] = str(info["picture"])
            out.append({"ruid": ruid, "name": name, "model": str(info.get("model") or ""),
                        "source": str(tg.get("source") or ""), "live": bool(tg.get("live")),
                        "nocloud": bool(tg.get("nocloud")), "hide": bool(tg.get("hide")), "pic": ruid in pics})
        out.sort(key=lambda x: (x["hide"], x["name"].lower()))
        _tonies.update(t=time.time(), list=out, pics=pics)
    return _tonies["list"]


@app.get("/api/tonies")
def tonies():
    return {"tonies": _tonie_list(), "readonly": READONLY}


@app.get("/api/tonies/{ruid}/picture")
def tonie_picture(ruid: str):
    if not RUID.match(ruid):
        raise HTTPException(404)
    if ruid not in _pic_cache:
        url = _tonies["pics"].get(ruid)
        if not url:
            raise HTTPException(404)
        try:  # Adresse kommt von TeddyCloud (tonies.json), nie aus der Anfrage
            if url.startswith("/"):
                r = TC.raw_get(url, "Tonie-Bild")
            elif url.startswith(("https://", "http://")):
                r = httpx.get(url, timeout=10, follow_redirects=True)
            else:
                raise HTTPException(404)
        except (ApiError, httpx.HTTPError):
            raise HTTPException(404) from None
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        if r.status_code != 200 or not ctype.startswith("image/") or ctype == "image/svg+xml" or len(r.content) > 3 << 20:
            raise HTTPException(404)
        if len(_pic_cache) > 500:
            _pic_cache.clear()
        _pic_cache[ruid] = (ctype, r.content)
    ctype, data = _pic_cache[ruid]
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


class AssignRequest(BaseModel):
    ruid: str = Field(pattern=r"^[0-9a-f]{16}$")
    path: str = Field(min_length=5, max_length=1000)  # TAF relativ zur Bibliothek, z. B. teddybridge/Kinder/x [id].taf
    expect: str = Field("", max_length=2000)          # Quelle, die der Nutzer beim Bestätigen gesehen hat


class AssignError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def _check_path(path):
    """Prüfung ohne TeddyCloud-Zugriff auf die Datei. -> neue Quelle lib://…"""
    parts = path.split("/")
    if not path.lower().endswith(".taf") or any(p in ("", ".", "..") or p.startswith("_src_") for p in parts) \
            or CTRL_RE.search(path) or "\\" in path:
        raise AssignError(422, t("ungültiger TAF-Pfad"))
    if not any(path.startswith(b + "/") for b in CFG.tc_scan_bases):
        raise AssignError(422, t("nur TAFs aus dem eigenen Basisordner können zugewiesen werden"))
    new = "lib://" + path
    if len(new.encode()) > 255:  # TeddyCloud speichert höchstens 255 Bytes und kürzt sonst stillschweigend
        raise AssignError(422, t("Pfad zu lang für TeddyCloud ({n} von max. 255 Bytes) – Ordner oder Titel kürzen",
                                 n=len(new.encode())))
    return new


def _current(ruid, expect):
    """Aktuelle Figur; Abbruch, wenn es sie nicht gibt oder die Belegung nicht mehr der bestätigten entspricht."""
    cur = next((x for x in _tonie_list(fresh=True) if x["ruid"] == ruid), None)
    if not cur:
        raise AssignError(404, t("Tonie nicht gefunden (nur bestehende Figuren)"))
    if cur["source"] != expect:
        raise AssignError(409, t("Die Belegung von „{name}“ hat sich inzwischen geändert – bitte neu auswählen",
                                 name=cur["name"]))
    return cur


def assign_tonie(ruid, path, expect):
    """Eine eigene TAF einer bestehenden Figur zuweisen (wie die TeddyCloud-Oberfläche). -> {name, old, new}"""
    if READONLY:
        raise AssignError(403, t("Nur-Lese-Modus: Zuweisen ist deaktiviert"))
    new = _check_path(path)
    parent, name = path.rsplit("/", 1)
    if not any(f.get("name") == name and not f.get("isDirectory") for f in TC.list_dir(parent) or []):
        raise AssignError(404, t("TAF nicht gefunden: {path}", path=path))
    with _assign_lock:
        cur = _current(ruid, expect)
        if cur["source"] != new:
            TC.set_tag(ruid, "source", new)
        if not cur["nocloud"]:
            TC.set_tag(ruid, "nocloud", "true")
        if cur["live"]:
            TC.set_tag(ruid, "live", "false")
        after = next((x for x in _tonie_list(fresh=True) if x["ruid"] == ruid), None)
        if not after or after["source"] != new:
            raise ApiError(t("TeddyCloud hat die Zuweisung nicht übernommen (gelesen: {src})",
                             src=(after or {}).get("source") or "–"))
    if cur["source"] != new:
        log.info("Tonie %s (%s) zugewiesen: Quelle %r -> %r", ruid, cur["name"], cur["source"] or "", new)
    return {"name": cur["name"], "old": cur["source"], "new": new}


@app.post("/api/tonies/assign")
def tonie_assign(req: AssignRequest):
    try:
        return {"ok": True, **assign_tonie(req.ruid, req.path.strip(), req.expect)}
    except AssignError as e:
        raise HTTPException(e.status, str(e)) from None


@app.post("/api/jobs/{jid}/tonie")
def job_tonie(jid: int, req: ReserveRequest):
    """Tonie vormerken: wird zugewiesen, sobald der Auftrag fertig ist (erste TAF bzw. Teil 1)."""
    if READONLY:
        raise HTTPException(403, t("Nur-Lese-Modus: Zuweisen ist deaktiviert"))
    if jid not in QUEUE.jobs:
        raise HTTPException(404, t("Auftrag nicht gefunden"))
    tonie = None
    if req.ruid:
        try:
            cur = _current(req.ruid, req.expect)
        except AssignError as e:
            raise HTTPException(e.status, str(e)) from None
        tonie = {"ruid": req.ruid, "name": cur["name"], "expect": cur["source"]}
    ok, why = QUEUE.set_tonie(jid, tonie)
    if not ok:
        if why == "missing":
            raise HTTPException(404, t("Auftrag nicht gefunden"))
        if why == "final":
            raise HTTPException(409, t("Auftrag ist schon abgeschlossen – bitte direkt zuweisen"))
        raise HTTPException(409, t("„{name}“ ist schon für „{title}“ vorgemerkt", name=tonie["name"], title=why))
    if tonie:
        log.info("Auftrag %s: Tonie %s (%s) vorgemerkt", jid, req.ruid, tonie["name"])
    return {"ok": True, "tonie": tonie}


def _auto_assign(jid, job):
    """Aufruf aus dem Worker, wenn ein Auftrag mit vorgemerktem Tonie fertig/übersprungen ist."""
    tn, tafs = job["tonie"], job.get("tafs") or []
    try:
        if not tafs:
            raise AssignError(404, t("keine TAF gefunden"))
        r = assign_tonie(tn["ruid"], tafs[0], tn["expect"])
        res, ok = t("Tonie „{name}“ zugewiesen (vorher: {old})", name=r["name"], old=r["old"] or t("Original-Inhalt")), True
    except (AssignError, ApiError) as e:
        res, ok = t("Tonie „{name}“ nicht zugewiesen: {err}", name=tn["name"], err=str(e)), False
        log.warning("Auftrag %s: %s", jid, res)
    QUEUE._set(jid, tonie_result=res, tonie_ok=ok)


QUEUE.on_final = _auto_assign


@app.get("/api/jobs")
def jobs():
    return QUEUE.snapshot()


@app.post("/api/jobs/clear")
def clear():
    QUEUE.clear_finished()
    return {"ok": True}
