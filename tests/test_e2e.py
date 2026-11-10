#!/usr/bin/env python3
"""End-to-End-Test gegen die Mock-Server. Start: venv/bin/python tests/test_e2e.py"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
ABS_P, TC_P, APP_P, ND_P = 23379, 28089, 28086, 24534  # andere Ports als die Demo
ND_PW = "ndpassword55"
UI_PW = "uipass-Geheim77"
WEBHOOK_SECRET = "WEBHOOKSECRET123"
TC_PW = "tcpassword987"
BASE = "teddybridge"

A = "aaaa1111-0000-4000-8000-000000000001"  # Serie "Die drei ??? Kids", 3 Dateien
B = "bbbb2222-0000-4000-8000-000000000002"  # Serie "Bibi Blocksberg", 4 Dateien, eine exclude, umgekehrt
C = "cccc3333-0000-4000-8000-000000000003"  # ohne Serie, Autor "Test", 100 Dateien
D = "dddd4444-0000-4000-8000-000000000004"  # Umwandeln schlägt fehl
E = "eeee5555-0000-4000-8000-000000000005"  # sehr langer Titel
F1 = "f0010000-0000-4000-8000-000000000101"  # Füllmaterial, Autor "Autor"
F2 = "f0020000-0000-4000-8000-000000000102"  # Füllmaterial, Uploads kommen leer an

fails = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


def wait_http(url):
    for _ in range(50):
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit(f"{url} nicht erreichbar")


def wait_jobs(c, n):
    for _ in range(300):
        jobs = c.get("/api/jobs").json()
        if len(jobs) >= n and all(j["state"] in ("done", "skipped", "error") for j in jobs):
            return jobs
        time.sleep(0.2)
    raise SystemExit("Warteschlange wird nicht fertig: " + str(c.get("/api/jobs").json()))


def tree(root):
    out = []
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            out.append(os.path.relpath(os.path.join(dp, n), root))
    return sorted(out)


def taf_lines(path):
    return [l.split(" | ")[1] for l in open(path).read().splitlines()[1:]]


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20


ABS_CH = [3.0, 2.0, 4.0, 1.0, 5.0, 2.0]  # Kapitel des m4b-Hörbuchs im ABS-Mock
M4B_ID = "abba4b00-0000-4000-8000-00000000004b"


def make_real_audio(base, tmp):
    """Echte Audiodateien (mit ffmpeg erzeugt) für Zusammenfügen und Kapitel-Schnitt. -> {Titel: Ordner}"""
    import test_audio as ta
    gen = os.path.join(tmp, "gen")
    os.makedirs(gen, exist_ok=True)
    v = []
    for i, secs in enumerate((0.3, 0.6, 1.2)):
        p = os.path.join(gen, f"v{i}.mp3")
        ta.tone(p, secs, freq=300 + 200 * i, extra=("-c:a", "libmp3lame", "-b:a", "96k", "-metadata", f"title=V{i}"))
        v.append(p)
    low = os.path.join(gen, "22k.mp3")
    ta.tone(low, 0.2, rate=22050, extra=("-c:a", "libmp3lame", "-b:a", "48k"))
    dirs = {t: os.path.join(base, t) for t in ("Lang", "Gemischt", "Nicht MP3", "Buch", "Buch Gross", "ENCODE-CRASH", "ENCODE-CRASH-ONCE", "Halb", "Alt",
                                               "Einzeldatei")}
    for d in dirs.values():
        os.makedirs(d)
    for i in range(1, 131):
        shutil.copy(v[(i * 7) % 3], os.path.join(dirs["Lang"], f"{i:03d}.mp3"))
    for d in ("ENCODE-CRASH", "ENCODE-CRASH-ONCE", "Halb", "Alt"):
        for i in (1, 2):
            shutil.copy(v[0], os.path.join(dirs[d], f"{i:03d}.mp3"))
    for i in range(1, 101):
        shutil.copy(low if i == 50 else v[1], os.path.join(dirs["Gemischt"], f"{i:03d}.mp3"))
    for i in range(1, 100):
        shutil.copy(v[1], os.path.join(dirs["Nicht MP3"], f"{i:03d}.mp3"))
    m4a = os.path.join(gen, "kurz.m4a")
    ta.tone(m4a, 0.3, extra=("-c:a", "aac", "-b:a", "64k"))
    for n in ("050a.m4a", "050b.m4a"):  # echte, kurze m4a mitten zwischen MP3 -> werden zusammengefügt
        shutil.copy(m4a, os.path.join(dirs["Nicht MP3"], n))
    ta.m4b(os.path.join(dirs["Buch"], "buch.m4b"), [3.0, 1.0, 4.0, 2.0, 5.0, 1.5, 2.5, 3.0, 1.0, 2.0, 4.0, 3.0], gen)
    ta.m4b(os.path.join(dirs["Buch Gross"], "buch.m4b"), [0.6 + (i % 5) * 0.3 for i in range(130)], gen)
    # eine Datei ohne Kapitel, 400 s, Pause (2 s Stille) bei 170–172 s
    ta.ff("-f", "lavfi", "-i", "sine=f=440:d=170", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=2",
          "-f", "lavfi", "-i", "sine=f=440:d=228", "-filter_complex",
          "[0]aformat=sample_rates=44100:channel_layouts=stereo[a];[2]aformat=sample_rates=44100:channel_layouts=stereo[c];"
          "[a][1][c]concat=n=3:v=0:a=1", "-c:a", "libmp3lame", "-b:a", "32k", os.path.join(dirs["Einzeldatei"], "alles.mp3"))
    return dirs


def make_nas(tmp):
    """Nachgebautes NAS (wie per NFS/SMB eingehängt). Erste Zeile jeder Datei = Kennung für die TAF-Prüfung."""
    nas = os.path.join(tmp, "nas")

    def f(rel, content=None):
        path = os.path.join(nas, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content if content is not None else (os.path.basename(rel) + "\n").encode() + b"x" * 3000)
    k = "hsp/Die drei ??? Kids"
    for n in ("10 Ende.mp3", "02 Mitte.mp3", "01 Anfang.mp3"):
        f(f"{k}/001 - Panik im Paradies/{n}")
    f(f"{k}/001 - Panik im Paradies/cover.jpg", PNG)
    f(f"{k}/001 - Panik im Paradies/info.txt", b"kein Audio")
    os.symlink("/etc/hostname", os.path.join(nas, k, "001 - Panik im Paradies", "05 boese.mp3"))
    os.mkfifo(os.path.join(nas, k, "001 - Panik im Paradies", "06 fifo.mp3"))
    f(f"{k}/002 - Mit CDs/CD2/01 c2.mp3")
    f(f"{k}/002 - Mit CDs/CD1/02 c1b.mp3")
    f(f"{k}/002 - Mit CDs/CD1/01 c1a.mp3")
    f(f"{k}/002 - Mit CDs/CD10/01 c10.mp3")
    f(f"{k}/002 - Mit CDs/CD1/folder.png", PNG)
    f("hsp/Bibi/Folge 1/x.m4a")
    f("hsp/Bibi/Folge 1/Bonus/extra.mp3")
    f("hsp/.versteckt/y.mp3")
    f("hsp/@eaDir/z.mp3")
    os.makedirs(os.path.join(nas, "hsp/leer"))
    os.symlink("/etc", os.path.join(nas, "hsp", "Ausbruch"))
    for i in range(1, 121):
        f(f"hsp/Gross/{i:03d}.mp3")
    f("musik/Album/01 track.flac")
    return nas


def main():
    tmp = tempfile.mkdtemp(prefix="tb-test-")
    lib = os.path.join(tmp, "tclib")
    K = os.path.join(lib, BASE, "Kinder")
    procs = []
    env = dict(os.environ,
               ABS_URL=f"http://127.0.0.1:{ABS_P}", ABS_TOKEN="mocktoken",
               TC_URL=f"http://127.0.0.1:{TC_P}", TC_AUTH=f"tcuser:{TC_PW}", TC_BASE=BASE,
               DISCORD_WEBHOOK=f"http://127.0.0.1:{TC_P}/_mock/discord/{WEBHOOK_SECRET}",
               LISTEN=f"127.0.0.1:{APP_P}",
               ND_URL=f"http://127.0.0.1:{ND_P}", ND_USER="nduser", ND_PASS=ND_PW,
               UI_USER="admin", UI_PASS=UI_PW, TRUSTED_PROXIES="127.0.0.1",
               TB_RETRY_SETTLE="0.3", TB_RETRY_WAIT_MAX="20", TC_BASE_EXTRA="altbasis")
    ui_hash = subprocess.run([PY, "-m", "app.pwhash"], cwd=ROOT, input=UI_PW + "\n", capture_output=True,
                             text=True, check=True).stdout.strip().split("=", 1)[1]
    env["UI_PASS_HASH"] = ui_hash
    del env["UI_PASS"]
    nas = make_nas(tmp)
    env["FS_ROOTS"] = f"Hörspiele={nas}/hsp; Musik={nas}/musik ;Offline=/nicht/eingehaengt"
    keep = os.path.join(tmp, "keep")
    mock_extra = ["--keep", keep]
    if shutil.which("ffmpeg"):
        import test_audio as ta
        os.makedirs(os.path.join(tmp, "m4bgen"))
        absbook = os.path.join(tmp, "absbuch.m4b")
        ta.m4b(absbook, ABS_CH, os.path.join(tmp, "m4bgen"))
        st = [sum(ABS_CH[:i]) for i in range(len(ABS_CH))]
        mock_extra += ["--abs-m4b", absbook, "--abs-m4b-chapters", json.dumps([[a, a + d] for a, d in zip(st, ABS_CH)])]
    applog = open(os.path.join(tmp, "app.log"), "w+")
    try:
        procs.append(subprocess.Popen([PY, "mock/mock_servers.py", "--data", lib, "--clean",
                                       "--abs-port", str(ABS_P), "--tc-port", str(TC_P), "--nd-port", str(ND_P),
                                       "--encode-delay", "0.2", "--upload-delay", "0.05",
                                       "--tc-auth", f"tcuser:{TC_PW}", *mock_extra], cwd=ROOT))
        procs.append(subprocess.Popen([PY, "-m", "app"], cwd=ROOT, env=env, stdout=applog, stderr=subprocess.STDOUT))
        wait_http(f"http://127.0.0.1:{TC_P}/_mock/log")
        wait_http(f"http://127.0.0.1:{APP_P}/api/config")
        c = httpx.Client(base_url=f"http://127.0.0.1:{APP_P}", timeout=30, auth=("admin", UI_PW),
                         headers={"Accept-Language": "de-CH,de;q=0.9"})  # Meldungen deutsch (Prüfungen unten)

        print("Passwort-Hash")
        sys.path.insert(0, ROOT)
        from app import pwhash, config as appconfig
        check(ui_hash.startswith("scrypt:32768:8:1:") and "$" not in ui_hash and UI_PW not in ui_hash, "Hash-Format ohne $ (systemd)")
        check(pwhash.verify(UI_PW, ui_hash) and not pwhash.verify(UI_PW + "x", ui_hash), "Hash prüft richtig/falsch")
        check(pwhash.make(UI_PW) != pwhash.make(UI_PW), "Hash mit zufälligem Salz")
        short = subprocess.run([PY, "-m", "app.pwhash"], cwd=ROOT, input="kurz\n", capture_output=True, text=True)
        check(short.returncode != 0 and "UI_PASS_HASH" not in short.stdout, "Hash-Tool lehnt kurzes Passwort ab")

        def cfg_err(**kw):
            base = {k: v for k, v in env.items() if not k.startswith("UI_")}
            old = dict(os.environ)
            os.environ.clear(); os.environ.update(base, **kw)
            try:
                return appconfig.load()
            except SystemExit as e:
                return str(e)
            finally:
                os.environ.clear(); os.environ.update(old)
        e1 = cfg_err(UI_USER="admin", UI_PASS_HASH="scrypt:1:2:3:geheimwert:xy")
        check(isinstance(e1, str) and ("Format" in e1 or "Parameter" in e1), "kaputter Hash: Startabbruch")
        check("geheimwert" not in str(e1), "Fehlermeldung nennt den Hash-Wert nicht")
        check(isinstance(cfg_err(UI_PASS_HASH=ui_hash), str), "Hash ohne Benutzer: Startabbruch")
        k = cfg_err(UI_USER="admin", UI_PASS="klartext-pw1")
        check(not isinstance(k, str) and k.ui_auth and k.ui_pass and not k.ui_pass_hash, "Klartext UI_PASS weiter möglich")
        h = cfg_err(UI_USER="admin", UI_PASS="klartext-pw1", UI_PASS_HASH=ui_hash)
        check(not isinstance(h, str) and h.ui_pass_hash and not h.ui_pass, "Hash hat Vorrang vor Klartext")
        check(isinstance(cfg_err(UI_USER="admin", UI_PASS="kurz"), str), "zu kurzes Klartext-Passwort: Startabbruch")

        print("Anmeldung")
        u = f"http://127.0.0.1:{APP_P}"
        r = httpx.get(u + "/")
        check(r.status_code == 303 and r.headers["location"] == "/login" and "www-authenticate" not in r.headers,
              "Seite ohne Anmeldung -> /login, kein Browser-Popup")
        lp = httpx.get(u + "/login")
        check(lp.status_code == 200 and 'autocomplete="current-password"' in lp.text and 'autocomplete="username"' in lp.text,
              "Login-Formular für Passwortmanager")
        check(all(h.headers.get("x-frame-options") == "DENY" and "frame-ancestors 'none'" in h.headers.get("content-security-policy", "")
                  and h.headers.get("x-content-type-options") == "nosniff" for h in (r, lp, httpx.get(u + "/api/libraries"))),
              "Security-Header (CSP, X-Frame-Options, nosniff) auch auf Weiterleitung und 401")
        check(httpx.get(u + "/api/libraries").status_code == 401 and httpx.get(f"{u}/api/cover/{A}").status_code == 401,
              "API und Cover geschützt (401, kein Redirect)")
        check(httpx.post(u + "/api/jobs", json={"items": [{"id": A}]}).status_code == 401, "Auftrag ohne Anmeldung abgelehnt")
        r = httpx.post(u + "/login", data={"username": "admin", "password": "falsch"})
        check(r.status_code == 303 and r.headers["location"] == "/login?fehler=1" and "set-cookie" not in r.headers,
              "falsches Passwort: kein Cookie")
        r = httpx.post(u + "/login", data={"username": "Admin", "password": UI_PW})
        check("set-cookie" not in r.headers, "falscher Benutzer: kein Cookie")
        check(httpx.get(u + "/api/config", headers={"Authorization": "Basic %%%"}).status_code == 401, "kaputter Basic-Header abgelehnt")
        check(httpx.get(u + "/api/config", headers={"Cookie": "tb_session=erfunden"}).status_code == 401, "erfundenes Cookie abgelehnt")
        b = httpx.Client(base_url=u, timeout=30, headers={"Accept-Language": "de"})
        r = b.post("/login", data={"username": "admin", "password": UI_PW})
        sc = r.headers.get("set-cookie", "")
        check(r.status_code == 303 and r.headers["location"] == "/" and "HttpOnly" in sc and "SameSite=strict" in sc.replace("Strict", "strict"),
              "Formular-Login setzt HttpOnly/SameSite-Cookie")
        check(b.get("/").status_code == 200 and b.get("/api/config").json()["auth"] is True, "mit Cookie angemeldet")
        check(b.get("/login").status_code == 303, "Login-Seite leitet Angemeldete weiter")
        tok = b.cookies.get("tb_session")
        r = b.post("/logout")
        check(r.status_code == 303 and r.headers["location"] == "/login", "Abmelden leitet zum Login")
        check(httpx.get(u + "/api/config", headers={"Cookie": f"tb_session={tok}"}).status_code == 401, "Sitzung nach Abmelden ungültig")
        check(c.get("/").status_code == 200, "Basic Auth für Skripte weiterhin möglich")

        print("Reverse Proxy")
        check("secure" not in sc.lower(), "über HTTP: Cookie ohne Secure")
        r = httpx.post(u + "/login", data={"username": "admin", "password": UI_PW},
                       headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.10"})
        check("secure" in r.headers.get("set-cookie", "").lower(), "über Proxy mit HTTPS: Cookie mit Secure")
        httpx.post(u + "/login", data={"username": "admin", "password": "falsch"}, headers={"X-Forwarded-For": "192.0.2.77"})
        applog.flush(); applog.seek(0)
        check("192.0.2.77" in applog.read(), "Fehlversuch loggt echte Client-IP vom Proxy")
        check(isinstance(cfg_err(TRUSTED_PROXIES="10.0.0.0/8"), str), "TRUSTED_PROXIES nur einzelne IPs")
        check(all(isinstance(cfg_err(TC_BASE_EXTRA=x), str) for x in ("../etc", "alt/../x", "a/_src_x")),
              "TC_BASE_EXTRA: kein '..', kein _src_")
        ok_cfg = cfg_err(TC_BASE_EXTRA=" alt/ , noch/einer ,")
        check(not isinstance(ok_cfg, str) and ok_cfg.tc_scan_bases == (BASE, "alt", "noch/einer"), "TC_BASE_EXTRA: Liste bereinigt")

        print("Quellen: mindestens eine, ABS optional")
        nosrc = dict(ABS_URL="", ABS_TOKEN="", ND_URL="", ND_USER="", ND_PASS="", FS_ROOTS="")
        e = cfg_err(**nosrc)
        check(isinstance(e, str) and "mindestens eine" in e, "keine Quelle: Startabbruch")
        check(isinstance(cfg_err(**dict(nosrc, ABS_URL="http://x")), str), "nur ABS_URL ohne Token: Startabbruch")
        check(isinstance(cfg_err(**dict(nosrc, ND_URL="http://x", ND_USER="u")), str), "Navidrome halb: Startabbruch")
        for label, kw in (("nur Navidrome", dict(ND_URL=env["ND_URL"], ND_USER="nduser", ND_PASS=ND_PW)),
                          ("nur Dateiquelle", dict(FS_ROOTS=env["FS_ROOTS"])),
                          ("nur ABS", dict(ABS_URL=env["ABS_URL"], ABS_TOKEN="mocktoken"))):
            sc = cfg_err(**dict(nosrc, **kw))
            check(not isinstance(sc, str), f"{label}: startet")
        sc = cfg_err(**dict(nosrc, ND_URL=env["ND_URL"], ND_USER="nduser", ND_PASS=ND_PW))
        check(not sc.abs_enabled and sc.nd_enabled, "ohne ABS: abs_enabled False")
        # App ohne ABS (nur Navidrome + Dateiquelle): Quellenliste, ABS-Endpunkte 404, ABS-Auftrag 400
        probe = r'''
import json
from fastapi.testclient import TestClient
from app import main
c = TestClient(main.app); c.auth = ("admin", main.CFG.ui_pass)
out = {"sources": c.get("/api/config").json()["sources"], "libs": c.get("/api/libraries").status_code,
       "cover": c.get("/api/cover/abcd1234").status_code,
       "job": c.post("/api/jobs", json={"items": [{"id": "abcd1234", "source": "abs", "title": "x"}]}).status_code}
print("RESULT" + json.dumps(out))
'''
        penv = dict(env, ABS_URL="", ABS_TOKEN="", UI_PASS=UI_PW, UI_PASS_HASH="", LISTEN="127.0.0.1:1")
        r = subprocess.run([PY, "-c", probe], cwd=ROOT, env=penv, capture_output=True, text=True, timeout=60)
        line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
        res = json.loads(line[6:]) if line else {}
        check(res.get("sources") == ["nd", "fs"], f"ohne ABS: Quellen nur nd+fs ({res.get('sources')})")
        check(res.get("libs") == 404 and res.get("cover") == 404, "ohne ABS: ABS-Endpunkte 404")
        check(res.get("job") == 400, "ohne ABS: ABS-Auftrag abgelehnt (400)")
        check("mocktoken" not in r.stdout + r.stderr, "ohne ABS: kein Token in Ausgabe")

        print("Folgennummern")
        sys.path.insert(0, ROOT)
        from app.worker import numbered, series_seq
        check(numbered("Das Fohlen", "1") == "001 - Das Fohlen", "Nummer vorangestellt")
        check(numbered("001 - Der Super-Papagei", "1") == "001 - Der Super-Papagei", "vorhandene Nummer nicht doppelt")
        check(numbered("Folge 12: Xy", "12") == "Folge 12: Xy" and numbered("Der 3. Mann", "1") == "001 - Der 3. Mann", "Folge-Präfix / Zahl im Titel")
        check(numbered("Spezial", "2.5") == "2.5 - Spezial" and numbered("Ohne", None) == "Ohne", "Dezimal / ohne Serie")
        check(series_seq({"seriesName": "Bibi und Tina 1-50 #7"}) == "7" and series_seq({"series": [{"name": "X", "sequence": "3"}]}) == "3"
              and series_seq({"seriesName": "Bibi und Tina 1-50"}) is None, "Folgennummer aus ABS-Metadaten")
        print("Stöbern")
        check(c.get("/api/config").json()["tcBase"] == BASE, "Basisordner aus TC_BASE")
        libs = c.get("/api/libraries").json()
        check([l["id"] for l in libs] == ["lib_kids"], "nur Buch-Bibliotheken, Podcast ausgeblendet")
        p0 = c.get("/api/libraries/lib_kids/items?page=0&limit=20").json()
        p1 = c.get("/api/libraries/lib_kids/items?page=1&limit=20").json()
        check(p0["total"] == 35 and len(p0["items"]) == 20 and len(p1["items"]) == 15, "Seitenweise Laden 20+15 von 35")
        check(not {i["id"] for i in p0["items"]} & {i["id"] for i in p1["items"]}, "Seiten überschneiden sich nicht")
        it = next(i for i in p0["items"] + p1["items"] if i["id"] == A)
        check(it["seq"] == "1", "Kachel hat Folgennummer")
        check(it["title"].startswith("Die drei") and it["author"] == "Ulf Blanck" and it["duration"] == 180
              and it["series"] == "Die drei ??? Kids #1", "Kachel-Daten (minified)")
        s = c.get("/api/libraries/lib_kids/search", params={"q": "bibi"}).json()
        check([i["id"] for i in s["items"]] == [B], "Suche findet Bibi")
        sers = c.get("/api/libraries/lib_kids/series").json()
        check([x["name"] for x in sers] == ["Benjamin Blümchen", "Bibi Blocksberg", "Bobo", "Die drei ??? Kids"]
              and next(x for x in sers if x["name"] == "Bobo")["count"] == 5, "Serien gelistet")
        bobo = next(x for x in sers if x["name"] == "Bobo")
        si = c.get(f"/api/libraries/lib_kids/series/{bobo['id']}/items?limit=3&page=0").json()
        si2 = c.get(f"/api/libraries/lib_kids/series/{bobo['id']}/items?limit=3&page=1").json()
        check(si["total"] == 5 and [i["title"] for i in si["items"] + si2["items"]] ==
              [f"Füllmaterial {n:02d}" for n in (5, 4, 3, 2, 1)], "Serien-Folgen nach Reihenfolge, seitenweise")
        check([i["seq"] for i in si["items"]] == ["1", "2", "3"] and si["items"][0]["series"] == "Bobo #1",
              "Serien-Folgen: Folgennummer, auch wenn ABS series als Objekt liefert")
        cols = c.get("/api/libraries/lib_kids/collections").json()
        check([x["name"] for x in cols] == ["Einschlafen", "Test/Fehler"], "Sammlungen gelistet")
        ci = c.get("/api/libraries/lib_kids/collections/col_1").json()
        check([i["id"] for i in ci["items"]] == [A, B] and ci["items"][1]["files"] == 3
              and ci["items"][0]["author"] == "Ulf Blanck", "Sammlungs-Items (expanded, ohne exclude)")
        r = c.get(f"/api/cover/{A}")
        check(r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG", "Cover über Backend")
        check(c.get("/api/cover/..%2F..%2Fetc").status_code in (404, 422, 502), "Cover-ID wird validiert")
        check(c.get("/api/present").json() == {"base": BASE, "ids": []}, "Status bei leerer Bibliothek")
        check(c.post("/api/jobs", json={"subfolder": "../x", "items": [{"id": A}]}).status_code == 502, "'..' im Unterordner abgelehnt")
        check(c.post("/api/jobs", json={"subfolder": "x/_src_1", "items": [{"id": A}]}).status_code == 502, "_src_ als Unterordner abgelehnt")

        print("Übertragen mit automatischer Ablage")
        # Rest eines abgebrochenen Laufs für E und eine fremde Datei
        os.makedirs(os.path.join(K, "Benjamin Blümchen", "_src_eeee5555"))
        open(os.path.join(K, "Benjamin Blümchen", "_src_eeee5555", "001.mp3"), "w").write("alt")
        open(os.path.join(K, "fremd.taf"), "w").write("x")
        # TeddyCloud legt die Dateien von B beim ersten Versuch leer an -> Upload wird wiederholt
        httpx.post(f"http://127.0.0.1:{TC_P}/_mock/empty-uploads", json={"match": "_src_bbbb2222", "mode": "once"})
        r = c.post("/api/jobs", json={"items": [{"id": A}, {"id": B}, {"id": C}, {"id": D}, {"id": E}]})
        check(r.status_code == 200, "Stapel angenommen")
        seen = set()
        for _ in range(300):
            for j in c.get("/api/jobs").json():
                seen.add(j["state"])
            if {"uploading", "encoding"} <= seen:
                break
            time.sleep(0.02)
        # Downloads sind im Mock zu kurz, um sie zuverlässig beim Polling zu sehen
        check({"uploading", "encoding"} <= seen, f"Fortschrittszustände sichtbar {sorted(seen)}")
        jobs = {j["item_id"]: j for j in wait_jobs(c, 5)}
        check(jobs[A]["state"] == "done" and jobs[A]["folder"] == f"{BASE}/Kinder/Die drei --- Kids"
              and jobs[A]["taf"] == "001 - Die drei --- Kids- Folge 1 [aaaa1111].taf", "A: Ordner nach Serie, Name bereinigt")
        check(jobs[A]["check"] == "3 Kapitel, 3:00" and jobs[A]["warning"] is None, "A: TAF-Prüfung 3 Kapitel")
        check(taf_lines(os.path.join(K, "Die drei --- Kids", jobs[A]["taf"])) == [f"AUDIO {A} {i}" for i in (1, 2, 3)],
              "A: Download nach Verbindungsabbruch wiederholt, Inhalt vollständig")
        check(jobs[B]["state"] == "done" and jobs[B]["m"] == 3 and jobs[B]["folder"] == f"{BASE}/Kinder/Bibi Blocksberg", "B: 3 von 4 Dateien, Serienordner")
        check(jobs[C]["state"] == "error" and "99" in jobs[C]["error"], "C: >99 ohne Aufteilen -> Fehler, nichts übertragen")
        check(jobs[D]["state"] == "error" and "Umwandeln" in jobs[D]["error"] and jobs[D]["folder"] == f"{BASE}/Kinder/Test",
              "D: Umwandel-Fehler, Ordner nach Autor")
        check(jobs[E]["state"] == "done" and len(jobs[E]["taf"]) == 120 + len(" [eeee5555].taf"), "E: Titel gekürzt, Altlast _src_ aufgeräumt")
        files = tree(lib)
        check(not any("_src_" in f for f in files), "keine _src_-Ordner übrig (auch nach Fehler)")
        check(f"{BASE}/Kinder/fremd.taf" in files, "fremde Datei unangetastet")
        check(taf_lines(os.path.join(K, "Bibi Blocksberg", "001 - Bibi Blocksberg - Hexen- -Spezial- [bbbb2222].taf")) ==
              [f"AUDIO {B} {i}" for i in (1, 3, 4)], "B: Reihenfolge nach index, exclude übersprungen")
        check(c.get("/api/present").json()["ids"] == ["aaaa1111", "bbbb2222", "eeee5555"], "Status 'schon in TeddyCloud' über den ganzen Baum")

        print("Aufteilen (>99)")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": C, "split": True}]})
        jc = wait_jobs(c, 1)[0]
        check(jc["state"] == "done" and jc["parts"] == 2 and jc["m"] == 100, "C: in 2 Teile aufgeteilt")
        check(jc["check"] == "50 Kapitel, 50:00 | 50 Kapitel, 50:00" and jc["warning"] is None, "C: Prüfung beider Teile")
        p1 = os.path.join(K, "Test", "Zu viele Dateien - Teil 1 von 2 [cccc3333-1].taf")
        p2 = os.path.join(K, "Test", "Zu viele Dateien - Teil 2 von 2 [cccc3333-2].taf")
        check(os.path.exists(p1) and os.path.exists(p2), "Teil-TAFs mit [id8-k] angelegt")
        check(os.path.exists(p1) and taf_lines(p1) == [f"AUDIO {C} {i}" for i in range(1, 51)]
              and taf_lines(p2) == [f"AUDIO {C} {i}" for i in range(51, 101)], "Teile 50/50 in richtiger Reihenfolge")
        # Teil 2 fehlt -> nur Teil 2 neu
        os.remove(p2)
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": C, "split": True}]})
        jc = wait_jobs(c, 1)[0]
        check(jc["state"] == "done" and jc["parts"] == 1 and jc["m"] == 50 and os.path.exists(p2), "fehlender Teil wird nachgeholt, vorhandener nicht doppelt")

        print("Teile nach Dauer")
        r = c.post("/api/jobs", json={"items": [{"id": C, "parts_min": 60, "split": True}]})
        check(r.status_code == 422, "Teile nach Dauer + Aufteilen: abgelehnt")
        r = c.post("/api/jobs", json={"bundle": "X", "items": [{"id": C, "parts_min": 60}, {"id": A}]})
        check(r.status_code == 422, "Teile nach Dauer in Sammel-TAF: abgelehnt")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": C, "parts_min": 60}]})
        jc = wait_jobs(c, 1)[0]
        check(jc["state"] == "skipped" and "2 Teile" in (jc["note"] or ""), "100 min à 60: gleiche 2 Teile schon da -> übersprungen")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": C, "parts_min": 30}]})
        jc = wait_jobs(c, 1)[0]
        check(jc["state"] == "error" and "2 Teilen" in jc["error"] and "3" in jc["error"], "andere Teilzahl: nicht gemischt, Fehler")
        os.remove(p1), os.remove(p2)
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": C, "parts_min": 30}]})
        jc = wait_jobs(c, 1)[0]
        q = [os.path.join(K, "Test", f"Zu viele Dateien - Teil {k} von 3 [cccc3333-{k}].taf") for k in (1, 2, 3)]
        check(jc["state"] == "done" and jc["parts"] == 3 and all(os.path.exists(x) for x in q), "100 min à 30: 3 Teil-TAFs")
        check(os.path.exists(q[2]) and [len(taf_lines(x)) for x in q] == [33, 34, 33]
              and sum((taf_lines(x) for x in q), []) == [f"AUDIO {C} {i}" for i in range(1, 101)], "Teile lückenlos in Reihenfolge")
        check("3 Teile" in (jc["note"] or ""), "Hinweis in der Warteschlange")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": A, "parts_min": 60}]})
        ja = wait_jobs(c, 1)[0]
        check(ja["state"] == "skipped" and "eine TAF" in (ja["note"] or ""), "schon als ganze TAF da: übersprungen mit Hinweis")
        for x in q:
            os.remove(x)
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": C, "split": True}]})
        jr = wait_jobs(c, 1)[0]
        check(jr["state"] == "done" and os.path.exists(p1) and os.path.exists(p2), "Zustand für folgende Tests wiederhergestellt")

        print("Tonie zuweisen")
        part1, part2 = f"{BASE}/Kinder/Test/Zu viele Dateien - Teil 1 von 2 [cccc3333-1].taf", f"{BASE}/Kinder/Test/Zu viele Dateien - Teil 2 von 2 [cccc3333-2].taf"
        check(jr["tafs"] == [part1, part2], f"Auftrag kennt seine TAFs (Teile in Reihenfolge) {jr['tafs']}")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": C, "split": True}]})
        js = wait_jobs(c, 1)[0]
        check(js["state"] == "skipped" and js["tafs"] == [part1, part2], "auch übersprungene Aufträge kennen ihre TAFs")
        c.post("/api/jobs/clear")
        ts = c.get("/api/tonies").json()["tonies"]
        check([x["ruid"] for x in ts] == ["e00403500aaa0001", "e00403500aaa0002"] and ts[0]["name"] == "Bobo Siebenschläfer – Folge 1"
              and ts[1]["name"] == "e00403500aaa0002", f"Figuren gelistet, System-Tag weggelassen, Name aus tonieInfo sonst ID {ts}")
        r = c.get("/api/tonies/e00403500aaa0001/picture")
        check(r.status_code == 200 and r.headers["content-type"] == "image/png", "Bild über das Backend (von TeddyCloud)")
        check(c.get("/api/tonies/e00403500aaa0002/picture").status_code == 404, "ohne Bild: 404")
        bobo, fremd = "e00403500aaa0001", "e00403500aaa0002"
        umlaut = f"{BASE}/Kinder/Test/Grüße & Co [aaaa9999].taf"
        open(os.path.join(lib, umlaut), "w").write("FAKE-TAF\n")
        os.makedirs(os.path.join(lib, BASE, "y" * 100, "z" * 100))
        lang = f"{BASE}/{'y' * 100}/{'z' * 100}/{'w' * 50}.taf"
        open(os.path.join(lib, lang), "w").write("FAKE-TAF\n")
        open(os.path.join(lib, "fremd.taf"), "w").write("FAKE-TAF\n")
        for body, code, what in (
                ({"ruid": bobo, "path": part1, "expect": "lib://anders"}, 409, "Belegung inzwischen geändert"),
                ({"ruid": bobo, "path": "fremd.taf", "expect": ""}, 422, "TAF ausserhalb des Basisordners"),
                ({"ruid": bobo, "path": f"{BASE}/../fremd.taf", "expect": ""}, 422, "'..' im Pfad"),
                ({"ruid": bobo, "path": f"{BASE}/Kinder/_src_x/a.taf", "expect": ""}, 422, "_src_-Ordner"),
                ({"ruid": bobo, "path": f"{BASE}/Kinder/fehlt.taf", "expect": ""}, 404, "TAF existiert nicht"),
                ({"ruid": bobo, "path": f"{BASE}/Kinder/Test", "expect": ""}, 422, "keine .taf"),
                ({"ruid": "e00403500aaa0009", "path": part1, "expect": ""}, 404, "unbekannte Figur"),
                ({"ruid": "0000000000000000", "path": part1, "expect": ""}, 404, "System-Tag"),
                ({"ruid": "E00403500AAA0001", "path": part1, "expect": ""}, 422, "ungültige ID"),
                ({"ruid": bobo, "path": lang, "expect": ""}, 422, "Pfad > 255 Bytes")):
            r = c.post("/api/tonies/assign", json=body)
            check(r.status_code == code, f"abgelehnt: {what} ({r.status_code})")
        mt = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/tags").json()
        check(mt["sets"] == [], "nach Ablehnungen nichts in TeddyCloud geändert")
        r = c.post("/api/tonies/assign", json={"ruid": bobo, "path": part1, "expect": ""})
        mt = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/tags").json()
        check(r.status_code == 200 and r.json()["old"] == "" and r.json()["new"] == "lib://" + part1, f"Bobo zugewiesen {r.text[:200]}")
        check(mt["tags"][bobo]["source"] == "lib://" + part1 and mt["tags"][bobo]["nocloud"] is True and mt["tags"][bobo]["live"] is False,
              "Quelle (mit Leerzeichen) gesetzt, keine Cloud an")
        check([x[1] for x in mt["sets"]] == ["source", "nocloud"], f"nur source + nocloud geändert {mt['sets']}")
        r = c.post("/api/tonies/assign", json={"ruid": fremd, "path": umlaut, "expect": "lib://fremd/eigenes Hörspiel.taf"})
        mt = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/tags").json()
        check(r.status_code == 200 and mt["tags"][fremd]["source"] == "lib://" + umlaut and mt["tags"][fremd]["live"] is False,
              "Umlaute und & richtig kodiert, live aus")
        check([x[1] for x in mt["sets"][2:]] == ["source", "live"] and r.json()["old"] == "lib://fremd/eigenes Hörspiel.taf",
              "nocloud schon an: nicht erneut gesetzt; alte Quelle zurückgemeldet")
        ts = {x["ruid"]: x for x in c.get("/api/tonies").json()["tonies"]}
        check(ts[bobo]["source"] == "lib://" + part1, "Liste zeigt neue Belegung")

        print("Tonie vormerken (Zuweisen nach Abschluss)")
        F7, F8 = "f0070000-0000-4000-8000-000000000107", "f0080000-0000-4000-8000-000000000108"
        mock = f"http://127.0.0.1:{TC_P}"
        httpx.post(f"{mock}/_mock/encode-delay", json={"secs": 3})
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": F7}, {"id": F8}]})
        jj = {j["item_id"]: j for j in c.get("/api/jobs").json()}
        j7, j8 = jj[F7]["id"], jj[F8]["id"]
        src_bobo = "lib://" + part1
        r = c.post(f"/api/jobs/{j7}/tonie", json={"ruid": bobo, "expect": "falsch"})
        check(r.status_code == 409, "vormerken mit veralteter Belegung: abgelehnt")
        r = c.post(f"/api/jobs/{j7}/tonie", json={"ruid": bobo, "expect": src_bobo})
        check(r.status_code == 200 and r.json()["tonie"]["name"].startswith("Bobo"), f"Bobo für F7 vorgemerkt {r.text[:150]}")
        r = c.post(f"/api/jobs/{j8}/tonie", json={"ruid": bobo, "expect": src_bobo})
        check(r.status_code == 409 and "vorgemerkt" in r.json()["detail"], "dieselbe Figur für zweiten Auftrag: abgelehnt")
        r = c.post(f"/api/jobs/{j8}/tonie", json={"ruid": fremd, "expect": "lib://" + umlaut})
        check(r.status_code == 200, "andere Figur für F8 vorgemerkt")
        check(c.post("/api/jobs/99999/tonie", json={"ruid": bobo, "expect": ""}).status_code == 404, "unbekannter Auftrag: 404")
        check(c.get("/api/jobs").json()[0]["tonie"]["ruid"] == bobo, "Vormerkung in der Warteschlange sichtbar")
        # Belegung von F8s Figur ändert sich zwischendurch (z. B. in TeddyCloud) -> dort nicht zuweisen
        httpx.post(f"{mock}/content/json/set/{fremd}", content=b"source=lib%3A%2F%2Fanders.taf",
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, auth=("tcuser", TC_PW))
        nsets = len(httpx.get(f"{mock}/_mock/tags").json()["sets"])
        jj = {j["item_id"]: j for j in wait_jobs(c, 2)}
        httpx.post(f"{mock}/_mock/encode-delay", json={"secs": 0.2})
        mt = httpx.get(f"{mock}/_mock/tags").json()
        check(jj[F7]["state"] == "done" and jj[F7]["tonie_ok"] is True and mt["tags"][bobo]["source"] == "lib://" + jj[F7]["tafs"][0],
              f"F7 fertig -> Bobo automatisch zugewiesen ({jj[F7].get('tonie_result')})")
        check(jj[F8]["state"] == "done" and jj[F8]["tonie_ok"] is False and "geändert" in jj[F8]["tonie_result"]
              and mt["tags"][fremd]["source"] == "lib://anders.taf", f"F8: Belegung inzwischen geändert -> nicht überschrieben ({jj[F8].get('tonie_result')})")
        check(len(mt["sets"]) == nsets + 1, "nur die eine Quelle von Bobo geändert (nocloud war schon an)")
        r = c.post(f"/api/jobs/{j7}/tonie", json={"ruid": bobo, "expect": mt["tags"][bobo]["source"]})
        check(r.status_code == 409 and "abgeschlossen" in r.json()["detail"], "abgeschlossener Auftrag: nicht mehr vormerken")
        disc = [m["content"] for m in httpx.get(f"{mock}/_mock/discord").json() if "Füllmaterial 07" in m.get("content", "")]
        check(disc and "🧸" in disc[-1], "Discord-Meldung nennt die Zuweisung")
        c.post("/api/jobs/clear")
        os.remove(os.path.join(lib, umlaut)), os.remove(os.path.join(lib, lang)), os.remove(os.path.join(lib, "fremd.taf"))
        shutil.rmtree(os.path.join(lib, BASE, "y" * 100))

        print("Eigener Unterordner und Duplikate")
        c.post("/api/jobs/clear")
        # A umbenennen und verschieben (z. B. nach Neu-Match) -> trotzdem Duplikat
        os.makedirs(os.path.join(lib, BASE, "Woanders"))
        os.rename(os.path.join(K, "Die drei --- Kids", jobs[A]["taf"]), os.path.join(lib, BASE, "Woanders", "Anderer Titel [aaaa1111].taf"))
        c.post("/api/jobs", json={"subfolder": "Eigene/Sammlung: Test", "items": [{"id": A}, {"id": B}, {"id": F1}]})
        j3 = {j["item_id"]: j for j in wait_jobs(c, 3)}
        check(j3[A]["state"] == "skipped" and j3[B]["state"] == "skipped", "vorhandene übersprungen (umbenannt/verschoben, anderer Zielordner)")
        check(j3[F1]["state"] == "done" and os.path.exists(os.path.join(lib, BASE, "Eigene", "Sammlung- Test", "005 - Füllmaterial 01 [f0010000].taf")),
              "eigener Unterordner angelegt (bereinigt)")
        check(j3[F1]["pct"] == 100 and j3[F1]["eta"] == 0 and j3[F1]["secs"] > 0, "fertiger Auftrag: 100 %, Restzeit 0, Hörzeit bekannt")

        print("Discord")
        for _ in range(50):  # Meldungen werden kurz nach dem jeweiligen Auftrag verschickt
            raw = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/discord").json()
            if len([m for m in raw if m["content"].startswith("📦")]) >= 3:
                break
            time.sleep(0.1)
        msgs = [m["content"] for m in raw]
        check([m for m in msgs if m.startswith("📦")] == ["📦 teddybridge: 3 übertragen, 0 übersprungen, 2 Fehler",
                                                         "📦 teddybridge: 2 übertragen, 0 übersprungen, 0 Fehler",
                                                         "📦 teddybridge: 1 übertragen, 2 übersprungen, 0 Fehler"],
              f"Summe nur bei Stapeln mit mehreren Aufträgen {[m for m in msgs if m.startswith('📦')]}")
        ok_msgs = [m for m in msgs if m.startswith("✅")]
        err_msgs = [m for m in msgs if m.startswith("❌")]
        check(len(ok_msgs) == 10 and all("Kapitel" in m and "📁 " + BASE in m for m in ok_msgs),
              f"pro fertigem Auftrag eine Meldung mit Kapiteln/Dauer und Ordner ({len(ok_msgs)})")
        check(len(err_msgs) == 3 and all("fehlgeschlagen:" in m for m in err_msgs), f"pro Fehler eine Meldung mit Grund ({len(err_msgs)})")
        check(len(msgs) == 16, f"übersprungene ohne eigene Meldung (insgesamt {len(msgs)})")
        check(all(m.get("allowed_mentions") == {"parse": []} for m in raw), "keine @-Erwähnungen aus Titeln")

        print("Leere Uploads")
        c.post("/api/jobs/clear")
        httpx.post(f"http://127.0.0.1:{TC_P}/_mock/empty-uploads", json={"match": "_src_f0020000", "mode": "always"})
        c.post("/api/jobs", json={"items": [{"id": F2}]})
        jf = wait_jobs(c, 1)[0]
        check(jf["state"] == "error" and "unvollständig" in jf["error"], f"dauerhaft leerer Upload -> klare Fehlermeldung ({jf.get('error')})")
        check(not any("_src_" in f for f in tree(lib)) and not any("[f0020000]" in f for f in tree(lib)),
              "nach leerem Upload aufgeräumt, keine TAF")
        tclog = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/log").json()
        check(not any(e["path"].startswith("/api/fileEncode") and "f0020000" in str(e) for e in tclog),
              "kein fileEncode mit leeren Quellen")

        print("Navidrome")
        N1, N2 = "N1hotzenplotzAAAAAAAAA", "N2kaputtBBBBBBBBBBBBBB"
        c.post("/api/jobs/clear")
        check("nd" in c.get("/api/config").json()["sources"], "Navidrome als Quelle aktiv")
        check(c.get("/api/nd/libraries").json() == [{"id": "1", "name": "Hörspiele"}, {"id": "2", "name": "Musik"}],
              "Musikordner gelistet")
        pages, pg = [], 0
        while True:
            d = c.get(f"/api/nd/libraries/1/items?page={pg}&limit=3&sort=media.metadata.title").json()
            pages += d["items"]
            if len(pages) >= d["total"]:
                break
            pg += 1
        check(len(pages) == 7 and len({i["id"] for i in pages}) == 7 and pg == 2, "Alben seitenweise, nur gewählter Ordner")
        h = next(i for i in pages if i["id"] == N1)
        check(h["mark"] == "nN1hotze" and h["files"] == 3 and h["author"] == "Otfried Preußler"
              and h["cover"].startswith("/api/nd/cover/al-") and h["source"] == "nd", "Album-Kachel")
        check([i["id"] for i in c.get("/api/nd/libraries/1/search", params={"q": "hotzen"}).json()["items"]] == [N1],
              "Navidrome-Suche")
        arts = c.get("/api/nd/libraries/1/series").json()
        op = next(a for a in arts if a["name"] == "Otfried Preußler")
        check(op["count"] == 2 and [a["name"] for a in arts] == ["Diverse", "Otfried Preußler"], "Künstler gelistet")
        aa = c.get(f"/api/nd/libraries/1/series/{op['id']}/items").json()
        check([i["id"] for i in aa["items"]] == [N1, N2], "Alben eines Künstlers nach Jahr")
        r = c.get(f"/api/nd/cover/{h['cover'].rsplit('/', 1)[1]}")
        check(r.status_code == 200 and r.content[:4] == b"\x89PNG", "Navidrome-Cover über Backend")
        r = c.post("/api/jobs", json={"items": [{"id": N1, "source": "nd", "lib": "1"}, {"id": N2, "source": "nd", "lib": "1"}]})
        check(r.status_code == 200, "Navidrome-Stapel angenommen")
        jobs = {j["item_id"]: j for j in wait_jobs(c, 2)}
        hdir = os.path.join(lib, BASE, "Hörspiele", "Otfried Preußler")
        taf = os.path.join(hdir, "Der Räuber Hotzenplotz [nN1hotze].taf")
        check(jobs[N1]["state"] == "done" and os.path.isfile(taf), f"Album als TAF ({jobs[N1].get('error')})")
        check(os.path.isfile(taf) and taf_lines(taf) == ["Hotz CD1-1", "Hotz CD1-2", "Hotz CD2-1"],
              "Titel nach CD und Titelnummer sortiert")
        check(jobs[N2]["state"] == "error" and "Navidrome Download" in (jobs[N2]["error"] or ""),
              f"Download-Fehler gemeldet ({jobs[N2].get('error')})")
        check(not any("_src_" in f for f in tree(lib)) and not any("[nN2kaput]" in f for f in tree(lib)),
              "nach Navidrome-Fehler aufgeräumt, keine TAF")
        check("nN1hotze" in c.get("/api/present").json()["ids"], "Navidrome-Album als vorhanden erkannt")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": N1, "source": "nd", "lib": "1"}]})
        check(wait_jobs(c, 1)[0]["state"] == "skipped", "Navidrome-Album erneut: übersprungen")
        print("Playlists")
        c.post("/api/jobs/clear")
        pls = c.get("/api/nd/playlists").json()
        check([(p["title"], p["mark"], p["files"], p["kind"]) for p in pls] == [("Gute Nacht", "pPLmixEE", 3, "playlist")],
              "Navidrome-Playlists gelistet")
        sr = c.get("/api/nd/libraries/1/search", params={"q": "hotz"}).json()
        check([x["id"] for x in sr["songs"]] == ["s13", "s12", "s11"] and sr["songs"][0]["album"] == "Der Räuber Hotzenplotz",
              "Suche liefert auch Titel")
        so = c.get(f"/api/nd/albums/{N1}/songs").json()
        check([x["title"] for x in so["songs"]] == ["Hotz CD1-1", "Hotz CD1-2", "Hotz CD2-1"], "Titel eines Albums sortiert")
        check(c.post("/api/jobs", json={"items": [{"id": "custom", "source": "nd", "kind": "songs", "songs": []}]}).status_code == 422,
              "leere eigene Playlist abgelehnt")
        check(c.post("/api/jobs", json={"items": [{"id": A, "kind": "playlist"}]}).status_code == 422, "Playlist nur bei Navidrome")
        mix = ["s12", "s901", "s11"]
        c.post("/api/jobs", json={"items": [{"id": pls[0]["id"], "source": "nd", "kind": "playlist", "lib": "1"},
                                            {"id": "custom", "source": "nd", "kind": "songs", "songs": mix, "title": "Mein Mix"}]})
        jobs = wait_jobs(c, 2)
        pdir = os.path.join(lib, BASE, "Playlists")
        ptaf = os.path.join(pdir, "Gute Nacht [pPLmixEE].taf")
        mtafs = [f for f in os.listdir(pdir) if f.startswith("Mein Mix [m")] if os.path.isdir(pdir) else []
        check(all(j["state"] == "done" for j in jobs), f"Playlist-Aufträge fertig ({[j.get('error') for j in jobs]})")
        check(os.path.isfile(ptaf) and taf_lines(ptaf) == ["F3", "Hotz CD1-1", "Lied"], "Navidrome-Playlist in Playlist-Reihenfolge")
        check(len(mtafs) == 1 and taf_lines(os.path.join(pdir, mtafs[0])) == ["Hotz CD1-2", "F1", "Hotz CD1-1"],
              "eigene Playlist in gewählter Reihenfolge")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": "custom", "source": "nd", "kind": "songs", "songs": mix, "title": "Anderer Name"},
                                            {"id": "custom", "source": "nd", "kind": "songs", "songs": mix[::-1], "title": "Umgekehrt"}]})
        st2 = [j["state"] for j in wait_jobs(c, 2)]
        check(st2 == ["skipped", "done"], f"gleiche Titelfolge übersprungen, andere Reihenfolge neu ({st2})")
        ndlog = httpx.get(f"http://127.0.0.1:{ND_P}/_mock/log").json()
        check(ndlog and all(e["method"] == "GET" and "&p=" not in e["path"] and "?p=" not in e["path"] for e in ndlog),
              "Navidrome: nur GET, nie Klartext-Passwort")

        print("Dateiquelle (NAS)")
        import base64 as b64
        def fid(rel):
            return b64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")
        check(c.get("/api/config").json()["sources"] == ["abs", "nd", "fs"], "Quelle 'fs' angeboten")
        check(c.get("/api/fs/libraries").json() == [{"id": "0", "name": "Hörspiele"}, {"id": "1", "name": "Musik"},
                                                    {"id": "2", "name": "Offline"}], "Freigaben aus FS_ROOTS")
        r = c.get("/api/fs/libraries/2/browse")
        check(r.status_code == 502 and "nicht erreichbar" in r.json()["error"], "nicht eingehängte Freigabe: verständlicher Fehler")
        root = c.get("/api/fs/libraries/0/browse").json()
        names = [e["title"] for e in root["entries"]]
        check(names == ["Bibi", "Die drei ??? Kids", "Gross", "leer"], f"Wurzel: sortiert, ohne Versteckte/@eaDir/Ausbruch ({names})")
        check(not root["here"]["item"] and root["crumbs"] == [{"id": "-", "name": "Hörspiele"}], "Wurzel ist kein Hörspiel")
        kids = next(e for e in root["entries"] if e["title"] == "Die drei ??? Kids")
        check(not kids["item"] and kids["dirs"] == 2, "Serienordner ist kein Hörspiel (nur Unterordner)")
        kd = c.get(f"/api/fs/libraries/0/browse?path={kids['id']}").json()
        ep1, ep2 = kd["entries"]
        check([e["title"] for e in kd["entries"]] == ["001 - Panik im Paradies", "002 - Mit CDs"]
              and [x["name"] for x in kd["crumbs"]] == ["Hörspiele", "Die drei ??? Kids"], "Folgen-Ordner + Brotkrumen")
        check(ep1["item"] and ep1["files"] == 3 and ep1["mark"].startswith("s") and len(ep1["mark"]) == 8
              and ep1["author"] == "Die drei ??? Kids", "Hörspiel-Ordner: 3 Audiodateien (ohne Symlink nach aussen, FIFO, txt)")
        check(ep2["item"] and ep2["files"] == 4 and ep2["dirs"] == 0, "CD-Unterordner gehören zum Hörspiel")
        r = c.get(ep1["cover"])
        check(r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/jpeg", "Cover aus Ordner")
        check(c.get(ep2["cover"]).content == PNG, "Cover aus CD1-Ordner")
        bibi = next(e for e in root["entries"] if e["title"] == "Bibi")
        f1 = c.get(f"/api/fs/libraries/0/browse?path={bibi['id']}").json()["entries"][0]
        check(f1["item"] and f1["files"] == 1 and f1["dirs"] == 1, "Hörspiel mit (Nicht-CD-)Unterordner: nur eigene Dateien")
        for bad in ("Li4", fid("../.."), fid("Ausbruch"), fid("Ausbruch/ssl"), fid("/etc"), "x" * 1401, "a/b"):
            code = c.get(f"/api/fs/libraries/0/browse", params={"path": bad}).status_code
            check(code in (404, 422, 502), f"Pfad-Ausbruch abgelehnt ({bad[:12]} -> {code})")
        check(c.get("/api/fs/libraries/9/browse").status_code == 502, "unbekannte Freigabe abgelehnt")
        s1 = c.get("/api/fs/libraries/0/search", params={"q": "paradies"}).json()["items"]
        check([i["title"] for i in s1] == ["001 - Panik im Paradies"], "Suche über Ordnernamen")
        check(c.get("/api/fs/libraries/0/search", params={"q": "cd1"}).json()["items"] == [], "CD-Ordner nicht als Treffer")
        check(c.post("/api/jobs", json={"items": [{"id": fid("Ausbruch"), "source": "fs", "lib": "0"}]}).status_code == 502,
              "Auftrag mit Pfad ausserhalb der Freigabe abgelehnt")
        check(c.post("/api/jobs", json={"items": [{"id": ep1["id"], "source": "fs"}]}).status_code == 422, "Auftrag ohne Freigabe abgelehnt")
        c.post("/api/jobs/clear")
        nas_before = tree(nas)
        mtimes = {p: os.lstat(os.path.join(nas, p)).st_mtime for p in nas_before}
        c.post("/api/jobs", json={"items": [{"id": ep1["id"], "source": "fs", "lib": "0", "title": ep1["title"]},
                                            {"id": ep2["id"], "source": "fs", "lib": "0", "title": ep2["title"]},
                                            {"id": f1["id"], "source": "fs", "lib": "0", "title": f1["title"]}]})
        jf = wait_jobs(c, 3)
        check([j["state"] for j in jf] == ["done"] * 3, f"drei Ordner übertragen ({[j['state'] + ' ' + str(j['error']) for j in jf]})")
        kdir = os.path.join(lib, BASE, "Hörspiele", "Die drei --- Kids")
        t1 = os.path.join(kdir, f"001 - Panik im Paradies [{ep1['mark']}].taf")
        t2 = os.path.join(kdir, f"002 - Mit CDs [{ep2['mark']}].taf")
        check(os.path.exists(t1) and os.path.exists(t2), "Ablage TC_BASE/Freigabe/Ordnerpfad, Titel = Ordnername")
        check(os.path.exists(t1) and taf_lines(t1) == ["01 Anfang.mp3", "02 Mitte.mp3", "10 Ende.mp3"], "natürliche Reihenfolge 01, 02, 10")
        check(os.path.exists(t2) and taf_lines(t2) == ["01 c1a.mp3", "02 c1b.mp3", "01 c2.mp3", "01 c10.mp3"], "CD1, CD2, CD10 in Reihenfolge")
        t3 = os.path.join(lib, BASE, "Hörspiele", "Bibi", f"Folge 1 [{f1['mark']}].taf")
        check(os.path.exists(t3) and taf_lines(t3) == ["x.m4a"], "Bonus-Unterordner nicht mitgenommen")
        check(not [p for p in tree(os.path.join(lib, BASE)) if "_src_" in p], "keine _src_-Reste")
        check(tree(nas) == nas_before and all(os.lstat(os.path.join(nas, p)).st_mtime == m for p, m in mtimes.items()),
              "NAS unverändert (nur gelesen)")
        check(ep1["id"] and {ep1["mark"], ep2["mark"]} <= set(c.get("/api/present").json()["ids"]), "Status 'in TeddyCloud' für Ordner")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": ep1["id"], "source": "fs", "lib": "0"}]})
        check([j["state"] for j in wait_jobs(c, 1)] == ["skipped"], "erneut: übersprungen")
        c.post("/api/jobs/clear")
        gross = next(e for e in root["entries"] if e["title"] == "Gross")
        check(gross["files"] == 120, "120 Dateien erkannt")
        c.post("/api/jobs", json={"items": [{"id": gross["id"], "source": "fs", "lib": "0"}]})
        jg = wait_jobs(c, 1)[0]
        check(jg["state"] == "error" and "Aufteilen" in (jg["error"] or ""), "über 99 ohne Aufteilen: Fehler")
        c.post("/api/jobs/clear")
        c.post("/api/jobs", json={"items": [{"id": gross["id"], "source": "fs", "lib": "0", "split": True}]})
        check(wait_jobs(c, 1)[0]["state"] == "done" and len([n for n in os.listdir(os.path.join(lib, BASE, "Hörspiele"))
                                                              if n.startswith("Gross - Teil")]) == 2, "über 99 mit Aufteilen: 2 Teile")
        c.post("/api/jobs/clear")
        mus = c.get("/api/fs/libraries/1/browse").json()["entries"][0]
        check(mus["mark"] != c.get("/api/fs/libraries/0/browse").json()["entries"][0]["mark"], "Kennzeichen je Freigabe verschieden")
        os.chmod(os.path.join(nas, "musik/Album/01 track.flac"), 0)
        c.post("/api/jobs", json={"items": [{"id": mus["id"], "source": "fs", "lib": "1"}]})
        jm = wait_jobs(c, 1)[0]
        os.chmod(os.path.join(nas, "musik/Album/01 track.flac"), 0o644)
        if os.geteuid() == 0:
            print("  (als root: Leserechte-Fehler nicht prüfbar)")
        else:
            check(jm["state"] == "error" and "nicht lesbar" in jm["error"], "nicht lesbare Datei: Fehler, aufgeräumt")
        c.post("/api/jobs/clear")

        print("Zusammenfügen und Kapitel (echtes Audio)")
        from app import mp3cat, worker
        if not shutil.which("ffmpeg"):
            print("  (ffmpeg fehlt: übersprungen)")
        else:
            echt = make_real_audio(os.path.join(nas, "hsp", "Echt"), tmp)
            eb = {e["title"]: e for e in c.get("/api/fs/libraries/0/browse",
                                               params={"path": next(e["id"] for e in c.get("/api/fs/libraries/0/browse").json()["entries"]
                                                                    if e["title"] == "Echt")}).json()["entries"]}
            hp = os.path.join(lib, BASE, "Hörspiele", "Echt")

            def taf(title, mark):
                return os.path.join(hp, f"{title} [{mark}].taf")

            def lines(path):
                return [l.split(" | ") for l in open(path).read().splitlines()[1:]]

            def kept(path):
                """vom Mock aufbewahrte Quellen der Umwandlung zu einer TAF, in Kapitelreihenfolge"""
                target = os.path.relpath(path, lib)
                d = os.path.join(keep, target.replace("/", "_"))
                return [os.path.join(d, n) for n in sorted(os.listdir(d))] if os.path.isdir(d) else []
            import test_audio as ta
            r = c.post("/api/jobs", json={"items": [{"id": eb["Lang"]["id"], "source": "fs", "lib": "0", "split": True, "merge": True}]})
            check(r.status_code == 422, "Aufteilen und Zusammenfügen gleichzeitig: abgelehnt")
            c.post("/api/jobs", json={"items": [{"id": eb["Lang"]["id"], "source": "fs", "lib": "0", "merge": True}]})
            j = wait_jobs(c, 1)[0]
            t = taf("Lang", eb["Lang"]["mark"])
            check(j["state"] == "done" and os.path.exists(t), f"130 MP3 zusammengefügt: {j['state']} {j.get('error') or ''}")
            ls = lines(t) if os.path.exists(t) else []
            check(len(ls) == 99 and j["check"].startswith("99 Kapitel") and not j["warning"], "eine TAF mit 99 Kapiteln, Prüfung ok")
            check(j["note"] == "130 Dateien zu 99 Kapiteln zusammengefügt", "Hinweis in der Warteschlange")
            check(j["m"] == 130 and j["n"] == 130, "Fortschritt zählt alle 130 Dateien")
            # Inhalt Stück für Stück nachrechnen: gleiche Gruppen wie der Worker, gleiche Bytes
            files = [{"ext": "mp3", "dur": 0, "size": os.path.getsize(os.path.join(echt["Lang"], n)), "p": os.path.join(echt["Lang"], n)}
                     for n in sorted(os.listdir(echt["Lang"]))]
            # zusammengefügte Kapitel: nur reine Audiodaten; einzelne Dateien: unverändert
            want = sum(sum(e - s for s, e, _ in (mp3cat.audio_range(u["p"]) for u in unit)) if len(unit) > 1 else unit[0]["size"]
                       for unit in worker.merge_plan(files))
            got = sum(int(l[2].split()[0].split("=")[1]) for l in ls)
            check(got == want, f"Audiodaten vollständig, nichts doppelt ({got} = {want} Bytes)")
            ok = True
            for (src, _, meta), unit in zip(ls, worker.merge_plan(files)):
                out = os.path.join(tmp, "nachgerechnet.mp3")
                mp3cat.concat([u["p"] for u in unit], out) if len(unit) > 1 else shutil.copy(unit[0]["p"], out)
                ok &= meta.split("sha=")[1] == hashlib.sha256(open(out, "rb").read()).hexdigest()[:16]
            check(ok, "jedes Kapitel bytegenau wie erwartet (Gruppen und Reihenfolge)")
            c.post("/api/jobs/clear")
            c.post("/api/jobs", json={"items": [{"id": eb["Lang"]["id"], "source": "fs", "lib": "0", "merge": True}]})
            check(wait_jobs(c, 1)[0]["state"] == "skipped", "erneut zusammenfügen: übersprungen")
            c.post("/api/jobs/clear")
            c.post("/api/jobs", json={"items": [{"id": gross["id"], "source": "fs", "lib": "0", "merge": True}]})
            check(wait_jobs(c, 1)[0]["state"] == "skipped", "schon als Teile vorhanden: Zusammenfügen übersprungen (kein Duplikat)")
            c.post("/api/jobs/clear")
            for title, what in (("Gemischt", "MP3 mit 22 kHz zwischen 44,1 kHz"), ("Nicht MP3", "m4a zwischen MP3")):
                c.post("/api/jobs", json={"items": [{"id": eb[title]["id"], "source": "fs", "lib": "0", "merge": True}]})
                j = wait_jobs(c, 1)[0]
                t = taf(title, eb[title]["mark"])
                ls = lines(t) if os.path.exists(t) else []
                exts = [os.path.splitext(l[0])[1] for l in ls]
                check(j["state"] == "done" and len(ls) == 99 and exts.count(".flac") >= 1 and set(exts) <= {".mp3", ".flac"},
                      f"{what}: mit ffmpeg zusammengefügt ({j['state']} {j.get('error') or ''}, {exts.count('.flac')}× FLAC)")
                got = sum(ta.decoded_seconds(p)[0] for p in kept(t))
                want = sum(ta.decoded_seconds(os.path.join(echt[title], n))[0] for n in os.listdir(echt[title]))
                check(abs(got - want) < 0.5, f"{what}: Gesamtlänge {got:.1f}s = Quellen {want:.1f}s")
                check(all(not ta.decoded_seconds(p)[1] for p in kept(t)), f"{what}: alle Kapitel dekodierbar")
                c.post("/api/jobs/clear")
            for title, n in (("Buch", 12), ("Buch Gross", 99)):
                c.post("/api/jobs", json={"items": [{"id": eb[title]["id"], "source": "fs", "lib": "0"}]})
                j = wait_jobs(c, 1)[0]
                t = taf(title, eb[title]["mark"])
                ls = lines(t) if os.path.exists(t) else []
                check(j["state"] == "done" and len(ls) == n and all(l[0].endswith(".m4a") for l in ls)
                      and j["check"].startswith(f"{n} Kapitel") and not j["warning"],
                      f"m4b mit {12 if n == 12 else 130} Kapiteln -> TAF mit {n} Kapiteln ({j['state']} {j.get('error') or ''})")
                check(j["note"] == ("1 Datei in 12 Kapitel geschnitten" if n == 12 else
                                    "1 Datei in 130 Kapitel geschnitten, zu 99 zusammengefügt"), f"Hinweis: {j['note']}")
                c.post("/api/jobs/clear")

            print("Kapitel ohne Kapitelmarken (an Pausen)")
            check(c.post("/api/jobs", json={"chapter_min": 2, "items": [fsi0 := {"id": eb["Einzeldatei"]["id"], "source": "fs", "lib": "0"}]})
                  .status_code == 422, "Kapitel alle 2 min: abgelehnt (mind. 3)")
            c.post("/api/jobs", json={"chapter_min": 3, "items": [fsi0]})
            j = wait_jobs(c, 1)[0]
            t = taf("Einzeldatei", eb["Einzeldatei"]["mark"])
            lens = [ta.decoded_seconds(p)[0] for p in kept(t)] if os.path.exists(t) else []
            check(j["state"] == "done" and len(lens) == 2 and abs(lens[0] - 171) < 1.5 and abs(sum(lens) - 400) < 1,
                  f"400 s, Kapitel alle 3 min: 2 Kapitel, Schnitt in der Pause bei 171 s ({lens}, {j['state']} {j.get('error') or ''})")
            check("Kapitel alle ca. 3 min" in (j["note"] or "") and "1 von 1 Schnitten in einer Pause" in (j["note"] or ""),
                  f"Hinweis: {j['note']}")
            c.post("/api/jobs/clear")

            print("Kapitel aus ABS (m4b) und Sammel-TAF")
            c.post("/api/jobs", json={"items": [{"id": M4B_ID}]})
            j = wait_jobs(c, 1)[0]
            t = os.path.join(lib, BASE, "Podcasts", "Testautor", f"Kapitelbuch [{M4B_ID[:8]}].taf")
            lens = [ta.decoded_seconds(p)[0] for p in kept(t)] if os.path.exists(t) else []
            check(j["state"] == "done" and len(lens) == len(ABS_CH) and all(abs(a - b) < 0.1 for a, b in zip(lens, ABS_CH)),
                  f"ABS-m4b: Kapitel aus den ABS-Metadaten, Längen stimmen ({j['state']} {j.get('error') or ''})")
            c.post("/api/jobs/clear")
            fsi = lambda title: {"id": eb[title]["id"], "source": "fs", "lib": "0", "title": title}  # noqa: E731
            for bad, what in (({"bundle": "X", "items": [fsi("Buch")]}, "nur ein Eintrag"),
                              ({"bundle": "  ", "items": [fsi("Buch"), fsi("Lang")]}, "ohne Namen"),
                              ({"bundle": "X", "items": [fsi("Buch"), fsi("Buch")]}, "doppelter Eintrag")):
                check(c.post("/api/jobs", json=bad).status_code == 422, f"Sammel-TAF {what}: abgelehnt")
            # Buch (12 Kapitel, m4b) + Lang (130 MP3) = 142 Stücke -> 99; Buchgrenze und alle 12 Buchkapitel bleiben
            c.post("/api/jobs", json={"bundle": "Sammel Test", "items": [fsi("Buch"), fsi("Lang")]})
            j = wait_jobs(c, 1)[0]
            tdir = os.path.join(lib, BASE, "Sammlungen")
            t = next((os.path.join(tdir, n) for n in (os.listdir(tdir) if os.path.isdir(tdir) else []) if n.startswith("Sammel Test [k")), "")
            ks = kept(t) if t else []
            check(j["state"] == "done" and len(ks) == 99 and j["check"].startswith("99 Kapitel"),
                  f"Sammel-TAF: 2 Hörbücher -> eine TAF mit 99 Kapiteln unter Sammlungen ({j['state']} {j.get('error') or ''})")
            check((j["note"] or "").startswith("2 Hörbücher, 99 Kapitel"), f"Hinweis: {j['note']}")
            secs12 = [3.0, 1.0, 4.0, 2.0, 5.0, 1.5, 2.5, 3.0, 1.0, 2.0, 4.0, 3.0]
            lens = [ta.decoded_seconds(p)[0] for p in ks]
            check(len(lens) == 99 and all(abs(a - b) < 0.1 for a, b in zip(lens[:12], secs12)) and all(p.endswith(".m4a") for p in ks[:12]),
                  "Sammel-TAF: erst die 12 Buchkapitel unverändert")
            check(all(p.endswith(".mp3") for p in ks[12:]), "Sammel-TAF: danach nur MP3 (keine Mischkapitel über die Buchgrenze)")
            want = sum(secs12) + sum(ta.decoded_seconds(os.path.join(echt["Lang"], n))[0] for n in os.listdir(echt["Lang"]))
            # jede in ein Kapitel zusammengefügte MP3 behält ihre Encoder-Pause (Delay + Auffüllung, ca. 25–50 ms),
            # die ffmpeg bei Einzeldateien über den LAME-Kopf wegschneidet (siehe test_audio)
            n_merged = 2 * (130 - (99 - 12))  # 130 Dateien -> 87 Kapitel: 43 Zusammenlegungen à 2 Dateien
            check(-0.3 < sum(lens) - want < 0.06 * n_merged, f"Sammel-TAF: Gesamtlänge {sum(lens):.1f}s = Quellen {want:.1f}s "
                  f"+ Encoder-Pausen ({(sum(lens) - want) * 1000 / n_merged:.0f} ms je zusammengefügter Datei)")
            c.post("/api/jobs/clear")
            c.post("/api/jobs", json={"bundle": "Anderer Name", "items": [fsi("Buch"), fsi("Lang")]})
            check(wait_jobs(c, 1)[0]["state"] == "skipped", "gleiche Auswahl erneut (anderer Name): übersprungen")
            c.post("/api/jobs/clear")
            # gemischte Quellen: ABS-m4b + NAS-m4b, nichts zusammenzufügen
            c.post("/api/jobs", json={"bundle": "Gemischte Quellen", "items": [{"id": M4B_ID, "title": "Kapitelbuch"}, fsi("Buch")],
                                      "subfolder": "Eigene"})
            j = wait_jobs(c, 1)[0]
            tdir = os.path.join(lib, BASE, "Eigene")
            t = next((os.path.join(tdir, n) for n in (os.listdir(tdir) if os.path.isdir(tdir) else []) if n.startswith("Gemischte Quellen [k")), "")
            lens = [ta.decoded_seconds(p)[0] for p in kept(t)] if t else []
            check(j["state"] == "done" and len(lens) == len(ABS_CH) + 12
                  and all(abs(a - b) < 0.1 for a, b in zip(lens, ABS_CH + secs12)),
                  f"Sammel-TAF aus ABS und NAS im eigenen Ordner: {len(ABS_CH)}+12 Kapitel in Auswahlreihenfolge ({j['state']} {j.get('error') or ''})")
            c.post("/api/jobs/clear")
            print("Alter Basisordner (TC_BASE_EXTRA)")
            alt = os.path.join(lib, "altbasis", "Irgendwo", f"Alt [{eb['Alt']['mark']}].taf")
            os.makedirs(os.path.dirname(alt))
            with open(alt, "w") as f:
                f.write("FAKE-TAF\nx | y\nx | y\n")
            before = sorted(tree(os.path.join(lib, "altbasis")))
            check(eb["Alt"]["mark"] in c.get("/api/present").json()["ids"], "TAF im alten Basisordner: als vorhanden angezeigt")
            c.post("/api/jobs", json={"items": [fsi("Alt")]})
            check(wait_jobs(c, 1)[0]["state"] == "skipped" and not os.path.exists(taf("Alt", eb["Alt"]["mark"])),
                  "TAF im alten Basisordner: übersprungen, nichts doppelt angelegt")
            check(sorted(tree(os.path.join(lib, "altbasis"))) == before, "alter Basisordner unverändert (nur gelesen)")
            c.post("/api/jobs/clear")

            print("Abbruch beim Umwandeln (TeddyCloud-Neustart)")
            c.post("/api/jobs", json={"items": [fsi("ENCODE-CRASH-ONCE")]})
            j = wait_jobs(c, 1)[0]
            t = taf("ENCODE-CRASH-ONCE", eb["ENCODE-CRASH-ONCE"]["mark"])
            check(j["state"] == "done" and os.path.exists(t) and len(lines(t)) == 2 and "automatisch wiederholt" in (j["warning"] or ""),
                  f"einmaliger Abbruch: halbe TAF entfernt, automatisch neu umgewandelt ({j['state']}, {j.get('warning')})")
            c.post("/api/jobs/clear")
            c.post("/api/jobs", json={"items": [fsi("ENCODE-CRASH")]})
            j = wait_jobs(c, 1)[0]
            check(j["state"] == "error" and "3× abgebrochen" in (j["error"] or "") and "entfernt" in j["error"]
                  and not os.path.exists(taf("ENCODE-CRASH", eb["ENCODE-CRASH"]["mark"])),
                  f"dauernder Abbruch: nach 2 Neuversuchen aufgegeben, keine halbe TAF übrig ({(j['error'] or '')[:80]}…)")
            c.post("/api/jobs/clear")
            # halbe TAF aus einem früheren Lauf (z. B. vor dem Update): nie löschen, aber klar melden statt „vorhanden“
            halb = taf("Halb", eb["Halb"]["mark"])
            os.makedirs(os.path.dirname(halb), exist_ok=True)
            with open(halb, "w") as f:
                f.write("FAKE-TAF\n")
            c.post("/api/jobs", json={"items": [fsi("Halb")]})
            j = wait_jobs(c, 1)[0]
            check(j["state"] == "error" and "unvollständige TAF" in (j["error"] or "") and "bitte dort löschen" in j["error"]
                  and os.path.exists(halb), f"alte halbe TAF: gemeldet statt übersprungen, nicht gelöscht ({j['state']})")
            os.remove(halb)  # „Nutzer löscht“ -> jetzt klappt es
            c.post("/api/jobs/clear")
            c.post("/api/jobs", json={"items": [fsi("Halb")]})
            check(wait_jobs(c, 1)[0]["state"] == "done", "nach dem Löschen: Übertragung klappt")
            c.post("/api/jobs/clear")
            check(not [p for p in tree(os.path.join(lib, BASE)) if "_src_" in p], "keine _src_-Reste (auch nach Fehlern)")
            check(all(os.path.getsize(os.path.join(d, n)) > 0 for d in echt.values() for n in os.listdir(d)),
                  "Testdateien auf dem NAS unverändert vorhanden")

        print("Sprache")
        EN = {"Accept-Language": "en-US,en;q=0.9"}
        h = c.get("/", headers=EN).text
        check('<html lang="en" data-lang-fixed="0">' in h, "Accept-Language en -> Seite englisch, umschaltbar")
        check('<html lang="de" data-lang-fixed="0">' in c.get("/").text, "Accept-Language de -> Seite deutsch")
        check('<html lang="de"' in c.get("/", headers={**EN, "Cookie": "tb_lang=de"}).text, "Cookie (Auswahl) schlägt Accept-Language")
        check('<html lang="en"' in httpx.get(f"http://127.0.0.1:{APP_P}/login", headers=EN, follow_redirects=False).text,
              "Login-Seite englisch")
        check(c.get("/api/config", headers=EN).json().get("lang") == "en", "api/config meldet Sprache")
        r = httpx.get(f"http://127.0.0.1:{APP_P}/api/config", headers=EN)
        check(r.status_code == 401 and r.json().get("error") == "Login required", "401 englisch")
        r = c.post("/api/jobs", headers=EN, json={"items": [{"id": A}], "bundle": "X"})
        check(r.status_code == 422 and "select at least two" in r.text, f"API-Fehler englisch ({r.text[:80]})")
        n_before = len(c.get("/api/jobs").json())
        c.post("/api/jobs", headers=EN, json={"items": [{"id": C}]})
        je = wait_jobs(c, n_before + 1)[-1]
        check(je["state"] == "error" and "TeddyCloud allows at most" in (je["error"] or ""),
              f"Auftrag in der Sprache der Anfrage ({(je['error'] or '')[:60]})")
        for _ in range(50):
            raw = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/discord").json()
            if any("failed:" in m["content"] for m in raw):
                break
            time.sleep(0.1)
        check(any(m["content"].startswith("❌ teddybridge:") and "failed:" in m["content"] for m in raw), "Discord englisch")
        check(isinstance(cfg_err(UI_LANG="fr"), str), "UI_LANG ungültig: Startabbruch")
        check(cfg_err(UI_LANG="EN").ui_lang == "en" and cfg_err(UI_LANG="auto").ui_lang == "", "UI_LANG en/auto")
        probe = r'''
from fastapi.testclient import TestClient
from app import main
c = TestClient(main.app); c.auth = ("admin", main.CFG.ui_pass)
h = c.get("/", headers={"Accept-Language": "en", "Cookie": "tb_lang=en"}).text
print("RESULT", '<html lang="de" data-lang-fixed="1">' in h, c.get("/api/config").json()["langFixed"])
'''
        penv = dict(env, UI_LANG="de", UI_PASS=UI_PW, UI_PASS_HASH="", LISTEN="127.0.0.1:1")
        r = subprocess.run([PY, "-c", probe], cwd=ROOT, env=penv, capture_output=True, text=True, timeout=60)
        check("RESULT True True" in r.stdout, f"UI_LANG=de: fest deutsch, ohne Auswahl ({r.stdout[-60:]}{r.stderr[-200:]})")

        print("Einbetten (FRAME_ANCESTORS)")
        check(cfg_err().frame_ancestors == (), "FRAME_ANCESTORS leer: kein Einbetten")
        fa = cfg_err(FRAME_ANCESTORS="http://TeddyCloud.example/, https://192.0.2.10:8443 http://teddycloud.example")
        check(fa.frame_ancestors == ("http://teddycloud.example", "https://192.0.2.10:8443"), f"FRAME_ANCESTORS normalisiert ({getattr(fa, 'frame_ancestors', fa)})")
        for bad in ("*", "teddycloud.example", "http://x/pfad", "javascript:alert(1)", "http://x;script-src *",
                    "http://u:p@x", "http://x:99999", "https://*.example"):
            check(isinstance(cfg_err(FRAME_ANCESTORS=bad), str), f"FRAME_ANCESTORS ungültig abgelehnt: {bad}")
        probe = r'''
from fastapi.testclient import TestClient
from app import main
c = TestClient(main.app)
for path in ("/", "/login", "/api/config"):
    h = c.get(path, follow_redirects=False).headers
    print("RESULT", path, "frame-ancestors http://tc.example:8080;" in h["content-security-policy"], "x-frame-options" in h)
'''
        penv = dict(env, FRAME_ANCESTORS="http://tc.example:8080", UI_PASS=UI_PW, UI_PASS_HASH="", LISTEN="127.0.0.1:1")
        r = subprocess.run([PY, "-c", probe], cwd=ROOT, env=penv, capture_output=True, text=True, timeout=60)
        check(r.stdout.count("True False") == 3, f"FRAME_ANCESTORS gesetzt: CSP erlaubt nur diese Adresse, kein X-Frame-Options "
                                                 f"({r.stdout[-120:]}{r.stderr[-200:]})")
        pg = c.get("/").text
        check("parent.postMessage({teddybridge: 'ready'}, '*')" in pg and "parent.postMessage" in httpx.get(
              f"http://127.0.0.1:{APP_P}/login").text, "Seiten melden sich im Rahmen (Plugin)")

        print("Regeln")
        abslog = httpx.get(f"http://127.0.0.1:{ABS_P}/_mock/log").json()
        check(all(e["method"] == "GET" for e in abslog), "ABS: nur GET-Aufrufe")
        tclog = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/log").json()
        dels = [e for e in tclog if e["path"].startswith(("/api/fileDelete", "/api/dirDelete"))]
        own_partial = [e for e in dels if "/_src_" not in e.get("rel", "")]
        check(dels and all("ENCODE-CRASH" in e.get("rel", "") and e["rel"].endswith(".taf") for e in own_partial),
              f"TC: nur _src_ gelöscht, ausser eigenen halben TAFs nach Abbruch ({len(dels)} Löschaufrufe, {len(own_partial)} halbe TAFs)")
        check(all(e["status"] != 401 for e in tclog), "TC: Basic Auth mitgeschickt")
        check(all("special=library" in e["path"] for e in tclog if e["path"].startswith("/api/") and not e["path"].startswith("/api/getTagIndex")),
              "TC: immer special=library (ausser Tonie-Liste)")
        check(all("special=" not in e["path"] for e in tclog if e["path"].startswith(("/api/getTagIndex", "/content/json/set/"))),
              "TC: Tonie-Aufrufe ohne special (sonst falscher Ordner)")

        print("TeddyCloud-eigene Anmeldung (web_auth)")
        def drop_taf(mark):
            for dp, _, fns in os.walk(lib):
                for fn in fns:
                    if fn.endswith(f"[{mark}].taf"):
                        os.remove(os.path.join(dp, fn))
        def transfer_a():
            n0 = len(c.get("/api/jobs").json())
            c.post("/api/jobs", json={"items": [{"id": A}]})
            return wait_jobs(c, n0 + 1)[-1]
        wa = lambda **kw: httpx.post(f"http://127.0.0.1:{TC_P}/_mock/web-auth", json=kw).json()["logins"]
        l0 = wa(enabled=True, reset=True)
        drop_taf(A[:8])
        j = transfer_a()
        l1 = wa(enabled=True)
        check(j["state"] == "done" and l1 - l0 == 1, f"Übertragen mit TeddyCloud-Anmeldung, einmal angemeldet ({j['state']}, {j.get('error')}, {l1 - l0} Anmeldungen)")
        drop_taf(A[:8])
        wa(enabled=True, reset=True)  # TeddyCloud neu gestartet: Sitzungen weg
        j = transfer_a()
        l2 = wa(enabled=True)
        check(j["state"] == "done" and l2 - l1 == 1, f"nach Neustart von TeddyCloud neu angemeldet ({j['state']}, {l2 - l1} Anmeldungen)")
        probe = r'''
from app import config, clients
cfg = config.load(); tc = clients.Tc(cfg)
out = []
for _ in range(2):
    try:
        tc.list_dir("")
        out.append("ok")
    except clients.ApiError as e:
        out.append(str(e))
print("RESULT", out)
'''
        penv = dict(env, TC_AUTH="tcuser:falsch", UI_PASS=UI_PW, UI_PASS_HASH="", LISTEN="127.0.0.1:1")
        r = subprocess.run([PY, "-c", probe], cwd=ROOT, env=penv, capture_output=True, text=True, timeout=60)
        l3 = wa(enabled=False)
        check(r.stdout.count("Anmeldung abgelehnt (TC_AUTH prüfen)") == 2 and l3 - l2 == 1,
              f"falsches TC_AUTH: klare Meldung, nur ein Anmeldeversuch (TeddyCloud sperrt nach 5) ({r.stdout[-160:]}{r.stderr[-200:]}, {l3 - l2})")
        check("falsch" not in r.stdout + r.stderr, "Passwort nicht in der Ausgabe")

        applog.flush(); applog.seek(0)
        text = applog.read()
        page = c.get("/").text + str(c.get("/api/config").json()) + str(c.get("/api/jobs").json())
        for secret in ("mocktoken", WEBHOOK_SECRET, TC_PW, ND_PW, UI_PW, ui_hash.rsplit(":", 1)[1]):
            check(secret not in text, f"Geheimnis nicht im Log ({secret[:4]}…)")
            check(secret not in page, f"Geheimnis nicht in der Oberfläche ({secret[:4]}…)")
            check(secret not in json.dumps(httpx.get(f"http://127.0.0.1:{TC_P}/_mock/discord").json()),
                  f"Geheimnis nicht in Discord-Meldungen ({secret[:4]}…)")
        check("Traceback" not in text, "keine unerwarteten Ausnahmen im Log")
    finally:
        for p in procs:
            p.terminate()
    print()
    print("ALLE TESTS OK" if not fails else f"{len(fails)} FEHLGESCHLAGEN: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
