import base64

import pytest

from app.core.crypto import CredentialCipher, CredentialCryptoError, generate_key


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher.from_config(f"v1:{generate_key()}", "v1")


def test_round_trip(cipher: CredentialCipher) -> None:
    token = cipher.encrypt("1//refresh-token-value", "email_account:abc:refresh_token")
    assert "refresh-token-value" not in token
    assert token.startswith("v1:")
    assert cipher.decrypt(token, "email_account:abc:refresh_token") == "1//refresh-token-value"


def test_nonce_is_random(cipher: CredentialCipher) -> None:
    assert cipher.encrypt("same", "ctx") != cipher.encrypt("same", "ctx")


def test_ciphertext_is_bound_to_context(cipher: CredentialCipher) -> None:
    token = cipher.encrypt("secret", "email_account:row-1:refresh_token")
    with pytest.raises(CredentialCryptoError):
        cipher.decrypt(token, "email_account:row-2:refresh_token")


def test_tampering_is_detected(cipher: CredentialCipher) -> None:
    version, _, encoded = cipher.encrypt("secret", "ctx").partition(":")
    raw = bytearray(base64.urlsafe_b64decode(encoded))
    raw[-1] ^= 0x01
    with pytest.raises(CredentialCryptoError):
        cipher.decrypt(f"{version}:{base64.urlsafe_b64encode(bytes(raw)).decode()}", "ctx")


def test_key_rotation_keeps_old_ciphertexts_readable() -> None:
    k1, k2 = generate_key(), generate_key()
    old = CredentialCipher.from_config(f"v1:{k1}", "v1")
    token = old.encrypt("secret", "ctx")
    rotated = CredentialCipher.from_config(f"v1:{k1},v2:{k2}", "v2")
    assert rotated.decrypt(token, "ctx") == "secret"
    assert rotated.needs_rotation(token)
    assert not rotated.needs_rotation(rotated.encrypt("secret", "ctx"))


def test_unknown_key_version_rejected(cipher: CredentialCipher) -> None:
    with pytest.raises(CredentialCryptoError, match="unknown key version"):
        cipher.decrypt("v9:AAAA", "ctx")


@pytest.mark.parametrize("raw,active,match", [
    ("", "v1", "no credential encryption keys"),
    ("v1:" + base64.urlsafe_b64encode(b"short").decode(), "v1", "32 bytes"),
    (f"v1:{'A' * 43}", "v2", "not configured"),
    ("nocolon", "v1", "version:base64key"),
])
def test_bad_configuration(raw: str, active: str, match: str) -> None:
    with pytest.raises(CredentialCryptoError, match=match):
        CredentialCipher.from_config(raw, active)


def test_errors_never_leak_key_material() -> None:
    key = generate_key()
    cipher = CredentialCipher.from_config(f"v1:{key}", "v1")
    with pytest.raises(CredentialCryptoError) as exc:
        cipher.decrypt("v1:" + "B" * 40, "ctx")
    assert key not in str(exc.value)
