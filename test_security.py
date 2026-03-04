"""
Comprehensive tests for darkmatter/security.py — signing, verification,
E2E encryption, challenge-response, and URL security assessment.
"""

import time
import pytest
from unittest.mock import MagicMock
from darkmatter.identity import generate_keypair
from darkmatter.security import (
    # Domain constants
    DOMAIN_MESSAGE,
    DOMAIN_PEER_UPDATE,
    DOMAIN_RELAY_POLL,
    DOMAIN_SDP,
    DOMAIN_SHARD,
    DOMAIN_LAN_BEACON,
    DOMAIN_CHALLENGE_RESPONSE,
    # Core signing
    sign_payload,
    verify_signed_payload,
    # Backward-compatible signing
    sign_message,
    verify_message,
    sign_peer_update,
    verify_peer_update_signature,
    sign_relay_poll,
    # Inbound verification
    verify_inbound,
    VerifiedPayload,
    # Outbound preparation
    prepare_outbound,
    # E2E encryption
    encrypt_for_peer,
    decrypt_from_peer,
    # Challenge-response
    create_challenge,
    prove_identity,
    verify_proof,
    _pending_challenges,
    CHALLENGE_TTL,
    # Shard/SDP/LAN helpers
    sign_shard,
    verify_shard_signature,
    sign_sdp,
    verify_sdp_signature,
    sign_lan_beacon,
    verify_lan_beacon,
    # URL assessment
    assess_url_security,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def keypair():
    """Generate a fresh Ed25519 keypair for testing."""
    return generate_keypair()


@pytest.fixture
def keypair2():
    """Generate a second keypair for two-party tests."""
    return generate_keypair()


# =============================================================================
# sign_payload / verify_signed_payload
# =============================================================================

class TestSignPayload:
    def test_roundtrip(self, keypair):
        priv, pub = keypair
        sig = sign_payload(priv, "test.domain", "field1", "field2")
        assert verify_signed_payload(pub, sig, "test.domain", "field1", "field2")

    def test_multiple_domains(self, keypair):
        priv, pub = keypair
        for domain in [DOMAIN_MESSAGE, DOMAIN_PEER_UPDATE, DOMAIN_SDP,
                       DOMAIN_SHARD, DOMAIN_LAN_BEACON]:
            sig = sign_payload(priv, domain, "a", "b")
            assert verify_signed_payload(pub, sig, domain, "a", "b")

    def test_domain_separation_prevents_replay(self, keypair):
        """A signature for domain A must NOT verify under domain B."""
        priv, pub = keypair
        sig = sign_payload(priv, DOMAIN_MESSAGE, "agent1", "msg1", "ts", "content")
        # Same fields, different domain — must fail
        assert not verify_signed_payload(pub, sig, DOMAIN_PEER_UPDATE, "agent1", "msg1", "ts", "content")

    def test_wrong_key_fails(self, keypair, keypair2):
        priv, pub = keypair
        _, pub2 = keypair2
        sig = sign_payload(priv, "test", "data")
        assert not verify_signed_payload(pub2, sig, "test", "data")

    def test_tampered_field_fails(self, keypair):
        priv, pub = keypair
        sig = sign_payload(priv, "test", "original")
        assert not verify_signed_payload(pub, sig, "test", "tampered")

    def test_empty_fields(self, keypair):
        priv, pub = keypair
        sig = sign_payload(priv, "test")
        assert verify_signed_payload(pub, sig, "test")


# =============================================================================
# Backward-compatible message signing
# =============================================================================

class TestMessageSigning:
    def test_roundtrip(self, keypair):
        priv, pub = keypair
        sig = sign_message(priv, "agent1", "msg1", "2024-01-01T00:00:00Z", "hello")
        assert verify_message(pub, sig, "agent1", "msg1", "2024-01-01T00:00:00Z", "hello")

    def test_wrong_content_fails(self, keypair):
        priv, pub = keypair
        sig = sign_message(priv, "agent1", "msg1", "ts", "hello")
        assert not verify_message(pub, sig, "agent1", "msg1", "ts", "goodbye")


class TestPeerUpdateSigning:
    def test_roundtrip(self, keypair):
        priv, pub = keypair
        sig = sign_peer_update(priv, "agent1", "https://example.com", "2024-01-01T00:00:00Z")
        assert verify_peer_update_signature(pub, sig, "agent1", "https://example.com", "2024-01-01T00:00:00Z")

    def test_wrong_url_fails(self, keypair):
        priv, pub = keypair
        sig = sign_peer_update(priv, "agent1", "https://a.com", "ts")
        assert not verify_peer_update_signature(pub, sig, "agent1", "https://b.com", "ts")


# =============================================================================
# E2E Encryption
# =============================================================================

class TestE2EEncryption:
    def test_roundtrip(self, keypair, keypair2):
        priv_a, pub_a = keypair
        priv_b, pub_b = keypair2

        plaintext = b"Hello, this is a secret message!"
        encrypted = encrypt_for_peer(plaintext, priv_a, pub_b)

        assert "nonce" in encrypted
        assert "ciphertext" in encrypted
        assert "sender_public_key_hex" in encrypted

        decrypted = decrypt_from_peer(encrypted, priv_b, pub_a)
        assert decrypted == plaintext

    def test_wrong_key_decryption_fails(self, keypair, keypair2):
        priv_a, pub_a = keypair
        _, pub_b = keypair2

        plaintext = b"secret"
        encrypted = encrypt_for_peer(plaintext, priv_a, pub_b)

        # Try to decrypt with wrong key
        wrong_priv, _ = generate_keypair()
        with pytest.raises(ValueError, match="Decryption failed"):
            decrypt_from_peer(encrypted, wrong_priv, pub_a)

    def test_large_payload(self, keypair, keypair2):
        priv_a, pub_a = keypair
        priv_b, pub_b = keypair2

        plaintext = b"x" * 65536
        encrypted = encrypt_for_peer(plaintext, priv_a, pub_b)
        decrypted = decrypt_from_peer(encrypted, priv_b, pub_a)
        assert decrypted == plaintext

    def test_bidirectional(self, keypair, keypair2):
        """Both parties can encrypt for each other."""
        priv_a, pub_a = keypair
        priv_b, pub_b = keypair2

        # A → B
        msg1 = b"from A to B"
        enc1 = encrypt_for_peer(msg1, priv_a, pub_b)
        assert decrypt_from_peer(enc1, priv_b, pub_a) == msg1

        # B → A
        msg2 = b"from B to A"
        enc2 = encrypt_for_peer(msg2, priv_b, pub_a)
        assert decrypt_from_peer(enc2, priv_a, pub_b) == msg2


# =============================================================================
# Challenge-Response
# =============================================================================

class TestChallengeResponse:
    def test_full_flow(self, keypair):
        priv, pub = keypair
        agent_id = "test-agent-123"

        challenge_id, challenge_hex = create_challenge(agent_id)
        assert challenge_id.startswith("ch-")
        assert len(challenge_hex) == 64  # 32 bytes hex

        proof_hex = prove_identity(challenge_hex, priv)
        assert verify_proof(agent_id, pub, challenge_id, proof_hex, challenge_hex)

    def test_challenge_consumed_after_verify(self, keypair):
        priv, pub = keypair
        agent_id = "test-agent"

        challenge_id, challenge_hex = create_challenge(agent_id)
        proof_hex = prove_identity(challenge_hex, priv)

        # First verify succeeds
        assert verify_proof(agent_id, pub, challenge_id, proof_hex, challenge_hex)
        # Second verify fails (challenge consumed)
        assert not verify_proof(agent_id, pub, challenge_id, proof_hex)

    def test_wrong_agent_fails(self, keypair):
        priv, pub = keypair

        challenge_id, challenge_hex = create_challenge("agent-a")
        proof_hex = prove_identity(challenge_hex, priv)

        # Wrong agent_id
        assert not verify_proof("agent-b", pub, challenge_id, proof_hex)

    def test_wrong_key_fails(self, keypair, keypair2):
        priv_a, pub_a = keypair
        _, pub_b = keypair2
        agent_id = "test"

        challenge_id, challenge_hex = create_challenge(agent_id)
        proof_hex = prove_identity(challenge_hex, priv_a)

        # Verify with wrong public key
        assert not verify_proof(agent_id, pub_b, challenge_id, proof_hex, challenge_hex)

    def test_expired_challenge(self, keypair):
        priv, pub = keypair
        agent_id = "test"

        challenge_id, challenge_hex = create_challenge(agent_id)

        # Manually expire the challenge
        stored = _pending_challenges[challenge_id]
        _pending_challenges[challenge_id] = (stored[0], stored[1], time.time() - CHALLENGE_TTL - 1)

        proof_hex = prove_identity(challenge_hex, priv)
        assert not verify_proof(agent_id, pub, challenge_id, proof_hex)

    def test_lookup_from_store(self, keypair):
        """verify_proof can look up challenge_hex from the store."""
        priv, pub = keypair
        agent_id = "store-test"

        challenge_id, challenge_hex = create_challenge(agent_id)
        proof_hex = prove_identity(challenge_hex, priv)

        # Don't pass challenge_hex — should look it up
        assert verify_proof(agent_id, pub, challenge_id, proof_hex)


# =============================================================================
# verify_inbound
# =============================================================================

class TestVerifyInbound:
    def _make_connection(self, agent_id, pub_key=None):
        conn = MagicMock()
        conn.agent_id = agent_id
        conn.agent_public_key_hex = pub_key
        return conn

    def test_connected_with_stored_key_valid(self, keypair):
        priv, pub = keypair
        agent_id = "sender1"

        sig = sign_message(priv, agent_id, "msg1", "ts1", "hello")
        data = {
            "from_agent_id": agent_id,
            "message_id": "msg1",
            "content": "hello",
            "timestamp": "ts1",
            "from_public_key_hex": pub,
            "signature_hex": sig,
        }
        connections = {agent_id: self._make_connection(agent_id, pub)}
        result = verify_inbound(data, connections)
        assert result.verified
        assert result.error is None

    def test_connected_key_mismatch(self, keypair, keypair2):
        priv, pub = keypair
        _, pub2 = keypair2
        agent_id = "sender"

        sig = sign_message(priv, agent_id, "msg1", "ts", "hi")
        data = {
            "from_agent_id": agent_id,
            "message_id": "msg1",
            "content": "hi",
            "timestamp": "ts",
            "from_public_key_hex": pub2,  # Different from stored
            "signature_hex": sig,
        }
        connections = {agent_id: self._make_connection(agent_id, pub)}
        result = verify_inbound(data, connections)
        assert not result.verified
        assert result.status_code == 403
        assert "mismatch" in result.error.lower()

    def test_connected_missing_signature_rejected(self, keypair):
        _, pub = keypair
        agent_id = "sender"
        data = {
            "from_agent_id": agent_id,
            "message_id": "msg1",
            "content": "hi",
            "timestamp": "",
            "signature_hex": None,
        }
        connections = {agent_id: self._make_connection(agent_id, pub)}
        result = verify_inbound(data, connections)
        assert not result.verified
        assert result.status_code == 403

    def test_connected_no_key_pins_new_key(self, keypair):
        priv, pub = keypair
        agent_id = "sender"

        sig = sign_message(priv, agent_id, "msg1", "ts", "hello")
        data = {
            "from_agent_id": agent_id,
            "message_id": "msg1",
            "content": "hello",
            "timestamp": "ts",
            "from_public_key_hex": pub,
            "signature_hex": sig,
        }
        conn = self._make_connection(agent_id, None)
        connections = {agent_id: conn}
        result = verify_inbound(data, connections)
        assert result.verified
        assert result.pinned_key
        assert conn.agent_public_key_hex == pub

    def test_not_connected_with_valid_signature(self, keypair):
        priv, pub = keypair
        agent_id = "stranger"

        sig = sign_message(priv, agent_id, "msg1", "ts", "hello")
        data = {
            "from_agent_id": agent_id,
            "message_id": "msg1",
            "content": "hello",
            "timestamp": "ts",
            "from_public_key_hex": pub,
            "signature_hex": sig,
        }
        result = verify_inbound(data, {})
        assert result.verified

    def test_not_connected_no_signature_rejected(self):
        data = {
            "from_agent_id": "unknown",
            "message_id": "msg1",
            "content": "hello",
            "timestamp": "",
        }
        result = verify_inbound(data, {})
        assert not result.verified
        assert result.status_code == 403

    def test_connected_no_key_no_sender_key_rejected(self):
        """Connected but neither side has a key — rejected (no backward compat)."""
        agent_id = "sender"
        data = {
            "from_agent_id": agent_id,
            "message_id": "msg1",
            "content": "hello",
            "timestamp": "ts",
        }
        conn = MagicMock()
        conn.agent_id = agent_id
        conn.agent_public_key_hex = None
        connections = {agent_id: conn}
        result = verify_inbound(data, connections)
        assert not result.verified
        assert result.status_code == 403


# =============================================================================
# Shard / SDP / LAN Beacon Signing
# =============================================================================

class TestShardSigning:
    def test_roundtrip(self, keypair):
        priv, pub = keypair
        sig = sign_shard(priv, "shard-1", "author-1", "content", "tag1,tag2")
        assert verify_shard_signature(pub, sig, "shard-1", "author-1", "content", "tag1,tag2")

    def test_tampered_content_fails(self, keypair):
        priv, pub = keypair
        sig = sign_shard(priv, "shard-1", "author-1", "original", "tag1")
        assert not verify_shard_signature(pub, sig, "shard-1", "author-1", "tampered", "tag1")


class TestSdpSigning:
    def test_roundtrip(self, keypair):
        priv, pub = keypair
        sig = sign_sdp(priv, "agent-1", "v=0\r\no=- 123 IN IP4 ...")
        assert verify_sdp_signature(pub, sig, "agent-1", "v=0\r\no=- 123 IN IP4 ...")

    def test_cross_domain_fails(self, keypair):
        priv, pub = keypair
        sig = sign_sdp(priv, "agent-1", "sdp-data")
        # Must not verify under different domain
        assert not verify_signed_payload(pub, sig, DOMAIN_LAN_BEACON, "agent-1", "sdp-data")


class TestLanBeaconSigning:
    def test_roundtrip(self, keypair):
        priv, pub = keypair
        sig = sign_lan_beacon(priv, "agent-1", "8100", "1704067200")
        assert verify_lan_beacon(pub, sig, "agent-1", "8100", "1704067200")

    def test_wrong_port_fails(self, keypair):
        priv, pub = keypair
        sig = sign_lan_beacon(priv, "agent-1", "8100", "ts")
        assert not verify_lan_beacon(pub, sig, "agent-1", "9999", "ts")


# =============================================================================
# assess_url_security
# =============================================================================

class TestAssessUrlSecurity:
    def test_https_secure(self):
        result = assess_url_security("https://example.com")
        assert result["secure"] is True
        assert result["warning"] is None

    def test_http_remote_insecure(self):
        result = assess_url_security("http://example.com")
        assert result["secure"] is False
        assert "insecure" in result["warning"].lower()
        assert result["is_local"] is False

    def test_http_localhost_acceptable(self):
        result = assess_url_security("http://127.0.0.1:8100")
        assert result["secure"] is False
        assert result["is_local"] is True
        assert "local" in result["warning"].lower()

    def test_invalid_url(self):
        result = assess_url_security("not-a-url")
        assert result["secure"] is False


# =============================================================================
# prepare_outbound
# =============================================================================

class TestPrepareOutbound:
    def test_signs_payload(self, keypair):
        priv, pub = keypair
        payload = {
            "message_id": "msg-1",
            "content": "hello",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        envelope = prepare_outbound(payload, priv, "agent1", pub)
        assert not envelope.encrypted
        assert envelope.payload["from_agent_id"] == "agent1"
        assert envelope.payload["from_public_key_hex"] == pub
        assert envelope.payload["signature_hex"] is not None

        # Verify the signature
        assert verify_message(
            pub, envelope.payload["signature_hex"],
            "agent1", "msg-1", "2024-01-01T00:00:00Z", "hello"
        )

    def test_encrypts_when_recipient_key_provided(self, keypair, keypair2):
        priv_a, pub_a = keypair
        priv_b, pub_b = keypair2

        payload = {
            "message_id": "msg-1",
            "content": "secret",
            "timestamp": "ts",
        }
        envelope = prepare_outbound(payload, priv_a, "agent-a", pub_a, recipient_public_key_hex=pub_b)
        assert envelope.encrypted
        assert envelope.payload["e2e_encrypted"] is True
        assert "encrypted_payload" in envelope.payload
