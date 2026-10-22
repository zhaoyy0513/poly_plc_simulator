"""Encryption for the credentials we store.

Scope, stated honestly: this keeps plaintext passwords out of `app.db`, so a
copied database is not a credential leak. It does not protect against someone
who can already read files on this machine - the key sits next to the data.
That is an acceptable trade for a single-user tool; real isolation needs an OS
keyring or a passphrase prompt at startup, which is a later decision.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILE = ".secret"


def _key_path(base: Path) -> Path:
    return Path(base) / KEY_FILE


def _fernet(base: Path) -> Fernet:
    path = _key_path(base)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)
        except OSError:  # Windows: chmod is best effort
            pass
    return Fernet(path.read_bytes())


def encrypt_secret(base: Path, plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet(base).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(base: Path, ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet(base).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:  # rotated or hand-edited key: treat as "no password"
        return ""


def has_secret(ciphertext: str) -> bool:
    return bool(ciphertext)
