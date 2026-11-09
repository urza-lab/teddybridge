#!/usr/bin/env python3
"""Einrichtung im Browser (1.2) gegen die Mock-Server: Start ohne Konfiguration -> Einrichtung mit Einmal-Code,
Verbindungstests, Speichern (Rechte 600, Hash statt Passwort), Neustart in die normale Oberfläche.

Start:  venv/bin/python tests/test_setup.py
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
ABS_P, TC_P, ND_P, APP_P = 27378, 27088, 27533, 27085
TC_PW, UI_PW = "tcpass-Setup$9", "uipass-setup-42"
FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        FAILS.append(msg)


def wait(url, cond=lambda r: True, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(url, timeout=2)
            if cond(r):
                return r
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"keine Antwort: {url}")


def main():
    tmp = tempfile.mkdtemp(prefix="tb-setup-")
    cfgdir = os.path.join(tmp, "config")
    os.makedirs(cfgdir)
    cfgfile = os.path.join(cfgdir, "teddybridge.env")
    nas = os.path.join(tmp, "nas", "hsp")
    os.makedirs(nas)
    base = f"http://127.0.0.1:{APP_P}"
    env = {"PATH": os.environ["PATH"], "TB_CONFIG_FILE": cfgfile, "LISTEN": f"127.0.0.1:{APP_P}",
           "TC_BASE": "von-der-umgebung"}
    procs = []
    log = open(os.path.join(tmp, "app.log"), "w+")
    try:
        procs.append(subprocess.Popen([PY, "mock/mock_servers.py", "--data", os.path.join(tmp, "lib"), "--clean",
                                       "--abs-port", str(ABS_P), "--tc-port", str(TC_P), "--nd-port", str(ND_P),
                                       "--tc-auth", f"tcuser:{TC_PW}"], cwd=ROOT))
        app = subprocess.Popen([PY, "-m", "app"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        procs.append(app)
        wait(f"http://127.0.0.1:{TC_P}/_mock/log")
        wait(f"{base}/api/setup/ping")

        print("Start ohne Konfiguration")
        check(httpx.get(f"{base}/api/setup/ping").json() == {"setup": True}, "Einrichtung statt Abbruch")
        r = httpx.get(f"{base}/login", follow_redirects=False)
        check(r.status_code == 303 and r.headers["location"] == "/setup", "/login (Healthcheck) führt zur Einrichtung")
        check(httpx.get(f"{base}/", follow_redirects=True).status_code == 200, "Startseite -> Einrichtung 200")
        r = httpx.get(f"{base}/setup")
        check("frame-ancestors 'none'" in r.headers.get("content-security-policy", "") and r.headers.get("x-frame-options") == "DENY",
              "Sicherheits-Header auf der Einrichtung")
        log.flush(); log.seek(0)
        m = re.search(r"Einmal-Code eingeben:\s+([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})", log.read())
        check(bool(m), "Einmal-Code steht im Log")
        code = m.group(1) if m else ""

        print("Einmal-Code")
        check(httpx.get(f"{base}/api/setup/state").status_code == 401, "ohne Code kein Zugriff")
        check(httpx.post(f"{base}/api/setup/save", json={}).status_code == 401, "ohne Code kein Speichern")
        t0 = time.time()
        r = httpx.post(f"{base}/api/setup/unlock", json={"code": "AAAA-BBBB-CCCC"})
        check(r.status_code == 403 and time.time() - t0 >= 0.9, "falscher Code abgelehnt und gebremst")
        c = httpx.Client(base_url=base, timeout=30, headers={"Accept-Language": "de"})
        r = c.post("/api/setup/unlock", json={"code": code.lower().replace("-", " ")})
        check(r.status_code == 200 and "tb_setup" in r.cookies, "Code (auch klein/mit Leerzeichen) schaltet frei")
        st = c.get("/api/setup/state").json()
        check(st["writable"] and st["fixed"] == ["TC_BASE"], f"Zustand: beschreibbar, TC_BASE fest von der Umgebung ({st['fixed']})")
        check(st["values"]["TC_BASE"] == "von-der-umgebung", "fester Wert wird angezeigt")

        print("Verbindungstests")
        tc = {"TC_URL": f"http://127.0.0.1:{TC_P}"}
        test = lambda kind, **v: c.post("/api/setup/test", json={"kind": kind, "values": v}).json()
        r = test("tc", TC_URL=f"http://127.0.0.1:{TC_P + 7}")
        check(not r["ok"] and "Verbindungsfehler" in r["message"], f"TeddyCloud nicht erreichbar ({r['message']})")
        r = test("tc", **tc)
        check(not r["ok"] and "verlangt eine Anmeldung" in r["message"], f"TeddyCloud ohne Zugangsdaten ({r['message']})")
        r = test("tc", **tc, tcUser="tcuser", tcPass=TC_PW)
        check(r["ok"], f"TeddyCloud mit Basic Auth ({r['message']})")
        httpx.post(f"http://127.0.0.1:{TC_P}/_mock/web-auth", json={"enabled": True, "reset": True})
        r = test("tc", **tc, tcUser="tcuser", tcPass=TC_PW)
        check(r["ok"], f"TeddyCloud mit eigener Anmeldung ({r['message']})")
        r = test("tc", **tc, tcUser="tcuser", tcPass="falsch")
        check(not r["ok"] and "Anmeldung abgelehnt" in r["message"], f"TeddyCloud falsches Passwort ({r['message']})")
        r = test("tc", TC_URL="ftp://x")
        check(not r["ok"] and "http://" in r["message"], "ungültige Adresse")
        r = test("abs", ABS_URL=f"http://127.0.0.1:{ABS_P}", ABS_TOKEN="mocktoken")
        check(r["ok"] and "Bibliotheken" in r["message"], f"Audiobookshelf ({r['message']})")
        r = test("abs", ABS_URL=f"http://127.0.0.1:{ABS_P}", ABS_TOKEN="falsch")
        check(not r["ok"], f"Audiobookshelf falscher Token ({r['message']})")
        r = test("nd", ND_URL=f"http://127.0.0.1:{ND_P}", ND_USER="nduser", ND_PASS="ndpassword55")
        check(r["ok"], f"Navidrome ({r['message']})")
        r = test("fs", FS_ROOTS=f"Hsp={nas}")
        check(r["ok"], f"Ordner ({r['message']})")
        r = test("fs", FS_ROOTS="Weg=/gibt/es/nicht")
        check(not r["ok"], f"Ordner fehlt ({r['message']})")
        r = c.post("/api/setup/test", headers={"Accept-Language": "en"}, json={"kind": "fs", "values": {"FS_ROOTS": "Weg=/gibt/es/nicht"}}).json()
        check("not readable" in r["message"], "Meldungen englisch")

        print("Speichern")
        good = {**tc, "tcUser": "tcuser", "tcPass": TC_PW, "useAbs": True, "ABS_URL": f"http://127.0.0.1:{ABS_P}",
                "ABS_TOKEN": "mocktoken", "UI_USER": "admin", "uiPass": UI_PW, "uiPass2": UI_PW, "UI_LANG": "de"}
        r = c.post("/api/setup/save", json={**good, "useAbs": False})
        check(r.status_code == 400 and "Quelle" in r.json()["error"], "ohne Quelle abgelehnt")
        r = c.post("/api/setup/save", json={**good, "uiPass2": "anders"})
        check(r.status_code == 400 and "überein" in r.json()["error"], "Passwörter verschieden abgelehnt")
        r = c.post("/api/setup/save", json={**good, "uiPass": "kurz", "uiPass2": "kurz"})
        check(r.status_code == 400, "zu kurzes Passwort abgelehnt")
        r = c.post("/api/setup/save", json={**good, "FRAME_ANCESTORS": "*"})
        check(r.status_code == 400 and "FRAME_ANCESTORS" in r.json()["error"], "ungültiger Wert: Prüfung wie beim Start")
        r = c.post("/api/setup/save", json={**good, "TC_URL": "http://x\nUI_USER=evil"})
        check(r.status_code == 400 and not os.path.exists(cfgfile), "Zeilenumbruch abgelehnt, nichts geschrieben")
        pid = app.pid
        r = c.post("/api/setup/save", json=good)
        check(r.status_code == 200, f"gültige Einrichtung gespeichert ({r.text[:120]})")
        mode = stat.S_IMODE(os.stat(cfgfile).st_mode) if os.path.exists(cfgfile) else None
        check(mode == 0o600, f"Datei mit Rechten 600 ({oct(mode or 0)})")
        text = open(cfgfile).read() if mode else ""
        check("UI_PASS_HASH=scrypt:" in text and UI_PW not in text, "Login als Hash, Passwort nicht im Klartext")
        check(f"TC_AUTH=tcuser:{TC_PW}" in text and "ABS_TOKEN=mocktoken" in text, "Zugangsdaten gespeichert")
        check("TC_BASE" not in text, "Wert aus der Umgebung nicht in die Datei geschrieben")

        print("Neustart in die normale Oberfläche")
        wait(f"{base}/api/setup/ping", lambda r: r.status_code == 401, timeout=30)
        check(app.poll() is None and app.pid == pid, "derselbe Prozess läuft weiter (execv)")
        ui = httpx.Client(base_url=base, timeout=30, auth=("admin", UI_PW))
        cfg = ui.get("/api/config").json()
        check(cfg.get("sources") == ["abs"] and cfg.get("tcBase") == "von-der-umgebung" and cfg.get("version"),
              f"läuft mit neuer Konfiguration, Umgebung hat Vorrang ({cfg.get('sources')}, {cfg.get('tcBase')})")
        check(httpx.get(f"{base}/api/config", auth=("admin", "falsch")).status_code == 401, "Login aktiv")
        libs = ui.get("/api/libraries")
        check(libs.status_code == 200, "ABS über die neue Konfiguration")
        n0 = httpx.post(f"http://127.0.0.1:{TC_P}/_mock/web-auth", json={"enabled": True}).json()["logins"]
        st = ui.get("/api/present")
        tcl = httpx.get(f"http://127.0.0.1:{TC_P}/_mock/log").json()
        check(st.status_code == 200 and any(e["path"].startswith("/api/fileIndex") and e["status"] == 200 for e in tcl[-20:]),
              "TeddyCloud mit eigener Anmeldung nach dem Neustart erreichbar")
        r = httpx.get(f"{base}/setup", follow_redirects=False)
        check(r.status_code != 200 or "Einmal-Code" not in r.text, "Einrichtung danach nicht mehr erreichbar")

        log.flush(); log.seek(0)
        text = log.read()
        check(TC_PW not in text and UI_PW not in text and "mocktoken" not in text, "keine Geheimnisse im Log")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    print("Umgebung hat Vorrang (Datei)")
    sys.path.insert(0, ROOT)
    from app import config
    f2 = os.path.join(tmp, "x.env")
    config.write_config_file(f2, {"TC_URL": "http://aus-datei", "TC_BASE": "datei", "ABS_URL": "http://abs"})
    old = dict(os.environ)
    try:
        os.environ.update(TC_BASE="umgebung", ABS_URL="")
        config.apply_config_file(f2)
        check(os.environ["TC_BASE"] == "umgebung" and os.environ["TC_URL"] == "http://aus-datei"
              and os.environ["ABS_URL"] == "http://abs", "gesetzte Umgebung gewinnt, leere wird aus der Datei gefüllt")
    finally:
        os.environ.clear(); os.environ.update(old)
    try:
        config.write_config_file(f2, {"TC_URL": "a\nb"})
        check(False, "Zeilenumbruch beim Schreiben abgelehnt")
    except ValueError:
        check(True, "Zeilenumbruch beim Schreiben abgelehnt")

    print("ALLE TESTS OK" if not FAILS else f"{len(FAILS)} FEHLGESCHLAGEN: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
