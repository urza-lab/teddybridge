"""Dritte Quelle: Ordner auf Dateiebene, z. B. ein NAS per NFS/SMB (nur lesend).

Das Einhängen macht der Nutzer (am besten nur lesend, z. B. als read-only Bind-Mount oder Docker-Volume mit :ro).
teddybridge liest nur: kein Schreiben, Umbenennen oder Löschen auf der Freigabe.

Ein „Hörspiel/Album“ ist ein Ordner mit Audiodateien. Liegen die Dateien in CD-Unterordnern
(„CD1“, „Disc 2“, „Teil 3“ …), gehören diese zum übergeordneten Ordner.
"""
import base64
import binascii
import hashlib
import os
import re
import stat
import time

from .clients import ApiError
from .i18n import t

AUDIO = {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".wma", ".aif", ".aiff", ".mka"}
IMAGES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
COVER_NAMES = ("cover", "folder", "front", "albumart", "album")
SKIP = {"@eadir", "#recycle", "#snapshot", "$recycle.bin", "system volume information", "lost+found", ".snapshot"}
DISC = re.compile(r"^(cd|disc|disk|teil|part|seite|side|tape|kassette|mc)[\s._-]*\d{1,3}(\b.*)?$", re.IGNORECASE)
MAX_ENTRIES = 3000          # pro Ordner
MAX_COVER = 10 << 20


def natural(name):
    """'Folge 2' vor 'Folge 10'."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def encode_id(rel):
    return base64.urlsafe_b64encode(os.fsencode(rel)).decode().rstrip("=") or "-"


def decode_id(item_id):
    if item_id == "-":
        return ""
    try:
        rel = os.fsdecode(base64.urlsafe_b64decode(item_id + "=" * (-len(item_id) % 4)))
    except (binascii.Error, ValueError):
        raise ApiError(t("ungültige Ordner-ID")) from None
    parts = rel.split("/")
    if rel.startswith("/") or "\x00" in rel or any(p in ("", ".", "..") for p in parts):
        raise ApiError(t("ungültige Ordner-ID"))
    return rel


def shown(name):
    """Anzeigename (nicht dekodierbare Bytes ersetzt)."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def parse_roots(spec):
    """FS_ROOTS='Hörspiele=/mnt/nas/hoerspiele;Musik=/mnt/nas/musik' -> [(name, pfad)]"""
    out = []
    for entry in (spec or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, path = entry.partition("=")
        name, path = name.strip(), path.strip()
        if not sep or not name or not path.startswith("/") or "/" in name:
            raise ValueError("FS_ROOTS: Einträge als Name=/absoluter/pfad, mit ';' getrennt")
        out.append((name, os.path.normpath(path)))
    if len({n for n, _ in out}) != len(out):
        raise ValueError("FS_ROOTS: Namen müssen eindeutig sein")
    return out


class Fs:
    def __init__(self, roots):
        self.roots = roots  # [(name, pfad)]

    # ---------- Pfade

    def root(self, lib):
        if not (lib or "").isdigit() or int(lib) >= len(self.roots):
            raise ApiError(t("unbekannter Ordner"))
        name, path = self.roots[int(lib)]
        if not os.path.isdir(path):
            raise ApiError(t("Freigabe „{name}“ ist nicht erreichbar (nicht eingehängt?)", name=name))
        return name, os.path.realpath(path)

    def _inside(self, root_real, path):
        real = os.path.realpath(path)
        return real if real == root_real or real.startswith(root_real + os.sep) else None

    def resolve(self, lib, item_id):
        """-> (Freigabe-Name, relativer Pfad, absoluter Pfad); nie ausserhalb der Freigabe (auch nicht per Symlink)."""
        name, root_real = self.root(lib)
        rel = decode_id(item_id)
        real = self._inside(root_real, os.path.join(root_real, rel))
        if not real or not os.path.isdir(real):
            raise ApiError(t("Ordner nicht gefunden"))
        return name, rel, real

    def mark(self, lib, item_id):
        """Kennzeichen 's' + 7 Hex-Zeichen (s kommt in ABS-IDs nie vor)."""
        name, _ = self.roots[int(lib)] if (lib or "").isdigit() and int(lib) < len(self.roots) else ("?", "")
        rel = decode_id(item_id)
        return "s" + hashlib.sha1(f"{name}\0{rel}".encode("utf-8", "surrogateescape")).hexdigest()[:7]

    # ---------- Inhalt

    def _scan(self, root_real, path):
        """-> (Unterordner[(name, pfad)], Audiodateien[(name, pfad)], Bilder[(name, pfad)]), sortiert, ohne Versteckte."""
        dirs, audio, images = [], [], []
        try:
            with os.scandir(path) as it:
                for n, e in enumerate(it):
                    if n >= MAX_ENTRIES:
                        break
                    nm = e.name
                    if nm.startswith(".") or nm.casefold() in SKIP:
                        continue
                    real = self._inside(root_real, e.path)
                    if not real:
                        continue  # Symlink nach ausserhalb
                    try:
                        st = os.stat(real)
                    except OSError:
                        continue
                    ext = os.path.splitext(nm)[1].lower()
                    if stat.S_ISDIR(st.st_mode):
                        dirs.append((nm, real))
                    elif stat.S_ISREG(st.st_mode):  # keine FIFOs/Geräte
                        if ext in AUDIO:
                            audio.append((nm, real))
                        elif ext in IMAGES:
                            images.append((nm, real))
        except OSError as e:
            raise ApiError(t("Ordner nicht lesbar ({err})", err=e.strerror or type(e).__name__)) from None
        key = lambda t: natural(t[0])
        return sorted(dirs, key=key), sorted(audio, key=key), sorted(images, key=key)

    def audio_files(self, root_real, path, scan=None):
        """Audiodateien eines Hörspiels in Abspielreihenfolge: direkt im Ordner, dann CD-Unterordner."""
        dirs, audio, _ = scan or self._scan(root_real, path)
        files = [p for _, p in audio]
        for nm, p in dirs:
            if DISC.match(nm):
                files += [q for _, q in self._scan(root_real, p)[1]]
        return files

    def _cover(self, images):
        for nm, p in images:
            if os.path.splitext(nm)[0].casefold() in COVER_NAMES:
                return p
        return images[0][1] if images else None

    def describe(self, lib, root_name, root_real, rel, path, scan=None):
        """Ordner als Kachel. 'item' nur, wenn er Audiodateien hat (direkt oder in CD-Unterordnern)."""
        scan = scan or self._scan(root_real, path)
        dirs, _, images = scan
        files = self.audio_files(root_real, path, scan)
        iid = encode_id(rel)
        parent = os.path.basename(os.path.dirname(rel)) if rel else ""
        cover = self._cover(images)
        if not cover:  # Cover im ersten CD-Ordner
            for nm, p in dirs:
                if DISC.match(nm):
                    cover = self._cover(self._scan(root_real, p)[2])
                    break
        name = shown(os.path.basename(rel) if rel else root_name)
        return {
            "id": iid, "source": "fs", "lib": lib, "title": name, "author": shown(parent), "series": "",
            "duration": 0, "files": len(files), "seq": None,
            "item": bool(files), "mark": self.mark(lib, iid) if files else None,
            "dirs": sum(1 for nm, _ in dirs if not DISC.match(nm)),
            "hasCover": bool(cover), "cover": f"/api/fs/libraries/{lib}/cover/{iid}" if cover else None,
        }

    def browse(self, lib, item_id="-"):
        root_name, root_real = self.root(lib)
        name, rel, path = self.resolve(lib, item_id)
        scan = self._scan(root_real, path)
        here = self.describe(lib, root_name, root_real, rel, path, scan)
        entries = []
        for nm, p in scan[0]:
            if DISC.match(nm) and here["item"]:
                continue  # CD-Ordner gehören zum aktuellen Hörspiel
            sub = f"{rel}/{nm}" if rel else nm
            try:
                entries.append(self.describe(lib, root_name, root_real, sub, p))
            except ApiError:
                continue
        crumbs, acc = [{"id": "-", "name": shown(root_name)}], ""
        for part in (rel.split("/") if rel else []):
            acc = f"{acc}/{part}" if acc else part
            crumbs.append({"id": encode_id(acc), "name": shown(part)})
        return {"here": here, "entries": entries, "crumbs": crumbs}

    def search(self, lib, q, limit=100, max_dirs=20000, max_s=8.0):
        root_name, root_real = self.root(lib)
        q = q.casefold()
        out, todo, seen, t0 = [], [""], 0, time.time()
        while todo and len(out) < limit and seen < max_dirs and time.time() - t0 < max_s:
            rel = todo.pop(0)
            path = os.path.join(root_real, rel) if rel else root_real
            try:
                dirs = self._scan(root_real, path)[0]
            except ApiError:
                continue
            for nm, p in dirs:
                seen += 1
                sub = f"{rel}/{nm}" if rel else nm
                if q in shown(nm).casefold() and not DISC.match(nm):
                    try:
                        out.append(self.describe(lib, root_name, root_real, sub, p))
                    except ApiError:
                        pass
                todo.append(sub)
        return out[:limit]

    def cover(self, lib, item_id):
        root_name, root_real = self.root(lib)
        _, rel, path = self.resolve(lib, item_id)
        d = self.describe(lib, root_name, root_real, rel, path)
        if not d["hasCover"]:
            return None
        scan = self._scan(root_real, path)
        p = self._cover(scan[2])
        if not p:
            for nm, sp in scan[0]:
                if DISC.match(nm):
                    p = self._cover(self._scan(root_real, sp)[2])
                    break
        if not p or os.path.getsize(p) > MAX_COVER:
            return None
        with open(p, "rb") as f:
            return f.read(), IMAGES[os.path.splitext(p)[1].lower()]

    # ---------- für den Worker

    def item(self, lib, item_id):
        """-> (Titel, Ordnerteile für die automatische Ablage, [absolute Dateipfade])"""
        root_name, root_real = self.root(lib)
        _, rel, path = self.resolve(lib, item_id)
        files = self.audio_files(root_real, path)
        title = shown(os.path.basename(rel) if rel else root_name)
        parents = [shown(p) for p in rel.split("/")[:-1]] if rel else []
        return title, [root_name] + parents, files, root_real

    def copy(self, root_real, src, dest):
        """Eine Datei lokal kopieren (nur lesen). Prüft erneut, dass sie in der Freigabe liegt."""
        real = self._inside(root_real, src)
        if not real:
            raise ApiError(t("Datei liegt ausserhalb der Freigabe"))
        try:
            st = os.stat(real)
            if not stat.S_ISREG(st.st_mode):
                raise ApiError(t("{name} ist keine normale Datei", name=shown(os.path.basename(src))))
            with open(real, "rb") as fi, open(dest, "wb") as fo:
                while chunk := fi.read(1 << 20):
                    fo.write(chunk)
        except OSError as e:
            raise ApiError(t("{name} nicht lesbar ({err})", name=shown(os.path.basename(src)), err=e.strerror or type(e).__name__)) from None
        if os.path.getsize(dest) != st.st_size:
            raise ApiError(t("{name}: unvollständig gelesen", name=shown(os.path.basename(src))))
