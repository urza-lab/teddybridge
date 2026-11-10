#!/usr/bin/env python3
"""Mock-Server für Audiobookshelf und TeddyCloud (nur Standardbibliothek).

Start:  python3 mock/mock_servers.py --data /pfad/zum/tc-library [--abs-port 13378] [--tc-port 8088]

ABS-Mock:  Token "mocktoken", nur GET erlaubt (jeder andere Aufruf wird protokolliert und mit 405 beantwortet).
TC-Mock:   Bibliothek = Verzeichnis --data. fileEncode schreibt eine Fake-TAF (Inhalt = Quell-Dateinamen).
           Ein Item, dessen Titel "ENCODE-FAIL" enthält, lässt fileEncode mit 500 scheitern.
Discord:   POST /_mock/discord/<beliebig> sammelt Webhook-Meldungen, GET /_mock/discord liefert sie.
Leer-Upload: POST /_mock/empty-uploads {"match": "<teil des pfads>", "mode": "once"|"always"} lässt
           fileUpload trotz 200 OK eine leere Datei anlegen (once: nur beim ersten Upload je Datei).
Web-Auth:  POST /_mock/web-auth {"enabled": true|false, "reset": true} schaltet die TeddyCloud-eigene Anmeldung
           (POST /api/auth/login, Cookie tc_web_session, 401 ohne WWW-Authenticate) ein/aus; reset = alle
           Sitzungen ungültig (wie ein Neustart von TeddyCloud). Benutzer:Passwort = --tc-auth.
ND-Mock:   Navidrome/Subsonic, Benutzer "nduser", Passwort "ndpassword55" (nur Token-Anmeldung t/s), nur GET.
           Ein Titel mit "DLFAIL" im Namen liefert beim Download einen Subsonic-Fehler (JSON, HTTP 200).
Diagnose:  GET /_mock/log auf allen Ports liefert das Aufrufprotokoll als JSON, POST /_mock/reset leert es.
"""
import argparse
import json
import hashlib
import os
import shutil
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

TOKEN = "mocktoken"

# ---------------------------------------------------------------- ABS-Daten

def _item(iid, lib, title, author, series=None, nfiles=3, excluded=(), shuffled=False, duration=None, seq=1):
    files = []
    order = list(range(1, nfiles + 1))
    if shuffled:
        order = list(reversed(order))
    for pos, idx in enumerate(order):
        files.append({
            "ino": f"{iid[:4]}ino{idx:03d}",
            "index": idx,
            "exclude": idx in excluded,
            "duration": 60.0,
            "metadata": {"filename": f"Track {idx:02d}.mp3", "ext": ".mp3", "size": 2048},
        })
    return {
        "id": iid, "libraryId": lib, "mediaType": "book",
        "media": {
            "metadata": {"title": title, "authors": [{"id": "au_" + author[:4], "name": author}],
                         "series": [{"id": "se_" + series[:4].replace(" ", ""), "name": series, "sequence": str(seq)}] if series else []},
            "duration": duration if duration is not None else 60.0 * nfiles,
            "numAudioFiles": nfiles,
            "coverPath": f"/metadata/items/{iid}/cover.jpg",
            "audioFiles": files,
        },
    }

M4B_ID = "abba4b00-0000-4000-8000-00000000004b"

LIBRARIES = [
    {"id": "lib_kids", "name": "Kinder", "mediaType": "book"},
    {"id": "lib_pod", "name": "Podcasts", "mediaType": "podcast"},
]

ITEMS = {i["id"]: i for i in [
    _item("aaaa1111-0000-4000-8000-000000000001", "lib_kids", "Die drei ??? Kids: Folge 1", "Ulf Blanck", "Die drei ??? Kids", 3),
    _item("bbbb2222-0000-4000-8000-000000000002", "lib_kids", "Bibi Blocksberg / Hexen: *Spezial*", "Elfie Donnelly", "Bibi Blocksberg", 4, excluded={2}, shuffled=True),
    _item("cccc3333-0000-4000-8000-000000000003", "lib_kids", "Zu viele Dateien", "Test", None, 100),
    _item("dddd4444-0000-4000-8000-000000000004", "lib_kids", "ENCODE-FAIL Hörspiel", "Test", None, 2),
    _item("eeee5555-0000-4000-8000-000000000005", "lib_kids", "Benjamin Blümchen " + "x" * 150, "Elfie Donnelly", "Benjamin Blümchen", 1),
] + [
    _item(f"f{n:03d}0000-0000-4000-8000-0000000001{n:02d}", "lib_kids", f"Füllmaterial {n:02d}", "Autor",
          "Bobo" if n <= 5 else None, 1, seq=6 - n)
    for n in range(1, 31)
]}

COLLECTIONS = [
    {"id": "col_1", "name": "Einschlafen", "libraryId": "lib_kids",
     "bookIds": ["aaaa1111-0000-4000-8000-000000000001", "bbbb2222-0000-4000-8000-000000000002"]},
    {"id": "col_2", "name": "Test/Fehler", "libraryId": "lib_kids",
     "bookIds": ["cccc3333-0000-4000-8000-000000000003", "dddd4444-0000-4000-8000-000000000004"]},
]

# 1x1 PNG
COVER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cff01f0005fe02fea7d6a9a90000000049454e44ae426082")


def minified(item):
    m = item["media"]
    return {"id": item["id"], "libraryId": item["libraryId"], "mediaType": "book",
            "media": {"metadata": {"title": m["metadata"]["title"],
                                   "authorName": ", ".join(x["name"] for x in m["metadata"]["authors"]),
                                   "seriesName": ", ".join(f'{x["name"]} #{x["sequence"]}' for x in m["metadata"]["series"])},
                      "duration": m["duration"],
                      "numAudioFiles": m["numAudioFiles"], "coverPath": m["coverPath"]}}


def collection_out(c):
    out = {k: v for k, v in c.items() if k != "bookIds"}
    out["books"] = [ITEMS[i] for i in c["bookIds"]]
    return out

# ---------------------------------------------------------------- Basis-Handler

class Base(BaseHTTPRequestHandler):
    log = None  # pro Klasse gesetzt
    server_version = "mock"

    def log_message(self, fmt, *args):
        pass

    def _record(self, status, extra=None):
        entry = {"t": time.time(), "method": self.command, "path": self.path, "status": status}
        if extra:
            entry.update(extra)
        self.log.append(entry)

    def send(self, status, body=b"", ctype="application/json", extra=None, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
            if ctype == "application/json":
                ctype = "text/plain; charset=utf-8"
        self._record(status, extra)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def diag(self):
        u = urlparse(self.path)
        if u.path == "/_mock/log":
            data = json.dumps(self.log).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True
        if u.path == "/_mock/reset":
            self.log.clear()
            self.send_response(204)
            self.end_headers()
            return True
        return False

# ---------------------------------------------------------------- ABS

class AbsHandler(Base):
    log = []
    dropped = set()  # einmalige Verbindungsabbrüche

    def _auth(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send(401, "Unauthorized")
            return False
        return True

    def do_POST(self):
        if self.diag():
            return
        self.send(405, "mock ABS: read only", extra={"write_attempt": True})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def do_GET(self):
        if self.diag() or not self._auth():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/").split("/")[1:]  # ["api", ...]

        if p == ["api", "libraries"]:
            return self.send(200, {"libraries": LIBRARIES})
        if len(p) >= 3 and p[:2] == ["api", "libraries"]:
            lib = p[2]
            if lib not in {l["id"] for l in LIBRARIES}:
                return self.send(404, "Not Found")
            items = [i for i in ITEMS.values() if i["libraryId"] == lib]
            if p[3:] == ["series"]:
                groups = {}
                for i in items:
                    for se in i["media"]["metadata"]["series"]:
                        groups.setdefault(se["id"], {"id": se["id"], "name": se["name"], "libraryId": lib, "books": []})["books"].append(i)
                res = list(groups.values())
                return self.send(200, {"results": res, "total": len(res), "limit": 0, "page": 0})
            if p[3:] == ["items"]:
                limit = int(q.get("limit", 0) or 0)
                if q.get("filter", "").startswith("series."):
                    import base64
                    sid = base64.b64decode(q["filter"][7:]).decode()
                    items = [i for i in items if any(se["id"] == sid for se in i["media"]["metadata"]["series"])]
                    if q.get("sort") == "sequence":
                        items.sort(key=lambda i: next(float(se["sequence"]) for se in i["media"]["metadata"]["series"] if se["id"] == sid))
                        q["sort"] = "none"
                page = int(q.get("page", 0) or 0)
                sort = q.get("sort", "media.metadata.title")
                if sort != "none":
                    key = (lambda i: i["media"]["metadata"].get("title", "").lower()) if "title" in sort else (lambda i: i["id"])
                    items.sort(key=key, reverse=q.get("desc") == "1")
                sel = items[page * limit:(page + 1) * limit] if limit else items
                out = [minified(i) for i in sel]
                if q.get("filter", "").startswith("series."):
                    # wie echtes ABS: bei Serien-Filter ist metadata.series ein einzelnes Objekt
                    for o, i in zip(out, sel):
                        o["media"]["metadata"]["series"] = next(
                            dict(se) for se in i["media"]["metadata"]["series"] if se["id"] == sid)
                return self.send(200, {"results": out, "total": len(items),
                                       "limit": limit, "page": page})
            if p[3:] == ["search"]:
                term = q.get("q", "").lower()
                limit = int(q.get("limit", 12) or 12)
                hits = [i for i in items if term in i["media"]["metadata"]["title"].lower()
                        or any(term in x["name"].lower() for x in i["media"]["metadata"]["authors"])][:limit]
                return self.send(200, {"book": [{"libraryItem": i, "matchKey": "title"} for i in hits],
                                       "podcast": [], "tags": [], "authors": [], "series": [], "narrators": []})
            if p[3:] == ["collections"]:
                cols = [collection_out(c) for c in COLLECTIONS if c["libraryId"] == lib]
                return self.send(200, {"results": cols, "total": len(cols), "limit": 0, "page": 0})
        if p == ["api", "collections"]:
            return self.send(200, {"collections": [collection_out(c) for c in COLLECTIONS]})
        if len(p) >= 3 and p[:2] == ["api", "items"]:
            item = ITEMS.get(p[2])
            if not item:
                return self.send(404, "Not Found")
            if len(p) == 3:
                return self.send(200, item)
            if p[3:] == ["cover"]:
                return self.send(200, COVER_PNG, "image/png")
            if len(p) == 6 and p[3] == "file" and p[5] == "download":
                af = next((a for a in item["media"]["audioFiles"] if a["ino"] == p[4]), None)
                if not af:
                    return self.send(404, "Not Found")
                if af.get("_path"):  # echte Datei (m4b-Test)
                    with open(af["_path"], "rb") as fh:
                        return self.send(200, fh.read(), "audio/mp4")
                data = (f"AUDIO {item['id']} {af['index']}\n".encode()) * 64
                key = (item["id"], af["ino"])
                if item["id"].startswith("aaaa1111") and af["index"] == 2 and key not in AbsHandler.dropped:
                    # einmaliger Verbindungsabbruch mitten im Download (Wiederholversuch testen)
                    AbsHandler.dropped.add(key)
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[:100])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                return self.send(200, data, "audio/mpeg")
        self.send(404, "Not Found")

# ---------------------------------------------------------------- TeddyCloud

class TcHandler(Base):
    log = []
    root = None
    encode_delay = 0.5
    auth = None
    discord = []
    crashed = set()  # Ziele, bei denen ENCODE-CRASH-ONCE schon einmal abgestürzt ist
    upload_delay = 0.0
    empty_uploads = {}   # match -> mode
    empty_seen = set()
    web_auth = False
    sessions = set()
    logins = 0
    # Tonie-Figuren wie /api/getTagIndex (content/<ruid[:8]>/<ruid[8:]>.json in echt)
    tags = {
        "e00403500aaa0001": {"type": "tag", "source": "", "live": False, "nocloud": False, "hide": False,
                             "tonieInfo": {"model": "10000001", "series": "Bobo Siebenschläfer", "episode": "Folge 1",
                                           "picture": "/img/bobo.png"}},
        "e00403500aaa0002": {"type": "tag", "source": "lib://fremd/eigenes Hörspiel.taf", "live": True, "nocloud": True,
                             "hide": False, "tonieInfo": {"model": "", "series": "", "episode": "", "picture": ""}},
        "0000000000000000": {"type": "system", "source": "", "live": False, "nocloud": False, "hide": False},
    }
    tag_sets = []

    def _lib(self, q):
        if q.get("special") != "library":
            self.send(400, "special=library missing")
            return False
        return True

    def _resolve(self, rel):
        rel = rel.strip().lstrip("/")
        full = os.path.normpath(os.path.join(self.root, rel))
        if not (full == self.root or full.startswith(self.root + os.sep)):
            raise ValueError("path escapes library")
        return full

    def do_GET(self):
        if self.diag():
            return
        u = urlparse(self.path)
        if u.path == "/_mock/tags":
            return self.send(200, {"sets": TcHandler.tag_sets, "tags": TcHandler.tags})
        if u.path == "/_mock/discord":
            data = json.dumps(self.discord).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
            return
        if not self._auth_ok():
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/getTagIndex":
            if "special" in q:  # wäre in echt die Bibliothek statt content
                return self.send(200, {"tags": []})
            return self.send(200, {"tags": [{"ruid": r, **v} for r, v in TcHandler.tags.items()]})
        if u.path == "/img/bobo.png":
            data = b"\x89PNG\r\n\x1a\nmock"
            self.send_response(200); self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            return
        if u.path == "/api/fileIndexV2":
            if not self._lib(q):
                return
            d = self._resolve(q.get("path", ""))
            files = []
            for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                fp = os.path.join(d, n)
                e = {"name": n, "isDir": os.path.isdir(fp)}
                if n.endswith(".taf") and os.path.isfile(fp):
                    lines = open(fp).read().splitlines()
                    if lines and lines[0] == "FAKE-TAF":
                        k = len(lines) - 1
                        e["tafHeader"] = {"valid": True, "lengthSeconds": 60 * k, "trackSeconds": [1 + 60 * i for i in range(k)]}
                files.append(e)
            return self.send(200, {"files": files})
        if u.path == "/api/fileIndex":
            if not self._lib(q):
                return
            d = self._resolve(q.get("path", ""))
            if not os.path.isdir(d):  # wie die echte TeddyCloud: 200 mit leerer Liste
                return self.send(200, {"files": []})
            files = [{"name": "..", "date": 0, "size": 0, "isDirectory": True}]
            for n in sorted(os.listdir(d)):
                fp = os.path.join(d, n)
                st = os.stat(fp)
                files.append({"name": n, "date": int(st.st_mtime), "size": st.st_size,
                              "isDirectory": os.path.isdir(fp)})
            return self.send(200, {"files": files})
        self.send(404, "Not Found")

    def _auth_ok(self):
        if TcHandler.web_auth:
            ck = self.headers.get("Cookie") or ""
            tok = next((c.split("=", 1)[1] for c in ck.replace(" ", "").split(";") if c.startswith("tc_web_session=")), None)
            if tok in TcHandler.sessions:
                return True
            self.send(401, {"error": "unauthorized", "message": "Login required"})
            return False
        if not self.auth:
            return True
        import base64
        exp = "Basic " + base64.b64encode(self.auth.encode()).decode()
        if self.headers.get("Authorization") != exp:
            self.send(401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="teddycloud"'})
            return False
        return True

    def do_POST(self):
        if self.diag():
            return
        u = urlparse(self.path)
        if u.path.startswith("/_mock/discord/"):
            self.discord.append(json.loads(self.body() or b"{}"))
            return self.send(204, b"", extra={"discord": True})
        if u.path == "/_mock/discord":
            return self.send(404, "")
        if u.path == "/_mock/web-auth":
            cfg = json.loads(self.body() or b"{}")
            TcHandler.web_auth = bool(cfg.get("enabled"))
            if cfg.get("reset"):
                TcHandler.sessions.clear()
            return self.send(200, {"logins": TcHandler.logins})
        if u.path == "/api/auth/login" and TcHandler.web_auth:
            TcHandler.logins += 1
            try:
                cred = json.loads(self.body() or b"{}")
            except ValueError:
                return self.send(400, {"error": "invalid_body"})
            if f"{cred.get('username')}:{cred.get('password')}" != self.auth:
                return self.send(401, {"error": "unauthorized", "message": "Invalid username or password"})
            import secrets
            tok = secrets.token_hex(32)
            TcHandler.sessions.add(tok)
            return self.send(200, {"ok": True, "username": cred["username"], "token": tok})
        if u.path == "/_mock/encode-delay":
            TcHandler.encode_delay = float(json.loads(self.body() or b"{}").get("secs", 0.5))
            return self.send(200, "OK")
        if u.path == "/_mock/empty-uploads":
            cfg = json.loads(self.body() or b"{}")
            TcHandler.empty_uploads[cfg["match"]] = cfg["mode"]
            return self.send(200, "OK")
        if not self._auth_ok():
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.startswith("/content/json/set/"):
            ruid = u.path.rsplit("/", 1)[1]
            body = self.body().decode()
            if ruid not in TcHandler.tags or "special" in q:
                return self.send(404, "Not Found")
            if self.headers.get("Content-Type") != "application/x-www-form-urlencoded" or "+" in body:
                return self.send(400, "bad form")
            k, _, v = body.partition("=")
            v = unquote(v).encode()[:255].decode("utf-8", "ignore")  # wie TeddyCloud: max. 255 Bytes
            tg = TcHandler.tags[ruid]
            if k == "source":
                tg["source"] = v
            elif k in ("live", "nocloud", "hide"):
                tg[k] = v == "true"
            TcHandler.tag_sets.append((ruid, k, v))
            return self.send(200, "OK")
        if not self._lib(q):
            return
        try:
            if u.path == "/api/dirCreate":
                rel = self.body().decode()
                d = self._resolve(rel)
                if not os.path.isdir(os.path.dirname(d)):
                    return self.send(500, "parent missing (not recursive)", extra={"rel": rel})
                if os.path.exists(d):
                    return self.send(500, "exists", extra={"rel": rel})
                os.mkdir(d)
                return self.send(200, "OK", extra={"rel": rel})
            if u.path == "/api/fileUpload":
                d = self._resolve(q.get("path", ""))
                if not os.path.isdir(d):
                    return self.send(500, "target dir missing")
                raw = b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + self.body()
                msg = BytesParser(policy=HTTP).parsebytes(raw)
                names = []
                for part in msg.iter_parts():
                    fn = part.get_filename()
                    if fn:
                        fn = os.path.basename(fn)
                        rel_f = q.get("path", "") + "/" + fn
                        mode = next((m for k, m in self.empty_uploads.items() if k in rel_f), None)
                        empty = mode == "always" or (mode == "once" and rel_f not in self.empty_seen)
                        self.empty_seen.add(rel_f)
                        with open(os.path.join(d, fn), "wb") as f:
                            f.write(b"" if empty else part.get_payload(decode=True))
                        names.append(fn)
                if not names:
                    return self.send(400, "no file")
                time.sleep(self.upload_delay)
                return self.send(200, "OK", extra={"files": names, "rel": q.get("path")})
            if u.path == "/api/fileEncode":
                form = parse_qs(self.body().decode(), keep_blank_values=True)
                target = form.get("target", [""])[0]
                sources = form.get("source", [])
                tfull = self._resolve(target)
                if os.path.exists(tfull):
                    return self.send(500, "target exists", extra={"target": target})
                if not sources or len(sources) > 99:
                    return self.send(400, "bad source count")
                for s in sources:
                    if not os.path.isfile(self._resolve(s)):
                        return self.send(500, f"source missing: {s}")
                time.sleep(self.encode_delay)
                crash_once = "ENCODE-CRASH-ONCE" in target and target not in TcHandler.crashed
                if crash_once:
                    TcHandler.crashed.add(target)
                if crash_once or ("ENCODE-CRASH" in target and "ENCODE-CRASH-ONCE" not in target):  # TeddyCloud stirbt mitten im Schreiben: halbe TAF bleibt, Proxy meldet 502
                    with open(tfull, "w") as f:
                        f.write("FAKE-TAF\n")
                    return self.send(502, "Bad Gateway (mock)", extra={"target": target})
                if "ENCODE-FAIL" in target:
                    return self.send(500, "encode failed (mock)", extra={"target": target})
                if getattr(self, "keep", None):  # Quellen aufbewahren, damit der Test jedes Kapitel prüfen kann
                    kd = os.path.join(self.keep, target.replace("/", "_"))
                    os.makedirs(kd, exist_ok=True)
                    for i, src in enumerate(sources, 1):
                        shutil.copy(self._resolve(src), os.path.join(kd, f"{i:03d}{os.path.splitext(src)[1]}"))
                with open(tfull, "w") as f:
                    f.write("FAKE-TAF\n")
                    for src in sources:
                        with open(self._resolve(src), "rb") as sf:
                            data = sf.read()
                        first = data.split(b"\n", 1)[0][:200].decode("utf-8", "replace").strip()
                        first = "".join(ch if ch.isprintable() and ch != "|" else "?" for ch in first)  # Binärdaten
                        f.write(f"{src} | {first} | size={len(data)} sha={hashlib.sha256(data).hexdigest()[:16]}\n")
                return self.send(200, "OK", extra={"target": target, "sources": sources})
            if u.path == "/api/fileDelete":
                rel = self.body().decode()
                fp = self._resolve(rel)
                if not os.path.isfile(fp):
                    return self.send(500, "not a file", extra={"rel": rel})
                os.remove(fp)
                return self.send(200, "OK", extra={"rel": rel})
            if u.path == "/api/dirDelete":
                rel = self.body().decode()
                d = self._resolve(rel)
                if not os.path.isdir(d) or os.listdir(d):
                    return self.send(500, "missing or not empty", extra={"rel": rel})
                os.rmdir(d)
                return self.send(200, "OK", extra={"rel": rel})
        except ValueError as e:
            return self.send(400, str(e))
        self.send(404, "Not Found")


# ---------------------------------------------------------------- Navidrome (Subsonic)

ND_USER, ND_PASS = "nduser", "ndpassword55"
ND_FOLDERS = [{"id": 1, "name": "Hörspiele"}, {"id": 2, "name": "Musik"}]


def _nd_album(aid, folder, name, artist, songs, year=2000, created="2026-01-01"):
    out = []
    for sid, title, disc, track in songs:
        data = f"{title}\n".encode() + b"x" * 200
        out.append({"id": sid, "title": title, "discNumber": disc, "track": track, "suffix": "mp3",
                    "size": len(data), "duration": 60, "isDir": False, "albumId": aid, "_data": data})
    return {"id": aid, "name": name, "artist": artist, "artistId": "ar" + "".join(ch for ch in artist if ch.isascii() and ch.isalnum())[:20],
            "songCount": len(out), "duration": 60 * len(out), "coverArt": f"al-{aid}_c0ffee", "year": year,
            "created": created, "_folder": folder, "song": out}


ND_ALBUMS = {a["id"]: a for a in [
    # bewusst ungeordnet: CD 2 vor CD 1, Titel 2 vor Titel 1
    _nd_album("N1hotzenplotzAAAAAAAAA", 1, "Der Räuber Hotzenplotz", "Otfried Preußler",
              [("s13", "Hotz CD2-1", 2, 1), ("s12", "Hotz CD1-2", 1, 2), ("s11", "Hotz CD1-1", 1, 1)],
              year=1969, created="2026-03-01"),
    _nd_album("N2kaputtBBBBBBBBBBBBBB", 1, "Kaputter Download", "Otfried Preußler",
              [("s21", "Gut", 1, 1), ("s22", "DLFAIL", 1, 2)], year=1970),
    *[_nd_album(f"N9fill{n:02d}CCCCCCCCCCCC", 1, f"Füll-Album {n:02d}", "Diverse", [(f"s9{n:02d}", f"F{n}", 1, 1)],
                year=2000 + n) for n in range(1, 6)],
    _nd_album("M1musikDDDDDDDDDDDDDDD", 2, "Ein Musikalbum", "Band", [("s31", "Lied", 1, 1)]),
]}
ND_SONGS = {s["id"]: s for a in ND_ALBUMS.values() for s in a["song"]}
for _a in ND_ALBUMS.values():
    for _s in _a["song"]:
        _s.update(album=_a["name"], artist=_a["artist"], coverArt=_a["coverArt"])
# Playlist bewusst in eigener Reihenfolge über mehrere Alben
ND_PLAYLISTS = {"PLmixEEEEEEEEEEEEEEEEE": {"id": "PLmixEEEEEEEEEEEEEEEEE", "name": "Gute Nacht", "owner": "nduser",
                                         "coverArt": "pl-PLmix_1", "songs": ["s903", "s11", "s31"]}}


def _nd_song(sid):
    return {k: v for k, v in ND_SONGS[sid].items() if not k.startswith("_")}


def _nd_pub(a, songs=False):
    d = {k: v for k, v in a.items() if not k.startswith("_") and k != "song"}
    if songs:
        d["song"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in a["song"]]
    return d


class NdHandler(Base):
    log = []

    def _ok(self, **payload):
        return self.send(200, {"subsonic-response": {"status": "ok", "version": "1.16.1", "type": "navidrome", **payload}})

    def _fail(self, code, message):
        return self.send(200, {"subsonic-response": {"status": "failed", "version": "1.16.1",
                                                     "error": {"code": code, "message": message}}})

    def do_POST(self):
        if self.diag():
            return
        self.send(405, "mock ND: read only", extra={"write_attempt": True})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def do_GET(self):
        if self.diag():
            return
        import hashlib
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if "p" in q:
            return self._fail(41, "Klartext-Passwort nicht erlaubt (mock)")
        if q.get("u") != ND_USER or q.get("t") != hashlib.md5((ND_PASS + q.get("s", "")).encode()).hexdigest():
            return self._fail(40, "Wrong username or password")
        m = u.path.rsplit("/", 1)[-1].removesuffix(".view")
        fid = int(q["musicFolderId"]) if q.get("musicFolderId") else None
        albums = [a for a in ND_ALBUMS.values() if fid is None or a["_folder"] == fid]
        if m == "ping":
            return self._ok()
        if m == "getMusicFolders":
            return self._ok(musicFolders={"musicFolder": ND_FOLDERS})
        if m == "getAlbumList2":
            t = q.get("type")
            if t == "newest":
                albums.sort(key=lambda a: a["created"], reverse=True)
            elif t == "alphabeticalByArtist":
                albums.sort(key=lambda a: (a["artist"].lower(), a["name"].lower()))
            else:
                albums.sort(key=lambda a: a["name"].lower())
            off, size = int(q.get("offset", 0)), int(q.get("size", 10))
            return self._ok(albumList2={"album": [_nd_pub(a) for a in albums[off:off + size]]})
        if m == "search3":
            qq = q.get("query", "").lower().strip('"')
            res = [_nd_pub(a) for a in albums if qq in a["name"].lower() or qq in a["artist"].lower()]
            songs = [_nd_song(s["id"]) for a in albums for s in a["song"] if qq in s["title"].lower()]
            return self._ok(searchResult3={"album": res[:int(q.get("albumCount", 20))],
                                           "song": songs[:int(q.get("songCount", 20))]})
        if m == "getPlaylists":
            return self._ok(playlists={"playlist": [{k: v for k, v in p.items() if k != "songs"} | {
                "songCount": len(p["songs"]), "duration": 60 * len(p["songs"])} for p in ND_PLAYLISTS.values()]})
        if m == "getPlaylist":
            p = ND_PLAYLISTS.get(q.get("id"))
            if not p:
                return self._fail(70, "Playlist not found")
            return self._ok(playlist={"id": p["id"], "name": p["name"], "songCount": len(p["songs"]),
                                      "entry": [_nd_song(s) for s in p["songs"]]})
        if m == "getSong":
            return self._ok(song=_nd_song(q["id"])) if q.get("id") in ND_SONGS else self._fail(70, "Song not found")
        if m == "getArtists":
            arts = {}
            for a in albums:
                arts.setdefault(a["artistId"], {"id": a["artistId"], "name": a["artist"], "albumCount": 0,
                                                "coverArt": f"ar-{a['artistId']}_1"})["albumCount"] += 1
            return self._ok(artists={"index": [{"name": "#", "artist": list(arts.values())}]})
        if m == "getArtist":
            al = [_nd_pub(a) for a in ND_ALBUMS.values() if a["artistId"] == q.get("id")]
            if not al:
                return self._fail(70, "Artist not found")
            return self._ok(artist={"id": q["id"], "name": al[0]["artist"], "album": al[::-1]})
        if m == "getAlbum":
            a = ND_ALBUMS.get(q.get("id"))
            return self._ok(album=_nd_pub(a, songs=True)) if a else self._fail(70, "Album not found")
        if m == "getCoverArt":
            return self.send(200, COVER_PNG, ctype="image/png")
        if m == "download":
            sng = ND_SONGS.get(q.get("id"))
            if not sng:
                return self._fail(70, "Song not found")
            if "DLFAIL" in sng["title"]:
                return self._fail(0, "download failed (mock)")
            return self.send(200, sng["_data"], ctype="audio/mpeg")
        return self._fail(0, f"unknown method {m}")


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="Verzeichnis für die TeddyCloud-Bibliothek")
    ap.add_argument("--abs-port", type=int, default=13378)
    ap.add_argument("--tc-port", type=int, default=8088)
    ap.add_argument("--nd-port", type=int, default=4533)
    ap.add_argument("--encode-delay", type=float, default=0.5)
    ap.add_argument("--upload-delay", type=float, default=0.0)
    ap.add_argument("--tc-auth", default=None, help="user:pass für Basic Auth")
    ap.add_argument("--clean", action="store_true", help="Bibliothek vorher leeren")
    ap.add_argument("--abs-m4b", default=None, help="echte m4b-Datei als ABS-Hörbuch M4B_ID (Bibliothek Podcasts)")
    ap.add_argument("--abs-m4b-chapters", default="[]", help="Kapitel dazu als JSON [[start, ende], ...]")
    ap.add_argument("--keep", default=None, help="Quelldateien jeder Umwandlung hierhin kopieren (für Tests)")
    a = ap.parse_args()
    if a.abs_m4b:
        chs = json.loads(a.abs_m4b_chapters)
        it = _item(M4B_ID, "lib_pod", "Kapitelbuch", "Testautor", nfiles=1, duration=chs[-1][1] if chs else 60.0)
        af = it["media"]["audioFiles"][0]
        af.update(_path=a.abs_m4b, duration=it["media"]["duration"])
        af["metadata"].update(filename="kapitelbuch.m4b", ext=".m4b", size=os.path.getsize(a.abs_m4b))
        it["media"]["chapters"] = [{"id": i, "start": st, "end": en, "title": f"Kapitel {i + 1}"} for i, (st, en) in enumerate(chs)]
        ITEMS[M4B_ID] = it
    TcHandler.keep = a.keep
    root = os.path.realpath(a.data)
    if a.clean and os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    TcHandler.root = root
    TcHandler.encode_delay = a.encode_delay
    TcHandler.auth = a.tc_auth
    TcHandler.upload_delay = a.upload_delay
    serve(AbsHandler, a.abs_port)
    serve(TcHandler, a.tc_port)
    serve(NdHandler, a.nd_port)
    print(f"ABS-Mock :{a.abs_port}  TC-Mock :{a.tc_port}  ND-Mock :{a.nd_port}  Bibliothek {root}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
