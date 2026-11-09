"""Konfiguration aus Umgebungsvariablen (systemd: EnvironmentFile=/etc/teddybridge/teddybridge.env)."""
import ipaddress
import os
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


def load() -> Config:
    if os.environ.get("TB_ENV_FILE"):
        _load_env_file(os.environ["TB_ENV_FILE"])
    if not os.environ.get("TC_URL", "").strip():
        raise SystemExit("Fehlende Konfiguration: TC_URL")
    # Quellen: mindestens eine von ABS, Navidrome, Dateiquelle. Halb ausgefüllt = Tippfehler -> Startabbruch.
    for name, keys in (("Audiobookshelf", ("ABS_URL", "ABS_TOKEN")), ("Navidrome", ("ND_URL", "ND_USER", "ND_PASS"))):
        have = [k for k in keys if os.environ.get(k, "").strip()]
        if have and len(have) < len(keys):
            raise SystemExit(f"{name}: {', '.join(k for k in keys if k not in have)} fehlt "
                             f"(entweder alle von {', '.join(keys)} setzen oder keine)")
    ui_user = os.environ.get("UI_USER", "").strip()
    ui_pass = os.environ.get("UI_PASS", "").strip()
    ui_hash = os.environ.get("UI_PASS_HASH", "").strip()
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
    for x in os.environ.get("TRUSTED_PROXIES", "").replace(" ", "").split(","):
        if x:
            try:
                proxies.append(str(ipaddress.ip_address(x)))
            except ValueError:
                raise SystemExit("TRUSTED_PROXIES: nur einzelne IP-Adressen, mit Komma getrennt") from None
    try:
        fs_roots = tuple(fs.parse_roots(os.environ.get("FS_ROOTS", "")))
    except ValueError as e:
        raise SystemExit(str(e)) from None
    extra = []
    for x in os.environ.get("TC_BASE_EXTRA", "").split(","):
        x = x.strip().strip("/")
        if not x:
            continue
        parts = x.split("/")
        if any(p in ("", ".", "..") or p.startswith("_src_") or any(ord(ch) < 32 for ch in p) for p in parts):
            raise SystemExit("TC_BASE_EXTRA: relative Ordner der TeddyCloud-Bibliothek, mit Komma getrennt")
        extra.append(x)
    if not (os.environ.get("ABS_URL", "").strip() or os.environ.get("ND_URL", "").strip() or fs_roots):
        raise SystemExit("Keine Quelle konfiguriert: mindestens eine von Audiobookshelf (ABS_URL+ABS_TOKEN), "
                         "Navidrome (ND_URL+ND_USER+ND_PASS) oder Dateiquelle (FS_ROOTS) einrichten")
    ui_lang = os.environ.get("UI_LANG", "").strip().lower()
    if ui_lang == "auto":
        ui_lang = ""
    if ui_lang not in ("", "de", "en"):
        raise SystemExit("UI_LANG: de, en oder leer (= Sprache des Browsers, umschaltbar)")
    auth = os.environ.get("TC_AUTH", "").strip()
    tc_auth = tuple(auth.split(":", 1)) if ":" in auth else None
    listen = os.environ.get("LISTEN", "0.0.0.0:8085")
    host, _, port = listen.rpartition(":")
    return Config(
        abs_url=os.environ.get("ABS_URL", "").strip().rstrip("/"),
        abs_token=os.environ.get("ABS_TOKEN", "").strip(),
        tc_url=os.environ["TC_URL"].rstrip("/"),
        tc_auth=tc_auth,
        tc_base=os.environ.get("TC_BASE", "teddybridge").strip().strip("/") or "teddybridge",
        discord_webhook=os.environ.get("DISCORD_WEBHOOK", "").strip(),
        host=host or "0.0.0.0",
        port=int(port or 8085),
        nd_url=os.environ.get("ND_URL", "").strip().rstrip("/"),
        nd_user=os.environ.get("ND_USER", "").strip(),
        nd_pass=os.environ.get("ND_PASS", "").strip(),
        ui_user=ui_user,
        ui_pass=ui_pass,
        ui_pass_hash=ui_hash,
        trusted_proxies=tuple(proxies),
        fs_roots=fs_roots,
        tc_base_extra=tuple(extra),
        ui_lang=ui_lang,
    )
