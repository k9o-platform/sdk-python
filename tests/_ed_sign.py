"""Test-only Ed25519 signer (RFC 8032 section 5.1.6) built on the SDK's verifier internals."""

from __future__ import annotations

import hashlib

from k9o import _ed25519 as ed


def public_key(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return ed._encode(ed._mul(a, ed._BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    pub = ed._encode(ed._mul(a, ed._BASE))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % ed._L
    r_enc = ed._encode(ed._mul(r, ed._BASE))
    k = int.from_bytes(hashlib.sha512(r_enc + pub + message).digest(), "little") % ed._L
    return r_enc + ((r + k * a) % ed._L).to_bytes(32, "little")
