# teddybridge

Weboberfläche, um Hörspiele und Musik aus **Audiobookshelf**, **Navidrome** oder einem **NAS-Ordner** zu durchstöbern und per Klick als **TAF-Datei** in die Bibliothek von **[TeddyCloud](https://github.com/toniebox-reverse-engineering/teddycloud)** zu übertragen. Die Zuweisung zu einem Tonie erledigt man danach wie gewohnt in TeddyCloud.

*A small self-hosted web UI that converts audiobooks/albums from Audiobookshelf, Navidrome or a plain folder into TeddyCloud TAF files. The UI is in German.*

---

## Executive Summary

| | |
|---|---|
| **Zweck** | Hörspiele/Alben aus vorhandenen Medienservern in TeddyCloud bringen, ohne Dateien von Hand zu kopieren und umzuwandeln. |
| **Quellen** | Audiobookshelf (Pflicht), Navidrome (optional), Ordner im Dateisystem, z. B. NAS per NFS/SMB (optional). Alle **nur lesend**. |
| **Ziel** | TeddyCloud-Bibliothek. Es wird nur angelegt; gelöscht werden ausschliesslich die eigenen temporären `_src_*`-Ordner – und eine TAF, die derselbe Lauf selbst angelegt hat und die nach einem Abbruch von TeddyCloud nachweislich unvollständig ist (vor dem automatischen Neuversuch). |
| **Technik** | Python 3.11+, FastAPI/uvicorn, httpx, eine HTML-Seite mit Vanilla-JS ohne Build-Schritt. Keine Datenbank. Optional ffmpeg (m4b-Kapitel, Zusammenfügen anderer Formate als MP3). |
| **Betrieb** | systemd-Dienst mit gehärteter Unit (Vorlage in `deploy/`). Gedacht für ein vertrauenswürdiges Heimnetz, optional hinter einem internen Reverse Proxy mit HTTPS. **Nicht ins Internet stellen.** |
| **Sicherheit** | Login mit scrypt-Hash und HttpOnly-Session-Cookie. Zugangsdaten zu ABS, Navidrome und TeddyCloud bleiben im Backend. Security-Header (CSP, `frame-ancestors 'none'`). Keine bekannten Schwachstellen in den Abhängigkeiten (pip-audit, Stand 2026-11-08). Details siehe [Sicherheit](#sicherheit). |

## Funktionsweise

1. **Stöbern:** Bibliotheken, Serien/Künstler, Sammlungen/Playlists, Ordner, Suche und Cover. Alle Anfragen an die Quellen laufen über das Backend; der Browser sieht nie einen Token.
2. **Status:** Jede TAF trägt ein 8-stelliges Kennzeichen im Namen, z. B. `Titel [1a2b3c4d].taf`. Ist es irgendwo unter dem Basisordner schon vorhanden, gilt das Hörspiel als übertragen und wird übersprungen, auch wenn es umbenannt oder verschoben wurde.
3. **Übertragen:** Ausgewählte Einträge kommen in eine Warteschlange und werden **nacheinander** abgearbeitet, weil TeddyCloud beim Umwandeln viel CPU braucht:
   1. temporären Ordner `<ziel>/_src_<id>` in TeddyCloud anlegen,
   2. Audiodateien einzeln aus der Quelle laden (lokales Temp-Verzeichnis) und hochladen,
   3. TeddyCloud wandelt sie in **eine** TAF um (`fileEncode`, max. 99 Dateien; grössere Hörspiele können in Teile gesplittet werden),
   4. TAF prüfen und `_src_` wieder löschen, auch bei Fehlern.
   5. Bricht TeddyCloud beim Umwandeln ab (z. B. Neustart oder Backup des Hosts), wartet teddybridge bis zu 45 min, bis TeddyCloud wieder antwortet, und prüft die TAF: wächst sie noch, wird gewartet; ist sie vollständig, ist der Auftrag fertig; ist sie unvollständig, wird diese eigene halbe TAF entfernt und aus den noch vorhandenen `_src_`-Dateien neu umgewandelt (höchstens 2 Neuversuche). Eine halbe TAF aus einem früheren Lauf wird nie gelöscht, sondern beim nächsten Übertragen gemeldet.
4. **Ablage:** automatisch `TC_BASE/<Bibliothek>/<Serie oder Autor>` bzw. `TC_BASE/<Freigabe>/<Ordnerpfad>`, alternativ ein frei gewählter Unterordner.
5. **Fortschritt:** Die Warteschlange im Browser zeigt pro Auftrag einen Balken, Prozent und die geschätzte Restzeit mit Uhrzeit („noch ca. 25 min, fertig ~20:15“). Beim Laden aus dem bisherigen Tempo; beim Umwandeln, für das TeddyCloud keinen Fortschritt meldet, aus der wachsenden TAF-Datei (TeddyCloud schreibt gleichmässig ca. 0,65 MB je Hörminute, ca. 17-fache Echtzeit). Beide Werte lernt teddybridge aus jedem fertigen Auftrag nach.
6. **Meldung:** optional per Discord-Webhook pro Auftrag – ✅ fertig (Kapitel, Dauer, Ordner) bzw. ❌ Fehler mit Grund; bei mehreren Aufträgen zusätzlich die Summe („x übertragen, y übersprungen, z Fehler“). Erwähnungen (`@everyone` o. ä. in Titeln) sind abgeschaltet.

Die Warteschlange lebt nur im Speicher. Ein Neustart verwirft offene Aufträge; das ist so gewollt.

### Mehr als 99 Dateien

Eine TAF kann höchstens 99 Kapitel haben (eine Quelldatei = ein Kapitel). Bei mehr Dateien fragt die Oberfläche:

- **Zusammenfügen** (Standard): eine einzige TAF mit 99 Kapiteln. Es werden immer die beiden aufeinanderfolgenden Dateien mit der kürzesten Gesamtdauer verbunden, bis 99 übrig sind – so verschwinden nur die Grenzen zwischen den kürzesten Kapiteln. **MP3** wird verlustfrei aneinandergehängt (ohne Neukodierung, ohne Zusatzprogramm); pro zusammengefügter Datei bleibt die übliche MP3-Encoder-Pause von ca. 35 ms. **Andere Formate** (m4a, FLAC, Ogg …) und MP3 mit unterschiedlicher Abtastrate oder Mono/Stereo werden mit **ffmpeg** zu einem FLAC-Kapitel verbunden (verlustfrei gegenüber der Quelle; TeddyCloud kodiert danach ohnehin nach Opus). Ohne ffmpeg geht Zusammenfügen nur bei einheitlichem MP3.
- **Aufteilen:** mehrere TAFs `<Titel> - Teil k von n [<id>-k].taf` mit gleich vielen Dateien; fehlt später ein Teil, wird nur dieser nachgeholt.
- **Weglassen.**

Ein Hörbuch gilt in beiden Formen als vorhanden: Gibt es schon Teile, wird nicht zusätzlich zusammengefügt (und umgekehrt).

### Eine Datei mit Kapiteln (m4b)

Ist **ffmpeg** installiert (optional), wird ein Hörbuch, das aus einer einzigen Datei mit Kapitelmarken besteht (typisch `.m4b`), an den Kapiteln verlustfrei (`-c copy`) geschnitten – jedes Kapitel wird ein TAF-Kapitel. Die Kapitel kommen bei Audiobookshelf aus dessen Metadaten, bei der Dateiquelle liest ffprobe sie direkt (nur lesend). Bei mehr als 99 Kapiteln werden die kürzesten benachbarten zusammengelegt (nur weniger Schnitte). Ohne ffmpeg oder ohne Kapitelmarken wird die Datei wie bisher als ein Kapitel übertragen.

### Mehrere Hörbücher in eine TAF

Ab zwei ausgewählten Einträgen gibt es „**als eine TAF**“ mit einem frei wählbaren Namen. Alle Einträge – auch aus verschiedenen Quellen (ABS, Navidrome, Dateiquelle) – landen **in der Reihenfolge der Auswahl** (die Kacheln zeigen dann 1, 2, 3 …) in einer TAF; jedes Hörbuch behält seine Kapitel. Sind es zusammen mehr als 99, wird automatisch zusammengefügt, über eine Grenze zwischen zwei Hörbüchern aber nur, wenn es nicht anders geht. Ablage: `TC_BASE/Sammlungen` oder der eigene Unterordner. Kennzeichen `k` + Hash über die Auswahl: dieselbe Auswahl in derselben Reihenfolge wird übersprungen, auch unter anderem Namen.

### Dateiquelle (NAS)

`FS_ROOTS` zeigt auf bereits eingehängte Ordner. Ein Ordner mit Audiodateien ist ein Hörspiel/Album; Unterordner wie `CD1`, `Disc 2` oder `Teil 3` gehören dazu. Titel = Ordnername, Cover = `cover.jpg`/`folder.jpg`/… Pfade ausserhalb der Freigabe (auch per Symlink) werden abgelehnt. Es wird nie auf die Freigabe geschrieben; einhängen sollte man sie trotzdem **nur lesend**.

> Die Dateiquelle ist bisher nur gegen ein nachgebautes Verzeichnis getestet, nicht gegen ein echtes NAS.

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen, unter systemd aus `/etc/teddybridge/teddybridge.env` (Vorlage: `deploy/teddybridge.env.example`, Rechte `600`). Fehlende oder ungültige Pflichtwerte brechen den Start mit einer Meldung ab, ohne Werte auszugeben.

### Zwingend

| Variable | Bedeutung |
|---|---|
| `ABS_URL` | Adresse von Audiobookshelf, z. B. `http://abs.example:13378` |
| `ABS_TOKEN` | API-Token eines **eigenen** ABS-Benutzers mit Lese- und Downloadrecht auf die gewünschten Bibliotheken |
| `TC_URL` | Adresse der TeddyCloud-Weboberfläche |

### Dringend empfohlen

| Variable | Bedeutung |
|---|---|
| `UI_USER` | Benutzername für das Login |
| `UI_PASS_HASH` | Passwort-Hash, erzeugen mit `venv/bin/python -m app.pwhash` (fragt das Passwort zweimal ab, mind. 8 Zeichen). Format `scrypt:n:r:p:salz:hash`, bewusst ohne `$`. |

Beide leer = **kein Login** (Warnung im Log). Nur eines gesetzt oder Hash ungültig = Startabbruch. `UI_PASS` im Klartext wird noch akzeptiert (mit Warnung), der Hash hat Vorrang.

**Passwort vergessen?** Mit `python -m app.pwhash` einen neuen Hash erzeugen, in die env-Datei eintragen und den Dienst neu starten.

### Optional

| Variable | Standard | Bedeutung |
|---|---|---|
| `TC_BASE` | `teddybridge` | Basisordner in der TeddyCloud-Bibliothek. Die Duplikaterkennung sucht nur hier. |
| `TC_BASE_EXTRA` | – | Weitere Ordner der Bibliothek (mit Komma getrennt), die **nur durchsucht** werden, ob ein Hörbuch schon vorhanden ist – z. B. ein früherer Basisordner, damit nichts doppelt übertragen wird. Dort wird nie geschrieben; bereits zugewiesene Tonies bleiben unberührt. |
| `TC_AUTH` | – | `user:pass`, falls TeddyCloud hinter Basic Auth liegt |
| `LISTEN` | `0.0.0.0:8085` | Adresse und Port |
| `TRUSTED_PROXIES` | – | IP(s) des Reverse Proxys, mit Komma getrennt (nur einzelne IPs). Dann gelten `X-Forwarded-For/-Proto`: echte Client-IP im Log, Cookie mit `Secure` bei HTTPS. |
| `DISCORD_WEBHOOK` | – | Discord-Webhook-URL: Meldung pro Auftrag (fertig/Fehler) und Summe pro Stapel. Wird nur beim Start gelesen. |
| `ND_URL`, `ND_USER`, `ND_PASS` | – | Navidrome als Quelle (alle drei nötig). Eigenen Benutzer ohne Adminrechte verwenden. |
| `FS_ROOTS` | – | Dateiquelle: `Name=/pfad`, mehrere mit `;`, z. B. `Hörspiele=/mnt/nas/hoerspiele;Musik=/mnt/nas/musik` |

Keine Kommentare hinter Werten: systemd liest sie als Teil des Werts.

## Installation (Debian/Ubuntu, systemd)

```sh
useradd --system --home /opt/teddybridge --shell /usr/sbin/nologin teddybridge
git clone https://github.com/urza-lab/teddybridge.git /opt/teddybridge
cd /opt/teddybridge
python3 -m venv venv && venv/bin/pip install -r requirements.txt
apt install ffmpeg                   # optional: m4b-Kapitel, Zusammenfügen von m4a/FLAC/Ogg

install -d -m 750 -o teddybridge -g teddybridge /etc/teddybridge
install -m 600 -o teddybridge -g teddybridge deploy/teddybridge.env.example /etc/teddybridge/teddybridge.env
venv/bin/python -m app.pwhash        # Hash in die env-Datei eintragen, übrige Werte ausfüllen

cp deploy/teddybridge.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now teddybridge
journalctl -u teddybridge -f
```

Danach unter `http://<host>:8085/` erreichbar. Für manuelle Starts: `TB_ENV_FILE=/etc/teddybridge/teddybridge.env venv/bin/python -m app`.

Die Unit läuft mit `ProtectSystem=strict` und `ProtectHome=yes`. NAS-Freigaben deshalb unter `/mnt` oder `/srv` einhängen, nicht unter `/home`.

## Docker (Begleitcontainer neben TeddyCloud)

Läuft TeddyCloud bereits in Docker, ist teddybridge am elegantesten ein zweiter Dienst im selben Compose-Stack: Beide liegen im selben Docker-Netz, teddybridge spricht TeddyCloud direkt unter dessen Dienstnamen an (`TC_URL=http://teddycloud`) – ohne Reverse Proxy dazwischen (keine Proxy-Zeitlimits oder 502 beim langen Umwandeln), und TeddyCloud muss für teddybridge nicht von aussen erreichbar sein.

Fertiges Image (amd64): `ghcr.io/urza-lab/teddybridge:latest`. Klonen ist nicht nötig, es reichen die Compose-Vorlage und die env-Datei:

```sh
cd <ordner-mit-der-teddycloud-compose>
curl -fsSLo teddybridge.env https://raw.githubusercontent.com/urza-lab/teddybridge/main/deploy/teddybridge.env.example
chmod 600 teddybridge.env
# TC_URL=http://teddycloud (Dienstname aus der TeddyCloud-Compose), übrige Werte ausfüllen
```

Dann den Dienst `teddybridge` aus [`deploy/docker-compose.example.yml`](deploy/docker-compose.example.yml) in die `docker-compose.yml` von TeddyCloud übernehmen (Variante A) oder als eigene Compose-Datei dem TeddyCloud-Netz beitreten lassen (Variante B), und:

```sh
docker compose run --rm -it teddybridge python -m app.pwhash    # Passwort-Hash erzeugen, in teddybridge.env eintragen
docker compose up -d teddybridge
```

Aktualisieren: `docker compose pull teddybridge && docker compose up -d teddybridge`. Selbst bauen statt fertiges Image: Repo klonen und in der Compose-Datei `build: ./teddybridge` statt `image:` verwenden.

Ohne TeddyCloud-Docker im selben Host geht es genauso als eigenständiger Container, z. B. mit `network_mode: host` und `TC_URL` auf die TeddyCloud-Adresse.

Das Image enthält ffmpeg und läuft als eigener Benutzer ohne Root-Rechte; die Vorlage setzt ausserdem ein schreibgeschütztes Dateisystem, `cap_drop: ALL` und `no-new-privileges`. Beschreibbar sind nur `/var/tmp` (Volume für Downloads – nicht im RAM, Quelldateien können gross sein) und ein kleines `/tmp`. Eine NAS-Freigabe wird nur lesend (`:ro`) eingebunden. Healthcheck: `/login`.

Hinweise: Ein Neustart des Containers verwirft die Warteschlange (wie beim systemd-Dienst). Liegt ein Reverse Proxy davor, dessen IP als `TRUSTED_PROXIES` eintragen (Docker behält die Quell-IP bei veröffentlichten Ports normalerweise bei).

## TeddyCloud-Plugin (optional)

TeddyCloud zeigt jeden Ordner unter `<teddycloud-data>/www/plugins/` als Menüpunkt im Plugins-Reiter. `plugin/teddybridge/index.html` öffnet von dort aus teddybridge:

1. In der Datei `TEDDYBRIDGE_URL` (zweimal) durch die Adresse von teddybridge ersetzen, z. B. `https://teddybridge.example.lan/` oder `http://<host>:8085/`.
2. Den Ordner `plugin/teddybridge` nach `<teddycloud-data>/www/plugins/` kopieren.

teddybridge lässt sich aus Sicherheitsgründen nicht in einen fremden Rahmen einbetten (`frame-ancestors 'none'`). Das Plugin leitet darum das ganze Fenster weiter; erlaubt der Browser das ohne Klick nicht, erscheint ein Knopf „teddybridge öffnen“.

## Tests

```sh
venv/bin/python tests/test_e2e.py     # Ende-zu-Ende gegen Mock-Server
venv/bin/python tests/test_audio.py   # MP3-Zusammenfügen und m4b-Kapitel mit echten Audiodateien (braucht ffmpeg)
```

Startet Mock-Server für Audiobookshelf, Navidrome und TeddyCloud (`mock/`) sowie ein nachgebautes NAS-Verzeichnis und prüft Login, Stöbern, Übertragen, Duplikate, Aufteilen, Zusammenfügen (bytegenau nachgerechnet bzw. jedes Kapitel dekodiert und nachgemessen), m4b-Kapitel (ABS und Dateiquelle), Sammel-TAFs, Aufräumen bei Fehlern, Pfad-Ausbrüche, Security-Header und dass **kein Geheimnis in Log oder Oberfläche** erscheint. Die Passwörter in Tests und Mock sind frei erfunden.

## Sicherheit

### Modell

teddybridge ist für ein **vertrauenswürdiges lokales Netz** gebaut. Wer sich anmelden kann, darf alle konfigurierten Quellen lesen und in TeddyCloud schreiben. Es gibt nur einen Benutzer.

### Security-Audit (Stand 2026-11-08)

**Umgesetzt**

- **Geheimnisse:** Tokens und Passwörter bleiben im Backend. Fehlermeldungen werden vor der Ausgabe gegen alle konfigurierten Geheimnisse geschwärzt. httpx-Logging ist gedrosselt, damit keine URLs mit Query-Parametern im Log landen. Die Tests prüfen das für jedes Geheimnis.
- **Login:** scrypt (n=2¹⁵, r=8, p=1, 16 Byte Salz). Vergleich in konstanter Zeit; das Passwort wird auch bei falschem Benutzer geprüft. 1 s Verzögerung und ein Log-Eintrag mit Client-IP pro Fehlversuch.
- **Sitzung:** 256-Bit-Zufallstoken im Cookie `HttpOnly`, `SameSite=Strict` und `Secure` bei HTTPS. Serverseitig wird nur der SHA-256 des Tokens gespeichert, maximal 200 Sitzungen. Logout entwertet die Sitzung serverseitig. `/api/*` antwortet ohne Anmeldung mit 401.
- **Basic Auth** für Skripte, ohne Browser-Popup.
- **Security-Header:** strikte CSP (nur eigene Quelle, keine externen Skripte), `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`. OpenAPI/Docs sind abgeschaltet, kein `Server`-Header.
- **Injection:** Alle IDs, die in URLs der Quellen landen, werden per Whitelist-Regex geprüft. Ziel- und Dateinamen werden bereinigt (kein `..`, keine Steuerzeichen, nie `_src_`). Das Frontend erzeugt DOM-Knoten mit `textContent` und nutzt kein `innerHTML`; Titel und Dateinamen können daher kein HTML einschleusen.
- **Dateiquelle:** Die IDs sind base64url des relativen Pfads. `..`, absolute Pfade, NUL und Symlinks aus der Freigabe heraus werden abgelehnt, auch beim Kopieren erneut geprüft. Nur normale Dateien, Cover max. 10 MB, Suche begrenzt (20 000 Ordner, 8 s).
- **Löschen in TeddyCloud** nur im eigenen `_src_*`-Ordner. Das prüft der Code vor jedem Löschaufruf. Einzige Ausnahme: die TAF, die derselbe Lauf gerade selbst angelegt hat, wenn sie nach einem Abbruch von TeddyCloud nachweislich unvollständig ist (Kopf ungültig oder falsche Kapitelzahl, Grösse unverändert) – vor dem Neuversuch bzw. beim Aufgeben. Eine TAF, die vor dem Lauf schon existierte, wird nie gelöscht.
- **Betrieb:** eigener Systembenutzer und gehärtete systemd-Unit (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `RestrictAddressFamilies`, `UMask=0077`, …).
- **Abhängigkeiten:** `pip-audit` meldet keine bekannten Schwachstellen.

**Im Audit gefunden und behoben**

- Security-Header fehlten (Clickjacking möglich) → CSP und Frame-Schutz ergänzt.
- Aufträge wurden als „fertig“ gemeldet, bevor `_src_` aufgeräumt war → Reihenfolge korrigiert.

**Bekannte Restrisiken (bewusst akzeptiert bzw. Sache des Betreibers)**

| Risiko | Bewertung | Empfehlung |
|---|---|---|
| Kein eigenes TLS: ohne Reverse Proxy gehen Passwort und Cookie im Klartext durchs LAN | mittel | Internen Reverse Proxy mit HTTPS davor setzen (`TRUSTED_PROXIES`) |
| Ohne `UI_USER`/`UI_PASS_HASH` kann jeder im Netz Aufträge starten | mittel | Login immer setzen |
| Fehlversuche werden pro Anfrage verzögert, aber parallele Versuche sind nicht begrenzt; keine Sperre | gering (LAN) | Nicht ins Internet stellen; ggf. Rate-Limit im Proxy |
| Navidrome-Anmeldung nach Subsonic-Protokoll (`md5(Passwort+Salz)`) | gering | Eigenen Navidrome-Benutzer mit eigenem Passwort verwenden |
| `TC_AUTH` geht per Basic Auth an TeddyCloud, bei `http://` unverschlüsselt | gering (LAN) | TeddyCloud per HTTPS ansprechen, wenn möglich |
| Dateiquelle: Wer auf die Freigabe **schreiben** kann, könnte theoretisch zwischen Pfadprüfung und Öffnen einen Symlink unterschieben (TOCTOU) | gering | Freigabe nur lesend einhängen; wer dort schreiben darf, sollte vertrauenswürdig sein |
| Sitzungen nur im Speicher, 30 Tage gültig | gering | Neustart meldet alle ab |

## Ideen für Version 2

- **Lange Hörbücher in Teile von ca. 60 min aufteilen** (wählbar). Hilft beim Wechsel zwischen Boxen (man verliert höchstens den angefangenen Teil) und umgeht das bekannte Problem, dass die Box bei Titeln über ca. 3 h 10 min die Position nicht mehr speichert.

### Bewusst nicht geplant: Hörfortschritt zwischen Boxen / mit Audiobookshelf

Wünschenswert, aber mit heutigen Mitteln nicht machbar. Die Toniebox speichert die Position ausschliesslich in einem kleinen internen Speicher der **Box** – nicht auf dem Tonie, nicht in der Cloud – und meldet sie auch nicht an den Server. teddyCloud ersetzt nur den Server (eigenes CA-Zertifikat, DNS); auf der Box läuft weiter die Original-Firmware. Damit kann weder teddyCloud noch teddybridge die Position lesen oder auf einer anderen Box setzen.

Auch eine per Hackiebox-Bootloader gepatchte CC3200-Box ändert daran nach heutigem Stand nichts: Ein solcher Patch existiert nicht, und laut den Entwicklern ist im Speicher kaum Platz, ein Speichern auf SD-Karte bräuchte einen grösseren Umbau. Eigenständige Custom-Firmwares (HackieboxNG für CC3200, TeddyBox für ESP32) sind noch nicht alltagstauglich. Sollte eine davon künftig die Position melden und übernehmen können, liesse sich ein Abgleich (auch mit ABS, dann mit Schreibrecht nur für den Fortschritt) nachrüsten.

## Lizenz

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).

Kurz: Nutzen, verändern und weitergeben ist erlaubt. Wer eine veränderte Version weitergibt oder für andere als Netzwerkdienst betreibt, muss deren Quellcode unter derselben Lizenz zugänglich machen.
