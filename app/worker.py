"""Warteschlange: ein Worker-Thread, ein Hörspiel nach dem anderen."""
import hashlib
import itertools
import logging
import math
import os
import queue
import re
import tempfile
import threading
import time

import httpx

from . import chapters, mp3cat
from .clients import ApiError
from .fs import shown
from . import i18n
from .i18n import t

log = logging.getLogger("teddybridge")

MAX_FILES = 99
BAD_CHARS = re.compile(r'[\\/:*?"<>|]')
CTRL = re.compile(r"[\x00-\x1f\x7f]")
MARKER = re.compile(r"\[([^\[\]/]{8})(?:-(\d{1,2}))?\]\.taf$", re.IGNORECASE)
FINAL = ("done", "skipped", "error")


def clean_name(s, maxlen=120):
    s = CTRL.sub("", BAD_CHARS.sub("-", s or "")).strip()
    s = s[:maxlen].rstrip(" .")
    return s or t("Hoerspiel")


def clean_folder(path):
    """Relativer Unterordner, ohne '..', ohne leere Teile, nie ein eigener _src_-Ordner."""
    parts = [p.strip() for p in (path or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    if not parts or any(p in (".", "..") or CTRL.search(p) or p.startswith("_src_") for p in parts):
        raise ApiError(t("ungültiger Zielordner"))
    return "/".join(clean_name(p, 80) for p in parts)


def id8(item_id, source="abs", kind="album", songs=None):
    """Kennzeichen im Dateinamen, immer 8 Zeichen. ABS: erste 8 Zeichen der (hexadezimalen) ID.
    Navidrome mit Präfix, das in Hex nie vorkommt: 'n' Album, 'p' Navidrome-Playlist (+ 7 Zeichen
    der ID), 'm' eigene Playlist (+ 7 Zeichen eines Hashes über die Titelfolge).
    Dateiquelle: 's' + Hash über Freigabe und Pfad (siehe fs.Fs.mark)."""
    if source != "nd":
        return item_id[:8]
    if kind == "songs":
        return "m" + hashlib.sha1(",".join(songs or []).encode()).hexdigest()[:7]
    return ("p" if kind == "playlist" else "n") + item_id[:7]


def find_marker(tc, base, short, max_depth=6):
    """Wo liegt die TAF mit diesem Kennzeichen? -> [(ordner, name)] (für die Prüfung auf halbe TAFs)."""
    out, todo = [], [(base, 0)]
    while todo:
        rel, depth = todo.pop()
        for f in tc.list_dir(rel) or []:
            n = f.get("name", "")
            if f.get("isDirectory"):
                if depth < max_depth and not n.startswith("_src_"):
                    todo.append((f"{rel}/{n}", depth + 1))
            else:
                m = MARKER.search(n)
                if m and m.group(1) == short and not m.group(2):
                    out.append((rel, n))
    return out


class Reserved(Exception):
    """Die Figur ist schon bei einem anderen offenen Auftrag vorgemerkt (Text = dessen Titel)."""


def markers(names):
    """Kennzeichen aus TAF-Namen: 'abcd1234' (ganzes Hörspiel) bzw. 'abcd1234-2' (Teil 2)."""
    out = set()
    for n in names:
        m = MARKER.search(n)
        if m:
            out.add(m.group(1) + (f"-{int(m.group(2))}" if m.group(2) else ""))
    return out


def scan_tree(tc, base, max_depth=6):
    """Alle TAF-Dateinamen unterhalb von base (ohne _src_-Ordner)."""
    return [n for _, n in scan_paths(tc, base, max_depth)]


def own_tafs(paths, short):
    """Pfade („ordner/name.taf“) der TAFs mit diesem Kennzeichen, ganze zuerst, Teile nach Nummer."""
    hits = []
    for d, n in paths:
        m = MARKER.search(n)
        if m and m.group(1) == short:
            hits.append((int(m.group(2) or 0), f"{d}/{n}"))
    return [p for _, p in sorted(hits)]


def scan_paths(tc, base, max_depth=6):
    """Alle TAFs unterhalb von base (ohne _src_-Ordner) als [(ordner, name)]."""
    names, todo = [], [(base, 0)]
    while todo:
        rel, depth = todo.pop()
        entries = tc.list_dir(rel)
        if entries is None:
            continue
        for f in entries:
            n = f.get("name", "")
            if f.get("isDirectory"):
                if depth < max_depth and not n.startswith("_src_"):
                    todo.append((f"{rel}/{n}", depth + 1))
            elif n.lower().endswith(".taf"):
                names.append((rel, n))
    return names


def series_list(md):
    """series als Liste; ABS liefert bei Serien-Filter ein einzelnes Objekt statt einer Liste."""
    s = md.get("series")
    if isinstance(s, dict):
        return [s]
    return [x for x in s if isinstance(x, dict)] if isinstance(s, list) else []


def series_seq(md):
    """Folgennummer der (ersten) Serie, aus series[] (expanded) oder seriesName 'Name #12' (minified)."""
    for s in series_list(md):
        return str(s.get("sequence") or "").strip() or None
    m = re.search(r"#\s*([^,#]+?)\s*$", (md.get("seriesName") or "").split(", ")[0])
    return m.group(1) if m else None


def numbered(title, seq):
    """'Das Fohlen', '1' -> '001 - Das Fohlen'; Titel, die schon mit der Nummer beginnen, bleiben."""
    if not seq:
        return title
    if re.fullmatch(r"\d+", seq):
        num = int(seq)
        lead = re.match(r"\s*(?:Folge\s*)?0*(\d+)\b", title or "", re.IGNORECASE)
        if lead and int(lead.group(1)) == num:
            return title
        return f"{num:03d} - {title}"
    if (title or "").startswith(seq):
        return title
    return f"{seq} - {title}"


def item_folder_parts(item, lib_name):
    """Logische Ablage: <Bibliothek>/<Serie> bzw. <Autor> bzw. 'Sonstige'."""
    md = (item.get("media") or {}).get("metadata") or {}
    group = ""
    if series_list(md):
        group = series_list(md)[0].get("name", "")
    if not group and md.get("seriesName"):
        group = re.sub(r"\s+#[^,]*$", "", md["seriesName"].split(", ")[0])
    if not group and md.get("authors"):
        group = md["authors"][0].get("name", "")
    if not group and md.get("authorName"):
        group = md["authorName"].split(", ")[0]
    return [clean_name(lib_name or t("Bibliothek"), 80), clean_name(group or t("Sonstige"), 80)]


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


ENC_SPEED = 17.0       # Umwandeln in TeddyCloud: Hörzeit / Rechenzeit (gemessen, wird nachgelernt)
TAF_BPS = 11400.0      # Bytes je Hörsekunde in der fertigen TAF (gemessen 0,65 MB/min, wird nachgelernt)
RETRY_MAX = 2          # Neuversuche, wenn TeddyCloud beim Umwandeln abbricht (z. B. Neustart/Backup)
RETRY_SETTLE = float(os.environ.get("TB_RETRY_SETTLE", "60"))         # so lange muss eine TAF unverändert sein
RETRY_WAIT_MAX = float(os.environ.get("TB_RETRY_WAIT_MAX", "2700"))   # so lange auf TeddyCloud warten (45 min)
ENC_POLL = 60.0        # Sekunden zwischen zwei Grössenabfragen der wachsenden TAF (selten: TeddyCloud schonen)


def audio_secs(items):
    """Hörzeit von Stücken/Dateien; ohne Dauer aus der Grösse geschätzt (128 kbit/s)."""
    return sum(x.get("dur") or (x.get("size") or (x.get("f") or {}).get("size") or 0) / 16000 for x in items)


def fmt_dur(secs):
    secs = int(secs or 0)
    return f"{secs // 3600} h {secs % 3600 // 60:02d} min" if secs >= 3600 else f"{max(1, round(secs / 60))} min"


def transfer_eta(elapsed, done, total):
    """Restzeit fürs Herunter-/Hochladen aus dem bisherigen Tempo (None, solange nichts fertig ist)."""
    if done <= 0 or total <= 0:
        return None
    return elapsed * (total - done) / done


def encode_eta(secs, elapsed, size, expected, rate, speed):
    """Restzeit fürs Umwandeln und Anteil 0..1. Mit wachsender Dateigrösse (size, rate = Bytes/s seit Beginn)
    aus der Grösse, sonst aus der Erfahrung (Hörzeit / speed)."""
    if size and expected and rate and rate > 0:
        frac = min(size / expected, 0.99)
        return max(expected - size, 0) / rate, frac
    est = secs / speed if speed else 0
    return max(est - elapsed, 30.0 if est else 0.0), (min(elapsed / est, 0.95) if est else None)


def encode_wait(items):
    """Wartezeit auf TeddyCloud beim Umwandeln, wächst mit der Hörzeit. Gemessen: ca. 17-fache Echtzeit
    (52 min in ca. 3 min, 10,9 h in 40 min); hier grosszügig 4-fach: 15 min + Hörzeit/4, mind. 60 min.
    Dauer unbekannt (Dateiquelle): aus der Grösse geschätzt (vorsichtig mit 64 kbit/s)."""
    secs = sum(x.get("dur") or (x.get("size") or (x.get("f") or {}).get("size") or 0) / 8000 for x in items)
    return max(3600.0, 900.0 + secs / 4)


def expand(files):
    """Dateien -> Stücke; ein Stück wird ein TAF-Kapitel (oder mit Nachbarn zusammengefügt).
    Eine Datei mit Kapitelmarken ('chapters', z. B. m4b) wird in ihre Kapitel zerlegt, sofern ffmpeg da ist.
    Stück: {f: Datei, start/end: Bereich in s (None = Anfang/Ende), dur, book: Nummer des Hörbuchs}"""
    pieces = []
    can_cut = chapters.available()
    for f in files:
        chs = f.get("chapters") or []
        if len(chs) >= 2 and can_cut and chapters.OUT_EXT.get((f.get("ext") or "").lower().lstrip(".")):
            end_all = f.get("dur") or chs[-1][1]
            for s, e in chapters.ranges(chs):
                pieces.append({"f": f, "start": s, "end": e, "dur": max((e if e is not None else end_all) - s, 0.1),
                               "book": f.get("book", 0)})
        else:
            pieces.append({"f": f, "start": None, "end": None, "dur": f.get("dur") or 0, "book": f.get("book", 0)})
    return pieces


def normalize(unit):
    """Aufeinanderfolgende Bereiche derselben Datei zu einem Bereich verbinden; ganze Datei -> start/end None."""
    out = []
    for p in unit:
        last = out[-1] if out else None
        if last and last["f"] is p["f"] and last["end"] is not None and p["start"] is not None \
                and abs(last["end"] - p["start"]) < 1e-6:
            out[-1] = {**last, "end": p["end"], "dur": last["dur"] + p["dur"]}
        else:
            out.append(dict(p))
    for p in out:
        if not p["start"] and p["end"] is None:
            p["start"] = None
    return out


def _whole(p):
    return p["start"] is None and p["end"] is None


def _ext(f):
    return (f.get("ext") or "").lower().lstrip(".")


def merge_units(pieces, limit=None):
    """Stücke zu höchstens `limit` Kapiteln zusammenlegen: kürzeste Nachbarn zuerst, Grenzen zwischen
    Hörbüchern nur, wenn es nicht anders geht. Ohne ffmpeg geht Zusammenfügen nur bei MP3."""
    limit = limit or MAX_FILES
    weights = [p["dur"] or (p["f"].get("size") or 16000) / 16000 for p in pieces]
    units = [[pieces[i] for i in g] for g in mp3cat.merge_groups(weights, limit, [p["book"] for p in pieces])]
    if not chapters.available():
        other = sorted({_ext(p["f"]) or "?" for u in units if len(normalize(u)) > 1 for p in u} - {"mp3"})
        if other:
            raise ApiError(t("Zusammenfügen von {fmts} braucht ffmpeg (nicht installiert) – bitte „Aufteilen“ wählen",
                             fmts=", ".join(other)))
    return units


def merge_plan(files):
    """Dateien zu höchstens MAX_FILES Kapiteln zusammenfügen (für Tests/Rückwärtskompatibilität):
    -> Liste von Datei-Listen."""
    return [[p["f"] for p in u] for u in merge_units(expand(files))]


def note_for(nbooks, nfiles, npieces, nunits):
    """Kurzer Hinweis für die Warteschlange, was mit den Kapiteln passiert ist."""
    one = nfiles == 1
    if npieces > nfiles:
        txt = t("{n} Datei in {p} Kapitel geschnitten" if one else "{n} Dateien in {p} Kapitel geschnitten",
                n=nfiles, p=npieces)
        if nunits < npieces:
            txt += t(", zu {u} zusammengefügt", u=nunits)
    elif nunits < nfiles:
        txt = t("{n} Datei zu {u} Kapiteln zusammengefügt" if one else "{n} Dateien zu {u} Kapiteln zusammengefügt",
                n=nfiles, u=nunits)
    else:
        txt = None
    if nbooks > 1:
        return t("{b} Hörbücher, {u} Kapitel", b=nbooks, u=nunits) + (f" ({txt})" if txt else "")
    return txt


def split_parts(files):
    """Gleichmässig in Teile zu höchstens MAX_FILES Dateien aufteilen."""
    n = math.ceil(len(files) / MAX_FILES)
    size = math.ceil(len(files) / n)
    return [files[i:i + size] for i in range(0, len(files), size)]


SNAP_MAX = 180.0  # Sekunden: so weit darf ein Schnitt höchstens zur nächsten Pause rücken
_grp = itertools.count(1)


def subdivide(pieces, target, only_whole=False):
    """Stücke, die deutlich länger als `target` Sekunden sind (> 1,5 ×), in gleich lange Bereiche zerlegen,
    sofern das geht (ffmpeg da, Format schneidbar, Dauer bekannt). only_whole: nur ganze Dateien (also
    Dateien ohne Kapitelmarken), Kapitelbereiche bleiben. Die neuen Stücke tragen 'cut' (Gruppe) und 'step';
    ihre inneren Grenzen rücken später an Pausen (snap_cuts)."""
    if not chapters.available():
        return list(pieces)
    out = []
    for p in pieces:
        dur = p.get("dur") or 0
        if dur > 1.5 * target and chapters.OUT_EXT.get(_ext(p["f"])) and not (only_whole and not _whole(p)) \
                and not p.get("cut"):
            k = round(dur / target)
            s0, step, g = p["start"] or 0.0, dur / k, next(_grp)
            for i in range(k):
                e = s0 + step * (i + 1) if i + 1 < k else p["end"]
                out.append({**p, "start": s0 + step * i, "end": e, "dur": step, "cut": g, "step": step})
        else:
            out.append(p)
    return out


def snap_cuts(pieces, sil):
    """Innere Schnitte zerlegter Stücke (gleiche 'cut'-Gruppe, gleiche Datei) zur nächsten Pause verschieben,
    höchstens SNAP_MAX bzw. ein Viertel der Stücklänge weit (so überholen sich Schnitte nie). Ändert die Stücke
    direkt. -> (verschoben, Schnitte gesamt)"""
    moved = total = 0
    sil = sorted(sil)
    for a, b in zip(pieces, pieces[1:]):
        if not a.get("cut") or a.get("cut") != b.get("cut") or a["end"] is None:
            continue
        total += 1
        x, win = a["end"], min(SNAP_MAX, 0.25 * a["step"])
        near = [s for s in sil if abs(s - x) <= win]
        if not near:
            continue
        s = min(near, key=lambda s: abs(s - x))
        a["end"] = b["start"] = s
        a["dur"] = s - (a["start"] or 0.0)
        b["dur"] = ((b["end"] if b["end"] is not None else (b["f"].get("dur") or s)) - s)
        moved += 1
    return moved, total


def split_by_time(pieces, target):
    """In Teile von etwa `target` Sekunden aufteilen (Option „Teile à ca. 60 min“). -> Liste von Stück-Listen.
    Geschnitten wird an Datei-/Kapitelgrenzen, jeweils an der Grenze, die der idealen Stelle am nächsten liegt.
    Ein einzelnes Stück, das deutlich länger ist (> 1,5 × target), wird vorher in gleich lange Bereiche
    zerlegt (subdivide). Höchstens 99 Teile."""
    out = subdivide(pieces, target)
    w = [p["dur"] or (p["f"].get("size") or 16000) / 16000 for p in out]
    total = sum(w)
    n = max(1, min(MAX_FILES, round(total / target), len(out)))
    if n == 1:
        return [out]
    bounds, acc = [], 0.0  # bounds[j] = Hörzeit vor Stück j+1
    for x in w[:-1]:
        acc += x
        bounds.append(acc)
    cuts, prev = [], 0
    for i in range(1, n):
        ideal = total * i / n
        lo, hi = prev + 1, len(out) - (n - i)  # jeder Teil mindestens ein Stück
        j = min(range(lo, hi + 1), key=lambda j: abs(bounds[j - 1] - ideal))
        cuts.append(j)
        prev = j
    edges = [0] + cuts + [len(out)]
    return [out[a:b] for a, b in zip(edges, edges[1:])]


def part_name(clean, k, n, short):
    """Name einer Teil-TAF; Teilnummer mit führender Null (ab 10 Teilen), damit TeddyCloud richtig sortiert."""
    return t("{clean} - Teil {k} von {n} [{mark}].taf", clean=clean, k=str(k).zfill(len(str(n))), n=n, mark=f"{short}-{k}")


PARTS_OF = re.compile(r" (?:von|of) (\d{1,2}) \[[^\[\]/]{8}-\d{1,2}\]\.taf$", re.IGNORECASE)


def nd_tracks(album):
    """Titel eines Navidrome-Albums in Abspielreihenfolge (CD, Titelnummer, sonst Reihenfolge der API)."""
    songs = [(i, s) for i, s in enumerate(album.get("song") or []) if not s.get("isDir")]
    songs.sort(key=lambda t: (t[1].get("discNumber") or 1, t[1].get("track") or 0, t[0]))
    return [s for _, s in songs]


class Queue:
    def __init__(self, cfg, abs_, tc, lib_name, nd=None, nd_folder_name=None, fs=None):
        self.cfg, self.abs, self.tc, self.lib_name = cfg, abs_, tc, lib_name
        self.nd, self.nd_folder_name, self.fs = nd, nd_folder_name, fs
        self.q = queue.Queue()
        self.jobs = {}
        self.batches = {}
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.changed = time.time()  # für den Status-Cache
        self.on_final = None  # main setzt das: vorgemerkten Tonie zuweisen (jid, job)
        self.speed, self.bps = ENC_SPEED, TAF_BPS
        try:  # eigene Verbindung, weil der Worker-Thread während des Umwandelns blockiert wartet
            self.tc_poll = type(tc)(cfg)
        except Exception:
            self.tc_poll = tc
        threading.Thread(target=self._run, daemon=True, name="worker").start()

    # ---------- öffentliche API

    def submit(self, items, subfolder=None, bundle=None, chapter_min=0, tonie=None):
        """bundle: Name einer Sammel-TAF – dann werden alle items (in dieser Reihenfolge) zu *einer* TAF.
        chapter_min: Dateien ohne Kapitelmarken bekommen etwa alle so viele Minuten ein Kapitel (0 = aus)."""
        sub = clean_folder(subfolder) if subfolder else None
        if bundle:
            key = ";".join(f"{i.get('source') or 'abs'}:{i.get('lib') or ''}:{i['id']}:{i.get('kind') or 'album'}"
                           + (":" + ",".join(i.get("songs") or []) if i.get("songs") else "") for i in items)
            items = [{"id": "bundle", "title": bundle, "source": "mix", "kind": "bundle", "items": list(items),
                      "mark": "k" + hashlib.sha1(key.encode()).hexdigest()[:7]}]  # 'k' kommt in Hex nie vor
        with self.lock:
            if tonie:  # tonie: gleich vormerken (nur bei genau einem Auftrag; main prüft das)
                other = next((j for j in self.jobs.values() if j["state"] not in FINAL
                              and (j.get("tonie") or {}).get("ruid") == tonie["ruid"]), None)
                if other:
                    raise Reserved(other["title"])
            bid = next(self.ids)
            jids = []
            for it in items:
                jid = next(self.ids)
                self.jobs[jid] = {
                    "id": jid, "batch": bid, "item_id": it["id"], "title": it.get("title") or it["id"],
                    "source": it.get("source") or "abs", "lib": it.get("lib"),
                    "kind": it.get("kind") or "album", "songs": list(it.get("songs") or []),
                    "items": it.get("items") or [],
                    "mark": it.get("mark") or (self.fs.mark(it.get("lib"), it["id"]) if it.get("source") == "fs" else
                             id8(it["id"], it.get("source") or "abs", it.get("kind") or "album", it.get("songs"))),
                    "subfolder": sub, "split": bool(it.get("split")), "merge": bool(it.get("merge")),
                    "parts_min": int(it.get("parts_min") or 0), "chapter_min": int(chapter_min or 0),
                    "folder": f"{self.cfg.tc_base}/{sub}" if sub else None,
                    "state": "waiting", "n": 0, "m": 0, "part": 0, "parts": 0, "error": None,
                    "taf": None, "tafs": [], "tonie": dict(tonie) if tonie else None, "tonie_result": None, "tonie_ok": None, "check": None, "warning": None, "note": None,
                    "secs": None, "eta": None, "pct": None, "created": time.time(), "finished": None,
                    "lang": i18n.get(),  # Sprache der Meldungen/Ordnernamen: die der Anfrage beim Einreihen
                }
                jids.append(jid)
            self.batches[bid] = {"jobs": jids, "results": {}, "notified": False}
        for jid in jids:
            self.q.put(jid)
        log.info("Stapel %s: %d Aufträge (%s)", bid, len(jids), "Ordner " + sub if sub else "automatische Ablage")
        return bid

    def snapshot(self):
        with self.lock:
            return [dict(j) for j in sorted(self.jobs.values(), key=lambda j: j["id"])]

    def set_tonie(self, jid, tonie):
        """Tonie für einen noch laufenden/wartenden Auftrag vormerken (None = Vormerkung entfernen).
        -> (ok, Grund): nicht möglich, wenn der Auftrag schon abgeschlossen ist oder die Figur bei einem
        anderen offenen Auftrag vorgemerkt ist."""
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                return False, "missing"
            if job["state"] in FINAL:
                return False, "final"
            if tonie:
                other = next((j for j in self.jobs.values() if j["id"] != jid and j["state"] not in FINAL
                              and (j.get("tonie") or {}).get("ruid") == tonie["ruid"]), None)
                if other:
                    return False, other["title"]
            job["tonie"] = tonie
            job["tonie_result"] = None
        self.changed = time.time()
        return True, None

    def clear_finished(self):
        with self.lock:
            for jid in [jid for jid, j in self.jobs.items() if j["state"] in FINAL]:
                del self.jobs[jid]
            for bid in [b for b, v in self.batches.items() if v["notified"]]:
                del self.batches[bid]

    # ---------- Worker

    def _set(self, jid, **kw):
        with self.lock:
            job = self.jobs[jid]
            job.update(kw)
            if kw.get("state") in FINAL:  # Ergebnis atomar im Stapel festhalten
                self.batches[job["batch"]]["results"][jid] = kw["state"]

    def _run(self):
        while True:
            jid = self.q.get()
            with self.lock:
                job = dict(self.jobs[jid])
            lang = i18n.set(job.get("lang"))
            try:
                self._run_one(jid, job)
            finally:
                i18n.reset(lang)

    def _run_one(self, jid, job):
        try:
            self._process(jid, job)
        except ApiError as e:
            self._fail(jid, str(e))
        except Exception as e:  # nie Details mit evtl. Geheimnissen ausgeben
            log.exception("Unerwarteter Fehler bei Auftrag %s", jid)
            self._fail(jid, t("interner Fehler ({name})", name=type(e).__name__))
        self.changed = time.time()
        try:  # vorgemerkter Tonie (seit 1.5.1): erst nach dem Abschluss zuweisen
            with self.lock:
                final = dict(self.jobs[jid])
            if final.get("tonie") and final["state"] in ("done", "skipped") and self.on_final:
                self.on_final(jid, final)
                self.changed = time.time()
        except Exception:
            log.exception("Fehler beim Zuweisen des vorgemerkten Tonies")
        try:
            with self.lock:
                final = dict(self.jobs[jid])
            self._notify_job(final)
        except Exception:
            log.exception("Fehler bei der Discord-Meldung")
        try:
            self._check_batch(job["batch"])
        except Exception:
            log.exception("Fehler bei der Stapel-Auswertung")

    def _fail(self, jid, msg):
        for s in self.cfg.secrets():
            msg = msg.replace(s, "***")
        log.warning("Auftrag %s fehlgeschlagen: %s", jid, msg)
        self._set(jid, state="error", error=msg, finished=time.time())

    def _prepare_abs(self, job):
        """-> (Titel, Ordnerteile, Folgennummer, Dateien[{ext, fetch(pfad)}])"""
        item_id = job["item_id"]
        item = self.abs.item(item_id)
        media = item.get("media") or {}
        md = media.get("metadata") or {}
        files = sorted((a for a in media.get("audioFiles") or [] if not a.get("exclude")),
                       key=lambda a: a.get("index", 0))
        out = [{"ext": (a.get("metadata") or {}).get("ext") or "", "dur": a.get("duration") or 0,
                "name": (a.get("metadata") or {}).get("filename") or "",
                "size": (a.get("metadata") or {}).get("size") or 0,
                "fetch": (lambda path, ino=a["ino"]: self.abs.download(item_id, ino, path))} for a in files]
        if len(out) == 1:  # eine Datei (m4b o. ä.): Kapitelmarken aus ABS
            chs = sorted((float(c.get("start") or 0), float(c.get("end") or 0)) for c in media.get("chapters") or [])
            out[0]["chapters"] = [c for c in chs if c[1] - c[0] >= chapters.MIN_CHAPTER]
        return (md.get("title") or job["title"], item_folder_parts(item, self.lib_name(item.get("libraryId"))),
                series_seq(md), out)

    def _prepare_nd(self, job):
        if not self.nd:
            raise ApiError(t("Navidrome ist nicht konfiguriert"))
        kind = job.get("kind") or "album"
        if kind == "playlist":
            pl = self.nd.playlist(job["item_id"])
            title, parts = pl.get("name") or job["title"], ["Playlists"]
            songs = [s for s in pl.get("entry") or [] if not s.get("isDir")]
        elif kind == "songs":
            title, parts = job["title"], ["Playlists"]
            songs = [self.nd.song(sid) for sid in job["songs"]]
        else:
            album = self.nd.album(job["item_id"])
            lib = self.nd_folder_name(job.get("lib")) if self.nd_folder_name else None
            group = album.get("artist") or album.get("displayArtist") or ""
            title = album.get("name") or job["title"]
            parts = [clean_name(lib or "Navidrome", 80), clean_name(group or "Sonstige", 80)]
            songs = nd_tracks(album)
        out = [{"ext": s.get("suffix") or "", "dur": s.get("duration") or 0, "size": s.get("size") or 0,
                "name": s.get("title") or "",
                "fetch": (lambda path, sid=s["id"], size=s.get("size"): self.nd.download(sid, path, size))}
               for s in songs]
        return title, parts, None, out

    def _prepare_fs(self, job):
        if not self.fs:
            raise ApiError(t("Dateiquelle ist nicht konfiguriert"))
        title, parts, files, root_real = self.fs.item(job.get("lib"), job["item_id"])
        out = [{"ext": os.path.splitext(p)[1].lstrip("."), "dur": 0, "size": _size(p), "name": shown(os.path.basename(p)),
                "fetch": (lambda path, src=p: self.fs.copy(root_real, src, path))} for p in files]
        if len(out) == 1 and chapters.available():  # eine Datei (m4b o. ä.): Kapitel direkt lesen
            try:
                out[0]["chapters"], out[0]["dur"] = chapters.probe(files[0])
            except ApiError as e:
                log.warning("Kapitel nicht lesbar, übertrage als eine Datei: %s", e)
        return title, [clean_name(x, 80) for x in parts], None, out

    def _prepare(self, job):
        prepare = {"nd": self._prepare_nd, "fs": self._prepare_fs}.get(job.get("source") or "abs", self._prepare_abs)
        return prepare(job)

    def _prepare_bundle(self, job):
        """Sammel-TAF: alle Hörbücher nacheinander; jede Datei merkt sich ihr Hörbuch (für die Kapitelgrenzen)."""
        files = []
        for k, it in enumerate(job["items"]):
            sub = {"item_id": it["id"], "title": it.get("title") or it["id"], "source": it.get("source") or "abs",
                   "lib": it.get("lib"), "kind": it.get("kind") or "album", "songs": list(it.get("songs") or [])}
            _, _, _, fs = self._prepare(sub)
            if not fs:
                raise ApiError(t("„{title}“: keine Audiodateien", title=sub["title"]))
            for f in fs:
                f["book"] = k
            files += fs
        return job["title"], [t("Sammlungen")], None, files

    def _process(self, jid, job):
        short = job["mark"]
        base = self.cfg.tc_base
        bundle = job.get("kind") == "bundle"

        title, parts, seq, files = self._prepare_bundle(job) if bundle else self._prepare(job)
        folder = f"{base}/{job['subfolder']}" if job["subfolder"] else "/".join([base] + parts)
        self._set(jid, title=title, folder=folder)

        if not files:
            raise ApiError(t("keine Audiodateien"))
        by_time = bool(job.get("parts_min")) and not bundle
        if len(files) > MAX_FILES and not (job["split"] or job.get("merge") or bundle or by_time):
            raise ApiError(t("{n} Audiodateien, TeddyCloud erlaubt max. {max} – nicht übertragen (weder Aufteilen noch "
                             "Zusammenfügen gewählt)", n=len(files), max=MAX_FILES))

        # Duplikatprüfung im ganzen Basisordner (auch wenn sich Serie/Ordner seither geändert hat)
        # auch in TC_BASE_EXTRA (z. B. alter Basisordner): dort nur suchen, nie schreiben
        bases = getattr(self.cfg, "tc_scan_bases", None) or (base,)
        paths = [x for b in bases for x in scan_paths(self.tc, b)]
        names = [n for _, n in paths]
        have = markers(names)
        if short in have:
            for d, n in [hit for b in bases for hit in find_marker(self.tc, b, short)]:
                h = self.tc.taf_header(d, n)
                if h is not None and not (h.get("valid") and h.get("trackSeconds") and h.get("lengthSeconds")):
                    raise ApiError(t("in TeddyCloud liegt eine unvollständige TAF „{name}“ in {folder} (früher abgebrochen) – "
                                     "bitte dort löschen und das Hörbuch neu übertragen", name=n, folder=d))
            log.info("Auftrag %s: [%s] schon vorhanden", jid, short)
            self._set(jid, state="skipped", finished=time.time(), tafs=own_tafs(paths, short),
                      note=t("schon als eine TAF vorhanden (zum Aufteilen erst in TeddyCloud löschen)") if by_time else None)
            return

        clean = clean_name(numbered(title, seq))
        pieces = expand(files)
        chunks, split_note, chap_note = None, None, None
        if job.get("chapter_min"):
            before = len(pieces)
            pieces = subdivide(pieces, job["chapter_min"] * 60, only_whole=True)
            if len(pieces) > before:
                chap_note = t("Kapitel alle ca. {m} min ergänzt", m=job["chapter_min"])
        if by_time:
            pieces = subdivide(pieces, job["parts_min"] * 60)
            chunks = split_by_time(pieces, job["parts_min"] * 60)
            if len(chunks) == 1:
                chunks = None
                split_note = t("nicht aufgeteilt (kürzer als ca. {m} min)", m=round(job["parts_min"] * 1.5)) \
                    if audio_secs(pieces) < job["parts_min"] * 90 else t("nicht aufteilbar (eine Datei ohne Kapitel)")
        elif len(pieces) > MAX_FILES and job["split"] and not bundle:
            chunks = split_parts(pieces)
        # Plan: [(TAF-Name, [Kapitel])], ein Kapitel = Liste von Stücken (ganze Dateien oder Kapitelbereiche)
        if chunks:
            n = len(chunks)
            prev_n = {int(m.group(1)) for x in names if (m := PARTS_OF.search(x)) and MARKER.search(x).group(1) == short}
            if prev_n and prev_n != {n}:  # schon anders aufgeteilt: nicht mischen
                raise ApiError(t("liegt in TeddyCloud schon in {old} Teilen vor, jetzt wären es {n} – nicht gemischt; "
                                 "zum neu Aufteilen die vorhandenen Teile in TeddyCloud löschen",
                                 old=", ".join(map(str, sorted(prev_n))), n=n))
            plan = [(part_name(clean, k, n, short), merge_units(c) if len(c) > MAX_FILES else [[p] for p in c])
                    for k, c in enumerate(chunks, 1) if f"{short}-{k}" not in have]
            if by_time:
                split_note = t("in {n} Teile à ca. {m} min aufgeteilt", n=n, m=round(audio_secs(pieces) / n / 60))
            if not plan:
                self._set(jid, state="skipped", finished=time.time(), note=split_note, tafs=own_tafs(paths, short))
                return
        else:
            if any(h.startswith(short + "-") for h in have):
                log.info("Auftrag %s: [%s] als Teile schon vorhanden", jid, short)
                self._set(jid, state="skipped", finished=time.time(), tafs=own_tafs(paths, short))
                return
            units = merge_units(pieces) if len(pieces) > MAX_FILES else [[p] for p in pieces]
            plan = [(f"{clean} [{short}].taf", units)]
        nunits = sum(len(c) for _, c in plan)
        note = note_for(len({f.get("book", 0) for f in files}), len(files), len(pieces), nunits)
        note = "; ".join(x for x in (split_note, chap_note, note) if x) or None
        if note:
            log.info("Auftrag %s: %s", jid, note)
        todo = [u for _, c in plan for u in c]
        synth = {}  # Datei -> alle zerlegten Stücke daraus (auch aus schon vorhandenen Teilen, damit Schnitte gleich bleiben)
        for p in pieces:
            if p.get("cut"):
                synth.setdefault(id(p["f"]), []).append(p)
        snapped = [0, 0]
        last_use = {}
        for k, u in enumerate(todo):
            for p in u:
                last_use[id(p["f"])] = k
        part_secs = [audio_secs([p for u in chunk for p in u]) for _, chunk in plan]
        enc_est = lambda k=0: sum(part_secs[k:]) / self.speed  # noqa: E731  (Umwandeln ab Teil k+1)
        self._set(jid, m=len(last_use), parts=len(plan), taf=", ".join(t for t, _ in plan), note=note,
                  secs=int(sum(part_secs)), eta=int(enc_est()), pct=0)

        self.tc.ensure_dirs(folder)
        src = f"{folder}/_src_{short}"
        created_src = False
        try:
            if self.tc.dir_exists(src):  # Rest eines abgebrochenen Laufs
                self._cleanup(src)
            self.tc.mkdir(src)
            created_src = True
            srcname = {}
            # /var/tmp statt /tmp: /tmp kann im RAM liegen, und eine Quelldatei (m4b) kann gross sein
            tmpbase = "/var/tmp" if os.access("/var/tmp", os.W_OK) else None
            t_transfer = time.time()
            with tempfile.TemporaryDirectory(prefix="teddybridge-", dir=tmpbase) as tmp:
                cache, n = {}, 0
                for k, unit in enumerate(todo, 1):
                    for p in unit:  # Quelldateien holen (jede nur einmal, auch wenn mehrere Kapitel daraus kommen)
                        if id(p["f"]) not in cache:
                            n += 1
                            self._set(jid, state="downloading", n=n)
                            ext = re.sub(r"[^a-z0-9]", "", _ext(p["f"])) or "mp3"
                            path = os.path.join(tmp, f"q{n:04d}.{ext}")
                            p["f"]["fetch"](path)
                            cache[id(p["f"])] = path
                            if id(p["f"]) in synth:  # Schnitte ohne Kapitelmarke: an die nächste Pause rücken
                                self._set(jid, state="analyzing", n=n)
                                moved, total = snap_cuts(synth[id(p["f"])], chapters.silences(path))
                                snapped[0] += moved
                                snapped[1] += total
                                self._set(jid, note="; ".join(x for x in (note, t("{k} von {n} Schnitten in einer Pause",
                                                                                   k=snapped[0], n=snapped[1])) if x))
                    self._set(jid, state="preparing" if len(unit) > 1 or not _whole(unit[0]) else "uploading", n=n)
                    local = self._build(unit, cache, tmp, k)
                    name = f"{k:03d}{os.path.splitext(local)[1] or '.mp3'}"
                    self._set(jid, state="uploading", n=n)
                    self.tc.upload(src, local, name)
                    srcname[id(unit)] = name
                    rest = transfer_eta(time.time() - t_transfer, k, len(todo))
                    self._set(jid, pct=int(100 * k / len(todo)), eta=int((rest or 0) + enc_est()))
                    if local not in cache.values():
                        os.remove(local)
                    for fid in [fid for fid, last in last_use.items() if last == k - 1 and fid in cache]:
                        os.remove(cache.pop(fid))  # wird nicht mehr gebraucht
            for k, (taf, chunk) in enumerate(plan, 1):
                self._set(jid, state="encoding", part=k, pct=0, eta=int(enc_est(k - 1)))
                sources = [f"{src}/{srcname[id(u)]}" for u in chunk]
                log.info("Auftrag %s: wandle %d Dateien um (Teil %d/%d)", jid, len(sources), k, len(plan))
                existed = self.tc.file_size(folder, taf) is not None  # dann nie löschen
                for attempt in range(1, RETRY_MAX + 2):
                    stop = threading.Event()
                    watch = threading.Thread(target=self._watch_encode, daemon=True, name="encode-watch",
                                             args=(jid, folder, taf, part_secs[k - 1], enc_est(k), stop))
                    t_enc = time.time()
                    watch.start()
                    try:
                        self.tc.encode(f"{folder}/{taf}", sources, encode_wait([p for u in chunk for p in u]))
                        self._learn(folder, taf, part_secs[k - 1], time.time() - t_enc)
                        break
                    except ApiError as e:
                        stop.set()
                        verdict = self._after_encode_error(jid, e, folder, taf, len(chunk), src, len(sources), existed)
                        if verdict == "complete":
                            self._set(jid, warning=t("TeddyCloud meldete einen Fehler ({err}), die TAF ist aber vollständig", err=e))
                            break
                        if verdict == "retry" and attempt <= RETRY_MAX:
                            log.warning("Auftrag %s: Umwandeln abgebrochen (%s), Neuversuch %d/%d", jid, e, attempt, RETRY_MAX)
                            self._set(jid, state="encoding", pct=0, eta=int(enc_est(k - 1)),
                                      warning=t("Umwandeln {n}× abgebrochen ({err}), automatisch wiederholt", n=attempt, err=e))
                            continue
                        if verdict == "retry":  # aufgegeben: eigene halbe TAF nicht liegen lassen
                            self._remove_own_partial(folder, taf, len(chunk), existed)
                            raise ApiError(t("{err} – TeddyCloud hat das Umwandeln {n}× abgebrochen, aufgegeben; die halbe TAF wurde "
                                             "entfernt, ein neuer Versuch ist jederzeit möglich", err=e, n=attempt)) from None
                        raise ApiError(self._explain_encode_error(e, folder, taf, len(chunk))) from None
                    finally:
                        stop.set()
            self._verify(jid, folder, [(taf, len(chunk)) for taf, chunk in plan])
        finally:
            if created_src:
                try:
                    self._cleanup(src)
                except ApiError as e:
                    log.warning("Aufräumen von _src_%s fehlgeschlagen: %s", short, e)
        # erst nach dem Aufräumen „fertig“ melden
        self._set(jid, state="done", finished=time.time(), eta=0, pct=100,
                  tafs=own_tafs(paths + [(folder, taf) for taf, _ in plan], short))
        log.info("Auftrag %s: fertig [%s]", jid, short)

    def _watch_encode(self, jid, folder, taf, secs, later, stop):
        """Während TeddyCloud umwandelt: Grösse der wachsenden TAF abfragen -> Anteil und Restzeit."""
        t0, expected = time.time(), secs * self.bps
        while not stop.wait(ENC_POLL):
            try:
                size = self.tc_poll.file_size(folder, taf)
            except Exception:
                size = None
            elapsed = time.time() - t0
            rest, frac = encode_eta(secs, elapsed, size, expected, (size or 0) / elapsed if elapsed else 0, self.speed)
            if not stop.is_set():
                self._set(jid, eta=int(rest + later), pct=int(100 * frac) if frac is not None else None)

    def _tc_state(self, folder, taf):
        """-> (Grösse oder None, TAF-Kopf oder None); wirft ApiError, wenn TeddyCloud nicht erreichbar ist."""
        if self.tc_poll.list_dir(folder) is None:
            raise ApiError(t("TeddyCloud antwortet nicht"))
        size = self.tc_poll.file_size(folder, taf)
        return size, (self.tc_poll.taf_header(folder, taf) if size is not None else None)

    def _after_encode_error(self, jid, err, folder, taf, expected, src, nsources, existed):
        """Nach einem Fehler beim Umwandeln: auf TeddyCloud warten, Lage prüfen.
        -> 'complete' (TAF trotzdem fertig), 'retry' (neu umwandeln; eigene halbe TAF ist weg) oder 'fatal'."""
        self._set(jid, state="retrying", eta=None)
        t0, step = time.time(), max(min(30.0, RETRY_SETTLE), 0.2)
        while True:  # 1. warten, bis TeddyCloud wieder antwortet (z. B. nach Neustart oder Backup)
            try:
                size, head = self._tc_state(folder, taf)
                break
            except ApiError:
                if time.time() - t0 > RETRY_WAIT_MAX:
                    log.warning("Auftrag %s: TeddyCloud nach %.0f min nicht wieder erreichbar", jid, RETRY_WAIT_MAX / 60)
                    return "fatal"
                time.sleep(step)
        while size is not None:  # 2. wächst die TAF noch? (nur die Verbindung weg, TeddyCloud rechnet weiter)
            time.sleep(RETRY_SETTLE)
            try:
                size2, head = self._tc_state(folder, taf)
            except ApiError:
                continue
            if size2 == size:
                break
            size = size2
        tracks = len((head or {}).get("trackSeconds") or [])
        if size is not None and head is not None and head.get("valid") and tracks == expected:
            log.info("Auftrag %s: TAF trotz Fehler vollständig (%d Kapitel)", jid, tracks)
            return "complete"
        if existed:
            return "fatal"  # gab es schon vor diesem Lauf: nie anfassen
        transient = size is not None or re.search(r"HTTP 5\d\d|Verbindungsfehler|antwortet nicht", str(err))
        if not transient:
            return "fatal"
        listed = self.tc_poll.list_dir(src) or []
        if len([f for f in listed if not f.get("isDirectory")]) < nsources:
            log.warning("Auftrag %s: Quelldateien in _src_ unvollständig, kein Neuversuch", jid)
            return "fatal"
        if size is not None and not self._remove_own_partial(folder, taf, expected, existed):
            return "fatal"
        return "retry"

    def _remove_own_partial(self, folder, taf, expected, existed):
        """Ausnahme von „nur _src_ löschen“ (vom Nutzer freigegeben): eine TAF, die *dieser* Lauf selbst angelegt
        hat und die nachweislich unvollständig ist, wird vor dem Neuversuch entfernt. -> True, wenn weg."""
        if existed or not taf.lower().endswith(".taf") or "/" in taf:
            return False
        try:
            size, head = self._tc_state(folder, taf)
            if size is None:
                return True
            tracks = len((head or {}).get("trackSeconds") or [])
            if head is not None and head.get("valid") and tracks == expected:
                return False  # vollständig: nicht löschen
            self.tc.delete_file(f"{folder}/{taf}")
            log.warning("Unvollständige eigene TAF entfernt: %s/%s (%d Bytes, %d von %d Kapiteln)",
                        folder, taf, size, tracks, expected)
            return self.tc_poll.file_size(folder, taf) is None
        except ApiError as e:
            log.warning("Halbe TAF konnte nicht entfernt werden: %s", e)
            return False

    def _explain_encode_error(self, err, folder, taf, expected):
        """Nach einem Fehler beim Umwandeln nachsehen, ob TeddyCloud eine halbe TAF hinterlassen hat
        (z. B. Neustart von TeddyCloud mitten im Umwandeln). Die wird nicht gelöscht (nur _src_ darf weg),
        aber klar gemeldet: sie blockiert sonst jeden neuen Versuch (Kennzeichen + Ziel existiert)."""
        msg = str(err)
        try:
            size = self.tc_poll.file_size(folder, taf)
            head = self.tc_poll.taf_header(folder, taf) if size is not None else None
        except Exception:
            return msg
        if size is None:
            return msg
        tracks = len((head or {}).get("trackSeconds") or [])
        if head is not None and head.get("valid") and tracks == expected:
            return msg  # TAF ist trotz Fehler vollständig
        log.warning("Unvollständige TAF nach Fehler: %s/%s (%d Bytes, %d von %d Kapiteln)", folder, taf, size, tracks, expected)
        return t("{err} – TeddyCloud hat das Umwandeln abgebrochen (Neustart/Absturz?). Eine unvollständige TAF "
                 "„{taf}“ ({mb} MB, {tracks} von {expected} Kapiteln) liegt in {folder}: bitte in TeddyCloud löschen "
                 "und das Hörbuch neu übertragen", err=msg, taf=taf, mb=size // 2 ** 20, tracks=tracks,
                 expected=expected, folder=folder)

    def _learn(self, folder, taf, secs, took):
        """Tempo und TAF-Bitrate aus dem echten Ergebnis nachlernen (gleitender Mittelwert, plausibel begrenzt)."""
        if secs < 600 or took < 30:
            return  # zu kurz für eine brauchbare Messung
        speed = secs / took
        if 2 <= speed <= 200:
            self.speed = round(0.5 * self.speed + 0.5 * speed, 2)
        try:
            size = self.tc.file_size(folder, taf)
        except ApiError:
            size = None
        if size and 2000 <= size / secs <= 64000:
            self.bps = round(0.5 * self.bps + 0.5 * size / secs)
        log.info("Umwandeln: %.0f-fache Echtzeit, %.0f kB je Hörminute (gelernt)", self.speed, self.bps * 60 / 1024)

    def _discord(self, text):
        if not self.cfg.discord_webhook:
            return
        try:
            r = httpx.post(self.cfg.discord_webhook, timeout=15,
                           json={"content": text[:1900], "allowed_mentions": {"parse": []}})  # kein @everyone aus Titeln
            if r.status_code >= 300:
                log.warning("Discord-Meldung: HTTP %s", r.status_code)
        except httpx.HTTPError as e:
            log.warning("Discord-Meldung fehlgeschlagen (%s)", type(e).__name__)

    def _notify_job(self, job):
        """Discord-Meldung pro Auftrag: fertig (mit Kapiteln/Dauer/Ordner) oder Fehler. Übersprungene nur in der Summe."""
        name = (job.get("taf") or job.get("title") or "?").removesuffix(".taf")
        if job["state"] == "done":
            text = t("✅ teddybridge: **{name}** fertig", name=name)
            if job.get("check"):
                text += f" – {job['check']}"
            elif job.get("secs"):
                text += f" – {fmt_dur(job['secs'])}"
            if job.get("warning"):
                text += f"\n⚠ {job['warning']}"
            text += f"\n📁 {job.get('folder') or ''}"
            if job.get("tonie_result"):
                text += f"\n{'🧸' if job.get('tonie_ok') else '⚠'} {job['tonie_result']}"
        elif job["state"] == "error":
            text = t("❌ teddybridge: **{name}** fehlgeschlagen: {err}", name=job.get("title") or name, err=job.get("error") or "?")
        else:
            return
        self._discord(text)

    def _build(self, unit, cache, tmp, k):
        """Ein TAF-Kapitel als lokale Datei bauen. -> Pfad.
        Ganze Datei: unverändert. Ein Kapitelbereich: verlustfrei geschnitten. Mehrere MP3: verlustfrei
        verbunden. Alles andere (andere Formate, gemischte MP3, Bereiche): mit ffmpeg zu FLAC verbunden."""
        unit = normalize(unit)
        dest = os.path.join(tmp, f"k{k:03d}")
        if len(unit) == 1:
            p = unit[0]
            if _whole(p):
                return cache[id(p["f"])]
            return chapters.cut(cache[id(p["f"])], _ext(p["f"]), p["start"], p["end"], dest)
        names = [p["f"].get("name") or t("Datei {n}", n=i + 1) for i, p in enumerate(unit)]
        if all(_whole(p) and _ext(p["f"]) == "mp3" for p in unit):
            try:
                mp3cat.concat([cache[id(p["f"])] for p in unit], dest + ".mp3", names)
                return dest + ".mp3"
            except ApiError as e:
                if not chapters.available():
                    raise
                log.info("MP3 nicht direkt verbindbar (%s), verbinde mit ffmpeg", e)
        if not chapters.available():
            raise ApiError(t("{name}: Zusammenfügen braucht ffmpeg (nicht installiert) – bitte „Aufteilen“ wählen", name=names[0]))
        return chapters.join([(cache[id(p["f"])], p["start"], p["end"]) for p in unit], dest)

    def _verify(self, jid, folder, plan):
        """Kapitelzahl und Gültigkeit der fertigen TAF(s) prüfen (nur lesend)."""
        checks, warns = [], []
        for taf, expected in plan:
            h = self.tc.taf_header(folder, taf)
            if h is None:
                return  # Server liefert keinen TAF-Kopf: Prüfung überspringen
            tracks = len(h.get("trackSeconds") or [])
            secs = int(h.get("lengthSeconds") or 0)
            label = t("{tracks} Kapitel, {mm}:{ss}", tracks=tracks, mm=secs // 60, ss=f"{secs % 60:02d}")
            if not h.get("valid") or tracks != expected:
                warns.append(t("{taf}: {label}, erwartet {expected} Kapitel", taf=taf, label=label, expected=expected)
                             + ("" if h.get("valid") else t(", TAF ungültig")))
            checks.append(label)
        with self.lock:
            before = self.jobs[jid].get("warning")  # z. B. „automatisch wiederholt“ nicht überschreiben
        self._set(jid, check=" | ".join(checks), warning="; ".join(([before] if before else []) + warns) or None)
        if warns:
            log.warning("Auftrag %s: Prüfung auffällig: %s", jid, "; ".join(warns))

    def _cleanup(self, src):
        """Löscht nur Dateien im eigenen _src_-Ordner und danach den Ordner selbst."""
        if not src.rsplit("/", 1)[-1].startswith("_src_"):
            raise ApiError(t("Aufräumen verweigert: kein _src_-Ordner"))
        if not self.tc.dir_exists(src):
            return
        listed = self.tc.list_dir(src) or []
        for f in listed:
            if not f.get("isDirectory"):
                self.tc.delete_file(f"{src}/{f['name']}")
        self.tc.delete_dir(src)

    def _check_batch(self, bid):
        with self.lock:
            b = self.batches.get(bid)
            if not b or b["notified"]:
                return
            if len(b["results"]) < len(b["jobs"]):
                return
            b["notified"] = True
            states = list(b["results"].values())
        x, y, z = states.count("done"), states.count("skipped"), states.count("error")
        msg = t("teddybridge: {x} übertragen, {y} übersprungen, {z} Fehler", x=x, y=y, z=z)
        log.info("Stapel %s abgeschlossen: %s", bid, msg)
        if len(states) > 1:  # bei einem einzelnen Auftrag reicht dessen eigene Meldung
            self._discord("📦 " + msg)
