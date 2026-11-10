"""HTTP-Clients für Audiobookshelf und Navidrome (nur lesend) und TeddyCloud (special=library)."""
import base64
import hashlib
import http.client
import secrets
import logging
import os
import re
import threading
import time
import urllib.parse
from urllib.parse import quote
import uuid

import httpx

from .i18n import t

log = logging.getLogger("teddybridge")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
UA = "teddybridge/1.0"


class ApiError(Exception):
    """Fehler mit bereinigter Meldung (keine URLs mit Query, keine Geheimnisse)."""


def _err(service, what, r: httpx.Response):
    text = (r.text or "").strip().replace("\n", " ")[:200]
    return ApiError(f"{service} {what}: HTTP {r.status_code} {text}".strip())


def check_id(item_id: str) -> str:
    if not ID_RE.match(item_id or ""):
        raise ApiError(t("ungültige ID"))
    return item_id


class Abs:
    """Audiobookshelf – ausschliesslich GET-Aufrufe."""

    def __init__(self, cfg):
        self.c = httpx.Client(
            base_url=cfg.abs_url,
            headers={"Authorization": f"Bearer {cfg.abs_token}", "User-Agent": UA},
            timeout=httpx.Timeout(30.0, read=300.0),
            transport=httpx.HTTPTransport(retries=2),  # Verbindungsaufbau wiederholen
        )

    def _get(self, path, what, **params):
        try:
            r = self.c.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as e:
            raise ApiError(t("ABS {what}: Verbindungsfehler ({name})", what=what, name=type(e).__name__)) from None
        if r.status_code != 200:
            raise _err("ABS", what, r)
        return r

    def libraries(self):
        return self._get("/api/libraries", "Bibliotheken").json().get("libraries", [])

    def items(self, lib, limit, page, sort, desc, filter=None):
        return self._get(f"/api/libraries/{check_id(lib)}/items", "Items", limit=limit, page=page,
                         sort=sort, desc=1 if desc else 0, minified=1, filter=filter).json()

    def series(self, lib):
        data = self._get(f"/api/libraries/{check_id(lib)}/series", "Serien", limit=1000, page=0, minified=1).json()
        return data.get("results") or []

    def search(self, lib, q, limit=50):
        return self._get(f"/api/libraries/{check_id(lib)}/search", "Suche", q=q, limit=limit).json()

    def collections(self, lib):
        data = self._get(f"/api/libraries/{check_id(lib)}/collections", "Sammlungen").json()
        if isinstance(data, list):
            return data
        return data.get("results") or data.get("collections") or []

    def item(self, item_id):
        return self._get(f"/api/items/{check_id(item_id)}", "Item", expanded=1).json()

    def cover(self, item_id):
        try:
            r = self.c.get(f"/api/items/{check_id(item_id)}/cover")
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        return r.content, r.headers.get("content-type", "image/jpeg")

    def download(self, item_id, ino, dest_path, tries=4):
        """Gestreamter Download; bei Verbindungsfehlern (z. B. von ABS geschlossene
        Keep-Alive-Verbindung) bis zu `tries` Versuche mit kurzer Pause."""
        url = f"/api/items/{check_id(item_id)}/file/{check_id(str(ino))}/download"
        for attempt in range(1, tries + 1):
            try:
                with self.c.stream("GET", url) as r:
                    if r.status_code != 200:
                        r.read()
                        raise _err("ABS", "Download", r)
                    with open(dest_path, "wb") as f:
                        for chunk in r.iter_bytes(1 << 16):
                            f.write(chunk)
                return
            except httpx.HTTPError as e:
                if attempt == tries:
                    raise ApiError(t("ABS {what}: Verbindungsfehler ({name})", what="Download", name=type(e).__name__)
                                   + f" ({tries}×)") from None
                log.warning("ABS Download: %s, Versuch %d/%d", type(e).__name__, attempt, tries)
                time.sleep(2 * attempt)



class Nd:
    """Navidrome über die Subsonic-API – ausschliesslich lesende Aufrufe.
    Anmeldung je Aufruf mit Token = md5(Passwort + Salz); URLs werden nie geloggt."""

    API = "1.16.1"

    def __init__(self, cfg):
        self.user, self.pw = cfg.nd_user, cfg.nd_pass
        self.c = httpx.Client(
            base_url=cfg.nd_url,
            headers={"User-Agent": UA},
            timeout=httpx.Timeout(30.0, read=300.0),
            transport=httpx.HTTPTransport(retries=2),
        )

    def _auth(self):
        salt = secrets.token_hex(8)
        return {"u": self.user, "t": hashlib.md5((self.pw + salt).encode()).hexdigest(), "s": salt,
                "v": self.API, "c": "teddybridge", "f": "json"}

    def _call(self, method, what, **params):
        q = {**self._auth(), **{k: v for k, v in params.items() if v is not None}}
        try:
            r = self.c.get(f"/rest/{method}", params=q)
        except httpx.HTTPError as e:
            raise ApiError(t("Navidrome {what}: Verbindungsfehler ({name})", what=what, name=type(e).__name__)) from None
        if r.status_code != 200:
            raise ApiError(f"Navidrome {what}: HTTP {r.status_code}")
        try:
            d = r.json()["subsonic-response"]
        except (ValueError, KeyError):
            raise ApiError(t("Navidrome {what}: unerwartete Antwort", what=what)) from None
        if d.get("status") != "ok":
            err = d.get("error") or {}
            raise ApiError(f"Navidrome {what}: {str(err.get('message') or t('Fehler'))[:200]} (Code {err.get('code')})")
        return d

    def folders(self):
        return (self._call("getMusicFolders", "Musikordner").get("musicFolders") or {}).get("musicFolder") or []

    def albums(self, folder, type_, size, offset):
        d = self._call("getAlbumList2", "Alben", type=type_, size=size, offset=offset, musicFolderId=folder)
        return (d.get("albumList2") or {}).get("album") or []

    def search(self, folder, q, limit=100, songs=0):
        """-> (Alben, Titel)"""
        d = self._call("search3", "Suche", query=q, albumCount=limit, artistCount=0, songCount=songs,
                       musicFolderId=folder)
        r = d.get("searchResult3") or {}
        return r.get("album") or [], r.get("song") or []

    def playlists(self):
        return (self._call("getPlaylists", "Playlists").get("playlists") or {}).get("playlist") or []

    def playlist(self, pl_id):
        return self._call("getPlaylist", "Playlist", id=check_id(pl_id)).get("playlist") or {}

    def song(self, song_id):
        return self._call("getSong", "Titel", id=check_id(song_id)).get("song") or {}

    def artists(self, folder):
        idx = (self._call("getArtists", "Künstler", musicFolderId=folder).get("artists") or {}).get("index") or []
        return [a for i in idx for a in i.get("artist") or []]

    def artist(self, artist_id):
        return self._call("getArtist", "Künstler", id=check_id(artist_id)).get("artist") or {}

    def album(self, album_id):
        return self._call("getAlbum", "Album", id=check_id(album_id)).get("album") or {}

    def cover(self, cover_id, size=300):
        try:
            r = self.c.get("/rest/getCoverArt", params={**self._auth(), "id": check_id(cover_id), "size": size})
        except httpx.HTTPError:
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image/"):
            return None
        return r.content, ctype

    def download(self, song_id, dest_path, size=None, tries=4):
        """Originaldatei gestreamt laden. Subsonic meldet Fehler als JSON mit HTTP 200,
        daher Inhaltstyp prüfen; bei bekannter Grösse auch die Länge."""
        check_id(song_id)
        for attempt in range(1, tries + 1):
            try:
                with self.c.stream("GET", "/rest/download", params={**self._auth(), "id": song_id}) as r:
                    ctype = r.headers.get("content-type", "")
                    if r.status_code != 200 or "json" in ctype or "xml" in ctype:
                        body = r.read().decode(errors="replace")
                        msg = t("Fehler")
                        try:
                            msg = (httpx.Response(200, content=body).json()["subsonic-response"]
                                   .get("error") or {}).get("message") or msg
                        except (ValueError, KeyError):
                            pass
                        raise ApiError(f"Navidrome Download: HTTP {r.status_code} {str(msg)[:200]}")
                    got = 0
                    with open(dest_path, "wb") as f:
                        for chunk in r.iter_bytes(1 << 16):
                            f.write(chunk)
                            got += len(chunk)
                if size and got != size:
                    raise httpx.ReadError(f"{got} von {size} Bytes")
                return
            except httpx.HTTPError as e:
                if attempt == tries:
                    raise ApiError(t("Navidrome {what}: Verbindungsfehler ({name})", what="Download", name=type(e).__name__)
                                   + f" ({tries}×)") from None
                log.warning("Navidrome Download: %s, Versuch %d/%d", type(e).__name__, attempt, tries)
                time.sleep(2 * attempt)


class Tc:
    """TeddyCloud – alle Pfade relativ zur Bibliothek (special=library)."""

    LIB = {"special": "library"}

    def __init__(self, cfg):
        self.c = httpx.Client(
            base_url=cfg.tc_url,
            auth=cfg.tc_auth,
            headers={"User-Agent": UA},
            timeout=httpx.Timeout(30.0, read=600.0),
        )
        self._url = urllib.parse.urlsplit(cfg.tc_url)
        self._auth_header = ("Basic " + base64.b64encode(":".join(cfg.tc_auth).encode()).decode()
                             if cfg.tc_auth else None)
        # TeddyCloud-eigene Anmeldung (frontend.web_auth_enabled, seit 2026): POST /api/auth/login -> Session-Token,
        # danach Cookie tc_web_session. Wird nur benutzt, wenn TeddyCloud mit 401 ohne WWW-Authenticate antwortet.
        self._creds = cfg.tc_auth
        self._session = None
        self._login_lock = threading.Lock()
        self._login_failed = 0.0  # Zeitpunkt der letzten abgelehnten Anmeldung (TeddyCloud sperrt nach 5 Fehlern)

    SESSION_COOKIE = "tc_web_session"
    LOGIN_BACKOFF = 300  # s nach abgelehnter Anmeldung nicht erneut versuchen

    def _cookie(self):
        s = self._session
        return {"Cookie": f"{self.SESSION_COOKIE}={s}"} if s else {}

    def _needs_login(self, r):
        """401 von der TeddyCloud-Anmeldung (nicht von Basic Auth eines Proxys, die schickt WWW-Authenticate)."""
        return r.status_code == 401 and self._creds and "www-authenticate" not in r.headers

    def _login(self, old):
        """Bei TeddyCloud anmelden (einmal für alle Threads). -> True, wenn eine (neue) Sitzung da ist."""
        with self._login_lock:
            if self._session and self._session != old:
                return True  # ein anderer Thread hat sich inzwischen angemeldet
            if time.time() - self._login_failed < self.LOGIN_BACKOFF:
                raise ApiError(t("TeddyCloud: Anmeldung abgelehnt (TC_AUTH prüfen)"))
            try:
                r = self.c.post("/api/auth/login", json={"username": self._creds[0], "password": self._creds[1]},
                                timeout=30.0)
            except httpx.HTTPError as e:
                raise ApiError(t("TeddyCloud {what}: Verbindungsfehler ({name})", what=t("Anmeldung"), name=type(e).__name__)) from None
            finally:
                self.c.cookies.clear()  # Set-Cookie nicht übernehmen, das Token wird selbst mitgeschickt
            token = None
            if r.status_code == 200:
                try:
                    token = r.json().get("token")
                except ValueError:
                    token = None
            if not token or not isinstance(token, str) or not token.isalnum():
                self._session = None
                if r.status_code == 429:
                    self._login_failed = time.time()
                    raise ApiError(t("TeddyCloud: zu viele Anmeldeversuche, bitte später erneut versuchen"))
                if r.status_code in (400, 401):
                    self._login_failed = time.time()
                    log.warning("TeddyCloud: Anmeldung abgelehnt (HTTP %s)", r.status_code)
                    raise ApiError(t("TeddyCloud: Anmeldung abgelehnt (TC_AUTH prüfen)"))
                raise ApiError(t("TeddyCloud {what}: HTTP {status}", what=t("Anmeldung"), status=r.status_code))
            self._session = token
            self._login_failed = 0.0
            log.info("TeddyCloud: angemeldet (eigene Anmeldung von TeddyCloud)")
            return True

    RETRY_STATUS = (502, 503, 504)  # kommt vom Reverse Proxy, wenn TeddyCloud kurz nicht antwortet
    TRIES = 5

    def _request(self, method, path, what, retry=True, timeout=None, params=None, **kw):
        """Ein Aufruf; bei 502/503/504 oder Verbindungsfehler (nur wenn retry) mehrere Versuche.
        Datei-Objekte in `files` werden vor jedem Versuch zurückgespult."""
        tries = self.TRIES if retry else 1
        relogged = False
        attempt = 0
        while attempt < tries:
            attempt += 1
            for f in (kw.get("files") or {}).values():
                f[1].seek(0)
            old = self._session
            try:
                headers = {**self._cookie(), **(kw.get("headers") or {})}
                r = self.c.request(method, path, params=params if params is not None else self.LIB,
                                   timeout=timeout or self.c.timeout, headers=headers,
                                   **{k: v for k, v in kw.items() if k != "headers"})
                if self._needs_login(r) and not relogged:
                    relogged = True
                    self._login(old)
                    attempt -= 1  # Anmeldung zählt nicht als Versuch
                    continue
            except httpx.HTTPError as e:
                if attempt == tries:
                    raise ApiError(t("TeddyCloud {what}: Verbindungsfehler ({name})", what=what, name=type(e).__name__)) from None
                log.warning("TeddyCloud %s: %s, Versuch %d/%d", what, type(e).__name__, attempt, tries)
            else:
                if r.status_code not in self.RETRY_STATUS or attempt == tries:
                    return r
                log.warning("TeddyCloud %s: HTTP %s, Versuch %d/%d", what, r.status_code, attempt, tries)
            time.sleep(min(2 * attempt, 8))

    def _post(self, path, what, timeout=None, retry=True, **kw):
        r = self._request("POST", path, what, retry=retry, timeout=timeout, **kw)
        if r.status_code != 200:
            raise _err("TeddyCloud", what, r)
        return r

    def list_dir(self, rel):
        """Liste der Einträge (None bei HTTP-Fehler).
        Achtung: Die echte TeddyCloud liefert auch für fehlende Ordner 200 mit leerer Liste,
        für Existenzprüfungen daher dir_exists() verwenden."""
        r = self._request("GET", "/api/fileIndex", "fileIndex", params={"path": rel, **self.LIB})
        if r.status_code in (401, 403):
            raise ApiError(t("TeddyCloud: Zugriff verweigert (HTTP {status}, TC_AUTH prüfen)", status=r.status_code))
        if r.status_code != 200:
            return None
        try:
            return [f for f in r.json().get("files", []) if f.get("name") not in (".", "..")]
        except ValueError:
            return None

    def taf_header(self, rel_dir, name):
        """TAF-Kopf (valid, lengthSeconds, trackSeconds) über fileIndexV2, None wenn nicht verfügbar."""
        try:
            r = self._request("GET", "/api/fileIndexV2", "fileIndexV2", params={"path": rel_dir, **self.LIB})
            if r.status_code != 200:
                return None
            for f in r.json().get("files", []):
                if f.get("name") == name:
                    return f.get("tafHeader")
        except (ApiError, ValueError):
            return None
        return None

    def mkdir(self, rel):
        try:
            self._post("/api/dirCreate", "dirCreate", retry=False, content=rel.encode())
        except ApiError:
            # evtl. hat ein vom Proxy abgebrochener Aufruf den Ordner doch angelegt
            time.sleep(2)
            if not self.dir_exists(rel):
                self._post("/api/dirCreate", "dirCreate", content=rel.encode())

    def dir_exists(self, rel):
        """Existenz über den Eintrag im übergeordneten Ordner prüfen."""
        parent, _, name = rel.rstrip("/").rpartition("/")
        entries = self.list_dir(parent) or []
        return any(f.get("name") == name and f.get("isDirectory") for f in entries)

    def ensure_dirs(self, rel):
        """Legt jede Ebene einzeln an (dirCreate ist nicht rekursiv)."""
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            if not self.dir_exists(sub):
                self.mkdir(sub)

    UPLOAD_TRIES = 4

    def file_size(self, rel_dir, name):
        """Grösse einer Datei laut fileIndex, None wenn nicht ermittelbar."""
        for f in self.list_dir(rel_dir) or []:
            if f.get("name") == name and not f.get("isDirectory"):
                return f.get("size")
        return None

    def _send_upload(self, rel_dir, local_path, name):
        """multipart-Upload mit http.client, gibt den HTTP-Status zurück.
        Grund (am echten Server gemessen, 2026-11-07): TeddyCloud (multipart_handle) wertet
        ein httpReceive mit 0 Bytes direkt nach den Headern als Ende des Bodys und antwortet
        200 OK mit leerer Datei. httpx schickt Header und Body getrennt, was in den meisten
        Fällen 0 Bytes ergab. Header und Body-Anfang im selben Paket senden (wie curl):
        in 11 von 11 Fällen erfolgreich."""
        u = self._url
        b = uuid.uuid4().hex
        safe = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        head = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{b}--\r\n".encode()
        size = os.path.getsize(local_path)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.hostname, u.port, timeout=600)
        try:
            conn.putrequest("POST", u.path.rstrip("/") + "/api/fileUpload?"
                            + urllib.parse.urlencode({"path": rel_dir, **self.LIB}))
            conn.putheader("Content-Type", f"multipart/form-data; boundary={b}")
            conn.putheader("Content-Length", str(len(head) + size + len(tail)))
            conn.putheader("User-Agent", UA)
            if self._auth_header:
                conn.putheader("Authorization", self._auth_header)
            for k, v in self._cookie().items():
                conn.putheader(k, v)
            with open(local_path, "rb") as fh:
                conn.endheaders(head + fh.read(1 << 16))  # Header + Body-Anfang in einem Paket
                while chunk := fh.read(1 << 16):
                    conn.send(chunk)
            conn.send(tail)
            r = conn.getresponse()
            r.read()
            return r.status, r.getheader("WWW-Authenticate")
        finally:
            conn.close()

    def upload(self, rel_dir, local_path, name):
        """Hochladen und Grösse prüfen. Falls die Datei trotzdem leer oder unvollständig
        ankommt bzw. ein 502/Verbindungsfehler auftritt: nur im eigenen _src_-Ordner
        löschen und erneut versuchen."""
        size = os.path.getsize(local_path)
        own = rel_dir.rstrip("/").rsplit("/", 1)[-1].startswith("_src_")
        got = last = None
        for attempt in range(1, self.UPLOAD_TRIES + 1):
            try:
                old = self._session
                status, www = self._send_upload(rel_dir, local_path, name)
                if status == 401 and self._creds and not www:
                    self._login(old)
                    status, www = self._send_upload(rel_dir, local_path, name)
            except (OSError, http.client.HTTPException) as e:
                status = None
                log.warning("TeddyCloud Upload: %s, Versuch %d/%d", type(e).__name__, attempt, self.UPLOAD_TRIES)
            if status in (401, 403):
                raise ApiError(t("TeddyCloud: Zugriff verweigert (HTTP {status}, TC_AUTH prüfen)", status=status))
            if status is not None and status != 200 and status not in self.RETRY_STATUS:
                raise ApiError(t("TeddyCloud Upload: HTTP {status}", status=status))
            if status == 200:
                got = last = self.file_size(rel_dir, name)
                if got == size:
                    return
                log.warning("TeddyCloud Upload: %s hat %s von %d Bytes, Versuch %d/%d",
                            name, got, size, attempt, self.UPLOAD_TRIES)
            elif status is not None:
                log.warning("TeddyCloud Upload: HTTP %s, Versuch %d/%d", status, attempt, self.UPLOAD_TRIES)
            if not own:
                break
            if got is not None:
                self.delete_file(f"{rel_dir}/{name}")
                got = None
            time.sleep(min(2 * attempt, 8))
        raise ApiError(t("TeddyCloud Upload: {name} kam unvollständig an ({last} von {size} Bytes, {n} Versuche).",
                         name=name, last=last, size=size, n=self.UPLOAD_TRIES))

    def encode(self, target, sources, wait=3600.0):
        """wait: so lange auf die Antwort warten (TeddyCloud antwortet erst, wenn die TAF fertig ist)."""
        data = {"target": target, "source": list(sources)}
        # nicht wiederholen: synchron und lang, und ein zweiter Aufruf scheitert am vorhandenen Ziel
        self._post("/api/fileEncode", "Umwandeln", data=data, retry=False,
                   timeout=httpx.Timeout(30.0, read=max(3600.0, float(wait))))

    # ---------- Tonies (Zuweisen, seit 1.5; ohne special=library, wie die TeddyCloud-Oberfläche)

    def tags(self):
        """Bekannte Tonie-Figuren: GET /api/getTagIndex -> [{ruid, type, source, live, nocloud, hide, tonieInfo{...}}]"""
        r = self._request("GET", "/api/getTagIndex", "getTagIndex", params={})
        if r.status_code in (401, 403):
            raise ApiError(t("TeddyCloud: Zugriff verweigert (HTTP {status}, TC_AUTH prüfen)", status=r.status_code))
        if r.status_code != 200:
            raise _err("TeddyCloud", "getTagIndex", r)
        try:
            tags = r.json().get("tags")
        except ValueError:
            tags = None
        if not isinstance(tags, list):
            raise ApiError(t("TeddyCloud getTagIndex: unerwartete Antwort"))
        return [x for x in tags if isinstance(x, dict)]

    def set_tag(self, ruid, field, value):
        """Ein Feld der Tonie-Einstellungen setzen (POST /content/json/set/<ruid>, Formular wie die TC-Oberfläche).
        Leerzeichen als %20: TeddyCloud dekodiert '+' nicht."""
        body = f"{field}={quote(str(value), safe='')}".encode()
        self._post(f"/content/json/set/{ruid}", "Tonie zuweisen", params={}, content=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})

    def raw_get(self, path, what):
        """GET auf TeddyCloud ohne Bibliotheks-Parameter (z. B. Tonie-Bilder) -> httpx.Response"""
        return self._request("GET", path, what, params={}, retry=False)

    def delete_file(self, rel):
        self._post("/api/fileDelete", "fileDelete", content=rel.encode())

    def delete_dir(self, rel):
        self._post("/api/dirDelete", "dirDelete", content=rel.encode())
