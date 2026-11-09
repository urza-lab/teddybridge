"""Gemeinsame Logik für die Einrichtung (app/setup.py) und die Einstellungsseite (app/main.py):
Werte aus TB_CONFIG_FILE lesen, Eingaben prüfen, Verbindungen testen, speichern, neu starten.

Geheimnisse (Passwörter, Token, Webhook) gehen nie an den Browser zurück, nur „gesetzt“. Leer gelassene
Geheimnisse behalten ihren bisherigen Wert. Gesetzte Umgebungsvariablen haben Vorrang und sind „fest“.
"""
import asyncio
import contextvars
import logging
import os
import secrets
import sys
import time
import types
import urllib.parse

import httpx

from . import config, fs, pwhash
from .clients import Abs, ApiError, Nd, Tc
from .i18n import t

log = logging.getLogger("teddybridge")

SECRET_KEYS = ("TC_AUTH", "ABS_TOKEN", "ND_PASS", "UI_PASS_HASH", "DISCORD_WEBHOOK")
SHOWN_KEYS = ("TC_URL", "TC_BASE", "ABS_URL", "ND_URL", "ND_USER", "FS_ROOTS", "UI_USER", "UI_LANG",
              "FRAME_ANCESTORS", "TRUSTED_PROXIES")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O, 1/I: gut abzutippen; 12 Zeichen = 60 Bit


def new_code():
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))


def norm_code(s):
    return "".join(ch for ch in str(s or "").upper() if ch.isalnum())


class Store:
    def __init__(self, path):
        self.path = path or ""
        self.file = config.read_config_file(self.path) if self.path else {}
        # Von der Umgebung gesetzt (nicht aus der Datei): hier nicht änderbar
        self.fixed = tuple(k for k in config.SETUP_KEYS
                           if os.environ.get(k, "").strip() and k not in config.FROM_FILE
                           and os.environ.get(k) != self.file.get(k))

    def writable(self):
        d = os.path.dirname(os.path.abspath(self.path)) if self.path else ""
        if not d or not os.path.isdir(d) or not os.access(d, os.W_OK):
            return False
        return not os.path.exists(self.path) or os.access(self.path, os.W_OK)

    def current(self, k):
        """Aktueller Wert: Umgebung (falls fest) oder Datei."""
        return os.environ.get(k, "").strip() if k in self.fixed else self.file.get(k, "")

    def state(self):
        auth = self.current("TC_AUTH")
        return {
            "path": self.path,
            "writable": self.writable(),
            "fixed": list(self.fixed),
            "values": {k: self.current(k) for k in SHOWN_KEYS},
            "secrets": {k: bool(self.current(k)) for k in SECRET_KEYS},
            "tcUser": auth.split(":", 1)[0] if ":" in auth else "",
            "minPass": pwhash.MIN_LEN,
        }

    def resolve(self, d):
        """Eingaben aus dem Browser -> Werte für die Datei. Leere Geheimnisse = bisherigen Wert behalten."""
        cur = self.current
        out = {"TC_URL": _url(d.get("TC_URL"))}
        tc_user, tc_pass = _s(d.get("tcUser"), 200), _s(d.get("tcPass"), 500)
        if ":" in tc_user:
            raise ValueError(t("TeddyCloud-Benutzer darf keinen Doppelpunkt enthalten"))
        if tc_user:
            old = cur("TC_AUTH")
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
            out["ABS_TOKEN"] = _s(d.get("ABS_TOKEN"), 4000) or cur("ABS_TOKEN")
        else:
            out["ABS_URL"] = out["ABS_TOKEN"] = ""
        if d.get("useNd"):
            out["ND_URL"] = _url(d.get("ND_URL"))
            out["ND_USER"] = _s(d.get("ND_USER"), 200)
            out["ND_PASS"] = _s(d.get("ND_PASS"), 500) or (cur("ND_PASS") if out["ND_USER"] == cur("ND_USER") else "")
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
            elif cur("UI_PASS_HASH") and user == cur("UI_USER"):
                out["UI_PASS_HASH"] = cur("UI_PASS_HASH")
            else:
                raise ValueError(t("Login: Passwort fehlt"))
            out["UI_USER"] = user
        out["UI_LANG"] = _s(d.get("UI_LANG"), 10)
        out["FRAME_ANCESTORS"] = _s(d.get("FRAME_ANCESTORS"), 1000)
        out["TRUSTED_PROXIES"] = _s(d.get("TRUSTED_PROXIES"), 1000)
        out["DISCORD_WEBHOOK"] = _s(d.get("DISCORD_WEBHOOK"), 1000) or cur("DISCORD_WEBHOOK")
        for k in self.fixed:  # Umgebung hat Vorrang
            out.pop(k, None)
        return out

    def effective(self, values):
        """Wie beim Start: Umgebung (fest) + neue Werte; für config.load(env=...)."""
        env = {k: v for k, v in os.environ.items() if k not in config.SETUP_KEYS or k in self.fixed}
        env.update({k: v for k, v in values.items() if k not in self.fixed})
        return env

    def for_test(self, d, kind):
        data = dict(d or {})
        data.update(useAbs=kind == "abs" or data.get("useAbs"), useNd=kind == "nd" or data.get("useNd"),
                    useFs=kind == "fs" or data.get("useFs"), noLogin=True)
        v = self.resolve(data)
        for k in self.fixed:
            v[k] = os.environ.get(k, "").strip()
        return v

    def save(self, payload):
        """-> (HTTP-Status, Antwort). Bei 200 ist die Datei geschrieben."""
        if not self.writable():
            return 409, {"error": t("{path} ist nicht beschreibbar (Docker: Volume für /config einhängen)", path=self.path)}
        try:
            values = self.resolve(payload)
            config.load(env=self.effective(values))  # dieselben Prüfungen wie beim Start
        except (ValueError, SystemExit) as e:
            return 400, {"error": _short(e)}
        try:
            config.write_config_file(self.path, values)
        except (OSError, ValueError) as e:
            log.error("Konfiguration: Speichern fehlgeschlagen (%s)", type(e).__name__)
            return 500, {"error": t("Speichern fehlgeschlagen ({name})", name=type(e).__name__)}
        self.file = {k: v for k, v in values.items() if v}
        log.info("Konfiguration gespeichert (%s), starte neu", ", ".join(k for k, v in values.items() if v))
        return 200, {"ok": True}


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


def _short(e):
    return (str(e) or type(e).__name__)[:300]


# ---------- Verbindungstests (blockierend, im Thread)

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
        return None, t("Antwort ist keine TeddyCloud-Bibliothek – Adresse prüfen")
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


async def run_test(store, d):
    """Verbindungstest aus dem Browser -> {ok, message}."""
    kind = (d or {}).get("kind")
    if kind not in TESTS:
        return {"ok": False, "message": "kind"}
    try:
        v = store.for_test((d or {}).get("values"), kind)
        ctx = contextvars.copy_context()  # Sprache der Anfrage in den Thread mitnehmen
        res = await asyncio.get_running_loop().run_in_executor(None, ctx.run, TESTS[kind], v)
    except (ValueError, ApiError) as e:
        return {"ok": False, "message": _short(e)}
    except Exception as e:  # ein Test darf nie die Seite abbrechen
        log.warning("Konfiguration: Test %s: %s", kind, type(e).__name__)
        return {"ok": False, "message": t("Fehler: {name}", name=type(e).__name__)}
    if isinstance(res, tuple):
        return {"ok": False, "message": res[1]}
    return {"ok": True, "message": res}


def restart_later(delay=1.0):
    """Nach der Antwort den Prozess durch sich selbst ersetzen (gleiche PID, liest die neue Konfiguration)."""
    def run():
        time.sleep(delay)
        for h in logging.getLogger().handlers:
            h.flush()
        sys.stdout.flush()
        sys.stderr.flush()
        # nur die echte Umgebung mitgeben: aus der Datei übernommene Werte liest der neue Prozess selbst wieder ein
        os.execve(sys.executable, [sys.executable, "-m", "app"], config.original_environ())
    import threading
    threading.Thread(target=run, daemon=True).start()
