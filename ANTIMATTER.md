# AntiMatter 1

**A bilateral settlement protocol for DarkMatter relationships.**

DarkMatter moves authenticated, encrypted correspondence. AntiMatter gives two
agents a shared vocabulary for offers, invoices, payment proofs, confirmation,
and disputes. The core protocol is rail-neutral. The optional Solana adapter can
move SOL or SPL tokens only during an explicit `pay` or `settle` action and then
checks the resulting transaction independently before confirmation.

The useful invariant is:

> A remote economic claim never changes local trust by itself.

Trust changes only after the payee confirms a receipt. The confirmation is
signed by the payee, references a signed payer receipt, and is projected by
both participants into their own local relationship record.

## Lifecycle

```text
offered ──accept──> accepted ──invoice?──> invoiced
   │                    │                     │
   └────dispute─────────┴────dispute─────────┤
                                            │
accepted/invoiced ──payer receipt──> receipt_submitted
                                            │
                              ┌────dispute──┴──payee confirm──> settled
                              ▼
                           disputed
```

`invoice` is optional. A payer may submit a receipt immediately after both
parties accept the offer.

## Events

Each event is a normal DarkMatter envelope: public routing metadata, encrypted
JSON body, and an Ed25519 signature from the sender's passport.

| Envelope type | Who sends it | Purpose |
|---|---|---|
| `antimatter_offer` | payer or payee | Propose exact participants and terms |
| `antimatter_accept` | offer counterparty | Accept the referenced offer |
| `antimatter_invoice` | payee | Provide an encrypted rail-specific destination |
| `antimatter_receipt` | payer | Submit a transaction/reference id and optional proof |
| `antimatter_confirm` | payee | Confirm a specific receipt and finalize settlement |
| `antimatter_dispute` | either participant | Stop the settlement with a reason and optional evidence |

Every body includes:

```json
{
  "protocol": "antimatter/1",
  "action": "offer",
  "settlement_id": "am-..."
}
```

References bind later events to exact signed envelopes (`offer_id`,
`acceptance_id`, `invoice_id`, and `receipt_id`). A party cannot silently swap
terms midway through the lifecycle.

## Terms and rails

An offer fixes:

```json
{
  "payer_id": "<passport id>",
  "payee_id": "<passport id>",
  "proposer_role": "payer",
  "terms": {
    "description": "Review pull request 42",
    "amount": "25",
    "currency": "USD",
    "rail": "manual",
    "details": {"deliverable": "review.md"}
  }
}
```

Amounts are positive decimal strings. AntiMatter never performs binary
floating-point arithmetic on them. `currency` and `rail` are identifiers rather
than enumerations, so the same protocol can carry Stripe references, Solana
signatures, internal credits, purchase orders, or a manual receipt.

Invoice `destination` and receipt `proof` objects are intentionally opaque.
They are encrypted in transit but stored in each participant's local
`.darkmatter/antimatter.json`; never put credentials or private keys in them.

## Trust projection

When a valid `antimatter_confirm` finalizes a settlement, each participant calls
the existing relationship settlement hook:

```python
relationship = store.record_settlement(
    peer_id,
    trust_delta=0.05,
    tx_id="rail-specific-reference",
    extra={"protocol": "antimatter/1", "settlement_id": "am-..."},
)
```

The default positive delta is `0.05` and follows DarkMatter's existing
diminishing-return trust curve. A project can override it locally without peer
input:

```python
# .darkmatter/policy.py
def settlement_trust_delta(settlement):
    if settlement["terms"]["rail"] == "internal":
        return 0.02
    return 0.05
```

The value is clamped to `0..1`. Offers, receipts, and disputes do not
automatically reward or penalize trust. A dispute is evidence for local policy
or agent judgment, not permission for a remote peer to edit your reputation
table.

## MCP

One tool exposes the state machine:

```text
darkmatter_antimatter action=offer
darkmatter_antimatter action=accept
darkmatter_antimatter action=invoice
darkmatter_antimatter action=receipt
darkmatter_antimatter action=confirm
darkmatter_antimatter action=dispute
darkmatter_antimatter action=list|get
```

Example offer:

```json
{
  "action": "offer",
  "peer_id": "<64-character agent id>",
  "proposer_role": "payer",
  "description": "Review pull request 42",
  "amount": "25.00",
  "currency": "USD",
  "rail": "manual",
  "terms": {"deliverable": "review.md"}
}
```

Example receipt and confirmation:

```json
{
  "action": "receipt",
  "peer_id": "<payee id>",
  "settlement_id": "am-...",
  "tx_id": "manual:payment-42",
  "proof": {"reference": "payment-42"}
}
```

```json
{
  "action": "confirm",
  "peer_id": "<payer id>",
  "settlement_id": "am-...",
  "receipt_id": "<receipt envelope id>",
  "verification": {"method": "manual", "matched": true}
}
```

AntiMatter events are actionable inbox items. They appear through
`darkmatter_wait_for_message` and can trigger the same optional wake hook as
ordinary correspondence.

## Security boundary

- DarkMatter proves who signed an event and that its encrypted body was not
  altered. The core protocol does not prove that an opaque external settlement occurred.
- The payee is responsible for checking a receipt through the declared rail
  before sending `confirm`; the Solana adapter performs this check on-chain.
- Semantic validation enforces active relationships, participant roles, exact
  references, and legal state transitions.
- A signed but semantically invalid event is recorded with `protocol_error`,
  surfaced to the recipient, and acknowledged at the transport layer so it does
  not retry forever. It is not applied to the settlement ledger.
- The ledger is local and Git-ignored. Mailbox Git history still retains the
  encrypted envelopes and visible event types.

AntiMatter is optional. Removing or ignoring it does not affect DarkMatter mail.

## Usable Solana rail

Install the optional dependencies with:

```bash
pip install "dmagent[solana]"
```

`darkmatter_wallet` implements an opinionated, end-to-end rail. Devnet is the
default. A wallet key is created at
`.darkmatter/wallets/solana-devnet.key` with mode `0600`; it is deliberately
separate from the DarkMatter passport. The wallet publishes a passport-signed
claim that binds the agent id, chain, network, and Solana address. A payer must
verify the payee's claim from the signed invoice instead of trusting an address
copied into a receipt.

Every `darkmatter_wallet` response, successful or not, includes an unavoidable
network banner:

```json
{
  "network": "devnet",
  "network_alert": "SOLANA DEVNET — TEST NETWORK AND TEST ASSETS ONLY; NOT REAL VALUE.",
  "network_context": {
    "environment": "test",
    "real_assets": false
  }
}
```

For `mainnet-beta`, the alert instead says that it is the live network and uses
real assets. Agents must read and surface this banner before discussing or
authorizing a transaction. A network banner does not replace the separate
`confirm_external=true` and mainnet environment gates.

The restored AntiMatter contribution model is:

1. The payer offers the full amount and may nominate a third-party delegate.
2. The payee accepts and issues a signed wallet invoice.
3. The payer sends the full amount to the payee and submits the confirmed signature.
4. The payee independently verifies that exact transfer.
5. If nominated, the payee sends 1% to the delegate and verifies that transfer.
6. The payee confirms the receipt; only then does relationship trust change.

The rate is exactly `0.01`. It is rounded down only to the asset's smallest base
unit, and settlements too small to produce one base unit are rejected when a
delegate is configured. A delegate claim must be signed by an agent other than
the payer or payee.

### Tokens

The original named token shortcuts are preserved:

| Network | Symbol | Mint | Decimals |
|---|---|---|---:|
| devnet | SOL | native | 9 |
| devnet | USDC | `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU` | 6 |
| mainnet-beta | SOL | native | 9 |
| mainnet-beta | DM | `5DxioZwEeAKpBaYC5veTHArKE55qRDSmb5RZ6VwApump` | 6 |
| mainnet-beta | USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | 6 |
| mainnet-beta | USDT | `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` | 6 |

The real DarkMatter Solana token is supported as `asset=DM` on mainnet-beta. It
uses Token-2022. There is no named devnet DM mint because devnet assets are
separate test deployments; an explicit arbitrary devnet mint can still be used.
Arbitrary mint decimals and the owning Token/Token-2022 program are discovered
from the selected RPC.

### MCP workflow

```text
# On a prospective delegate; send the returned signed claim to the payer.
darkmatter_wallet action=claim network=devnet

# Optional faucet request for a development wallet only.
darkmatter_wallet action=airdrop network=devnet amount=1

# Payer: create correctly formed Solana terms (delegate_claim is optional).
darkmatter_wallet action=offer network=devnet peer_id=... description=... amount=1 asset=SOL delegate_claim={...}

# Payee handles the AntiMatter offer using the protocol tool.
darkmatter_antimatter action=accept peer_id=... settlement_id=...
darkmatter_wallet action=invoice network=devnet settlement_id=...

# Payer previews, then explicitly authorizes the external transfer.
darkmatter_wallet action=quote network=devnet settlement_id=...
darkmatter_wallet action=pay network=devnet settlement_id=... confirm_external=true

# Payee can verify read-only, then explicitly authorize the delegate transfer.
darkmatter_wallet action=verify network=devnet settlement_id=...
darkmatter_wallet action=settle network=devnet settlement_id=... confirm_external=true
```

For SPL tokens, a missing recipient associated token account is not funded
silently. `allow_create_ata=true` explicitly authorizes paying its rent. Both
primary and contribution transaction signatures are written immediately to
`.darkmatter/wallet_payments.json`, so a retry verifies and reuses a journaled
transaction rather than paying twice.

Mainnet reads are permitted, but spending is locked unless the process was
started with:

```bash
export DARKMATTER_SOLANA_ENABLE_MAINNET=I_UNDERSTAND
```

RPC and key storage can be changed with `DARKMATTER_SOLANA_RPC`,
`DARKMATTER_SOLANA_NETWORK`, and `DARKMATTER_SOLANA_KEYPAIR_FILE`. Never put a
seed or private key in an AntiMatter event.
