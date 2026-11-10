"""Sprache der Meldungen (Deutsch/Englisch).

Quelltexte sind deutsch; t() liefert je nach aktueller Sprache den deutschen Text oder die englische
Übersetzung aus EN (Schlüssel = deutscher Text, Platzhalter im str.format-Stil).

Aktuelle Sprache: pro HTTP-Anfrage (Middleware) bzw. pro Auftrag (Worker-Thread) in einer ContextVar.
Ohne gesetzte Sprache gilt DEFAULT. Logs bleiben deutsch.
"""
import contextvars

LANGS = ("de", "en")
DEFAULT = "de"
_cur = contextvars.ContextVar("teddybridge_lang", default=None)


def norm(lang):
    """'de-CH' -> 'de'; Unbekanntes -> None."""
    lang = (lang or "").strip().lower()[:2]
    return lang if lang in LANGS else None


def from_accept(header):
    """Erste unterstützte Sprache aus Accept-Language (Reihenfolge des Browsers, q-Werte grob beachtet)."""
    best = []
    for i, part in enumerate((header or "").split(",")):
        code, _, q = part.strip().partition(";q=")
        try:
            qv = float(q) if q else 1.0
        except ValueError:
            qv = 0.0
        lang = norm(code)
        if lang and qv > 0:
            best.append((-qv, i, lang))
    return min(best)[2] if best else None


def get():
    return _cur.get() or DEFAULT


def set(lang):
    """Sprache für den aktuellen Kontext (Anfrage/Thread) setzen. -> Token für reset()."""
    return _cur.set(norm(lang) or DEFAULT)


def reset(token):
    _cur.reset(token)


def t(text, lang=None, **kw):
    lang = norm(lang) or get()
    out = EN.get(text, text) if lang == "en" else text
    return out.format(**kw) if kw else out


EN = {
    # --- HTTP/API
    "Anmeldung erforderlich": "Login required",
    "Audiobookshelf ist nicht konfiguriert": "Audiobookshelf is not configured",
    "Navidrome ist nicht konfiguriert": "Navidrome is not configured",
    "Dateiquelle ist nicht konfiguriert": "File source is not configured",
    "Sammlung nicht gefunden": "Collection not found",
    "ungültiger Musikordner": "invalid music folder",
    "ungültige Ordner-ID": "invalid folder ID",
    "ungültige ID": "invalid ID",
    "Nur-Lese-Modus: Übertragen ist deaktiviert": "Read-only mode: transfers are disabled",
    "Dateiquelle: Freigabe fehlt": "File source: share missing",
    "entweder Aufteilen oder Zusammenfügen": "either split or merge, not both",
    "Playlists gibt es nur bei Navidrome": "Playlists exist only in Navidrome",
    "eigene Playlist: ungültige oder leere Titelliste": "custom playlist: invalid or empty track list",
    "eigene Playlist: Name fehlt": "custom playlist: name missing",
    "Sammel-TAF: Name fehlt": "Combined TAF: name missing",
    "Sammel-TAF: mindestens zwei Einträge auswählen": "Combined TAF: select at least two items",
    "Sammel-TAF: ein Eintrag ist doppelt ausgewählt": "Combined TAF: an item is selected twice",
    # --- Ablage (Ordner- und Dateinamen in TeddyCloud)
    "Sonstige": "Other",
    "Bibliothek": "Library",
    "Sammlungen": "Collections",
    "Hoerspiel": "Audiobook",
    "{clean} - Teil {k} von {n} [{mark}].taf": "{clean} - Part {k} of {n} [{mark}].taf",
    # --- Auftrag
    "ungültiger Zielordner": "invalid target folder",
    "interner Fehler ({name})": "internal error ({name})",
    "„{title}“: keine Audiodateien": "“{title}”: no audio files",
    "keine Audiodateien": "no audio files",
    "schon als eine TAF vorhanden (zum Aufteilen erst in TeddyCloud löschen)":
        "already present as one TAF (delete it in TeddyCloud first to split it)",
    "nicht aufgeteilt (kürzer als ca. {m} min)": "not split (shorter than about {m} min)",
    "nicht aufteilbar (eine Datei ohne Kapitel)": "cannot be split (one file without chapters)",
    "in {n} Teile à ca. {m} min aufgeteilt": "split into {n} parts of about {m} min",
    "Kapitel alle ca. {m} min ergänzt": "added a chapter about every {m} min",
    "{k} von {n} Schnitten in einer Pause": "{k} of {n} cuts placed in a pause",
    "Pausen suchen": "Finding pauses",
    "Kapitel nach Dauer: mindestens 3 min": "chapters by duration: at least 3 min",
    "liegt in TeddyCloud schon in {old} Teilen vor, jetzt wären es {n} – nicht gemischt; "
    "zum neu Aufteilen die vorhandenen Teile in TeddyCloud löschen":
        "already in TeddyCloud as {old} parts, now it would be {n} – not mixed; "
        "delete the existing parts in TeddyCloud to split it again",
    "entweder Teile nach Dauer oder Aufteilen/Zusammenfügen": "either parts by duration or split/merge",
    "eine Sammel-TAF kann nicht nach Dauer aufgeteilt werden": "a combined TAF cannot be split by duration",
    "Teile nach Dauer: mindestens 10 min": "parts by duration: at least 10 min",
    "{n} Audiodateien, TeddyCloud erlaubt max. {max} – nicht übertragen (weder Aufteilen noch Zusammenfügen gewählt)":
        "{n} audio files, TeddyCloud allows at most {max} – not transferred (neither split nor merge chosen)",
    "in TeddyCloud liegt eine unvollständige TAF „{name}“ in {folder} (früher abgebrochen) – bitte dort löschen und das Hörbuch neu übertragen":
        "TeddyCloud contains an incomplete TAF “{name}” in {folder} (aborted earlier) – please delete it there and transfer the audiobook again",
    "Zusammenfügen von {fmts} braucht ffmpeg (nicht installiert) – bitte „Aufteilen“ wählen":
        "Merging {fmts} needs ffmpeg (not installed) – please choose “Split”",
    "{name}: Zusammenfügen braucht ffmpeg (nicht installiert) – bitte „Aufteilen“ wählen":
        "{name}: merging needs ffmpeg (not installed) – please choose “Split”",
    "Datei {n}": "file {n}",
    "{n} Datei in {p} Kapitel geschnitten": "{n} file cut into {p} chapters",
    "{n} Dateien in {p} Kapitel geschnitten": "{n} files cut into {p} chapters",
    ", zu {u} zusammengefügt": ", merged into {u}",
    "{n} Datei zu {u} Kapiteln zusammengefügt": "{n} file merged into {u} chapters",
    "{n} Dateien zu {u} Kapiteln zusammengefügt": "{n} files merged into {u} chapters",
    "{b} Hörbücher, {u} Kapitel": "{b} audiobooks, {u} chapters",
    "TeddyCloud meldete einen Fehler ({err}), die TAF ist aber vollständig":
        "TeddyCloud reported an error ({err}), but the TAF is complete",
    "Umwandeln {n}× abgebrochen ({err}), automatisch wiederholt":
        "encoding aborted {n}× ({err}), retried automatically",
    "{err} – TeddyCloud hat das Umwandeln {n}× abgebrochen, aufgegeben; die halbe TAF wurde entfernt, ein neuer Versuch ist jederzeit möglich":
        "{err} – TeddyCloud aborted encoding {n}×, giving up; the partial TAF was removed, you can try again any time",
    "{err} – TeddyCloud hat das Umwandeln abgebrochen (Neustart/Absturz?). Eine unvollständige TAF „{taf}“ ({mb} MB, {tracks} von {expected} Kapiteln) liegt in {folder}: bitte in TeddyCloud löschen und das Hörbuch neu übertragen":
        "{err} – TeddyCloud aborted encoding (restart/crash?). An incomplete TAF “{taf}” ({mb} MB, {tracks} of {expected} chapters) is in {folder}: please delete it in TeddyCloud and transfer the audiobook again",
    "TeddyCloud antwortet nicht": "TeddyCloud is not responding",
    "{tracks} Kapitel, {mm}:{ss}": "{tracks} chapters, {mm}:{ss}",
    "{taf}: {label}, erwartet {expected} Kapitel": "{taf}: {label}, expected {expected} chapters",
    ", TAF ungültig": ", TAF invalid",
    "Aufräumen verweigert: kein _src_-Ordner": "cleanup refused: not a _src_ folder",
    # --- Einrichtung (app/setup.py)
    "Einmal-Code erforderlich": "One-time code required",
    "Einmal-Code falsch": "Wrong one-time code",
    "ungültige Eingabe": "invalid input",
    "Adresse bitte mit http:// oder https:// angeben, z. B. http://teddycloud": "Please enter the address with http:// or https://, e.g. http://teddycloud",
    "TeddyCloud-Benutzer darf keinen Doppelpunkt enthalten": "The TeddyCloud user must not contain a colon",
    "TeddyCloud: Passwort fehlt": "TeddyCloud: password missing",
    "Login: Benutzer fehlt": "Login: user missing",
    "Login: Passwort mindestens {n} Zeichen": "Login: password needs at least {n} characters",
    "Login: Passwörter stimmen nicht überein": "Login: passwords do not match",
    "Login: Passwort fehlt": "Login: password missing",
    "TeddyCloud-Adresse fehlt": "TeddyCloud address missing",
    "TeddyCloud erreichbar, Bibliothek lesbar": "TeddyCloud reachable, library readable",
    "Antwort ist keine TeddyCloud-Bibliothek – Adresse prüfen": "The answer is not a TeddyCloud library – check the address",
    "TeddyCloud verlangt eine Anmeldung: Benutzer und Passwort eintragen": "TeddyCloud requires a login: enter user and password",
    "TeddyCloud: Zugriff verweigert – Benutzer und Passwort prüfen": "TeddyCloud: access denied – check user and password",
    "TeddyCloud antwortet mit HTTP {status} – Adresse prüfen": "TeddyCloud answers with HTTP {status} – check the address",
    "Audiobookshelf: Adresse und API-Token eintragen": "Audiobookshelf: enter address and API token",
    "Audiobookshelf erreichbar: {n} Bibliotheken ({names})": "Audiobookshelf reachable: {n} libraries ({names})",
    "Navidrome: Adresse, Benutzer und Passwort eintragen": "Navidrome: enter address, user and password",
    "Navidrome erreichbar: {n} Musikordner": "Navidrome reachable: {n} music folders",
    "Dateiquelle: mindestens einen Ordner als Name=/pfad eintragen": "File source: enter at least one folder as Name=/path",
    "nicht lesbar oder nicht vorhanden: {list}": "not readable or missing: {list}",
    "{n} Ordner lesbar": "{n} folder(s) readable",
    "Fehler: {name}": "Error: {name}",
    "{path} ist nicht beschreibbar (Docker: Volume für /config einhängen)": "{path} is not writable (Docker: mount a volume for /config)",
    "Speichern fehlgeschlagen ({name})": "Saving failed ({name})",
    "Einstellungen im Browser sind nicht aktiv (TB_CONFIG_FILE fehlt)": "Settings in the browser are not active (TB_CONFIG_FILE missing)",
    "Einmal-Code falsch oder abgelaufen": "One-time code wrong or expired",
    "Aktuelles Passwort falsch": "Current password is wrong",
    "Es läuft noch eine Übertragung – bitte warten, bis die Warteschlange fertig ist": "A transfer is still running – please wait until the queue is finished",
    # --- Discord
    "✅ teddybridge: **{name}** fertig": "✅ teddybridge: **{name}** done",
    "❌ teddybridge: **{name}** fehlgeschlagen: {err}": "❌ teddybridge: **{name}** failed: {err}",
    "teddybridge: {x} übertragen, {y} übersprungen, {z} Fehler": "teddybridge: {x} transferred, {y} skipped, {z} errors",
    # --- Quellen/Clients
    "ABS {what}: Verbindungsfehler ({name})": "ABS {what}: connection error ({name})",
    "Navidrome {what}: Verbindungsfehler ({name})": "Navidrome {what}: connection error ({name})",
    "Navidrome {what}: unerwartete Antwort": "Navidrome {what}: unexpected response",
    "TeddyCloud {what}: Verbindungsfehler ({name})": "TeddyCloud {what}: connection error ({name})",
    "TeddyCloud: Zugriff verweigert (HTTP {status}, TC_AUTH prüfen)": "TeddyCloud: access denied (HTTP {status}, check TC_AUTH)",
    "TeddyCloud Upload: HTTP {status}": "TeddyCloud upload: HTTP {status}",
    "TeddyCloud: Anmeldung abgelehnt (TC_AUTH prüfen)": "TeddyCloud: login rejected (check TC_AUTH)",
    "TeddyCloud: zu viele Anmeldeversuche, bitte später erneut versuchen": "TeddyCloud: too many login attempts, please try again later",
    "TeddyCloud {what}: HTTP {status}": "TeddyCloud {what}: HTTP {status}",
    "Anmeldung": "login",
    "Fehler": "error",
    "Kapitel lesen: unerwartete Ausgabe von ffprobe": "reading chapters: unexpected output from ffprobe",
    "Format .{ext} kann nicht geschnitten werden": "format .{ext} cannot be cut",
    "Kapitel schneiden: leeres Ergebnis": "cutting chapters: empty result",
    "Zusammenfügen: leeres Ergebnis": "merging: empty result",
    "{what} fehlgeschlagen: {detail}": "{what} failed: {detail}",
    "unbekannter Ordner": "unknown folder",
    "Freigabe „{name}“ ist nicht erreichbar (nicht eingehängt?)": "share “{name}” is not reachable (not mounted?)",
    "Ordner nicht gefunden": "folder not found",
    "Ordner nicht lesbar ({err})": "folder not readable ({err})",
    "Datei liegt ausserhalb der Freigabe": "file is outside the share",
    "{name} ist keine normale Datei": "{name} is not a regular file",
    "{name} nicht lesbar ({err})": "{name} not readable ({err})",
    "{name}: unvollständig gelesen": "{name}: read incompletely",
    "{name}: keine MP3-Audiodaten gefunden": "{name}: no MP3 audio data found",
    "TeddyCloud Upload: {name} kam unvollständig an ({last} von {size} Bytes, {n} Versuche).":
        "TeddyCloud upload: {name} arrived incomplete ({last} of {size} bytes, {n} attempts).",
    "{name}: anderes MP3-Format ({fmt} statt {fmt0}), kann nicht zusammengefügt werden – bitte „Aufteilen“ wählen":
        "{name}: different MP3 format ({fmt} instead of {fmt0}), cannot be merged – please choose “Split”",
    "Kapitel lesen": "reading chapters",
    "Kapitel schneiden": "cutting chapters",
    "Zusammenfügen": "merging",
}
