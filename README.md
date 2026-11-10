# teddybridge

A web UI to browse audiobooks, radio plays and music from **Audiobookshelf**, **Navidrome** or a **NAS folder** and transfer them with one click as **TAF files** into the library of **[TeddyCloud](https://github.com/toniebox-reverse-engineering/teddycloud)**. Afterwards you can assign a TAF to one of your existing Tonie figures right in teddybridge, or in TeddyCloud as usual.

> **Languages:** The user interface is available in **English and German** – it follows the browser language and can be switched in the header, or pinned with `UI_LANG`. Logs are in German.

---

## Executive summary

| | |
|---|---|
| **Purpose** | Get audiobooks/albums from existing media servers into TeddyCloud without copying and converting files by hand. |
| **Sources** | Audiobookshelf, Navidrome and/or folders in the file system (e.g. a NAS via NFS/SMB) – at least one, any combination. All **read-only**. |
| **Target** | The TeddyCloud library. teddybridge only creates files. It deletes nothing but its own temporary `_src_*` folders – and a TAF that the same run created itself and that is provably incomplete after TeddyCloud aborted (before the automatic retry). |
| **Tech** | Python 3.11+, FastAPI/uvicorn, httpx, a single HTML page with vanilla JS and no build step. No database. Optional ffmpeg (m4b chapters, merging formats other than MP3). |
| **Deployment** | Docker container (prebuilt image `ghcr.io/urza-lab/teddybridge`) or systemd service, both hardened (templates in `deploy/`). First-time setup in the browser (since 1.2) or via environment variables. Meant for a trusted home network, optionally behind an internal reverse proxy with HTTPS. **Do not expose it to the internet.** |
| **Security** | Login with scrypt hash and HttpOnly session cookie. Credentials for ABS, Navidrome and TeddyCloud stay in the backend. Security headers (CSP; framing only by origins listed in `FRAME_ANCESTORS`). No known vulnerabilities in dependencies (pip-audit, as of 2026-11-08). See [Security](#security). |

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
6. **Assign to a Tonie** (since 1.5): see [below](#assign-to-a-tonie).
7. **Notifications:** optionally via Discord webhook per job – ✅ done (chapters, duration, folder) or ❌ error with reason; for batches of several jobs also a summary (transferred, skipped, errors). Mentions (`@everyone` etc. in titles) are disabled.

The queue lives in memory only. A restart discards pending jobs; this is intended.

### More than 99 files

A TAF can have at most 99 chapters (one source file = one chapter). With more files the UI asks:

- **Merge** (default): a single TAF with 99 chapters. teddybridge repeatedly joins the two adjacent files with the shortest combined duration until 99 remain – so only the boundaries between the shortest chapters disappear. **MP3** is concatenated losslessly (no re-encoding, no extra tool); each merged file keeps the usual MP3 encoder gap of about 35 ms. **Other formats** (m4a, FLAC, Ogg …) and MP3 with differing sample rate or mono/stereo are joined with **ffmpeg** into one FLAC chapter (lossless relative to the source; TeddyCloud encodes to Opus afterwards anyway). Without ffmpeg, merging only works for uniform MP3.
- **Split:** several TAFs `<title> - Part k of n [<id>-k].taf` (German: `Teil k von n`) with the same number of files each; if a part is missing later, only that part is transferred again. From 10 parts on, the number gets a leading zero (`Part 03 of 12`) so TeddyCloud sorts the parts correctly.
- **Skip.**

An audiobook counts as present in either form: if parts already exist, it is not merged in addition (and vice versa).

### Parts of about 60 min (option, since 1.3)

Tick “**in parts of 60 min**” next to the transfer button (remembered in the browser) to split long audiobooks into several TAFs of about one hour each, e.g. `<title> - Part 03 of 12 [<id>-3].taf`. This helps when switching between boxes (you lose at most the current part) and avoids the known issue that the box no longer saves the position in very long TAFs (more than about 3 h 10 min).

- The number of parts is the total duration divided by 60 min, rounded (so an 80-min book stays **one** TAF, a 100-min book becomes 2 parts of 50 min).
- Cuts are made at **file or chapter boundaries**, each at the boundary closest to the ideal point – so parts are roughly, not exactly, 60 min.
- A single long file without usable boundaries (e.g. one 10-hour MP3) is cut with **ffmpeg** (included in the Docker image) into ranges of about 60 min, each cut moved to the nearest **pause** (see below); without ffmpeg it stays one TAF.
- More than 99 files within one part are merged as described above; the 99-file question does not appear.
- Same markers as the 99-file split: missing parts are transferred again, existing ones skipped. If the book already exists as **one** TAF, it is skipped (delete it in TeddyCloud first to get parts). If it already exists in a **different number** of parts, teddybridge refuses instead of mixing two splits.
- Not available for “as one TAF” collections.

### One file with chapters (m4b)

If **ffmpeg** is installed (optional), an audiobook consisting of a single file with chapter marks (typically `.m4b`) is cut losslessly (`-c copy`) at the chapters – every chapter becomes a TAF chapter. With Audiobookshelf the chapters come from its metadata; with the file source, ffprobe reads them directly (read-only). With more than 99 chapters, the shortest adjacent ones are combined (simply fewer cuts). Without ffmpeg or without chapter marks, the file is transferred as one chapter – unless you use the option below.

### Chapters for files without chapter marks (option, since 1.4)

A long file without chapter marks (e.g. a 10-hour MP3) normally becomes a TAF with a single chapter, so you cannot skip on the box. Enter a number in “**Chapters every … min**” next to the transfer button (3–120, empty = off, remembered in the browser): such files then get a chapter about every that many minutes.

- Only files **without** chapter marks are touched, and only if they are longer than 1.5 × the interval; real chapters (m4b, ABS metadata) and normal multi-file audiobooks stay as they are.
- Each cut is moved to the nearest **pause** (silence of at least 0.4 s below −35 dB, found with ffmpeg `silencedetect`), at most 3 min or a quarter of the interval away, so a chapter does not end mid-sentence. If there is no pause nearby, the cut stays at the exact time. The queue shows how many cuts landed in a pause.
- Finding pauses reads the whole file once (roughly 1–2 min for a 10-hour book); the cutting itself is lossless (`-c copy`).
- Combinable with “in parts of 60 min”: the parts are then cut at these chapter boundaries.
- File source: works for a folder with a single file (its duration is read with ffprobe); for multi-file folders the file durations are unknown, so nothing is added.
- Needs ffmpeg (always in the Docker image).

### Several audiobooks in one TAF

With two or more selected items you can choose “**as one TAF**” with a name of your choice. All items – even from different sources (ABS, Navidrome, file source) – end up in one TAF **in the order you selected them** (the tiles then show 1, 2, 3 …); each audiobook keeps its chapters. If together they exceed 99 chapters, they are merged automatically, but across the boundary between two audiobooks only if there is no other way. Placement: `TC_BASE/Collections` (German UI: `Sammlungen`) or your own subfolder. Marker `k` + hash over the selection: the same selection in the same order is skipped, even under a different name.

### Assign to a Tonie

Finished (or skipped, i.e. already present) jobs in the queue get a button **“Assign to Tonie”** (one per part for split books). It opens the list of figures TeddyCloud knows (picture, name, current assignment, searchable). After picking a figure, a confirmation shows what it plays now and what it will play; only then is it changed.

- Same API calls as TeddyCloud's own web UI: `POST /content/json/set/<ruid>` with `source=lib://<path>`, plus `nocloud=true` (if not set yet) and `live=false` (if it was on). Nothing else is touched (name, model, hide, claimed …).
- Only **existing** figures (no new tags, no system tags) and only TAFs below teddybridge's own base folder(s) that actually exist.
- The previous assignment is shown afterwards and logged (`Tonie … assigned: source '…' -> '…'`), so it can be restored in TeddyCloud.
- If the assignment changed in the meantime (e.g. in TeddyCloud), teddybridge refuses and asks to select again. After saving, it reads the figure back and reports an error if TeddyCloud did not take the new source.
- TeddyCloud stores at most 255 bytes for the source; longer paths are refused instead of being cut off silently.
- Figures are read from the default content folder (no per-box overlay). The box picks up the new content the next time the figure is placed.
- Disabled in read-only mode.

### File source (NAS)

`FS_ROOTS` points to folders that are already mounted. A folder with audio files is one audiobook/album; subfolders like `CD1`, `Disc 2` or `Teil 3` belong to it. Title = folder name, cover = `cover.jpg`/`folder.jpg`/… Paths outside the share (also via symlink) are rejected. teddybridge never writes to the share; still, mount it **read-only**.

> The file source has only been tested against a simulated directory so far, not against a real NAS.

## Configuration

### Setup in the browser (since 1.2)

Start teddybridge without configuration and open `http://<host>:8085/`. A setup page asks for:

1. the TeddyCloud address, plus user and password if TeddyCloud requires a login
2. at least one source: Audiobookshelf, Navidrome or folders
3. the login for teddybridge, or none
4. optional settings: language, base folder, embedding in TeddyCloud, reverse proxy, Discord

Every connection can be tested before saving. Saving runs the same checks as a normal start, writes the settings to `TB_CONFIG_FILE` with permissions `600` and restarts teddybridge with them. Passwords and tokens are never shown again, only marked as saved. The login is stored as a hash.

**One-time code.** The setup page first asks for a one-time code from the log (`docker logs teddybridge` or `journalctl -u teddybridge`). Without the code nobody can take over a fresh instance that someone else reached first.

**Where the settings are stored.** The Docker image uses `/config/teddybridge.env`, so mount a volume at `/config` (the compose template does). The systemd template uses `/var/lib/teddybridge/teddybridge.env`. Without `TB_CONFIG_FILE`, there is no setup page and missing values abort the start as before.

**Changing settings later.** Click *Settings* in the header. The page looks like the setup, pre-filled with the current values; passwords and tokens left empty keep their saved value. With a login, saving asks for the current teddybridge password. Without a login, the page first asks for a one-time code that a button writes to the log (valid for 10 minutes, usable once). Saving restarts teddybridge, so it is refused while a transfer is running or waiting; afterwards you log in again. Environment variables always take precedence over the file and are shown as fixed. To start over, delete the file and restart.

### Environment variables

All settings can also come from environment variables: under systemd from `/etc/teddybridge/teddybridge.env`, under Docker from the compose file's `env_file` (template: `deploy/teddybridge.env.example`, permissions `600`). A variable that is set and not empty wins over the setup file. Without setup, missing or invalid required values abort the start with a message that does not reveal any values.

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

### Login (optional)

| Variable | Meaning |
|---|---|
| `UI_USER` | User name for the login |
| `UI_PASS_HASH` | Password hash, created with `venv/bin/python -m app.pwhash` (asks for the password twice, min. 8 characters). Format `scrypt:n:r:p:salt:hash`, deliberately without `$`. |

Both empty = **no login** (note in the log). That is fine if only trusted people can reach teddybridge's port, e.g. a home network. Note that protecting TeddyCloud with a password does **not** protect teddybridge: its port (8085) is reached directly, also when it is shown inside TeddyCloud. Only one set or invalid hash = start aborted. A plain-text `UI_PASS` is still accepted (with a warning); the hash takes precedence.

**Forgot the password?** Create a new hash with `python -m app.pwhash` (Docker: `docker compose run --rm -it teddybridge python -m app.pwhash`), put it into the env file and restart the service or container.

### Optional

| Variable | Default | Meaning |
|---|---|---|
| `TC_BASE` | `teddybridge` | Base folder in the TeddyCloud library. Files are placed here and existing TAFs are searched here. |
| `TC_BASE_EXTRA` | – | Further library folders (comma-separated) that are **only searched** for existing items – e.g. a previous base folder, so nothing is transferred twice. teddybridge never writes there; Tonies already assigned stay untouched. |
| `TC_AUTH` | – | `user:pass` if TeddyCloud requires a login: TeddyCloud's own web login (setting `frontend.web_auth_enabled`, newer versions) or Basic Auth of a proxy in front of it. teddybridge logs in once and keeps the session; after a rejected login it waits 5 minutes before trying again (TeddyCloud locks an IP after 5 failures for 10 minutes). In a Docker env file, put the value in single quotes if the password contains `$`, `"` or `\`. |
| `LISTEN` | `0.0.0.0:8085` | Address and port |
| `TRUSTED_PROXIES` | – | IP(s) of the reverse proxy, comma-separated (single IPs only). Then `X-Forwarded-For/-Proto` are honored: real client IP in the log, cookie with `Secure` on HTTPS. |
| `DISCORD_WEBHOOK` | – | Discord webhook URL: message per job (done/error) and summary per batch. Read only at startup. |
| `FRAME_ANCESTORS` | – | Address(es) of TeddyCloud as the browser sees it, e.g. `http://teddycloud.example` or `http://192.0.2.10` (scheme, host, optional port, no path; comma-separated). Allows the [plugin](#teddycloud-plugin-optional) to show teddybridge **inside** TeddyCloud. Empty: teddybridge cannot be framed at all. |
| `UI_LANG` | – | `en` or `de` pins the language of the UI, error messages, Discord messages and folder names (e.g. `Collections`/`Sammlungen`); the language selector is hidden. Empty: browser language (English if it is neither), switchable in the header; a job uses the language of the browser that queued it. |

No comments after values: systemd treats them as part of the value.

## Installation (Debian/Ubuntu, systemd)

```sh
useradd --system --home /opt/teddybridge --shell /usr/sbin/nologin teddybridge
git clone https://github.com/urza-lab/teddybridge.git /opt/teddybridge
cd /opt/teddybridge
python3 -m venv venv && venv/bin/pip install -r requirements.txt
apt install ffmpeg                   # optional: m4b chapters, merging m4a/FLAC/Ogg

cp deploy/teddybridge.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now teddybridge
journalctl -u teddybridge -f         # shows the one-time code for the setup
```

Then open `http://<host>:8085/` and follow the [setup](#setup-in-the-browser-since-12). To configure with an env file instead:

```sh
install -d -m 750 -o teddybridge -g teddybridge /etc/teddybridge
install -m 600 -o teddybridge -g teddybridge deploy/teddybridge.env.example /etc/teddybridge/teddybridge.env
venv/bin/python -m app.pwhash        # put the hash into the env file, fill in the other values
systemctl restart teddybridge
```
 For manual starts: `TB_ENV_FILE=/etc/teddybridge/teddybridge.env venv/bin/python -m app`.

The unit runs with `ProtectSystem=strict` and `ProtectHome=yes`. Mount NAS shares under `/mnt` or `/srv`, not under `/home`.

## Docker (companion container next to TeddyCloud)

If TeddyCloud already runs in Docker, the most elegant setup is teddybridge as a second service in the same compose stack: both share a Docker network, teddybridge talks to TeddyCloud directly by its service name (`TC_URL=http://teddycloud`) – without a reverse proxy in between (no proxy timeouts or 502 during long encodes), and TeddyCloud does not need to be reachable from outside for teddybridge.

Prebuilt image (amd64): `ghcr.io/urza-lab/teddybridge:latest`. No need to clone:

1. Copy the `teddybridge` service from [`deploy/docker-compose.example.yml`](deploy/docker-compose.example.yml) into TeddyCloud's `docker-compose.yml` (variant A), or use it as a separate compose file that joins the TeddyCloud network (variant B). Keep the `teddybridge-config` volume, and add both volumes to the top-level `volumes:` section.
2. Start it: `docker compose up -d teddybridge`
3. Get the one-time code: `docker logs teddybridge`
4. Open `http://<host>:8085/` and follow the [setup](#setup-in-the-browser-since-12). As TeddyCloud address in the same stack, enter `http://teddycloud`. If TeddyCloud has a login, enter its user and password. To show teddybridge inside TeddyCloud via the [plugin](#teddycloud-plugin-optional), enter TeddyCloud's address from your browser's address bar under *More settings*, e.g. `https://teddycloud.example.com`.

**With an env file instead:** download the template, fill it in and add `env_file: ./teddybridge.env` to the service. In the env file, put `TC_AUTH` in single quotes if the password contains `$`.

```sh
curl -fsSLo teddybridge.env https://raw.githubusercontent.com/urza-lab/teddybridge/main/deploy/teddybridge.env.example
chmod 600 teddybridge.env
docker compose run --rm -it teddybridge python -m app.pwhash    # password hash for UI_PASS_HASH
docker compose up -d teddybridge
```

Update: `docker compose pull teddybridge && docker compose up -d teddybridge` – only while the queue is empty: the queue lives in memory, so a running transfer is lost (TeddyCloud may still finish an encode that was already running; transfer the item again afterwards, teddybridge then skips it or reports an incomplete TAF). To build it yourself instead of using the prebuilt image: clone the repo and use `build: ./teddybridge` instead of `image:` in the compose file.

Without TeddyCloud in Docker on the same host, it works just as well as a standalone container, e.g. with `network_mode: host` and `TC_URL` pointing to the TeddyCloud address.

The image includes ffmpeg and runs as its own non-root user; the template also sets a read-only file system, `cap_drop: ALL` and `no-new-privileges`. Only `/config` (volume for the settings), `/var/tmp` (volume for downloads – not in RAM, source files can be large) and a small `/tmp` are writable. A NAS share is mounted read-only (`:ro`). Health check: `/login`.

Notes: restarting the container discards the queue (same as the systemd service). If a reverse proxy sits in front, put its IP into `TRUSTED_PROXIES` (Docker normally keeps the source IP on published ports).

## TeddyCloud plugin (optional)

The plugin in `plugin/teddybridge/` adds a “teddybridge” entry to TeddyCloud that opens teddybridge. TeddyCloud lists every folder under `/teddycloud/data/www/plugins/` that contains a `plugin.json`. Installing it is a manual step, also in the same compose stack: by design, teddybridge only writes to the library through the API, never into TeddyCloud itself.

On first use the plugin asks for the address at which **the browser** reaches teddybridge, e.g. `https://teddybridge.example.lan/` or `http://<host>:8085/` (not the Docker service name), and stores it in that browser. To change it later, open the plugin page with `?setup` (`…/plugins/teddybridge/index.html?setup`), or click “Change address” while it opens. To preset the address for all browsers, replace `TEDDYBRIDGE_URL` in `index.html` (see below).

**Inside TeddyCloud or in its own window.** teddybridge is shown inside TeddyCloud if `FRAME_ANCESTORS` contains TeddyCloud's address as the browser sees it, e.g. `FRAME_ANCESTORS=http://192.0.2.10` when TeddyCloud is at `http://192.0.2.10` and teddybridge at `http://192.0.2.10:8085`. Otherwise the plugin redirects the whole window to teddybridge (after about 2–3 s).

With login, embedding needs both on the **same site**: the same host name or IP with different ports, or subdomains of the same registered domain (`tc.example.com` / `tb.example.com`). The session cookie is `SameSite=Strict`, so browsers do not send it to a framed teddybridge on another site (e.g. different IPs, or `teddycloud.lan` / `teddybridge.lan`). teddybridge then shows a hint with a link to open it in its own window. Without login this restriction does not apply.

**From the TeddyCloud plugin collection**

The plugin is also part of the community collection [TeddyCloudPlugins](https://github.com/henryk86/TeddyCloudPlugins): download `teddybridge-<version>.zip` from its latest release and unzip it into TeddyCloud's `plugins` folder, so that `plugins/teddybridge/plugin.json` exists. The collection may lag behind this repository by a version.

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

If the browser does not allow the redirect without a click, an “Open teddybridge” button stays. The plugin follows the browser language (German or English).

## Tests

```sh
venv/bin/python tests/test_e2e.py     # end-to-end against mock servers
venv/bin/python tests/test_audio.py   # MP3 merging and m4b chapters with real audio files (needs ffmpeg)
venv/bin/python tests/test_setup.py   # setup and settings page: one-time codes, tests, saving, restart
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
- **Security headers:** strict CSP (own origin only, no external scripts), `frame-ancestors 'none'` and `X-Frame-Options: DENY` (with `FRAME_ANCESTORS`: only the listed origins, no `X-Frame-Options`), `nosniff`, `Referrer-Policy`. OpenAPI/docs are disabled, no `Server` header.
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
| Without `UI_USER`/`UI_PASS_HASH`, anyone on the network can start jobs | medium | Set a login unless only trusted people reach the port |
| With `FRAME_ANCESTORS`, the listed origins can frame teddybridge (clickjacking from there) | low | List only TeddyCloud's own address |
| Failed attempts are delayed per request, but parallel attempts are not limited; no lockout | low (LAN) | Do not expose to the internet; rate-limit in the proxy if needed |
| Navidrome login per the Subsonic protocol (`md5(password+salt)`) | low | Use a dedicated Navidrome user with its own password |
| `TC_AUTH` (and the TeddyCloud session) is sent to TeddyCloud unencrypted over `http://` | low (LAN) | Reach TeddyCloud via HTTPS if possible |
| File source: someone who can **write** to the share could in theory swap in a symlink between path check and open (TOCTOU) | low | Mount the share read-only; anyone with write access should be trusted |
| Sessions in memory only, valid for 30 days | low | A restart logs everyone out |
| Setup page (only without configuration): anyone with the one-time code from the log can configure the instance | low | Complete the setup right after the first start; the code is only shown in the log |
| Settings page without login: anyone on the network can request a one-time code, but only someone who can read the log can use it | low | Set a login; then the page needs the session and the current password |
| Assign to a Tonie changes an existing figure's content (the only change to existing TeddyCloud settings) | low | Only on button press with confirmation, only existing figures and own TAFs, refused if the assignment changed meanwhile; old source shown and logged; set a login if others are on the network |

## Ideas for later versions

- Optional parallel processing in the queue (e.g. two jobs at once), since TeddyCloud encodes on a single CPU core.

### Deliberately not planned: listening progress across boxes / with Audiobookshelf

Desirable, but not possible with today's means. The Toniebox stores the position only in a small internal memory of the **box** – not on the Tonie, not in the cloud – and does not report it to the server. TeddyCloud only replaces the server (own CA certificate, DNS); the box keeps running the original firmware. So neither TeddyCloud nor teddybridge can read the position or set it on another box.

A CC3200 box patched via the Hackiebox bootloader does not change this either as of today: no such patch exists, and according to the developers there is hardly any room in memory; saving to the SD card would require a larger rework. Standalone custom firmwares (HackieboxNG for CC3200, TeddyBox for ESP32) are not yet ready for everyday use. Should one of them be able to report and restore the position in the future, a sync (also with ABS, then with write access only for progress) could be added.

**Update 2026-11:** a CC3200 custom firmware in **alpha** ([Oaradla/teddycloud_cfw](https://github.com/Oaradla/teddycloud_cfw), planned to move into regular TeddyCloud later) already reports start/stop events, the current title and play/pause to TeddyCloud and allows remote control. Continuing a Tonie on another box is not possible yet, but the developer wants to try to implement it. If that lands in TeddyCloud, resuming across boxes would happen between box and TeddyCloud; teddybridge would only be involved for an optional sync with Audiobookshelf.

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).

In short: you may use, modify and share it. Anyone who distributes a modified version, or runs one as a network service for others, must make its source code available under the same license.
