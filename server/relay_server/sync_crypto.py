"""Keys, signatures and batch encryption for multi-machine sync.

Only imported when sync is enabled. Needs PyNaCl (the ``sync`` extra).

Each host has one Ed25519 key (``<data_dir>/sync.key``, mode 0600). It signs
everything the host writes to the shared folder, and its X25519 form (derived
by libsodium) receives encrypted batches. The private key never leaves the
host.

Batch envelope (a JSON file)::

    {"v": 1, "from": host_id, "created": iso, "first": id, "last": id, "count": n,
     "wraps": {host_id: b64(SealedBox(file_key))}, "ct": b64(SecretBox(file_key, body)),
     "sig": b64(Ed25519 over the canonical JSON of every other field)}

``body`` is one format byte (0 = raw, 1 = zlib) followed by the JSON list of
events. Compression happens before encryption (ciphertext doesn't compress).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib
from pathlib import Path
from typing import Any

try:
    from nacl import exceptions as _nacl_exc
    from nacl import public as _public
    from nacl import secret as _secret
    from nacl import signing as _signing
    from nacl import utils as _nacl_utils
except ImportError as exc:  # pragma: no cover — exercised via require_crypto()
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None

KEY_FILE = "sync.key"
ENVELOPE_VERSION = 1
_FMT_RAW = 0
_FMT_ZLIB = 1
_COMPRESS_MIN = 256


class SyncCryptoError(Exception):
    """A record or batch failed verification or decryption."""


def crypto_available() -> bool:
    return _IMPORT_ERROR is None


def require_crypto() -> None:
    if _IMPORT_ERROR is not None:
        raise RuntimeError(
            "relay sync needs PyNaCl. Install the sync extra: "
            "`uv sync --directory <relay>/server --extra sync --inexact`"
        ) from _IMPORT_ERROR


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def fingerprint(public_key_b64: str) -> str:
    """Short human-comparable fingerprint, e.g. ``k3qd-7x2m-ab4p``."""
    digest = hashlib.sha256(_unb64(public_key_b64)).digest()
    text = base64.b32encode(digest).decode("ascii").lower()[:12]
    return "-".join(text[i : i + 4] for i in range(0, 12, 4))


# ---------------------------------------------------------------------------
# Host key
# ---------------------------------------------------------------------------


class HostKey:
    def __init__(self, signing_key: "_signing.SigningKey"):
        self._sk = signing_key

    @property
    def public_b64(self) -> str:
        return _b64(bytes(self._sk.verify_key))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_b64)

    def sign(self, message: bytes) -> str:
        return _b64(self._sk.sign(message).signature)

    def open_wrap(self, wrapped_b64: str) -> bytes:
        box = _public.SealedBox(self._sk.to_curve25519_private_key())
        return box.decrypt(_unb64(wrapped_b64))


def load_host_key(data_dir: Path) -> HostKey | None:
    require_crypto()
    path = Path(data_dir) / KEY_FILE
    if not path.exists():
        return None
    seed = bytes.fromhex(path.read_text().strip())
    return HostKey(_signing.SigningKey(seed))


def ensure_host_key(data_dir: Path) -> HostKey:
    """Load this host's key, generating it (0600) on first use."""
    require_crypto()
    key = load_host_key(data_dir)
    if key is not None:
        return key
    sk = _signing.SigningKey.generate()
    path = Path(data_dir) / KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(bytes(sk).hex() + "\n")
    return HostKey(sk)


def verify(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    require_crypto()
    try:
        _signing.VerifyKey(_unb64(public_key_b64)).verify(message, _unb64(signature_b64))
    except (_nacl_exc.BadSignatureError, ValueError, TypeError, _nacl_exc.CryptoError):
        return False
    return True


# ---------------------------------------------------------------------------
# Signed records (membership, join requests)
# ---------------------------------------------------------------------------


def sign_record(key: HostKey, record: dict) -> dict:
    body = {k: v for k, v in record.items() if k != "sig"}
    return {**body, "sig": key.sign(canonical(body))}


def verify_record(record: dict, public_key_b64: str) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("sig"), str):
        return False
    body = {k: v for k, v in record.items() if k != "sig"}
    return verify(public_key_b64, canonical(body), record["sig"])


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def _pack_body(events: list[dict]) -> bytes:
    raw = canonical(events)
    if len(raw) >= _COMPRESS_MIN:
        return bytes([_FMT_ZLIB]) + zlib.compress(raw, 6)
    return bytes([_FMT_RAW]) + raw


def _unpack_body(body: bytes) -> list[dict]:
    if not body:
        raise SyncCryptoError("empty batch body")
    fmt, rest = body[0], body[1:]
    try:
        if fmt == _FMT_ZLIB:
            rest = zlib.decompress(rest)
        elif fmt != _FMT_RAW:
            raise SyncCryptoError(f"unknown batch body format {fmt}")
        events = json.loads(rest)
    except (zlib.error, ValueError) as exc:
        raise SyncCryptoError(f"malformed batch body: {exc}") from exc
    if not isinstance(events, list):
        raise SyncCryptoError("batch body isn't a list")
    return events


def seal_batch(
    key: HostKey,
    sender_id: str,
    events: list[dict],
    recipients: dict[str, str],
    created: str,
) -> dict:
    """Encrypt events to every recipient (host_id -> public key b64) and sign."""
    require_crypto()
    if not events:
        raise ValueError("empty batch")
    file_key = _nacl_utils.random(_secret.SecretBox.KEY_SIZE)
    ct = _secret.SecretBox(file_key).encrypt(_pack_body(events))
    wraps = {}
    for host_id, pk_b64 in sorted(recipients.items()):
        curve_pk = _signing.VerifyKey(_unb64(pk_b64)).to_curve25519_public_key()
        wraps[host_id] = _b64(_public.SealedBox(curve_pk).encrypt(file_key))
    envelope = {
        "v": ENVELOPE_VERSION,
        "from": sender_id,
        "created": created,
        "first": events[0]["id"],
        "last": events[-1]["id"],
        "count": len(events),
        "wraps": wraps,
        "ct": _b64(bytes(ct)),
    }
    envelope["sig"] = key.sign(canonical(envelope))
    return envelope


def verify_batch(envelope: dict, sender_public_b64: str) -> bool:
    if not isinstance(envelope, dict) or envelope.get("v") != ENVELOPE_VERSION:
        return False
    return verify_record(envelope, sender_public_b64)


def open_batch(envelope: dict, key: HostKey, my_host_id: str) -> list[dict] | None:
    """Decrypt a verified batch. None if it wasn't encrypted to this host."""
    require_crypto()
    wrap = (envelope.get("wraps") or {}).get(my_host_id)
    if wrap is None:
        return None
    try:
        file_key = key.open_wrap(wrap)
        body = _secret.SecretBox(file_key).decrypt(_unb64(envelope["ct"]))
    except (_nacl_exc.CryptoError, ValueError, KeyError) as exc:
        raise SyncCryptoError(f"can't decrypt batch: {exc}") from exc
    return _unpack_body(body)
