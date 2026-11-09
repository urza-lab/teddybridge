"""Passwort-Hashes für das Login (scrypt aus der Standardbibliothek, kein Zusatzpaket).

Format: scrypt:<n>:<r>:<p>:<salz>:<hash>  (Salz/Hash base64url ohne '=').
Bewusst ohne '$', weil systemd in EnvironmentFile '$VAR' ersetzt.

Hash erzeugen:  /opt/teddybridge/venv/bin/python -m app.pwhash
"""
import base64
import getpass
import hashlib
import hmac
import os
import sys

N, R, P = 2 ** 15, 8, 1  # ca. 0,1 s pro Prüfung
MIN_LEN = 8
_MAXMEM = 128 << 20


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _derive(pw, salt, n, r, p):
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=32)


def make(pw, n=N, r=R, p=P):
    salt = os.urandom(16)
    return f"scrypt:{n}:{r}:{p}:{_b64(salt)}:{_b64(_derive(pw, salt, n, r, p))}"


def parse(h):
    """Hash zerlegen; ValueError bei falschem Format (ohne den Wert zu nennen)."""
    try:
        kind, n, r, p, salt, dk = h.split(":")
        n, r, p = int(n), int(r), int(p)
        salt, dk = _unb64(salt), _unb64(dk)
    except Exception:
        raise ValueError("UI_PASS_HASH hat kein gültiges Format (erwartet scrypt:n:r:p:salz:hash)") from None
    if kind != "scrypt" or n < 2 ** 14 or n & (n - 1) or not (1 <= r <= 32) or not (1 <= p <= 4) \
            or len(salt) < 16 or len(dk) != 32:
        raise ValueError("UI_PASS_HASH hat ungültige Parameter")
    return n, r, p, salt, dk


def verify(pw, h):
    n, r, p, salt, dk = parse(h)
    return hmac.compare_digest(_derive(pw, salt, n, r, p), dk)


def main():
    if sys.stdin.isatty():
        pw = getpass.getpass("Neues Passwort: ")
        if pw != getpass.getpass("Wiederholen: "):
            raise SystemExit("Die Eingaben stimmen nicht überein.")
    else:  # für Skripte: Passwort als erste Zeile auf stdin
        pw = sys.stdin.readline().rstrip("\n")
    if len(pw) < MIN_LEN:
        raise SystemExit(f"Das Passwort muss mindestens {MIN_LEN} Zeichen haben.")
    print("UI_PASS_HASH=" + make(pw))


if __name__ == "__main__":
    main()
