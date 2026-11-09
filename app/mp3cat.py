"""MP3-Dateien verlustfrei aneinanderhängen (ohne ffmpeg, ohne Neukodierung).

Wofür: Hat ein Hörbuch mehr als 99 Dateien, werden kurze, benachbarte Dateien zu einer zusammengefügt,
damit alles in *eine* TAF passt (TeddyCloud macht aus jeder Quelldatei ein Kapitel).

Pro Datei werden Tags (ID3v2 am Anfang, ID3v1/APEv2 am Ende) und der Xing/Info/VBRI-Kopf entfernt;
der bezieht sich nur auf die einzelne Datei und würde den Decoder bei der Gesamtlänge verwirren.
Übrig bleiben reine MPEG-Audio-Frames, die direkt hintereinander gültig sind, solange
MPEG-Version, Abtastrate und Mono/Stereo übereinstimmen (wird geprüft).
"""
import os
import struct

from .clients import ApiError

# Bitraten (kbit/s) für Layer III: [MPEG1, MPEG2/2.5]
_BITRATES = ([0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
             [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0])
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}  # Versions-Bits -> Raten
_SCAN = 64 << 10  # so weit nach dem ersten Frame suchen (Müll/Padding nach den Tags)


def _header(b):
    """4 Bytes -> (version_bits, rate, mono, frame_len) oder None, wenn kein gültiger Layer-III-Kopf."""
    if len(b) < 4 or b[0] != 0xFF or (b[1] & 0xE0) != 0xE0:
        return None
    ver = (b[1] >> 3) & 3
    layer = (b[1] >> 1) & 3
    br_i = (b[2] >> 4) & 15
    sr_i = (b[2] >> 2) & 3
    if ver == 1 or layer != 1 or br_i in (0, 15) or sr_i == 3:  # reserviert / nicht Layer III / free format
        return None
    rate = _RATES[ver][sr_i]
    br = _BITRATES[0 if ver == 3 else 1][br_i] * 1000
    pad = (b[2] >> 1) & 1
    mono = (b[3] >> 6) == 3
    length = (144 if ver == 3 else 72) * br // rate + pad
    return ver, rate, mono, length


def _id3v2_size(h):
    if len(h) >= 10 and h[:3] == b"ID3" and all(x < 0x80 for x in h[6:10]):
        size = (h[6] << 21) | (h[7] << 14) | (h[8] << 7) | h[9]
        return 10 + size + (10 if h[5] & 0x10 else 0)  # + Footer
    return 0


def audio_range(path):
    """-> (start, ende, format) der reinen Audio-Frames einer MP3-Datei."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        # Ende: ID3v1 (128 Bytes "TAG") und/oder APEv2 ("APETAGEX"-Footer) abschneiden, in beliebiger Reihenfolge
        end = size
        for _ in range(3):
            if end >= 128:
                f.seek(end - 128)
                if f.read(3) == b"TAG":
                    end -= 128
                    continue
            if end >= 32:
                f.seek(end - 32)
                foot = f.read(32)
                if foot[:8] == b"APETAGEX":
                    tag_size = struct.unpack("<I", foot[12:16])[0]
                    has_header = struct.unpack("<I", foot[20:24])[0] & 0x80000000
                    end -= tag_size + (32 if has_header else 0)
                    continue
            break
        # Anfang: (evtl. mehrere) ID3v2-Tags überspringen
        pos = 0
        while True:
            f.seek(pos)
            n = _id3v2_size(f.read(10))
            if not n:
                break
            pos += n
        # ersten Frame suchen, der von einem zweiten gültigen Frame gefolgt wird (gegen Zufallstreffer)
        f.seek(pos)
        buf = f.read(_SCAN + 4096)
        start = fmt = None
        for i in range(0, min(len(buf) - 4, _SCAN)):
            h = _header(buf[i:i + 4])
            if not h:
                continue
            nxt = buf[i + h[3]:i + h[3] + 4]
            h2 = _header(nxt) if len(nxt) == 4 else None
            if h2 and h2[:3] == h[:3] or (not h2 and pos + i + h[3] >= end):
                start, fmt = pos + i, h
                break
        if start is None or start >= end:
            raise ApiError(f"{os.path.basename(path)}: keine MP3-Audiodaten gefunden")
        # Xing/Info/VBRI-Frame am Anfang? Er enthält kein Audio, nur Infos zu dieser einen Datei.
        ver, _, mono, flen = fmt
        f.seek(start)
        first = f.read(flen)
        side = (32 if not mono else 17) if ver == 3 else (17 if not mono else 9)
        side += 0 if first[1] & 1 else 2  # mit CRC-Schutz 2 Bytes mehr
        if first[4 + side:8 + side] in (b"Xing", b"Info") or first[36:40] == b"VBRI":
            start += flen
    return start, end, fmt[:3]


def concat(paths, dest, names=None):
    """Mehrere MP3-Dateien zu einer zusammenfügen. Wirft ApiError bei unpassendem Format.
    names: Anzeigenamen für Fehlermeldungen (sonst Dateinamen)."""
    names = names or [os.path.basename(p) for p in paths]
    ranges = []
    for p, nm in zip(paths, names):
        try:
            ranges.append(audio_range(p))
        except ApiError:
            raise ApiError(f"{nm}: keine MP3-Audiodaten gefunden") from None
    fmt0 = ranges[0][2]
    for nm, (_, _, fmt) in zip(names, ranges):
        if fmt != fmt0:
            raise ApiError(f"{nm}: anderes MP3-Format ({_fmt(fmt)} statt {_fmt(fmt0)}), "
                           f"kann nicht zusammengefügt werden – bitte „Aufteilen“ wählen")
    with open(dest, "wb") as out:
        for p, (start, end, _) in zip(paths, ranges):
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)


def _fmt(fmt):
    ver, rate, mono = fmt
    return f"MPEG{ {3: '1', 2: '2', 0: '2.5'}[ver] } {rate} Hz {'mono' if mono else 'stereo'}"


def merge_groups(weights, limit, groups_of=None):
    """Benachbarte Einträge zusammenlegen, bis höchstens `limit` Gruppen übrig sind.
    Es wird immer das benachbarte Paar mit dem kleinsten Gesamtgewicht (Dauer oder Grösse) zusammengelegt,
    so verschwinden nur die Grenzen zwischen den kürzesten Kapiteln.
    groups_of: optional je Eintrag eine Kennung (z. B. das Hörbuch); über eine Grenze zwischen zwei
    Kennungen wird nur zusammengelegt, wenn es anders nicht geht.
    -> Liste von Index-Listen in Originalreihenfolge."""
    groups = [[i] for i in range(len(weights))]
    w = list(weights)
    tag = [(g, g) for g in (groups_of or [0] * len(weights))]  # (Kennung erstes, Kennung letztes Element)
    while len(groups) > limit:
        k = min(range(len(groups) - 1), key=lambda i: (tag[i][1] != tag[i + 1][0], w[i] + w[i + 1], i))
        groups[k:k + 2] = [groups[k] + groups[k + 1]]
        w[k:k + 2] = [w[k] + w[k + 1]]
        tag[k:k + 2] = [(tag[k][0], tag[k + 1][1])]
    return groups
