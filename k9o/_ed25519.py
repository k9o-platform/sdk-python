"""
Ed25519 signature verification (RFC 8032, section 5.1.7) in pure Python integer arithmetic.

The SDK has no cryptography dependency, so receipt verification uses this module. It only verifies; it holds no
secret key, so constant-time behaviour is not needed. A verification takes a few milliseconds.

`verify(public_key, message, signature)` is cofactorless: it accepts a signature when `[S]B == R + [h]A`, rejects a
non-canonical `S` (`S >= L`) and rejects a public key or `R` that is not a valid curve point encoding.
"""

from __future__ import annotations

import hashlib

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# Extended homogeneous coordinates (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z.
_Point = tuple[int, int, int, int]


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _recover_x(_BY, 0)
assert _BX is not None
_BASE: _Point = (_BX, _BY, 1, _BX * _BY % _P)
_IDENTITY: _Point = (0, 1, 1, 0)


def _add(a: _Point, b: _Point) -> _Point:
    a1 = (a[1] - a[0]) * (b[1] - b[0]) % _P
    b1 = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c1 = 2 * a[3] * b[3] * _D % _P
    d1 = 2 * a[2] * b[2] % _P
    e, f, g, h = b1 - a1, d1 - c1, d1 + c1, b1 + a1
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: _Point) -> _Point:
    result = _IDENTITY
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _equal(a: _Point, b: _Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _decode(encoded: bytes) -> _Point | None:
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _encode(point: _Point) -> bytes:
    zinv = pow(point[2], _P - 2, _P)
    x = point[0] * zinv % _P
    y = point[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True when `signature` (64 bytes) is a valid Ed25519 signature of `message` under `public_key` (32 bytes)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a_point = _decode(public_key)
    r_point = _decode(signature[:32])
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    return _equal(_mul(s, _BASE), _add(r_point, _mul(h, a_point)))
