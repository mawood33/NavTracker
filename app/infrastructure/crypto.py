import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoHandler:
    def __init__(
        self,
        public_key_path: str | None = None,
        private_key_path: str | None = None,
    ):
        self.public_key = None
        self.private_key = None

        if public_key_path:
            self.public_key = serialization.load_pem_public_key(
                Path(public_key_path).read_bytes()
            )

        if private_key_path:
            self.private_key = serialization.load_pem_private_key(
                Path(private_key_path).read_bytes(),
                password=None,
            )

    def encrypt(self, message: str) -> str:
        if self.public_key is None:
            raise ValueError("Public key is not loaded")

        aes_key = AESGCM.generate_key(bit_length=256)
        nonce = AESGCM.generate_key(bit_length=96)
        ciphertext = AESGCM(aes_key).encrypt(
            nonce,
            message.encode("utf-8"),
            None,
        )

        encrypted_key = self.public_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        return json.dumps(
            {
                "algorithm": "AES-256-GCM+RSA-OAEP-SHA256",
                "ciphertext": base64.b64encode(ciphertext).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "encrypted_key": base64.b64encode(encrypted_key).decode(),
            },
            separators=(",", ":"),
        )

    def decrypt(self, encrypted_message: str) -> str:
        if self.private_key is None:
            raise ValueError("Private key is not loaded")

        payload = json.loads(encrypted_message)

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
