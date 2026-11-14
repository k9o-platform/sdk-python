"""Wiring test for k9o.kibo encrypt/decrypt against the REAL bound core (native
uniffi binding). Exercises the wasm-equivalent native path WITHOUT a live KMS,
via the posture-floor path (rejected pre-KMS). Full round-trip needs real KMS
creds (staging conformance ZK step)."""

import asyncio
import os

import pytest

from k9o.errors import KError

# The generated native binding is a build artifact (gitignored). Where it is not built, or another test file has
# stood in a stub for it (test_kibo_client_core), there is no real core to run: skip instead of failing on a stub.
try:
    from k9o.kibo._ffi import kibo_kms_rest_ffi as _real_ffi
except ImportError:
    _real_ffi = None
if _real_ffi is None or "__file__" not in vars(_real_ffi):  # the stub answers any attribute, so read its dict
    if os.environ.get("K9O_REQUIRE_NATIVE_BINDING"):  # the release job sets this: a missing binding is a failure
        raise RuntimeError("K9O_REQUIRE_NATIVE_BINDING is set but the generated kibo_kms_rest_ffi binding is not built")
    pytest.skip("the generated kibo_kms_rest_ffi binding is not built here", allow_module_level=True)

from k9o.kibo import (
    encrypt,
    EncryptInput,
    FfiCredential,
    FfiKeyRef,
    FfiProviderConfig,
    FfiPosture,
    FfiAttestationTier,
    FfiPosturePolicy,
    FfiMinPosture,
    KmsProvider,
)


def _floor_violation_input() -> EncryptInput:
    # byok requested, but the project floor requires hyok -> byok < hyok.
    return EncryptInput(
        provider=KmsProvider.AWS_KMS,
        posture=FfiPosture.BYOK,
        strategy=None,
        oracle_mode=None,
        kms_cred=FfiCredential(secret="dummy-token", expires_at_unix_secs=9_999_999_999),
        key_ref=FfiKeyRef(version=1, external_ref="arn:aws:kms:us-east-1:000000000000:key/abc-123"),
        provider_config=FfiProviderConfig(aws_region="us-east-1", vault_address=None),
        attestation_tier=FfiAttestationTier.SOFTWARE,
        encryption_context=[],
        plaintext=b"hi",
        posture_policy=FfiPosturePolicy(min_custody_posture=FfiMinPosture.HYOK, allowed_oracle_modes=None),
        expected_key_refs=["arn:aws:kms:us-east-1:000000000000:key/abc-123"],
    )


def test_posture_floor_violation_maps_to_kerror() -> None:
    async def run() -> None:
        with pytest.raises(KError) as ei:
            await encrypt(_floor_violation_input())
        k = ei.value
        assert k.code == "K9O_CRYPTO_POSTURE_FLOOR_VIOLATION"
        assert k.context.get("dimension") == "custody_posture"

    asyncio.run(run())
