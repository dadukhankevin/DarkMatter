"""v3 security primitives — domain-separated signatures and sealed encryption."""

import pytest

from darkmatter.identity import generate_keypair
from darkmatter.security import (
    DOMAIN_ENVELOPE,
    DOMAIN_MESSAGE,
    decrypt_from_peer,
    encrypt_for_peer,
    sign_message,
    sign_payload,
    verify_message,
    verify_signed_payload,
)


@pytest.fixture
def keypair():
    return generate_keypair()


@pytest.fixture
def keypair2():
    return generate_keypair()


def test_sign_payload_roundtrip(keypair):
    priv, pub = keypair
    sig = sign_payload(priv, DOMAIN_ENVELOPE, "a", "b")
    assert verify_signed_payload(pub, sig, DOMAIN_ENVELOPE, "a", "b")
    assert not verify_signed_payload(pub, sig, DOMAIN_MESSAGE, "a", "b")


def test_sign_message_domain_separated(keypair):
    priv, pub = keypair
    sig = sign_message(priv, "from", "id", "ts", "hi")
    assert verify_message(pub, sig, "from", "id", "ts", "hi")
    assert not verify_message(pub, sig, "from", "id", "ts", "no")
    assert verify_signed_payload(pub, sig, DOMAIN_MESSAGE, "from", "id", "ts", "hi")


def test_encrypt_roundtrip(keypair, keypair2):
    a_priv, a_pub = keypair
    b_priv, b_pub = keypair2
    blob = encrypt_for_peer(b"secret", a_priv, b_pub)
    assert decrypt_from_peer(blob, b_priv, a_pub) == b"secret"
    with pytest.raises(ValueError):
        decrypt_from_peer(blob, a_priv, a_pub)


def test_encrypt_wrong_hkdf_info_fails(keypair, keypair2):
    a_priv, a_pub = keypair
    b_priv, b_pub = keypair2
    blob = encrypt_for_peer(b"secret", a_priv, b_pub, info=b"other")
    with pytest.raises(ValueError):
        decrypt_from_peer(blob, b_priv, a_pub)
