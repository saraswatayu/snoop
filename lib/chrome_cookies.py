"""lib/chrome_cookies.py — read cookies from Chromium-family browsers.

Modeled on `mvanhorn/last30days-skill`'s lib/chrome_cookies.py with two
differences:

1. Generalized to "Chromium-family" — works for Chrome, Brave, Edge,
   Arc, Vivaldi (any browser with the same v10 cookie encryption).
2. Adds a Linux path. On most Linux distros, Chrome cookies are stored
   unencrypted on disk (`enc[0..3] != "v10"`) so we just read them.
   On distros where libsecret/kwallet IS configured for Chrome (rare),
   the cookie values ARE encrypted and we currently can't decrypt them
   — we'll surface that as a capability gap in diagnose.

Windows: deliberately not supported in v1. Chromium 130+ added App-Bound
Encryption (v20 prefix) which uses DPAPI + per-app keys; cracking that
requires either Chrome's process or a known-public bypass. Document the
limit; if a real user needs it, we add it then.

Auth chain for Google cookies specifically:
  1. Try the active Chrome profile (~/Library/Application Support/Google/Chrome/Default)
  2. If a different profile is in use, scan all "Profile N" dirs by mtime
  3. Fall back to Brave / Edge / Arc / Vivaldi if Chrome isn't installed
  4. Return None if no usable session cookies found anywhere

Cookies of interest for the Google account lookup:
  SID, SSID, HSID, APISID, SAPISID                  — legacy session cookies
  __Secure-1PSID, __Secure-3PSID                    — modern session cookies
  __Secure-1PAPISID, __Secure-3PAPISID              — modern API auth cookies

SAPISID (or __Secure-1PAPISID / __Secure-3PAPISID) is what feeds the
SAPISIDHASH computation in lib/google_account.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


# ---- v10 encryption constants (shared across Chromium browsers) -------------

_SALT = b"saltysalt"
_PBKDF2_ITERATIONS = 1003
_KEY_LENGTH = 16
_IV_HEX = "20" * 16  # 16 space bytes


# ---- browser registry -------------------------------------------------------


# Each entry: (display_name, macOS_db_path_template, macOS_keychain_service)
# Path template uses {home} replacement.
_MACOS_BROWSERS = [
    ("chrome",
     "{home}/Library/Application Support/Google/Chrome",
     "Chrome Safe Storage"),
    ("brave",
     "{home}/Library/Application Support/BraveSoftware/Brave-Browser",
     "Brave Safe Storage"),
    ("edge",
     "{home}/Library/Application Support/Microsoft Edge",
     "Microsoft Edge Safe Storage"),
    ("arc",
     "{home}/Library/Application Support/Arc/User Data",
     "Arc Safe Storage"),
    ("vivaldi",
     "{home}/Library/Application Support/Vivaldi",
     "Vivaldi Safe Storage"),
]


_LINUX_BROWSERS = [
    ("chrome", "{home}/.config/google-chrome"),
    ("chromium", "{home}/.config/chromium"),
    ("brave", "{home}/.config/BraveSoftware/Brave-Browser"),
    ("edge", "{home}/.config/microsoft-edge"),
    ("vivaldi", "{home}/.config/vivaldi"),
]


# ---- profile discovery ------------------------------------------------------


def _profile_dirs(browser_root: Path) -> list[Path]:
    """Return the list of profile directories under a browser root, with
    "Default" first if it exists and remaining profiles sorted by mtime
    descending (most-recently-used first)."""
    if not browser_root.is_dir():
        return []
    out: list[Path] = []
    default = browser_root / "Default"
    if default.is_dir():
        out.append(default)
    try:
        numbered = [
            c for c in browser_root.iterdir()
            if c.is_dir() and c.name.startswith("Profile ")
        ]
        out.extend(sorted(numbered, key=lambda p: p.stat().st_mtime, reverse=True))
    except OSError:
        pass
    return out


def _find_cookie_db(browser_root: Path) -> Path | None:
    """Find a 'Cookies' SQLite file under any profile of the browser. Newer
    Chrome versions store it under `Network/Cookies` instead of directly
    under the profile dir."""
    for profile in _profile_dirs(browser_root):
        for relative in ("Network/Cookies", "Cookies"):
            candidate = profile / relative
            if candidate.is_file():
                return candidate
    return None


# ---- macOS keychain access --------------------------------------------------


def _macos_keychain_passphrase(service_name: str) -> bytes | None:
    """Fetch the encryption passphrase for `service_name` from macOS Keychain.

    May trigger a one-time interactive system dialog ("Snoop wants to use
    your confidential information stored in 'Chrome Safe Storage' in your
    keychain"). Subsequent calls in the same session are silent.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service_name],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.info("keychain access for %s failed: %s", service_name, e)
        return None
    if result.returncode != 0:
        logger.info("keychain rejected %s: %s",
                    service_name, result.stderr.strip()[:120])
        return None
    passphrase = result.stdout.strip()
    return passphrase.encode("utf-8") if passphrase else None


def _derive_aes_key(passphrase: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha1", passphrase, _SALT, _PBKDF2_ITERATIONS, dklen=_KEY_LENGTH,
    )


# ---- v10 decryption (openssl CLI) -------------------------------------------


def _decrypt_v10(ciphertext: bytes, aes_key: bytes, db_version: int) -> str | None:
    """Decrypt a 'v10'-prefixed Chromium cookie value using openssl.

    Chrome 130+ (db_version >= 24) prepends a 32-byte SHA-256 prefix on
    decrypted values which we strip.
    """
    if not ciphertext:
        return None
    try:
        result = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d",
             "-K", aes_key.hex(), "-iv", _IV_HEX, "-nopad"],
            input=ciphertext, capture_output=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.info("openssl decrypt failed: %s", e)
        return None
    if result.returncode != 0:
        return None
    plaintext = _strip_pkcs7(result.stdout)
    if plaintext is None:
        return None
    if db_version >= 24 and len(plaintext) > 32:
        plaintext = plaintext[32:]
    return plaintext.decode("utf-8", errors="replace")


def _strip_pkcs7(data: bytes) -> bytes | None:
    if not data:
        return None
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        return None
    return data[:-pad]


# ---- SQLite read ------------------------------------------------------------


def _query_cookies(
    db_path: Path,
    domain_suffix: str,
    cookie_names: list[str],
    decrypt_value: bool,
    aes_key: bytes | None,
) -> dict[str, str]:
    """Open the cookies DB (via a temp copy so we don't lock the browser's
    live DB) and return {name: value} for cookies matching domain + names.

    If decrypt_value is True, attempts v10 decryption on `encrypted_value`
    when `value` is empty. If decrypt_value is False (Linux unencrypted
    path), just reads `value` directly.
    """
    if not db_path.is_file():
        return {}

    # Copy to temp — the live DB may be locked by a running browser.
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        shutil.copy2(str(db_path), tmp_path)
    except OSError as e:
        logger.info("could not copy cookie db %s: %s", db_path, e)
        Path(tmp_path).unlink(missing_ok=True)
        return {}

    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(tmp_path)
        try:
            cur = conn.cursor()
            # Read DB version (Chrome 130+ needs the SHA-256 prefix strip).
            db_version = 0
            try:
                cur.execute("SELECT value FROM meta WHERE key='version'")
                row = cur.fetchone()
                if row:
                    db_version = int(row[0])
            except sqlite3.Error:
                pass

            placeholders = ",".join("?" * len(cookie_names))
            cur.execute(
                f"SELECT name, value, encrypted_value FROM cookies "
                f"WHERE host_key LIKE ? AND name IN ({placeholders})",
                [f"%{domain_suffix}"] + list(cookie_names),
            )
            for name, value, enc in cur.fetchall():
                if value:
                    out[name] = value
                    continue
                if not decrypt_value or aes_key is None or not enc:
                    continue
                if enc[:3] == b"v10":
                    decrypted = _decrypt_v10(enc[3:], aes_key, db_version)
                    if decrypted:
                        out[name] = decrypted
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.info("sqlite read failed for %s: %s", db_path, e)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return out


# ---- public API -------------------------------------------------------------


def list_supported_browsers() -> list[str]:
    """Names of browsers that this loader knows how to read on the current OS."""
    if sys.platform == "darwin":
        return [n for n, *_ in _MACOS_BROWSERS]
    if sys.platform.startswith("linux"):
        return [n for n, _ in _LINUX_BROWSERS]
    return []


def get_cookies(
    domain_suffix: str,
    cookie_names: list[str],
    *,
    browsers: list[str] | None = None,
) -> dict[str, str]:
    """Read named cookies for a domain from any installed Chromium-family browser.

    Args:
        domain_suffix: host_key suffix to match. For Google use ".google.com"
            (the leading dot matches Google's standard cookie scope).
        cookie_names: cookie name allowlist.
        browsers: optional restriction. If None, tries all known browsers in
            registry order and returns the FIRST one that yields a non-empty
            result.

    Returns:
        {cookie_name: value} dict. Empty dict if no cookies found anywhere or
        if running on an unsupported platform.
    """
    if sys.platform == "darwin":
        return _get_cookies_macos(domain_suffix, cookie_names, browsers)
    if sys.platform.startswith("linux"):
        return _get_cookies_linux(domain_suffix, cookie_names, browsers)
    logger.info("chrome_cookies: platform %s not supported in v1", sys.platform)
    return {}


def _get_cookies_macos(
    domain_suffix: str,
    cookie_names: list[str],
    browsers: list[str] | None,
) -> dict[str, str]:
    home = str(Path.home())
    for name, root_tpl, keychain_svc in _MACOS_BROWSERS:
        if browsers is not None and name not in browsers:
            continue
        root = Path(root_tpl.format(home=home))
        db = _find_cookie_db(root)
        if db is None:
            continue
        passphrase = _macos_keychain_passphrase(keychain_svc)
        aes_key = _derive_aes_key(passphrase) if passphrase else None
        cookies = _query_cookies(
            db, domain_suffix, cookie_names,
            decrypt_value=True, aes_key=aes_key,
        )
        if cookies:
            logger.info("loaded %d cookies from %s", len(cookies), name)
            return cookies
    return {}


def _get_cookies_linux(
    domain_suffix: str,
    cookie_names: list[str],
    browsers: list[str] | None,
) -> dict[str, str]:
    home = str(Path.home())
    for name, root_tpl in _LINUX_BROWSERS:
        if browsers is not None and name not in browsers:
            continue
        root = Path(root_tpl.format(home=home))
        db = _find_cookie_db(root)
        if db is None:
            continue
        # On Linux most distros default to no encryption (`value` field is
        # populated directly). If the user has configured libsecret/kwallet
        # for Chrome, the values would be encrypted with a different scheme;
        # we can't decrypt those in v1 and `_query_cookies` returns empty.
        cookies = _query_cookies(
            db, domain_suffix, cookie_names,
            decrypt_value=False, aes_key=None,
        )
        if cookies:
            logger.info("loaded %d cookies from %s (linux)", len(cookies), name)
            return cookies
    return {}


# ---- Google-specific convenience --------------------------------------------


GOOGLE_SESSION_COOKIE_NAMES = [
    # Legacy session + API auth cookies.
    "SID", "SSID", "HSID", "APISID", "SAPISID",
    "LSID", "OSID",
    # Modern (post-2020) session cookies. Required for the People API to
    # accept the request — sending only the legacy set yields a 401
    # SESSION_COOKIE_INVALID even when SAPISIDHASH and the API key/origin
    # pairing are correct.
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "__Secure-OSID",
]


def get_google_cookies() -> dict[str, str]:
    """Convenience: fetch Google session cookies from any Chromium browser
    on this machine. Returns empty dict if nothing usable found."""
    return get_cookies(".google.com", GOOGLE_SESSION_COOKIE_NAMES)
