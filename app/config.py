"""Konfiguration aus Umgebungsvariablen (systemd: EnvironmentFile=/etc/teddybridge/teddybridge.env)."""
import ipaddress
import os
import re
from urllib.parse import urlsplit
from dataclasses import dataclass

from . import fs, pwhash


@dataclass(frozen=True)
class Config:
    abs_url: str
    abs_token: str
    tc_url: str
    tc_auth: tuple | None
    tc_base: str
    discord_webhook: str
    host: str
    port: int
    nd_url: str = ""
    nd_user: str = ""
    nd_pass: str = ""
    ui_user: str = ""
    ui_pass: str = ""
    ui_pass_hash: str = ""
    trusted_proxies: tuple = ()
    fs_roots: tuple = ()  # ((Name, Pfad), ...) – Dateiquelle, z. B. NAS per NFS/SMB
    tc_base_extra: tuple = ()  # weitere Ordner, die nur auf vorhandene TAFs durchsucht werden (nie beschrieben)
    ui_lang: str = ""  # "de"/"en" = fest; leer = Browser-Sprache, umschaltbar in der Oberfläche
    frame_ancestors: tuple = ()  # Origins, die teddybridge in einem Rahmen zeigen dürfen (z. B. TeddyCloud)

    @property
    def tc_scan_bases(self):
        """Alle Ordner für die Duplikaterkennung: TC_BASE zuerst, dann TC_BASE_EXTRA."""
        return (self.tc_base,) + tuple(b for b in self.tc_base_extra if b != self.tc_base)

    @property
    def ui_auth(self):
        return bool(self.ui_user and (self.ui_pass or self.ui_pass_hash))

    @property
    def abs_enabled(self):
        return bool(self.abs_url and self.abs_token)

    @property
    def nd_enabled(self):
        return bool(self.nd_url and self.nd_user and self.nd_pass)

    def secrets(self):
        """Alle Werte, die nie in Logs/Ausgaben auftauchen dürfen."""
        s = [self.abs_token, self.discord_webhook, self.nd_pass, self.ui_pass, self.ui_pass_hash]
        if self.tc_auth:
            s += [self.tc_auth[1], f"{self.tc_auth[0]}:{self.tc_auth[1]}"]
        return [x for x in s if x and len(x) >= 4]


class MissingConfig(SystemExit):
    """TeddyCloud-Adresse oder Quelle fehlt: mit TB_CONFIG_FILE startet dann die Einrichtung im Browser."""


# Schlüssel, die die Einrichtung (Weboberfläche) in TB_CONFIG_FILE schreiben darf
SETUP_KEYS = ("TC_URL", "TC_AUTH", "TC_BASE", "ABS_URL", "ABS_TOKEN", "ND_URL", "ND_USER", "ND_PASS", "FS_ROOTS",
              "UI_USER", "UI_PASS_HASH", "UI_LANG", "FRAME_ANCESTORS", "DISCORD_WEBHOOK", "TRUSTED_PROXIES")


def read_config_file(path):
    """KEY=VALUE-Datei der Einrichtung -> dict (nur SETUP_KEYS). Fehlt die Datei: leer."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k in SETUP_KEYS:
                    out[k] = v
    except FileNotFoundError:
        pass
    return out


def apply_config_file(path):
    """Werte aus der Datei übernehmen, ausser die Umgebung setzt sie (nicht leer): Umgebung hat Vorrang."""
    for k, v in read_config_file(path).items():
        if not os.environ.get(k, "").strip():
            os.environ[k] = v


def write_config_file(path, values):
    """Atomar schreiben, Rechte 600. Werte ohne Anführungszeichen, eine Zeile je Schlüssel."""
    lines = ["# teddybridge – von der Einrichtung im Browser geschrieben. Umgebungsvariablen haben Vorrang.\n"]
    for k in SETUP_KEYS:
        v = values.get(k, "")
        if any(ch in v for ch in "\r\n\0"):
            raise ValueError(k)
        if v:
            lines.append(f"{k}={v}\n")
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_env_file(path):
    """KEY=VALUE-Datei einlesen (nur für manuelle Starts; systemd nutzt EnvironmentFile).
    Bereits gesetzte Variablen werden nicht überschrieben. Werte werden nie ausgegeben."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)


def load(env=None) -> Config:
    """Konfiguration prüfen und laden. env: eigene Werte (Einrichtung) statt der Umgebung."""
    if env is None:
        if os.environ.get("TB_ENV_FILE"):
            _load_env_file(os.environ["TB_ENV_FILE"])
        if os.environ.get("TB_CONFIG_FILE"):
            apply_config_file(os.environ["TB_CONFIG_FILE"])
        env = os.environ
    if not env.get("TC_URL", "").strip():
        raise MissingConfig("Fehlende Konfiguration: TC_URL")
    # Quellen: mindestens eine von ABS, Navidrome, Dateiquelle. Halb ausgefüllt = Tippfehler -> Startabbruch.
    for name, keys in (("Audiobookshelf", ("ABS_URL", "ABS_TOKEN")), ("Navidrome", ("ND_URL", "ND_USER", "ND_PASS"))):
        have = [k for k in keys if env.get(k, "").strip()]
        if have and len(have) < len(keys):
            raise SystemExit(f"{name}: {', '.join(k for k in keys if k not in have)} fehlt "
                             f"(entweder alle von {', '.join(keys)} setzen oder keine)")
    ui_user = env.get("UI_USER", "").strip()
    ui_pass = env.get("UI_PASS", "").strip()
    ui_hash = env.get("UI_PASS_HASH", "").strip()
    if ui_hash:
        try:
            pwhash.parse(ui_hash)
        except ValueError as e:
            raise SystemExit(f"Login: {e}") from None
        ui_pass = ""  # Hash hat Vorrang, Klartext wird ignoriert
    if bool(ui_user) != bool(ui_pass or ui_hash):
        raise SystemExit("Login: UI_USER und UI_PASS_HASH (oder UI_PASS) müssen beide gesetzt oder beide leer sein")
    if ui_pass and len(ui_pass) < pwhash.MIN_LEN:
        raise SystemExit(f"Login: UI_PASS muss mindestens {pwhash.MIN_LEN} Zeichen haben")
    proxies = []
    for x in env.get("TRUSTED_PROXIES", "").replace(" ", "").split(","):
        if x:
            try:
                proxies.append(str(ipaddress.ip_address(x)))
            except ValueError:
                raise SystemExit("TRUSTED_PROXIES: nur einzelne IP-Adressen, mit Komma getrennt") from None
    try:
        fs_roots = tuple(fs.parse_roots(env.get("FS_ROOTS", "")))
    except ValueError as e:
        raise SystemExit(str(e)) from None
    extra = []
    for x in env.get("TC_BASE_EXTRA", "").split(","):
        x = x.strip().strip("/")
        if not x:
            continue
        parts = x.split("/")
        if any(p in ("", ".", "..") or p.startswith("_src_") or any(ord(ch) < 32 for ch in p) for p in parts):
            raise SystemExit("TC_BASE_EXTRA: relative Ordner der TeddyCloud-Bibliothek, mit Komma getrennt")
        extra.append(x)
    if not (env.get("ABS_URL", "").strip() or env.get("ND_URL", "").strip() or fs_roots):
        raise MissingConfig("Keine Quelle konfiguriert: mindestens eine von Audiobookshelf (ABS_URL+ABS_TOKEN), "
                         "Navidrome (ND_URL+ND_USER+ND_PASS) oder Dateiquelle (FS_ROOTS) einrichten")
    ui_lang = env.get("UI_LANG", "").strip().lower()
    if ui_lang == "auto":
        ui_lang = ""
    if ui_lang not in ("", "de", "en"):
        raise SystemExit("UI_LANG: de, en oder leer (= Sprache des Browsers, umschaltbar)")
    frames = []
    for x in env.get("FRAME_ANCESTORS", "").replace(" ", ",").split(","):
        x = x.strip().rstrip("/")
        if not x:
            continue
        u = urlsplit(x)
        if (u.scheme not in ("http", "https") or not u.hostname or u.path or u.query or u.fragment
                or u.username or u.password or "*" in x or not re.fullmatch(r"[A-Za-z0-9.:\[\]/-]+", x)):
            raise SystemExit("FRAME_ANCESTORS: Adressen wie http://teddycloud.example oder http://192.0.2.10:8080, "
                             "ohne Pfad, mit Komma getrennt")
        try:
            u.port
        except ValueError:
            raise SystemExit("FRAME_ANCESTORS: ungültiger Port") from None
        frames.append(f"{u.scheme}://{u.netloc.lower()}")
    auth = env.get("TC_AUTH", "").strip()
    tc_auth = tuple(auth.split(":", 1)) if ":" in auth else None
    listen = env.get("LISTEN", "0.0.0.0:8085")
    host, _, port = listen.rpartition(":")
    return Config(
        abs_url=env.get("ABS_URL", "").strip().rstrip("/"),
        abs_token=env.get("ABS_TOKEN", "").strip(),
        tc_url=env["TC_URL"].rstrip("/"),
        tc_auth=tc_auth,
        tc_base=env.get("TC_BASE", "teddybridge").strip().strip("/") or "teddybridge",
        discord_webhook=env.get("DISCORD_WEBHOOK", "").strip(),
        host=host or "0.0.0.0",
        port=int(port or 8085),
        nd_url=env.get("ND_URL", "").strip().rstrip("/"),
        nd_user=env.get("ND_USER", "").strip(),
        nd_pass=env.get("ND_PASS", "").strip(),
        ui_user=ui_user,
        ui_pass=ui_pass,
        ui_pass_hash=ui_hash,
        trusted_proxies=tuple(proxies),
        fs_roots=fs_roots,
        tc_base_extra=tuple(extra),
        ui_lang=ui_lang,
        frame_ancestors=tuple(dict.fromkeys(frames)),
    )
