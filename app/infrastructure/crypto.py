import base64
import json
import os
import logging
from pathlib import Path

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

FERNET_KEY_REQUIRED = (
    "Fernet mode requires FERNET_KEY. "
    "Set it to a url-safe base64-encoded 32-byte key. "
    "A blank value or the YAML literal None is unset, not a key."
)
FERNET_KEY_INVALID = (
    "Invalid FERNET_KEY. Expected a url-safe base64-encoded 32-byte key."
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
        self.public_key = None
        self.private_key = None
        self.fernet = None
        self.fernet_key = None

        logger.info(f'Crypto mode: {mode}')
        if mode == "fernet":
            if public_key_path or private_key_path:
                raise ValueError("Fernet mode does not use public/private key paths")

            self.fernet_key = parse_fernet_key(fernet_key)
            if self.fernet_key is None:
                raise RuntimeError(FERNET_KEY_REQUIRED)
            self.fernet = Fernet(self.fernet_key)
            return

        if public_key_path:
            public_bytes = Path(public_key_path).read_bytes()

            if mode == "ssh":
                self.public_key = serialization.load_ssh_public_key(public_bytes)
            elif b"-----BEGIN CERTIFICATE-----" in public_bytes:
                self.public_key = x509.load_pem_x509_certificate(
                    public_bytes
                ).public_key()
            else:
                self.public_key = serialization.load_pem_public_key(public_bytes)

            if not isinstance(self.public_key, rsa.RSAPublicKey):
                raise TypeError("RSA public keys are required for this encryption method")

        if private_key_path:
            private_bytes = Path(private_key_path).read_bytes()

            if mode == "ssh":
                self.private_key = serialization.load_ssh_private_key(
                    private_bytes,
                    password=private_key_password,
                )
            else:
                self.private_key = serialization.load_pem_private_key(
                    private_bytes,
                    password=private_key_password,
                )

            if not isinstance(self.private_key, rsa.RSAPrivateKey):
                raise TypeError("RSA private keys are required for this encryption method")

    def encrypt(self, message: str) -> str:
        if self.mode == "fernet":
            token = self.fernet.encrypt(message.encode("utf-8"))
            return json.dumps({
                "algorithm": "FERNET",
                "token": token.decode("ascii"),
            })

        if self.public_key is None:
            raise ValueError("Public key is not loaded")

        aes_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)  # AES-GCM uses a 12-byte nonce
        ciphertext = AESGCM(aes_key).encrypt(
            nonce, message.encode("utf-8"), None
        )

        encrypted_key = self.public_key.encrypt(
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
            if self.fernet is None:
                raise ValueError("A Fernet key is not loaded")
            return self.fernet.decrypt(
                payload["token"].encode("ascii")
            ).decode("utf-8")

        if self.private_key is None:
            raise ValueError("Private key is not loaded")

        aes_key = self.private_key.decrypt(
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
