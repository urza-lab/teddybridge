#!/usr/bin/env python3
"""Tests für MP3-Zusammenfügen (app/mp3cat.py) und Kapitel-Schnitt (app/chapters.py) mit echten Audiodateien.
Braucht ffmpeg/ffprobe (erzeugt die Testdateien und dekodiert die Ergebnisse). Start: venv/bin/python tests/test_audio.py"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import chapters, mp3cat  # noqa: E402
from app.clients import ApiError  # noqa: E402

FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def tone(path, secs, rate=44100, ch=2, freq=440, extra=()):
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={rate}:duration={secs}",
       "-ac", str(ch), *extra, path)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def decoded_seconds(path):
    """Wirklich dekodierte Länge (nicht die Angabe im Kopf) und Dekodierfehler."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                       capture_output=True)
    return len(r.stdout) / 2 / 8000, r.stderr.decode(errors="replace").strip()


def m4b(path, chapter_secs, tmp):
    """m4b (AAC) mit Kapitelmarken erzeugen."""
    raw = os.path.join(tmp, "raw.m4a")
    tone(raw, sum(chapter_secs), extra=("-c:a", "aac", "-b:a", "64k"))
    meta = os.path.join(tmp, "chapters.txt")
    with open(meta, "w") as f:
        f.write(";FFMETADATA1\ntitle=Testbuch\n")
        t = 0
        for i, s in enumerate(chapter_secs, 1):
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(t * 1000)}\nEND={int((t + s) * 1000)}\ntitle=Kapitel {i}\n")
            t += s
    ff("-i", raw, "-i", meta, "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-c", "copy", "-f", "mp4", path)


def main():
    if not chapters.available():
        print("ffmpeg/ffprobe fehlt – Test übersprungen")
        return 0
    tmp = tempfile.mkdtemp(prefix="tb-audio-")
    try:
        print("Gruppieren (merge_groups)")
        for n in (100, 139, 198, 199, 702):
            w = [random.Random(n + i).uniform(5, 600) for i in range(n)]
            g = mp3cat.merge_groups(w, 99)
            check(len(g) == 99 and [i for grp in g for i in grp] == list(range(n)),
                  f"{n} Einträge -> 99 Gruppen, alle in Reihenfolge, keiner doppelt/verloren")
        g = mp3cat.merge_groups([100, 1, 1, 100, 100], 4)
        check(g == [[0], [1, 2], [3], [4]], "kürzeste Nachbarn werden zuerst zusammengelegt")
        check(mp3cat.merge_groups([5] * 10, 99) == [[i] for i in range(10)], "≤ Grenze: nichts zusammengelegt")

        from app.worker import encode_wait
        check(encode_wait([{"dur": 52 * 60}]) == 3600, "Wartezeit Umwandeln: mind. 60 min")
        check(encode_wait([{"dur": 16.7 * 3600}]) > 4 * 3600, "Wartezeit wächst mit der Hörzeit (17 h Hörbuch > 4 h)")
        check(encode_wait([{"dur": 0, "size": 500 << 20}]) > 3 * 3600, "Dateiquelle ohne Dauer: aus Grösse geschätzt")
        print("Planung (Stücke, Buchgrenzen, ohne ffmpeg)")
        from app import worker
        f1 = {"ext": "m4b", "dur": 30.0, "chapters": [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)], "book": 0}
        f2 = {"ext": "mp3", "dur": 5.0, "book": 1}
        ps = worker.expand([f1, f2])
        check([(p["start"], p["end"]) for p in ps] == [(0.0, 10.0), (10.0, 20.0), (20.0, None), (None, None)],
              "m4b in lückenlose Kapitelbereiche zerlegt, letzter bis Dateiende")
        n = worker.normalize(ps[:3])
        check(len(n) == 1 and n[0]["start"] is None and n[0]["end"] is None, "alle Kapitel einer Datei = ganze Datei")
        n = worker.normalize(ps[1:3])
        check(len(n) == 1 and n[0]["start"] == 10.0 and n[0]["end"] is None, "benachbarte Kapitel = ein Bereich")
        u = worker.merge_units(ps, limit=2)
        check([[p["book"] for p in g] for g in u] == [[0, 0, 0], [1]], "Buchgrenze bleibt, solange es geht")
        check(worker.note_for(1, 139, 139, 99) == "139 Dateien zu 99 Kapiteln zusammengefügt"
              and worker.note_for(1, 1, 12, 12) == "1 Datei in 12 Kapitel geschnitten"
              and worker.note_for(1, 5, 5, 5) is None
              and worker.note_for(2, 3, 3, 3) == "2 Hörbücher, 3 Kapitel", "Hinweistexte")
        print("Teile à ca. 60 min")
        H = 3600
        mp = lambda d: {"ext": "mp3", "dur": d, "book": 0}  # noqa: E731
        lens = lambda cs: [round(sum(p["dur"] for p in c) / 60) for c in cs]  # noqa: E731
        check(len(worker.split_by_time(worker.expand([mp(600)] * 8), H)) == 1, "80 min: bleibt eine TAF")
        cs = worker.split_by_time(worker.expand([mp(600)] * 25), H)
        check(len(cs) == 4 and sum(len(c) for c in cs) == 25 and lens(cs) == [60, 60, 70, 60],
              "250 min aus 10-min-Dateien: 4 Teile, an Dateigrenzen nahe 60 min")
        cs = worker.split_by_time(worker.expand([mp(3000), mp(3000), mp(600), mp(4800)]), H)
        check(lens(cs) == [50, 60, 80], "190 min (50/50/10/80): 3 Teile, Schnitt an der jeweils nächsten Grenze")
        fa = {"ext": "mp3", "dur": 4 * H + 600, "book": 0}
        real = chapters.available
        chapters.available = lambda: True
        try:
            cs = worker.split_by_time(worker.expand([fa]), H)
            rs = [(round(c[0]["start"]), c[0]["end"] and round(c[0]["end"])) for c in cs]
            check(len(cs) == 4 and rs[0] == (0, 3750) and rs[-1] == (11250, None),
                  "eine lange Datei: in 4 gleich lange Bereiche geschnitten, letzter bis Dateiende")
            m4 = {"ext": "m4b", "dur": 3 * H, "chapters": [(i * 600.0, (i + 1) * 600.0) for i in range(18)], "book": 0}
            cs = worker.split_by_time(worker.expand([m4]), H)
            check(len(cs) == 3 and [len(c) for c in cs] == [6, 6, 6], "m4b: an Kapitelgrenzen in 3 Teile")
            cs = worker.split_by_time(worker.expand([mp(30)] * 400), H)
            check(len(cs) == 3 and all(len(c) > 99 for c in cs) and all(len(worker.merge_units(c)) <= 99 for c in cs),
                  "viele kurze Dateien: je Teil > 99 Stücke, Zusammenfügen pro Teil")
        finally:
            chapters.available = real
        chapters.available = lambda: False
        try:
            check(len(worker.split_by_time(worker.expand([fa]), H)) == 1, "ohne ffmpeg: eine lange Datei bleibt ganz")
        finally:
            chapters.available = real
        print("Kapitel ohne Marken: Zerlegen und Schnitte an Pausen")
        chapters.available = lambda: True
        try:
            f = {"ext": "mp3", "dur": 1800.0, "book": 0}
            ps = worker.subdivide(worker.expand([f, mp(300)]), 600, only_whole=True)
            check(len(ps) == 4 and ps[0]["start"] == 0.0 and ps[2]["end"] is None and not ps[3].get("cut"),
                  "30-min-Datei à 10 min: 3 Bereiche, kurze Datei bleibt")
            m4 = worker.expand([{"ext": "m4b", "dur": 3 * H, "chapters": [(0.0, H), (H, 3 * H)], "book": 0}])
            check(len(worker.subdivide(m4, 600, only_whole=True)) == 2, "Kapitelbereiche werden beim Kapitel-Fallback nicht zerlegt")
            moved, total = worker.snap_cuts(ps, [590.0, 1400.0, 1900.0])
            check((moved, total) == (1, 2) and ps[0]["end"] == ps[1]["start"] == 590.0 and ps[1]["end"] == 1200.0
                  and abs(ps[0]["dur"] - 590) < 1e-6 and abs(ps[1]["dur"] - 610) < 1e-6,
                  "Schnitt rückt zur nahen Pause (590), zu weit entfernte (1400: 200 > 150 s) bleibt exakt")
            check(ps[2]["end"] is None and ps[3]["start"] is None, "Grenze zur nächsten Datei bleibt unberührt")
        finally:
            chapters.available = real
        if chapters.available():
            sp = os.path.join(tmp, "pause.mp3")
            ff("-f", "lavfi", "-i", "sine=d=5", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1", "-f", "lavfi", "-i", "sine=d=4",
               "-filter_complex", "[0]aformat=sample_rates=44100:channel_layouts=mono[a];[2]aformat=sample_rates=44100:channel_layouts=mono[c];"
               "[a][1][c]concat=n=3:v=0:a=1", "-c:a", "libmp3lame", "-b:a", "32k", sp)
            sil = chapters.silences(sp)
            check(len(sil) == 1 and abs(sil[0] - 5.5) < 0.2, f"Pause in echter MP3 gefunden ({sil})")
        check(worker.part_name("Buch", 3, 12, "abcd1234") == "Buch - Teil 03 von 12 [abcd1234-3].taf"
              and worker.part_name("Buch", 2, 2, "abcd1234") == "Buch - Teil 2 von 2 [abcd1234-2].taf",
              "Teil-Namen: führende Null ab 10 Teilen, Kennzeichen ohne")
        check(worker.markers(["Buch - Teil 03 von 12 [abcd1234-3].taf"]) == {"abcd1234-3"}, "Kennzeichen erkannt")
        check(worker.PARTS_OF.search("Buch - Part 03 of 12 [abcd1234-3].taf").group(1) == "12", "Teilzahl (EN) erkannt")

        real = chapters.available
        chapters.available = lambda: False
        try:
            check(len(worker.expand([f1])) == 1, "ohne ffmpeg: m4b bleibt ein Kapitel")
            try:
                worker.merge_units(worker.expand([{"ext": "m4a", "dur": 1, "book": 0}] * 120))
                check(False, "ohne ffmpeg: m4a zusammenfügen abgelehnt")
            except ApiError as e:
                check("braucht ffmpeg" in str(e) and "Aufteilen" in str(e), "ohne ffmpeg: m4a zusammenfügen abgelehnt")
            check(len(worker.merge_units(worker.expand([{"ext": "mp3", "dur": 1, "book": 0}] * 120))) == 99,
                  "ohne ffmpeg: MP3 zusammenfügen geht")
        finally:
            chapters.available = real

        print("Fortschritt und Restzeit")
        check(worker.transfer_eta(60, 0, 10) is None and worker.transfer_eta(60, 3, 12) == 180, "Restzeit Laden aus bisherigem Tempo")
        rest, frac = worker.encode_eta(3600, 600, 100 << 20, 400 << 20, (100 << 20) / 600, 17)
        check(abs(rest - 1800) < 1 and abs(frac - 0.25) < 1e-6, "Umwandeln: Restzeit/Anteil aus wachsender TAF")
        rest, frac = worker.encode_eta(17 * 600, 300, None, 0, 0, 17)
        check(abs(rest - 300) < 1 and abs(frac - 0.5) < 1e-6, "Umwandeln ohne Grösse: aus Erfahrungswert")
        rest, frac = worker.encode_eta(17 * 600, 900, None, 0, 0, 17)
        check(rest == 30 and frac == 0.95, "länger als geschätzt: nie 0 und nie 100 % vor dem Ende")
        rest, frac = worker.encode_eta(3600, 600, 500 << 20, 400 << 20, 1e6, 17)
        check(rest == 0 and frac == 0.99, "grösser als erwartet: 99 %, bis TeddyCloud fertig meldet")
        check(worker.fmt_dur(59) == "1 min" and worker.fmt_dur(1500) == "25 min" and worker.fmt_dur(39200) == "10 h 53 min",
              "Dauer lesbar")

        print("MP3 zusammenfügen")
        parts = []
        variants = [  # Tags/Header, die in echten Hörbuch-MP3s vorkommen
            ("cbr_id3v2.mp3", 2.0, ("-c:a", "libmp3lame", "-b:a", "128k", "-metadata", "title=Eins", "-id3v2_version", "3")),
            ("vbr_xing.mp3", 3.0, ("-c:a", "libmp3lame", "-q:a", "4", "-metadata", "title=Zwei")),
            ("id3v1.mp3", 1.5, ("-c:a", "libmp3lame", "-b:a", "128k", "-write_id3v1", "1", "-metadata", "title=Drei")),
            ("ohne_tags.mp3", 2.5, ("-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0", "-id3v2_version", "0")),
        ]
        for name, secs, extra in variants:
            p = os.path.join(tmp, name)
            tone(p, secs, extra=extra)
            parts.append((p, secs))
        # zusätzlich: APEv2-Tag am Ende und Müll-Bytes zwischen Tag und erstem Frame
        ape = os.path.join(tmp, "ape.mp3")
        shutil.copy(parts[0][0], ape)
        body = b"Title\x00" + b"x" * 8
        item = len(b"x" * 8).to_bytes(4, "little") + (0).to_bytes(4, "little") + body
        foot = b"APETAGEX" + (2000).to_bytes(4, "little") + (len(item) + 32).to_bytes(4, "little") + \
            (1).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\x00" * 8
        with open(ape, "ab") as f:
            f.write(item + foot)
        parts.append((ape, 2.0))
        for p, secs in parts:
            s, e, fmt = mp3cat.audio_range(p)
            check(0 <= s < e <= os.path.getsize(p) and fmt[1] == 44100, f"{os.path.basename(p)}: Audiobereich gefunden")
        s_xing, _, _ = mp3cat.audio_range(os.path.join(tmp, "vbr_xing.mp3"))
        with open(os.path.join(tmp, "vbr_xing.mp3"), "rb") as f:
            raw = f.read()
        check(b"Xing" not in raw[s_xing:s_xing + 200] and b"Info" not in raw[s_xing:s_xing + 200],
              "Xing/Info-Kopf wird übersprungen")
        _, e_ape, _ = mp3cat.audio_range(ape)
        check(e_ape <= os.path.getsize(parts[0][0]) and b"APETAGEX" not in open(ape, "rb").read()[:e_ape],
              "APEv2-Tag am Ende wird abgeschnitten")
        _, e_v1, _ = mp3cat.audio_range(os.path.join(tmp, "id3v1.mp3"))
        check(open(os.path.join(tmp, "id3v1.mp3"), "rb").read()[e_v1:e_v1 + 3] == b"TAG", "ID3v1-Tag am Ende wird abgeschnitten")

        out = os.path.join(tmp, "zusammen.mp3")
        mp3cat.concat([p for p, _ in parts], out)
        want = sum(s for _, s in parts)
        got, err = decoded_seconds(out)
        check(not err, f"Ergebnis dekodiert ohne Fehler ({err[:80] or 'keine Meldung'})")
        # pro Datei bleibt die Encoder-Verzögerung/Auffüllung von LAME (ca. 25–50 ms) als kurze Stille an der Naht
        check(-0.02 < got - want < 0.06 * len(parts), f"dekodierte Länge {got:.2f}s ≈ Summe {want:.2f}s "
              f"(+{(got - want) * 1000 / len(parts):.0f} ms je Naht)")
        with open(out, "rb") as f:
            data = f.read()
        check(b"ID3" not in data[:10] and b"Xing" not in data and b"APETAGEX" not in data and b"TAG" != data[-128:-125],
              "keine Tags/Xing-Köpfe im Ergebnis")

        print("MP3: unverträgliche Formate")
        low = os.path.join(tmp, "22k.mp3")
        tone(low, 1, rate=22050, extra=("-c:a", "libmp3lame", "-b:a", "64k"))
        mono = os.path.join(tmp, "mono.mp3")
        tone(mono, 1, ch=1, extra=("-c:a", "libmp3lame", "-b:a", "64k"))
        for bad, what in ((low, "andere Abtastrate"), (mono, "mono statt stereo")):
            try:
                mp3cat.concat([parts[0][0], bad], os.path.join(tmp, "x.mp3"))
                check(False, f"{what}: abgelehnt")
            except ApiError as e:
                check("anderes MP3-Format" in str(e) and "Aufteilen" in str(e), f"{what}: abgelehnt mit Hinweis")
        nomp3 = os.path.join(tmp, "kein.mp3")
        with open(nomp3, "wb") as f:
            f.write(b"AUDIO kein mp3\n" + b"x" * 3000)
        try:
            mp3cat.audio_range(nomp3)
            check(False, "Nicht-MP3 abgelehnt")
        except ApiError as e:
            check("keine MP3-Audiodaten" in str(e), "Nicht-MP3 abgelehnt")

        print("m4b an Kapiteln schneiden")
        secs = [3.0, 1.0, 4.0, 2.0, 5.0, 1.5, 2.5, 3.0, 1.0, 2.0, 4.0, 3.0]
        book = os.path.join(tmp, "buch.m4b")
        m4b(book, secs, tmp)
        outdir = os.path.join(tmp, "kap")
        os.makedirs(outdir)
        cut = chapters.split(book, "m4b", outdir, 99)
        check(len(cut) == 12 and all(p.endswith(".m4a") for p in cut), f"12 Kapitel -> 12 Stücke (.m4a): {len(cut)}")
        lens = [decoded_seconds(p) for p in cut]
        check(all(not e for _, e in lens), "alle Stücke dekodieren ohne Fehler")
        check(all(abs(l - s) < 0.1 for (l, _), s in zip(lens, secs)), "Stücklängen = Kapitellängen (±0,1 s)")
        total = sum(l for l, _ in lens)
        check(-0.05 < total - sum(secs) < len(cut) * 1024 / 44100, f"Summe {total:.2f}s ≈ Buchlänge {sum(secs):.2f}s (nichts verloren)")

        print("m4b mit mehr als 99 Kapiteln")
        rnd = random.Random(7)
        secs = [round(rnd.uniform(0.6, 3.0), 2) for _ in range(130)]
        big = os.path.join(tmp, "gross.m4b")
        m4b(big, secs, tmp)
        outdir = os.path.join(tmp, "kap2")
        os.makedirs(outdir)
        cut = chapters.split(big, "m4b", outdir, 99)
        check(len(cut) == 99, f"130 Kapitel -> 99 Stücke: {len(cut)}")
        total = sum(decoded_seconds(p)[0] for p in cut)
        frame = 1024 / 44100  # ein AAC-Frame: so viel Vorlauf darf jeder verlustfreie Schnitt mitbringen
        check(-0.05 < total - sum(secs) < len(cut) * frame, f"Summe {total:.2f}s ≈ Buchlänge {sum(secs):.2f}s "
              f"(+{(total - sum(secs)) * 1000 / len(cut):.0f} ms je Stück, max. 1 AAC-Frame = {frame * 1000:.0f} ms)")

        print("ohne Kapitel / unbekanntes Format")
        plain = os.path.join(tmp, "plain.m4a")
        tone(plain, 2, extra=("-c:a", "aac"))
        check(chapters.split(plain, "m4a", tmp, 99) == [], "Datei ohne Kapitel: nicht geschnitten")
        check(chapters.split(plain, "wma", tmp, 99) == [], "unbekanntes Format: nicht geschnitten")
        one = os.path.join(tmp, "eins.m4b")
        m4b(one, [3.0], tmp)
        check(chapters.split(one, "m4b", tmp, 99) == [], "nur 1 Kapitel: nicht geschnitten")
        broken = os.path.join(tmp, "kaputt.m4b")
        with open(broken, "wb") as f:
            f.write(b"\x00" * 1000)
        try:
            chapters.split(broken, "m4b", tmp, 99)
            check(False, "kaputte Datei: Fehler gemeldet")
        except ApiError as e:
            check("Kapitel lesen" in str(e) and tmp not in str(e), "kaputte Datei: verständlicher Fehler ohne Pfad")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALLE TESTS OK" if not FAILS else f"\n{len(FAILS)} FEHLER")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
