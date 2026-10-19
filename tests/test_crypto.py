import json

import pytest
from app.infrastructure.crypto import (
    FERNET_KEY_INVALID,
    FERNET_KEY_REQUIRED,
    CryptoHandler,
    build_crypto_handler,
    parse_fernet_key,
)
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def test_parse_treats_blank_and_yaml_none_as_unset():
    assert parse_fernet_key(None) is None
    assert parse_fernet_key("") is None
    assert parse_fernet_key("  ") is None
    assert parse_fernet_key("None") is None
    assert parse_fernet_key("null") is None


def test_parse_rejects_a_malformed_key():
    with pytest.raises(RuntimeError, match=FERNET_KEY_INVALID):
        parse_fernet_key("not-a-key")


def test_fernet_handler_uses_the_supplied_key():
    key = Fernet.generate_key()
    handler = CryptoHandler(mode="fernet", fernet_key=key)
    token = handler.encrypt("tracker-fix")

    assert handler.fernet_key == key
    payload = json.loads(token)
    assert payload["algorithm"] == "FERNET"
    assert Fernet(key).decrypt(payload["token"].encode("ascii")) == b"tracker-fix"
    assert (
        CryptoHandler(mode="fernet", fernet_key=key.decode("ascii")).decrypt(token)
        == "tracker-fix"
    )


def test_fernet_handler_rejects_a_missing_key():
    with pytest.raises(RuntimeError, match=FERNET_KEY_REQUIRED):
        CryptoHandler(mode="fernet")
    with pytest.raises(RuntimeError, match=FERNET_KEY_REQUIRED):
        CryptoHandler(mode="fernet", fernet_key="None")
    with pytest.raises(RuntimeError, match=FERNET_KEY_REQUIRED):
        CryptoHandler(mode="fernet", fernet_key="  ")


def test_fernet_handler_rejects_an_invalid_key():
    with pytest.raises(RuntimeError, match=FERNET_KEY_INVALID):
        CryptoHandler(mode="fernet", fernet_key="not-a-key")


def test_build_crypto_handler_passes_the_fernet_key():
    key = Fernet.generate_key()
    handler = build_crypto_handler(
        encryption_required=True,
        mode="fernet",
        public_key_path=None,
        fernet_key=key,
    )
    assert handler is not None
    assert handler.fernet_key == key
    assert handler.decrypt(handler.encrypt("ok")) == "ok"


@pytest.mark.parametrize("mode", ["pem", "ssh"])
@pytest.mark.parametrize("fernet_key", [None, "not-a-key"])
def test_pem_and_ssh_handlers_ignore_the_fernet_key(mode, fernet_key):
    with pytest.raises(RuntimeError, match=f"{mode} mode requires ENCRYPTION_CERT"):
        CryptoHandler(mode=mode, fernet_key=fernet_key)


def test_fernet_key_for_mode_skips_other_modes():
    from app.infrastructure.crypto import fernet_key_for_mode

    assert fernet_key_for_mode("pem", "not-a-key") is None
    assert fernet_key_for_mode("ssh", None) is None
    with pytest.raises(RuntimeError, match="fernet mode requires FERNET_KEY"):
        fernet_key_for_mode("fernet", None)
    key = Fernet.generate_key()
    assert fernet_key_for_mode("fernet", key) == key


def _key_files(tmp_path, mode):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if mode == "ssh":
        public = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    else:
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    public_path = tmp_path / "public"
    private_path = tmp_path / "private"
    public_path.write_bytes(public)
    private_path.write_bytes(private)
    return public_path, private_path


@pytest.mark.parametrize("mode", ["pem", "ssh"])
def test_pem_and_ssh_round_trip_without_using_a_fernet_key(tmp_path, mode):
    public_path, private_path = _key_files(tmp_path, mode)
    handler = build_crypto_handler(
        encryption_required=True,
        mode=mode,
        public_key_path=str(public_path),
        fernet_key="not-a-key",
    )
    opener = CryptoHandler(
        mode=mode,
        public_key_path=str(public_path),
        private_key_path=str(private_path),
    )
    assert handler is not None
    assert opener.decrypt(handler.encrypt("tracker-fix")) == "tracker-fix"


def test_build_crypto_handler_skips_crypto_when_encryption_is_off():
    assert (
        build_crypto_handler(
            encryption_required=False,
            mode="fernet",
            public_key_path="/data/certs/client/tls.crt",
            fernet_key=None,
        )
        is None
    )
