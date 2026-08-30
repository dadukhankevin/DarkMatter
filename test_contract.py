"""v3 contract: domain-separated envelopes, passport, relationship store."""

import json
import multiprocessing
from datetime import datetime, timedelta, timezone

import pytest

from darkmatter.contract.contact import create_contact_card, verify_contact_card
from darkmatter.contract.envelope import open_envelope, seal_envelope
from darkmatter.contract.forwarding import (
    create_forward_package,
    create_message_record,
    verify_forward_package,
)
from darkmatter.contract.tenure import create_passport_claim
from darkmatter.identity import generate_keypair
from darkmatter.security import DOMAIN_ENVELOPE, DOMAIN_MESSAGE, sign_message, verify_message
from darkmatter.store import LocalStore


@pytest.fixture
def keys():
    return generate_keypair(), generate_keypair()


def test_sign_message_is_domain_separated(keys):
    (priv, pub), _ = keys
    sig = sign_message(priv, "a", "m1", "ts", "hello")
    assert verify_message(pub, sig, "a", "m1", "ts", "hello")
    from darkmatter.security import verify_signed_payload
    assert verify_signed_payload(pub, sig, DOMAIN_MESSAGE, "a", "m1", "ts", "hello")
    assert not verify_signed_payload(pub, sig, DOMAIN_ENVELOPE, "a", "m1", "ts", "hello")


def test_envelope_roundtrip(keys):
    (a_priv, a_pub), (b_priv, b_pub) = keys
    env = seal_envelope(a_priv, a_pub, b_pub, "message", {"content": "hi"})
    public = env.to_public_dict()
    assert "content" not in json.dumps(public)
    opened = open_envelope(public, b_priv)
    assert opened.body["content"] == "hi"
    assert opened.from_id == a_pub
    assert opened.to_id == b_pub


def test_envelope_wrong_recipient_fails(keys):
    (a_priv, a_pub), (b_priv, b_pub) = keys
    c_priv, _ = generate_keypair()
    env = seal_envelope(a_priv, a_pub, b_pub, "introduce", {"remote": "/tmp/x"})
    with pytest.raises(ValueError):
        open_envelope(env.to_public_dict(), c_priv)


def test_envelope_rejects_mismatched_sender_and_bad_expiry(keys):
    (a_priv, a_pub), (_, b_pub) = keys
    with pytest.raises(ValueError, match="from_id"):
        seal_envelope(a_priv, b_pub, b_pub, "message", {"content": "no"})
    with pytest.raises(ValueError, match="expires_at"):
        seal_envelope(
            a_priv, a_pub, b_pub, "message", {"content": "no"},
            expires_at="sometime later",
        )


def test_malformed_public_envelope_fails_closed(keys):
    (_, _), (b_priv, _) = keys
    with pytest.raises(ValueError, match="Malformed envelope"):
        open_envelope({"type": "message"}, b_priv)


def test_envelope_tamper_fails(keys):
    (a_priv, a_pub), (b_priv, b_pub) = keys
    env = seal_envelope(a_priv, a_pub, b_pub, "message", {"content": "x"})
    data = env.to_public_dict()
    data["to"] = "00" * 32
    with pytest.raises(ValueError, match="signature"):
        open_envelope(data, b_priv)


def test_contact_card_roundtrip_and_tamper(keys):
    (private_key, agent_id), _ = keys
    card = create_contact_card(
        private_key,
        agent_id,
        "/tmp/mailbox.git",
        display_name="opal-fox",
        passport=create_passport_claim(
            private_key, agent_id, "2025-01-01T00:00:00+00:00",
        ),
    )
    verified = verify_contact_card(card)
    assert verified["agent_id"] == agent_id
    assert verified["passport"]["agent_id"] == agent_id
    card["locator"] = "/tmp/impostor.git"
    with pytest.raises(ValueError, match="signature"):
        verify_contact_card(card)


def test_forward_package_preserves_original_and_signed_hops():
    a_priv, a_id = generate_keypair()
    b_priv, b_id = generate_keypair()
    _, c_id = generate_keypair()
    envelope_id = "original-message"
    timestamp = datetime.now(timezone.utc).isoformat()
    record = create_message_record(
        a_priv, a_id, b_id, envelope_id, timestamp, "portable truth",
        metadata={"topic": "test"},
    )
    original = seal_envelope(
        a_priv,
        a_id,
        b_id,
        "message",
        {"content": "portable truth", "provenance": record},
        envelope_id=envelope_id,
        timestamp=timestamp,
    ).to_public_dict()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    package = create_forward_package(
        b_priv,
        b_id,
        c_id,
        original,
        record,
        note="C should see this",
        max_hops=2,
        expires_at=expires_at,
    )
    verified = verify_forward_package(
        package,
        envelope_from=b_id,
        envelope_to=c_id,
        envelope_expires_at=expires_at,
    )
    assert verified["original_envelope"] == original
    assert verified["message"]["content"] == "portable truth"
    assert verified["path"][0]["note"] == "C should see this"
    assert verified["path"][0]["hops_remaining"] == 1


def test_forward_package_rejects_tampered_original_content():
    a_priv, a_id = generate_keypair()
    b_priv, b_id = generate_keypair()
    _, c_id = generate_keypair()
    timestamp = datetime.now(timezone.utc).isoformat()
    record = create_message_record(
        a_priv, a_id, b_id, "m1", timestamp, "original",
    )
    original = seal_envelope(
        a_priv,
        a_id,
        b_id,
        "message",
        {"content": "original", "provenance": record},
        envelope_id="m1",
        timestamp=timestamp,
    ).to_public_dict()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    package = create_forward_package(
        b_priv, b_id, c_id, original, record, expires_at=expires_at,
    )
    package["message"]["content"] = "rewritten"
    with pytest.raises(ValueError, match="message record signature"):
        verify_forward_package(
            package,
            envelope_from=b_id,
            envelope_to=c_id,
            envelope_expires_at=expires_at,
        )


def test_passport_and_relationships(tmp_path):
    store = LocalStore(tmp_path)
    assert len(store.agent_id) == 64
    assert store.passport_path().name == "passport"
    assert store.passport_path().stat().st_mode & 0o777 == 0o600

    rel = store.upsert_relationship("peer1", peer_locator="/tmp/b.git")
    assert rel.state == "pending"
    store.upsert_relationship("peer1", state="active")
    assert store.get_relationship("peer1").state == "active"

    store.adjust_trust("peer1", 0.2)
    assert store.get_relationship("peer1").trust == 0.2
    store.record_settlement("peer1", trust_delta=0.1, tx_id="tx1")
    again = store.get_relationship("peer1")
    assert again.last_settlement["tx_id"] == "tx1"
    assert again.trust > 0.2

    store.append_inbox({"id": "m1", "type": "message", "from": "peer1", "content": "yo"})
    assert len(store.unconsumed_messages()) == 1
    got = store.consume_inbox()
    assert got[0]["content"] == "yo"
    assert store.unconsumed_messages() == []


def test_legacy_passport_key_still_loads(tmp_path):
    priv, pub = generate_keypair()
    dm = tmp_path / ".darkmatter"
    dm.mkdir()
    (dm / "passport.key").write_text(priv + "\n")
    store = LocalStore(tmp_path)
    assert store.agent_id == pub
    assert store.passport_path().stat().st_mode & 0o777 == 0o600


def _append_inbox_batch(root: str, prefix: str) -> None:
    store = LocalStore(root)
    for index in range(20):
        store.append_inbox({
            "id": f"{prefix}-{index}",
            "type": "message",
            "from": prefix,
            "content": "safe",
        })


def test_store_serializes_cross_process_writers(tmp_path):
    root = str(tmp_path / "shared")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_inbox_batch, args=(root, prefix))
        for prefix in ("a", "b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert len(LocalStore(root).load_inbox()) == 40
