"""Tests for lib/chrome_cookies.py.

These tests build real SQLite cookie DBs in tmp_path and exercise the
read path end-to-end. The decryption path (macOS keychain + openssl) is
covered by tests with known-plaintext fixtures and openssl-stub
monkeypatches; we don't need real keychain access in CI.

Windows is explicitly not supported in v1 — there's a test confirming
it returns empty there. macOS and Linux paths are tested via monkeypatch
of sys.platform.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import lib.chrome_cookies as cc


# ---- helpers ----------------------------------------------------------------


def make_cookie_db(path: Path, rows: list[tuple]) -> None:
    """Build a minimal Chromium cookies SQLite file.

    rows = [(host_key, name, value, encrypted_value_bytes), ...]
    Use empty bytes for encrypted_value when value is set directly (Linux
    unencrypted path). Use empty string for value when only encrypted is set.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE cookies (
            host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB
        )
    """)
    cur.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    cur.execute("INSERT INTO meta VALUES ('version', '20')")  # pre-Chrome-130
    for host_key, name, value, enc in rows:
        cur.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?)",
            (host_key, name, value, enc),
        )
    conn.commit()
    conn.close()


def make_chrome_layout(tmp_path: Path, profile: str, rows: list[tuple]) -> Path:
    """Build the macOS Chrome layout under tmp_path. Returns home dir."""
    home = tmp_path / "home"
    root = home / "Library" / "Application Support" / "Google" / "Chrome"
    profile_dir = root / profile
    db = profile_dir / "Cookies"
    make_cookie_db(db, rows)
    return home


def make_linux_chrome_layout(tmp_path: Path, profile: str, rows: list[tuple]) -> Path:
    """Build the Linux Chrome layout."""
    home = tmp_path / "home"
    db = home / ".config" / "google-chrome" / profile / "Cookies"
    make_cookie_db(db, rows)
    return home


# ---- list_supported_browsers ------------------------------------------------


def test_list_supported_browsers_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    names = cc.list_supported_browsers()
    assert "chrome" in names
    assert "brave" in names


def test_list_supported_browsers_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    names = cc.list_supported_browsers()
    assert "chrome" in names
    assert "chromium" in names


def test_list_supported_browsers_windows_empty(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert cc.list_supported_browsers() == []


# ---- Linux read path (unencrypted) ------------------------------------------


def test_linux_reads_unencrypted_cookies(tmp_path, monkeypatch):
    home = make_linux_chrome_layout(tmp_path, "Default", [
        (".google.com", "SID", "sid-value-123", b""),
        (".google.com", "SAPISID", "sapisid-value-abc", b""),
        (".other.com", "irrelevant", "skip-me", b""),  # wrong domain
    ])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    out = cc.get_cookies(".google.com", ["SID", "SAPISID", "missing"])
    assert out == {"SID": "sid-value-123", "SAPISID": "sapisid-value-abc"}


def test_linux_returns_empty_when_no_chrome_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: tmp_path))
    assert cc.get_cookies(".google.com", ["SID"]) == {}


def test_linux_falls_through_to_chromium_when_chrome_empty(tmp_path, monkeypatch):
    """Chrome dir exists but DB has no matching cookies → try chromium."""
    home = tmp_path / "home"
    # Chrome: DB exists but only has cookies for a different domain
    chrome_db = home / ".config" / "google-chrome" / "Default" / "Cookies"
    make_cookie_db(chrome_db, [(".other.com", "X", "y", b"")])
    # Chromium: has the SID we want
    chromium_db = home / ".config" / "chromium" / "Default" / "Cookies"
    make_cookie_db(chromium_db, [(".google.com", "SID", "from-chromium", b"")])

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    out = cc.get_cookies(".google.com", ["SID"])
    assert out == {"SID": "from-chromium"}


def test_linux_respects_browsers_filter(tmp_path, monkeypatch):
    """If caller restricts to brave only, Chrome shouldn't be tried even
    if it has the cookies."""
    home = tmp_path / "home"
    chrome_db = home / ".config" / "google-chrome" / "Default" / "Cookies"
    make_cookie_db(chrome_db, [(".google.com", "SID", "from-chrome", b"")])
    # Brave: no cookies file
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    out = cc.get_cookies(".google.com", ["SID"], browsers=["brave"])
    assert out == {}


def test_linux_prefers_default_profile_over_numbered(tmp_path, monkeypatch):
    home = tmp_path / "home"
    # Numbered profile: has SID
    p1_db = home / ".config" / "google-chrome" / "Profile 1" / "Cookies"
    make_cookie_db(p1_db, [(".google.com", "SID", "from-profile-1", b"")])
    # Default: also has SID
    default_db = home / ".config" / "google-chrome" / "Default" / "Cookies"
    make_cookie_db(default_db, [(".google.com", "SID", "from-default", b"")])

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    out = cc.get_cookies(".google.com", ["SID"])
    # Default profile is tried first
    assert out == {"SID": "from-default"}


def test_linux_reads_network_subdir_path(tmp_path, monkeypatch):
    """Chrome v100+ stores Cookies under Default/Network/ instead of
    directly under Default/. Verify both paths are checked."""
    home = tmp_path / "home"
    db = home / ".config" / "google-chrome" / "Default" / "Network" / "Cookies"
    make_cookie_db(db, [(".google.com", "SID", "modern-path", b"")])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    out = cc.get_cookies(".google.com", ["SID"])
    assert out == {"SID": "modern-path"}


# ---- macOS read path (encrypted) --------------------------------------------


def test_macos_decrypts_v10_cookie(tmp_path, monkeypatch):
    """Build a v10-encrypted cookie value, mock the keychain + openssl,
    confirm the round-trip yields the expected plaintext."""
    # 1) Real PBKDF2 derivation
    passphrase = b"fake-keychain-secret"
    aes_key = cc._derive_aes_key(passphrase)

    # 2) Fake a v10-encrypted cookie by running openssl ourselves to encrypt
    #    a known plaintext, then prepend "v10".
    import subprocess
    plaintext = b"session-id-value-here"
    # PKCS7 pad to 16 bytes
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    enc_result = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc",
         "-K", aes_key.hex(), "-iv", cc._IV_HEX, "-nopad"],
        input=padded, capture_output=True, check=True,
    )
    ciphertext = b"v10" + enc_result.stdout

    # 3) Build a macOS-shaped cookie DB
    home = make_chrome_layout(tmp_path, "Default", [
        (".google.com", "SID", "", ciphertext),
    ])

    # 4) Mock keychain access to return our passphrase
    monkeypatch.setattr(cc, "_macos_keychain_passphrase",
                        lambda svc: passphrase if svc == "Chrome Safe Storage" else None)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))

    out = cc.get_cookies(".google.com", ["SID"])
    assert out == {"SID": "session-id-value-here"}


def test_macos_skips_encrypted_when_keychain_denied(tmp_path, monkeypatch):
    """If the keychain access fails, encrypted cookies are silently skipped
    (the user might still have unencrypted ones from another browser)."""
    ciphertext = b"v10" + b"\x00" * 16  # garbage v10 prefix
    home = make_chrome_layout(tmp_path, "Default", [
        (".google.com", "SID", "", ciphertext),
    ])
    monkeypatch.setattr(cc, "_macos_keychain_passphrase", lambda svc: None)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))

    out = cc.get_cookies(".google.com", ["SID"])
    # No usable cookies; result is empty (not a crash)
    assert out == {}


# ---- _decrypt_v10 + PKCS7 ---------------------------------------------------


def test_strip_pkcs7_rejects_invalid_pad():
    assert cc._strip_pkcs7(b"") is None
    # Pad byte 0 is invalid
    assert cc._strip_pkcs7(b"abc\x00") is None
    # Pad byte > 16 is invalid
    assert cc._strip_pkcs7(b"abc" + bytes([17]) * 17) is None


def test_strip_pkcs7_unpads_valid():
    # 5 bytes of \x05 padding
    data = b"hello" + b"\x05\x05\x05\x05\x05"
    assert cc._strip_pkcs7(data) == b"hello"


# ---- domain filter ----------------------------------------------------------


def test_domain_filter_uses_suffix_match(tmp_path, monkeypatch):
    """A cookie with host_key='accounts.google.com' should match a query for
    suffix '.google.com' (the trailing-dot scope)."""
    home = make_linux_chrome_layout(tmp_path, "Default", [
        ("accounts.google.com", "SID", "subdomain-sid", b""),
        (".google.com", "SAPISID", "etld-sapisid", b""),
    ])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    out = cc.get_cookies(".google.com", ["SID", "SAPISID"])
    # Both should be picked up: SID from accounts.google.com (suffix match)
    # and SAPISID from .google.com directly.
    assert set(out.keys()) == {"SID", "SAPISID"}


# ---- Windows not-supported --------------------------------------------------


def test_windows_returns_empty(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert cc.get_cookies(".google.com", ["SID"]) == {}


# ---- get_google_cookies convenience -----------------------------------------


def test_get_google_cookies_uses_canonical_cookie_names(tmp_path, monkeypatch):
    home = make_linux_chrome_layout(tmp_path, "Default", [
        (".google.com", "SAPISID", "abc", b""),
        (".google.com", "__Secure-1PAPISID", "modern-abc", b""),
        (".google.com", "irrelevant", "skip", b""),
    ])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    out = cc.get_google_cookies()
    assert out == {"SAPISID": "abc", "__Secure-1PAPISID": "modern-abc"}
