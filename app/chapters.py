"""Einzelne Hörbuchdatei mit Kapitelmarken (typisch .m4b) an den Kapiteln schneiden (optional, braucht ffmpeg).

TeddyCloud macht aus jeder Quelldatei ein TAF-Kapitel. Ein m4b ist aber meist *eine* Datei mit internen
Kapiteln und würde so zu einer TAF mit nur einem Kapitel (kein Vor-/Zurückspringen; die Box speichert bei
sehr langen Titeln ausserdem die Position nicht zuverlässig). Darum: an den Kapitelmarken verlustfrei
(-c copy, keine Neukodierung) in Stücke schneiden. Mehr als 99 Kapitel: die kürzesten benachbarten
Kapitel werden zusammengelegt (nur weniger Schnitte, kein Zusammenfügen nötig).

Ist ffmpeg/ffprobe nicht installiert, passiert nichts (eine Datei = ein Kapitel wie bisher).
"""
import json
import os
import re
import shutil
import subprocess

from .clients import ApiError
from .i18n import t
from .mp3cat import merge_groups

OUT_EXT = {"m4b": "m4a", "m4a": "m4a", "mp4": "m4a", "aac": "m4a", "mp3": "mp3", "ogg": "ogg", "oga": "ogg",
           "opus": "opus", "flac": "flac", "mka": "mka"}
MIN_CHAPTER = 0.5  # Sekunden; kürzere „Kapitel“ (Fehler im Tagging) zählen nicht als eigene Schnitte


def available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(cmd, what, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ApiError(f"{t(what)}: {type(e).__name__}") from None
    if r.returncode != 0:
        last = ((r.stderr or "").strip().splitlines()[-1:] or ["?"])[0]
        for arg in cmd:  # keine lokalen Pfade in Meldungen
            if os.sep in arg:
                last = last.replace(arg, os.path.basename(arg))
        raise ApiError(t("{what} fehlgeschlagen: {detail}", what=t(what), detail=last[:200]))
    return r.stdout


def probe(path):
    """-> (Kapitel [(start, ende)], Gesamtdauer) in Sekunden."""
    out = _run(["ffprobe", "-v", "error", "-show_chapters", "-show_entries", "format=duration", "-of", "json", path],
               "Kapitel lesen", 120)
    try:
        d = json.loads(out)
        total = float((d.get("format") or {}).get("duration") or 0)
        chs = [(float(c["start_time"]), float(c["end_time"])) for c in d.get("chapters") or []]
    except (ValueError, KeyError, TypeError):
        raise ApiError(t("Kapitel lesen: unerwartete Ausgabe von ffprobe")) from None
    chs = sorted(c for c in chs if c[1] - c[0] >= MIN_CHAPTER)
    return chs, total


def silences(path, noise_db=-35, min_len=0.4, timeout=3600):
    """Pausen in einer Audiodatei finden (ffmpeg silencedetect, liest die ganze Datei). -> Mitten der Pausen in s."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-i", path, "-vn", "-sn", "-dn",
           "-af", f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ApiError(f"{t('Pausen suchen')}: {type(e).__name__}") from None
    if r.returncode != 0:
        raise ApiError(t("{what} fehlgeschlagen: {detail}", what=t("Pausen suchen"), detail="ffmpeg"))
    out, start = [], None
    for line in (r.stderr or "").splitlines():
        m = SIL.search(line)
        if not m:
            continue
        if m.group(1) == "start":
            start = float(m.group(2))
        elif start is not None:
            out.append((start + float(m.group(2))) / 2)
            start = None
    return out


SIL = re.compile(r"silence_(start|end): (-?\d+(?:\.\d+)?)")


def ranges(chs):
    """Kapitel -> lückenlose Bereiche [(start, ende|None)]: jeder reicht bis zum Beginn des nächsten,
    der erste beginnt bei 0, der letzte geht bis zum Dateiende (None)."""
    starts = [0.0] + [c[0] for c in chs[1:]]
    return [(s, starts[i + 1] if i + 1 < len(starts) else None) for i, s in enumerate(starts)]


def cut(path, ext, start, end, dest_base):
    """Bereich verlustfrei herausschneiden (-c copy). -> Pfad (Endung passend zum Format)."""
    out_ext = OUT_EXT.get((ext or "").lower().lstrip("."))
    if not out_ext:
        raise ApiError(t("Format .{ext} kann nicht geschnitten werden", ext=ext))
    dest = f"{dest_base}.{out_ext}"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start or 0:.3f}", "-i", path]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0):.3f}"]
    cmd += ["-map", "0:a:0", "-c", "copy", "-map_chapters", "-1", "-map_metadata", "-1", dest]
    _run(cmd, "Kapitel schneiden", 1800)
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise ApiError(t("Kapitel schneiden: leeres Ergebnis"))
    return dest


def join(parts, dest_base):
    """Beliebige Audiodateien bzw. Bereiche daraus zu *einer* Datei verbinden (neu kodiert als FLAC, also ohne
    weiteren Qualitätsverlust; TeddyCloud kodiert ohnehin nach Opus). parts: [(pfad, start|None, ende|None)].
    Einheitlich 48 kHz stereo (das braucht Opus ohnehin), damit unterschiedliche Formate zusammenpassen.
    -> Pfad (.flac)"""
    dest = f"{dest_base}.flac"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    for path, start, end in parts:
        if start:
            cmd += ["-ss", f"{start:.3f}"]
        if end is not None:
            cmd += ["-t", f"{end - (start or 0):.3f}"]
        cmd += ["-i", path]
    chains = "".join(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo[a{i}];"
                     for i in range(len(parts)))
    graph = chains + "".join(f"[a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
    cmd += ["-filter_complex", graph, "-map", "[out]", "-c:a", "flac", "-compression_level", "5",
            "-map_metadata", "-1", dest]
    _run(cmd, "Zusammenfügen", 3600)
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise ApiError(t("Zusammenfügen: leeres Ergebnis"))
    return dest


def split(path, ext, outdir, limit):
    """Datei an den Kapitelmarken schneiden. -> Liste der Stück-Pfade in Reihenfolge, [] wenn < 2 Kapitel.
    (Hilfsfunktion; der Worker plant Kapitel über worker.expand und schneidet mit cut.)"""
    if not OUT_EXT.get((ext or "").lower().lstrip(".")):
        return []
    chs, _ = probe(path)
    if len(chs) < 2:
        return []
    rs = ranges(chs)
    groups = merge_groups([(e if e is not None else chs[-1][1]) - s for s, e in rs], limit)
    return [cut(path, ext, rs[g[0]][0], rs[g[-1]][1], os.path.join(outdir, f"kap{k + 1:03d}"))
            for k, g in enumerate(groups)]
