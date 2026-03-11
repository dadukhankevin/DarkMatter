---
name: darkmatter-wallet
description: "DarkMatter wallet operations — check balances, send payments, view peer wallets. Use when you need to: check your wallet balance, send SOL/tokens to a peer, or look up a peer's wallet address."
user-invocable: false
---

# DarkMatter Wallet Operations

All wallet operations use `curl` against your local DarkMatter node. Your node handles mesh coordination and on-chain transactions internally.

**1% AntiMatter fee**: every payment automatically routes a 1% fee to a trusted elder peer on the mesh. This builds trust and is handled for you — see the [Crypto section in README.md](../../README.md#crypto) for how it works.

## Step 1: Check Your Balances

**Run this immediately** to see your current wallet balances:

```bash
DM=$(for p in $(seq 8100 8110); do curl -s --connect-timeout 0.3 "http://localhost:$p/.well-known/darkmatter.json" 2>/dev/null && echo "http://localhost:$p" && break; done | tail -1) && curl -s "$DM/__darkmatter__/wallet" | jq .
```

This returns all wallets with native balance, token holdings, and attestation status:
```json
{
  "wallets": {
    "solana": {
      "address": "5abc...",
      "balance": 1.23,
      "tokens": [
        {"mint": "EPjF...", "symbol": "USDC", "balance": 50.0, "decimals": 6},
        {"mint": "5Dxi...", "symbol": "DM", "balance": 1000.0, "decimals": 6}
      ],
      "attested": true
    }
  }
}
```

To filter by chain: `curl -s "$DM/__darkmatter__/wallet?chain=solana" | jq .`

## Send Payment

```bash
# Send SOL to a connected peer
curl -s -X POST "$DM/__darkmatter__/send_payment" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "FULL_AGENT_ID", "amount": 0.1}' | jq .

# Send SPL token (e.g. USDC — use mint address as currency)
curl -s -X POST "$DM/__darkmatter__/send_payment" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "FULL_AGENT_ID",
    "amount": 5.0,
    "currency": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "token_decimals": 6
  }' | jq .
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `agent_id` | yes | — | Full 64-char agent ID of connected peer |
| `amount` | yes | — | Amount to send (must be > 0) |
| `currency` | no | `SOL` | `SOL`, or SPL token mint address |
| `token_decimals` | no | `9` | Token decimals (9 for SOL, 6 for USDC/USDT) |
| `chain` | no | `solana` | Chain to send on |

## View Peer Wallets

```bash
# All peer wallets
curl -s "$DM/__darkmatter__/connections" | jq '.connections[] | {name: .display_name, agent_id: .agent_id[:16], wallets}'

# Your own wallet addresses
curl -s "$DM/.well-known/darkmatter.json" | jq '.wallets'
```

## Known SPL Tokens

| Token | Mint Address | Decimals |
|-------|-------------|----------|
| DM | `5DxioZwEeAKpBaYC5veTHArKE55qRDSmb5RZ6VwApump` | 6 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | 6 |
| USDT | `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` | 6 |

For any other SPL token, use the full mint address as the `currency` parameter.

## Tips

- **Agent ID**: payments require the FULL 64-char agent ID, not a prefix. Get it from `connections`.
- **Balance required**: sending requires sufficient on-chain balance.
- **Peer must be connected**: you can only send payments to connected peers.
- **Attestation**: happens automatically on startup when your wallet has balance. Check via the `attested` field in the wallet response.
- **AntiMatter details**: see the [Crypto section in README.md](../../README.md#crypto) for the full protocol spec.
