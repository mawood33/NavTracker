import base64
import json
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


def _read_public_material(mode: str, path: str) -> bytes:
    file = Path(path)
    if not file.is_file():
        raise RuntimeError(
            f"{mode} mode requires ENCRYPTION_CERT. No file at {path}."
        )
    try:
        return file.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"{mode} mode requires ENCRYPTION_CERT. Could not read {path}."
        ) from exc


FERNET_KEY_REQUIRED = (
    "fernet mode requires FERNET_KEY. "
    "Set it to a url-safe base64-encoded 32-byte key. "
    "A blank value or the YAML literal None is unset, not a key."
)
FERNET_KEY_INVALID = (
    "fernet mode requires a valid FERNET_KEY. "
    "Expected a url-safe base64-encoded 32-byte key."
)

_UNSET_FERNET_VALUES = {"", "none", "null"}


def parse_fernet_key(value: bytes | str | None) -> bytes | None:
    """Return a Fernet key, or None when the setting is unset.

    PyYAML loads `fernet_key: None` as the string "None", not as null.
    That placeholder is unset. A present value must already be a Fernet key.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError(FERNET_KEY_INVALID) from exc
    else:
        text = value
    text = text.strip()
    if text.lower() in _UNSET_FERNET_VALUES:
        return None
    key = text.encode("ascii")
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(FERNET_KEY_INVALID) from exc
    return key


def fernet_key_for_mode(mode: str, value: bytes | str | None) -> bytes | None:
    """Return a Fernet key when that mode is selected.

    pem and ssh do not read this value. Callers must not pass it through
    a parser first: an unused FERNET_KEY is not validated.
    """
    if mode != "fernet":
        return None
    key = parse_fernet_key(value)
    if key is None:
        raise RuntimeError(FERNET_KEY_REQUIRED)
    return key


def require_public_material(mode: str, public_key_path: str | None) -> str | None:
    """pem and ssh encrypt with ENCRYPTION_CERT. Fernet does not."""
    if mode == "fernet":
        return None
    path = (public_key_path or "").strip()
    if not path:
        raise RuntimeError(f"{mode} mode requires ENCRYPTION_CERT.")
    return path


def build_crypto_handler(
    *,
    encryption_required: bool,
    mode: str,
    public_key_path: str | None,
    fernet_key: bytes | str | None,
) -> "CryptoHandler | None":
    if not encryption_required:
        return None
    if mode == "ssh":
        return CryptoHandler(
            mode="ssh",
            public_key_path=public_key_path,
            private_key_path=None,
        )
    if mode == "fernet":
        return CryptoHandler(mode="fernet", fernet_key=fernet_key)
    return CryptoHandler(
        mode="pem",
        public_key_path=public_key_path,
        private_key_path=None,
    )


class CryptoHandler:
    def __init__(
        self,
        public_key_path: str | None = None,
        private_key_path: str | None = None,
        *,
        mode: str = "pem",  # "pem", "ssh", or "fernet"
        fernet_key: bytes | str | None = None,
        private_key_password: bytes | None = None,
    ):
        if mode not in {"pem", "ssh", "fernet"}:
            raise ValueError("mode must be 'pem', 'ssh', or 'fernet'")

        self.mode = mode
        self.public_key: rsa.RSAPublicKey | None = None
        self.private_key: rsa.RSAPrivateKey | None = None
        self.fernet: Fernet | None = None
        self.fernet_key: bytes | None = None

        logger.info(f'Crypto mode: {mode}')
        if mode == "fernet":
            if public_key_path or private_key_path:
                raise ValueError("Fernet mode does not use public/private key paths")

            key = fernet_key_for_mode(mode, fernet_key)
            if key is None:
                raise RuntimeError(FERNET_KEY_REQUIRED)
            self.fernet_key = key
            self.fernet = Fernet(key)
            return

        checked_path = require_public_material(mode, public_key_path)
        if checked_path is None:
            raise RuntimeError(f"{mode} mode requires ENCRYPTION_CERT.")
        public_bytes = _read_public_material(mode, checked_path)
        loaded_public: object
        try:
            if mode == "ssh":
                loaded_public = serialization.load_ssh_public_key(public_bytes)
            elif b"-----BEGIN CERTIFICATE-----" in public_bytes:
                loaded_public = x509.load_pem_x509_certificate(
                    public_bytes
                ).public_key()
            else:
                loaded_public = serialization.load_pem_public_key(public_bytes)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"{mode} mode requires a valid ENCRYPTION_CERT."
            ) from exc

        if not isinstance(loaded_public, rsa.RSAPublicKey):
            raise RuntimeError(
                f"{mode} mode requires a valid ENCRYPTION_CERT."
            )
        self.public_key = loaded_public

        if private_key_path:
            private_bytes = Path(private_key_path).read_bytes()
            loaded_private: object
            if mode == "ssh":
                loaded_private = serialization.load_ssh_private_key(
                    private_bytes,
                    password=private_key_password,
                )
            else:
                loaded_private = serialization.load_pem_private_key(
                    private_bytes,
                    password=private_key_password,
                )

            if not isinstance(loaded_private, rsa.RSAPrivateKey):
                raise TypeError("RSA private keys are required for this encryption method")
            self.private_key = loaded_private

    def encrypt(self, message: str) -> str:
        if self.mode == "fernet":
            fernet = self.fernet
            if fernet is None:
                raise ValueError("A Fernet key is not loaded")
            token = fernet.encrypt(message.encode("utf-8"))
            return json.dumps({
                "algorithm": "FERNET",
                "token": token.decode("ascii"),
            })

        public_key = self.public_key
        if public_key is None:
            raise ValueError("Public key is not loaded")

        aes_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)  # AES-GCM uses a 12-byte nonce
        ciphertext = AESGCM(aes_key).encrypt(
            nonce, message.encode("utf-8"), None
        )

        encrypted_key = public_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        return json.dumps({
            "algorithm": "AES-256-GCM+RSA-OAEP-SHA256",
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "encrypted_key": base64.b64encode(encrypted_key).decode("ascii"),
        }, separators=(",", ":"))

    def decrypt(self, encrypted_message: str) -> str:
        payload = json.loads(encrypted_message)

        if payload.get("algorithm") == "FERNET":
            fernet = self.fernet
            if fernet is None:
                raise ValueError("A Fernet key is not loaded")
            token = payload.get("token")
            if not isinstance(token, str):
                raise ValueError("Fernet payload is missing a token")
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")

        private_key = self.private_key
        if private_key is None:
            raise ValueError("Private key is not loaded")

        aes_key = private_key.decrypt(
            base64.b64decode(payload["encrypted_key"]),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        plaintext = AESGCM(aes_key).decrypt(
            base64.b64decode(payload["nonce"]),
            base64.b64decode(payload["ciphertext"]),
            None,
        )
        return plaintext.decode("utf-8")
