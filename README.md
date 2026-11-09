# teddybridge

A web UI to browse audiobooks, radio plays and music from **Audiobookshelf**, **Navidrome** or a **NAS folder** and transfer them with one click as **TAF files** into the library of **[TeddyCloud](https://github.com/toniebox-reverse-engineering/teddycloud)**. You then assign them to a Tonie in TeddyCloud as usual.

> **Languages:** The user interface is available in **English and German** – it follows the browser language and can be switched in the header, or pinned with `UI_LANG`. Logs are in German.

---

## Executive summary

| | |
|---|---|
| **Purpose** | Get audiobooks/albums from existing media servers into TeddyCloud without copying and converting files by hand. |
| **Sources** | Audiobookshelf, Navidrome and/or folders in the file system (e.g. a NAS via NFS/SMB) – at least one, any combination. All **read-only**. |
| **Target** | The TeddyCloud library. teddybridge only creates files. It deletes nothing but its own temporary `_src_*` folders – and a TAF that the same run created itself and that is provably incomplete after TeddyCloud aborted (before the automatic retry). |
| **Tech** | Python 3.11+, FastAPI/uvicorn, httpx, a single HTML page with vanilla JS and no build step. No database. Optional ffmpeg (m4b chapters, merging formats other than MP3). |
| **Deployment** | Docker container (prebuilt image `ghcr.io/urza-lab/teddybridge`) or systemd service, both hardened (templates in `deploy/`). Meant for a trusted home network, optionally behind an internal reverse proxy with HTTPS. **Do not expose it to the internet.** |
| **Security** | Login with scrypt hash and HttpOnly session cookie. Credentials for ABS, Navidrome and TeddyCloud stay in the backend. Security headers (CSP, `frame-ancestors 'none'`). No known vulnerabilities in dependencies (pip-audit, as of 2026-11-08). See [Security](#security). |

## How it works

1. **Browse:** libraries, series/artists, collections/playlists, folders, search and covers. All requests to the sources go through the backend; the browser never sees a token.
2. **Status:** Every TAF carries an 8-character marker in its name, e.g. `Title [1a2b3c4d].taf`. If that marker already exists anywhere below the base folder, the item counts as transferred and is skipped, even if it was renamed or moved.
3. **Transfer:** Selected items go into a queue and are processed **one after another**, because TeddyCloud uses a lot of CPU while encoding:
   1. create a temporary folder `<target>/_src_<id>` in TeddyCloud,
   2. download the audio files from the source one by one (local temp directory) and upload them,
   3. TeddyCloud encodes them into **one** TAF (`fileEncode`, max. 99 files; with more, files are merged or split, see [More than 99 files](#more-than-99-files)),
   4. check the TAF and delete `_src_` again, also on errors.
   5. If TeddyCloud aborts while encoding (e.g. a restart or a host backup), teddybridge waits up to 45 min for TeddyCloud to respond again and checks the TAF: if it is still growing, it waits; if it is complete, the job is done; if it is incomplete, this half-written TAF of its own is removed and re-encoded from the `_src_` files that are still there (at most 2 retries). A half-written TAF from an earlier run is never deleted, but reported on the next transfer.
4. **Placement:** automatically `TC_BASE/<library>/<series or author>` (Audiobookshelf), `TC_BASE/<music folder>/<album artist>` or `TC_BASE/Playlists` (Navidrome), `TC_BASE/<share>/<folder path>` (file source), or a subfolder of your choice.
5. **Progress:** The queue in the browser shows a bar, percentage and estimated remaining time with clock time per job. While downloading, the estimate comes from the current speed; while encoding – TeddyCloud reports no progress – from the growing TAF file (TeddyCloud writes steadily about 0.65 MB per minute of audio, about 17× real time). teddybridge refines both values after every finished job.
6. **Notifications:** optionally via Discord webhook per job – ✅ done (chapters, duration, folder) or ❌ error with reason; for batches of several jobs also a summary (transferred, skipped, errors). Mentions (`@everyone` etc. in titles) are disabled.

The queue lives in memory only. A restart discards pending jobs; this is intended.

### More than 99 files

A TAF can have at most 99 chapters (one source file = one chapter). With more files the UI asks:

- **Merge** (default): a single TAF with 99 chapters. teddybridge repeatedly joins the two adjacent files with the shortest combined duration until 99 remain – so only the boundaries between the shortest chapters disappear. **MP3** is concatenated losslessly (no re-encoding, no extra tool); each merged file keeps the usual MP3 encoder gap of about 35 ms. **Other formats** (m4a, FLAC, Ogg …) and MP3 with differing sample rate or mono/stereo are joined with **ffmpeg** into one FLAC chapter (lossless relative to the source; TeddyCloud encodes to Opus afterwards anyway). Without ffmpeg, merging only works for uniform MP3.
- **Split:** several TAFs `<title> - Part k of n [<id>-k].taf` (German: `Teil k von n`) with the same number of files each; if a part is missing later, only that part is transferred again.
- **Skip.**

An audiobook counts as present in either form: if parts already exist, it is not merged in addition (and vice versa).

### One file with chapters (m4b)

If **ffmpeg** is installed (optional), an audiobook consisting of a single file with chapter marks (typically `.m4b`) is cut losslessly (`-c copy`) at the chapters – every chapter becomes a TAF chapter. With Audiobookshelf the chapters come from its metadata; with the file source, ffprobe reads them directly (read-only). With more than 99 chapters, the shortest adjacent ones are combined (simply fewer cuts). Without ffmpeg or without chapter marks, the file is transferred as one chapter.

### Several audiobooks in one TAF

With two or more selected items you can choose “**as one TAF**” with a name of your choice. All items – even from different sources (ABS, Navidrome, file source) – end up in one TAF **in the order you selected them** (the tiles then show 1, 2, 3 …); each audiobook keeps its chapters. If together they exceed 99 chapters, they are merged automatically, but across the boundary between two audiobooks only if there is no other way. Placement: `TC_BASE/Collections` (German UI: `Sammlungen`) or your own subfolder. Marker `k` + hash over the selection: the same selection in the same order is skipped, even under a different name.

### File source (NAS)

`FS_ROOTS` points to folders that are already mounted. A folder with audio files is one audiobook/album; subfolders like `CD1`, `Disc 2` or `Teil 3` belong to it. Title = folder name, cover = `cover.jpg`/`folder.jpg`/… Paths outside the share (also via symlink) are rejected. teddybridge never writes to the share; still, mount it **read-only**.

> The file source has only been tested against a simulated directory so far, not against a real NAS.

## Configuration

All settings come from environment variables: under systemd from `/etc/teddybridge/teddybridge.env`, under Docker from the compose file's `env_file` (template: `deploy/teddybridge.env.example`, permissions `600`). Missing or invalid required values abort the start with a message that does not reveal any values.

### Required

| Variable | Meaning |
|---|---|
| `TC_URL` | Address of the TeddyCloud web UI |

Plus **at least one source**; several can be combined, the UI then shows tabs. A source is either filled in completely or left empty entirely – a half-filled source (e.g. `ABS_URL` without `ABS_TOKEN`) aborts the start so that a typo does not go unnoticed.

| Source | Variables | Meaning |
|---|---|---|
| Audiobookshelf | `ABS_URL`, `ABS_TOKEN` | Address, e.g. `http://abs.example:13378`, and API token of a **dedicated** ABS user with read and download permission on the desired libraries |
| Navidrome | `ND_URL`, `ND_USER`, `ND_PASS` | Address and credentials of a dedicated user **without** admin rights |
| File source | `FS_ROOTS` | `Name=/path`, several separated by `;`, e.g. `Audiobooks=/mnt/nas/audiobooks;Music=/mnt/nas/music` (see [File source](#file-source-nas)) |

### Strongly recommended

| Variable | Meaning |
|---|---|
| `UI_USER` | User name for the login |
| `UI_PASS_HASH` | Password hash, created with `venv/bin/python -m app.pwhash` (asks for the password twice, min. 8 characters). Format `scrypt:n:r:p:salt:hash`, deliberately without `$`. |

Both empty = **no login** (warning in the log). Only one set or invalid hash = start aborted. A plain-text `UI_PASS` is still accepted (with a warning); the hash takes precedence.

**Forgot the password?** Create a new hash with `python -m app.pwhash` (Docker: `docker compose run --rm -it teddybridge python -m app.pwhash`), put it into the env file and restart the service or container.

### Optional

| Variable | Default | Meaning |
|---|---|---|
| `TC_BASE` | `teddybridge` | Base folder in the TeddyCloud library. Files are placed here and existing TAFs are searched here. |
| `TC_BASE_EXTRA` | – | Further library folders (comma-separated) that are **only searched** for existing items – e.g. a previous base folder, so nothing is transferred twice. teddybridge never writes there; Tonies already assigned stay untouched. |
| `TC_AUTH` | – | `user:pass` if TeddyCloud is behind Basic Auth |
| `LISTEN` | `0.0.0.0:8085` | Address and port |
| `TRUSTED_PROXIES` | – | IP(s) of the reverse proxy, comma-separated (single IPs only). Then `X-Forwarded-For/-Proto` are honored: real client IP in the log, cookie with `Secure` on HTTPS. |
| `DISCORD_WEBHOOK` | – | Discord webhook URL: message per job (done/error) and summary per batch. Read only at startup. |
| `UI_LANG` | – | `en` or `de` pins the language of the UI, error messages, Discord messages and folder names (e.g. `Collections`/`Sammlungen`); the language selector is hidden. Empty: browser language (English if it is neither), switchable in the header; a job uses the language of the browser that queued it. |

No comments after values: systemd treats them as part of the value.

## Installation (Debian/Ubuntu, systemd)

```sh
useradd --system --home /opt/teddybridge --shell /usr/sbin/nologin teddybridge
git clone https://github.com/urza-lab/teddybridge.git /opt/teddybridge
cd /opt/teddybridge
python3 -m venv venv && venv/bin/pip install -r requirements.txt
apt install ffmpeg                   # optional: m4b chapters, merging m4a/FLAC/Ogg

install -d -m 750 -o teddybridge -g teddybridge /etc/teddybridge
install -m 600 -o teddybridge -g teddybridge deploy/teddybridge.env.example /etc/teddybridge/teddybridge.env
venv/bin/python -m app.pwhash        # put the hash into the env file, fill in the other values

cp deploy/teddybridge.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now teddybridge
journalctl -u teddybridge -f
```

Then reachable at `http://<host>:8085/`. For manual starts: `TB_ENV_FILE=/etc/teddybridge/teddybridge.env venv/bin/python -m app`.

The unit runs with `ProtectSystem=strict` and `ProtectHome=yes`. Mount NAS shares under `/mnt` or `/srv`, not under `/home`.

## Docker (companion container next to TeddyCloud)

If TeddyCloud already runs in Docker, the most elegant setup is teddybridge as a second service in the same compose stack: both share a Docker network, teddybridge talks to TeddyCloud directly by its service name (`TC_URL=http://teddycloud`) – without a reverse proxy in between (no proxy timeouts or 502 during long encodes), and TeddyCloud does not need to be reachable from outside for teddybridge.

Prebuilt image (amd64): `ghcr.io/urza-lab/teddybridge:latest`. No need to clone; the compose template and the env file are enough:

```sh
cd <folder-with-the-teddycloud-compose>
curl -fsSLo teddybridge.env https://raw.githubusercontent.com/urza-lab/teddybridge/main/deploy/teddybridge.env.example
chmod 600 teddybridge.env
# TC_URL=http://teddycloud (service name from the TeddyCloud compose), fill in the other values
```

Then copy the `teddybridge` service from [`deploy/docker-compose.example.yml`](deploy/docker-compose.example.yml) into TeddyCloud's `docker-compose.yml` (variant A), or use it as a separate compose file that joins the TeddyCloud network (variant B), and run:

```sh
docker compose run --rm -it teddybridge python -m app.pwhash    # create password hash, put it into teddybridge.env
docker compose up -d teddybridge
```

Update: `docker compose pull teddybridge && docker compose up -d teddybridge`. To build it yourself instead of using the prebuilt image: clone the repo and use `build: ./teddybridge` instead of `image:` in the compose file.

Without TeddyCloud in Docker on the same host, it works just as well as a standalone container, e.g. with `network_mode: host` and `TC_URL` pointing to the TeddyCloud address.

The image includes ffmpeg and runs as its own non-root user; the template also sets a read-only file system, `cap_drop: ALL` and `no-new-privileges`. Only `/var/tmp` (volume for downloads – not in RAM, source files can be large) and a small `/tmp` are writable. A NAS share is mounted read-only (`:ro`). Health check: `/login`.

Notes: restarting the container discards the queue (same as the systemd service). If a reverse proxy sits in front, put its IP into `TRUSTED_PROXIES` (Docker normally keeps the source IP on published ports).

## TeddyCloud plugin (optional)

The plugin in `plugin/teddybridge/` adds a “teddybridge” entry to TeddyCloud that opens teddybridge. TeddyCloud lists every folder under `/teddycloud/data/www/plugins/` that contains a `plugin.json`. Installing it is a manual step, also in the same compose stack: by design, teddybridge only writes to the library through the API, never into TeddyCloud itself.

On first use the plugin asks for the address at which **the browser** reaches teddybridge, e.g. `https://teddybridge.example.lan/` or `http://<host>:8085/` (not the Docker service name), and stores it in that browser. “Change address” is shown for 1.5 s before it redirects. To preset the address for all browsers, replace `TEDDYBRIDGE_URL` in `index.html` (see below).

**TeddyCloud in Docker**

The official TeddyCloud compose has its own `plugins` volume for this (`/teddycloud/data/www/plugins`); whatever is there survives TeddyCloud updates. On the Docker host:

```sh
mkdir -p /tmp/teddybridge-plugin/teddybridge && cd /tmp/teddybridge-plugin/teddybridge
for f in index.html plugin.json; do
  curl -fsSLO https://raw.githubusercontent.com/urza-lab/teddybridge/main/plugin/teddybridge/$f
done
# optional, preset the address:
# sed -i 's#TEDDYBRIDGE_URL#http://<host>:8085/#' index.html
docker cp /tmp/teddybridge-plugin/teddybridge teddycloud:/teddycloud/data/www/plugins/
```

`teddycloud` is the container name (`docker ps` shows it). No restart is needed; after reloading the TeddyCloud page, the entry “teddybridge” appears.

If your own compose lacks the `plugins` volume, a copied plugin would be gone after the next update. In that case mount the folder instead, in the `teddycloud` service:

```yaml
    volumes:
      # … existing entries …
      - ./plugins/teddybridge:/teddycloud/data/www/plugins/teddybridge:ro
```

(Put both files into `./plugins/teddybridge/`, then run `docker compose up -d teddycloud`. This briefly restarts TeddyCloud, so not during a running transfer.)

**TeddyCloud without Docker**

Copy the folder `plugin/teddybridge` to `<teddycloud-data>/www/plugins/`.

For security reasons teddybridge cannot be embedded in a foreign frame (`frame-ancestors 'none'`). The plugin therefore redirects the whole window; if the browser does not allow that without a click, an “Open teddybridge” button stays. The plugin follows the browser language (German or English).

## Tests

```sh
venv/bin/python tests/test_e2e.py     # end-to-end against mock servers
venv/bin/python tests/test_audio.py   # MP3 merging and m4b chapters with real audio files (needs ffmpeg)
```

Starts mock servers for Audiobookshelf, Navidrome and TeddyCloud (`mock/`) plus a simulated NAS directory and checks login, browsing, transfer, duplicates, splitting, merging (verified byte by byte, or every chapter decoded and measured), m4b chapters (ABS and file source), multi-audiobook TAFs, starting with only one source, both languages, cleanup on errors, path escapes, security headers, and that **no secret appears in the log or UI**. Passwords in tests and mocks are made up.

## Security

### Model

teddybridge is built for a **trusted local network**. Whoever can log in may read all configured sources and write to TeddyCloud. There is only one user.

### Security audit (as of 2026-11-08)

**Implemented**

- **Secrets:** Tokens and passwords stay in the backend. Error messages are redacted against all configured secrets before output. httpx logging is throttled so that no URLs with query parameters end up in the log. The tests check this for every secret.
- **Login:** scrypt (n=2¹⁵, r=8, p=1, 16-byte salt). Constant-time comparison; the password is checked even for a wrong user name. 1 s delay and a log entry with client IP per failed attempt.
- **Session:** 256-bit random token in a cookie with `HttpOnly`, `SameSite=Strict` and `Secure` on HTTPS. Only the SHA-256 of the token is stored server-side, max. 200 sessions. Logout invalidates the session server-side. `/api/*` answers 401 without login.
- **Basic Auth** for scripts, without a browser popup.
- **Security headers:** strict CSP (own origin only, no external scripts), `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`. OpenAPI/docs are disabled, no `Server` header.
- **Injection:** All IDs that end up in source URLs are validated with an allow-list regex. Target and file names are sanitized (no `..`, no control characters, never `_src_`). The frontend builds DOM nodes with `textContent` and never uses `innerHTML`, so titles and file names cannot inject HTML.
- **File source:** IDs are base64url of the relative path. `..`, absolute paths, NUL and symlinks leading out of the share are rejected, and checked again when copying. Regular files only, covers max. 10 MB, search limited (20,000 folders, 8 s).
- **Deleting in TeddyCloud** only inside its own `_src_*` folder; the code checks this before every delete call. Single exception: the TAF the same run has just created, if it is provably incomplete after TeddyCloud aborted (invalid header or wrong chapter count, size no longer changing) – before the retry or when giving up. A TAF that existed before the run is never deleted.
- **Runtime:** dedicated system user and hardened systemd unit (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `RestrictAddressFamilies`, `UMask=0077`, …), or a non-root container with read-only file system, `cap_drop: ALL` and `no-new-privileges`.
- **Dependencies:** `pip-audit` reports no known vulnerabilities.

**Found and fixed in the audit**

- Security headers were missing (clickjacking possible) → CSP and frame protection added.
- Jobs were reported as “done” before `_src_` was cleaned up → order fixed.

**Known residual risks (accepted, or the operator's responsibility)**

| Risk | Rating | Recommendation |
|---|---|---|
| No built-in TLS: without a reverse proxy, password and cookie cross the LAN in plain text | medium | Put an internal reverse proxy with HTTPS in front (`TRUSTED_PROXIES`) |
| Without `UI_USER`/`UI_PASS_HASH`, anyone on the network can start jobs | medium | Always set a login |
| Failed attempts are delayed per request, but parallel attempts are not limited; no lockout | low (LAN) | Do not expose to the internet; rate-limit in the proxy if needed |
| Navidrome login per the Subsonic protocol (`md5(password+salt)`) | low | Use a dedicated Navidrome user with its own password |
| `TC_AUTH` is sent to TeddyCloud via Basic Auth, unencrypted over `http://` | low (LAN) | Reach TeddyCloud via HTTPS if possible |
| File source: someone who can **write** to the share could in theory swap in a symlink between path check and open (TOCTOU) | low | Mount the share read-only; anyone with write access should be trusted |
| Sessions in memory only, valid for 30 days | low | A restart logs everyone out |

## Ideas for version 2

- **Split long audiobooks into parts of about 60 min** (optional). Helps when switching between boxes (you lose at most the current part) and avoids the known issue that the box no longer saves the position for tracks longer than about 3 h 10 min.

### Deliberately not planned: listening progress across boxes / with Audiobookshelf

Desirable, but not possible with today's means. The Toniebox stores the position only in a small internal memory of the **box** – not on the Tonie, not in the cloud – and does not report it to the server. TeddyCloud only replaces the server (own CA certificate, DNS); the box keeps running the original firmware. So neither TeddyCloud nor teddybridge can read the position or set it on another box.

A CC3200 box patched via the Hackiebox bootloader does not change this either as of today: no such patch exists, and according to the developers there is hardly any room in memory; saving to the SD card would require a larger rework. Standalone custom firmwares (HackieboxNG for CC3200, TeddyBox for ESP32) are not yet ready for everyday use. Should one of them be able to report and restore the position in the future, a sync (also with ABS, then with write access only for progress) could be added.

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).

In short: you may use, modify and share it. Anyone who distributes a modified version, or runs one as a network service for others, must make its source code available under the same license.
