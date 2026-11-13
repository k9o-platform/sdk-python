"""Code-point-profile canonical JSON and RFC 8032 Ed25519 verification, against published vectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from k9o import _ed25519
from k9o._jcs import canonicalize

from ._ed_sign import public_key, sign

CASES = json.load(open(Path(__file__).parent / "fixtures" / "jcs-codepoint-cases.json", encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_code_point_profile_cases(case):
    if "expectError" in case:
        with pytest.raises(ValueError):
            canonicalize(case["input"])
    else:
        assert canonicalize(case["input"]) == case["expectCanonical"].encode("utf-8")


def test_integers_only_up_to_2_53_minus_1():
    assert canonicalize(12345678901234) == b"12345678901234"
    assert canonicalize(-7) == b"-7"
    assert canonicalize(1.0) == b"1"
    assert canonicalize(-0.0) == b"0"
    assert canonicalize(True) == b"true"
    for bad in (2**53, -(2**53), 10**21, 0.5, 1e300, {"a": [1.25]}):
        with pytest.raises(ValueError):
            canonicalize(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(bad):
    with pytest.raises(ValueError):
        canonicalize(bad)
    with pytest.raises(ValueError):
        canonicalize({"a": [bad]})


def test_other_types_lone_surrogates_and_non_string_keys_are_rejected():
    for bad in (object(), {1: "a"}, "\ud800", {"\udc00": 1}, b"x"):
        with pytest.raises(ValueError):
            canonicalize(bad)


def test_keys_sort_by_utf8_bytes_not_utf16_units():
    value = {"\U0001F600": 1, "\ufb00": 2, "\u20ac": 3, "\r": 4, "1": 5}
    keys = json.loads(canonicalize(value), object_pairs_hook=lambda pairs: [k for k, _ in pairs])
    assert keys == ["\r", "1", "\u20ac", "\ufb00", "\U0001F600"]


def test_no_whitespace_and_nested_sorting():
    assert canonicalize({"b": [1, {"z": 1, "a": 2}], "a": None}) == b'{"a":null,"b":[1,{"a":2,"z":1}]}'


# RFC 8032 section 7.1 test vectors 1 to 3.
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("seed,pub,msg,sig", RFC8032)
def test_rfc8032_vectors(seed, pub, msg, sig):
    pk, m, s = bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)
    assert _ed25519.verify(pk, m, s)
    assert public_key(bytes.fromhex(seed)) == pk  # the test signer agrees with the RFC
    assert sign(bytes.fromhex(seed), m) == s
    assert not _ed25519.verify(pk, m + b"x", s)
    flipped = bytearray(s)
    flipped[10] ^= 1
    assert not _ed25519.verify(pk, m, bytes(flipped))
    flipped = bytearray(s)
    flipped[40] ^= 1
    assert not _ed25519.verify(pk, m, bytes(flipped))


def test_non_canonical_s_and_bad_encodings_are_rejected():
    seed, pub, msg, sig = RFC8032[0]
    pk, s = bytes.fromhex(pub), bytes.fromhex(sig)
    s_int = int.from_bytes(s[32:], "little") + _ed25519._L
    assert not _ed25519.verify(pk, b"", s[:32] + s_int.to_bytes(32, "little"))
    assert not _ed25519.verify(pk[:31], b"", s)
    assert not _ed25519.verify(pk, b"", s[:63])
    assert not _ed25519.verify(b"\xff" * 32, b"", s)  # y >= p
