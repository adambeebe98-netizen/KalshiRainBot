"""
Shared test infrastructure.

config.SETTINGS is a frozen dataclass read once at import time from
os.environ — that means DB_PATH (and everything else) can't be swapped
per-test-method within one process the way you might expect. Instead,
each test FILE calls use_temp_db() at MODULE level, before importing
storage/shadow/bot/anything that transitively imports config — this
gives that whole file's test run one isolated, throwaway SQLite file, and
individual test methods clear just the tables they touch in setUp()
rather than swapping the whole DB.

This mirrors how the real app actually behaves (config is read once per
process) rather than fighting it with importlib.reload gymnastics.
"""
from __future__ import annotations

import atexit
import os
import tempfile


def use_temp_db(starting_bankroll_cents: str = "50000") -> str:
    """Call this ONCE, at the top of a test file, before any import of
    storage/shadow/risk_manager/bot/etc. Returns the temp file path (rarely
    needed directly — tests almost always just want storage.init_db()
    called against it, which callers should do themselves right after)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    os.environ["STARTING_BANKROLL_CENTS"] = starting_bankroll_cents
    os.environ.setdefault("KALSHI_API_KEY_ID", "test-key-id")
    os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", _test_rsa_key_path())
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
    atexit.register(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    return tmp.name


def _test_rsa_key_path() -> str:
    """A real (but throwaway) RSA private key — KalshiClient's constructor
    parses whatever KALSHI_PRIVATE_KEY_PATH points at as a real PEM file,
    so /dev/null or a made-up path would fail construction outright.
    Generated once per test process and reused."""
    global _cached_key_path
    if _cached_key_path and os.path.exists(_cached_key_path):
        return _cached_key_path

    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    tmp.write(pem)
    tmp.close()
    atexit.register(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    _cached_key_path = tmp.name
    return _cached_key_path


_cached_key_path: str | None = None


def clear_tables(conn, *table_names: str) -> None:
    """Wipe specific tables between test methods within the same file,
    since they all share one DB (see use_temp_db's docstring for why)."""
    for name in table_names:
        conn.execute(f"DELETE FROM {name}")
    conn.commit()
