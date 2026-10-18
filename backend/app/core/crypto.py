"""Authenticated encryption for stored credentials (OAuth refresh tokens).

AES-256-GCM with versioned keys. Ciphertexts are bound to their context (e.g. the owning row id and
field name) via associated data, so a ciphertext copied to another row will not decrypt.

Format: "<key_version>:<base64url(nonce || ciphertext_with_tag)>"
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12


class CredentialCryptoError(Exception):
    """Raised for misconfiguration or failed decryption. Never includes key or plaintext material."""


@dataclass(frozen=True)
class CredentialCipher:
    keys: dict[str, bytes]
    active_version: str

    def __post_init__(self) -> None:
        if self.active_version not in self.keys:
            raise CredentialCryptoError(f"active key version {self.active_version!r} is not configured")
        for version, key in self.keys.items():
            if len(key) != 32:
                raise CredentialCryptoError(f"key {version!r} must be exactly 32 bytes")

    @classmethod
    def from_config(cls, raw_keys: str, active_version: str) -> CredentialCipher:
        keys: dict[str, bytes] = {}
        for item in filter(None, (part.strip() for part in raw_keys.split(","))):
            version, sep, encoded = item.partition(":")
            if not sep or not version or ":" in encoded:
                raise CredentialCryptoError("keys must be formatted as 'version:base64key'")
            try:
                keys[version] = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            except (binascii.Error, ValueError) as exc:
                raise CredentialCryptoError(f"key {version!r} is not valid base64") from exc
        if not keys:
            raise CredentialCryptoError("no credential encryption keys configured")
        return cls(keys=keys, active_version=active_version)

    def encrypt(self, plaintext: str, context: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(self.keys[self.active_version]).encrypt(
            nonce, plaintext.encode(), context.encode()
        )
        return f"{self.active_version}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"

    def decrypt(self, token: str, context: str) -> str:
        version, sep, encoded = token.partition(":")
        if not sep or version not in self.keys:
            raise CredentialCryptoError("ciphertext references an unknown key version")
        try:
            blob = base64.urlsafe_b64decode(encoded)
            nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
            return AESGCM(self.keys[version]).decrypt(nonce, sealed, context.encode()).decode()
        except (InvalidTag, binascii.Error, ValueError) as exc:
            raise CredentialCryptoError("credential decryption failed") from exc

    def needs_rotation(self, token: str) -> bool:
        return token.partition(":")[0] != self.active_version


def generate_key() -> str:
    """Return a new base64url-encoded 256-bit key suitable for CREDENTIAL_ENCRYPTION_KEYS."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()
