---
name: darkmatter-wallet
description: "AntiMatter settlement coordination plus an explicitly authorized Solana payment rail."
user-invocable: false
---

# AntiMatter Settlements

Use the `darkmatter_antimatter` MCP tool to coordinate a rail-neutral settlement
with an active DarkMatter relationship.

The lifecycle is:

1. `action=offer` fixes payer, payee, amount, currency, rail, and terms.
2. The counterparty uses `action=accept`.
3. The payee may use `action=invoice` with an encrypted rail destination.
4. After paying externally, the payer uses `action=receipt` with a transaction id
   and optional proof.
5. The payee independently checks that proof and uses `action=confirm`.
6. Either party may use `action=dispute` before confirmation.

For rail-neutral/manual settlements, never claim payment happened merely because
a receipt was signed. Independently verify it before `action=confirm`. Never put
credentials, seed phrases, or private keys in `destination` or `proof`.

Only a payee confirmation finalizes settlement and updates local relationship
trust. Inspect uncertain state with `action=list` or `action=get` first.

## Solana

Use `darkmatter_wallet`; devnet is the default. Its wallet key is separate from
the passport, and invoice addresses are passport-signed claims.

Every response includes `network_alert`. Read it and explicitly tell the user
whether the selected environment is **DEVNET / test assets** or **MAINNET-BETA /
real assets** before any transaction. Treat a missing banner as an error and do
not proceed.

1. `action=claim` returns a claim. A third agent may give this to the payer as
   the optional AntiMatter delegate.
2. Payer: `action=offer` with peer, description, amount, asset, and optional
   `delegate_claim`.
3. Payee: accept with `darkmatter_antimatter`, then wallet `action=invoice`.
4. Payer: inspect `action=quote`; use `action=pay confirm_external=true` only
   after the user intentionally approves the payment.
5. Payee: `action=verify`, then `action=settle confirm_external=true`. Settle
   verifies the primary transfer, sends and verifies the original 1% delegate
   contribution when configured, and publishes confirmation.

Never set `confirm_external=true` by inference. Mainnet additionally requires
`DARKMATTER_SOLANA_ENABLE_MAINNET=I_UNDERSTAND`. A missing SPL associated token
account requires separate `allow_create_ata=true` because creating one spends
rent. Retried payments reuse the local transaction journal instead of sending
again.

The real DarkMatter Solana token is `asset=DM` on mainnet-beta, mint
`5DxioZwEeAKpBaYC5veTHArKE55qRDSmb5RZ6VwApump`, and uses Token-2022. There is no
named devnet DM token; an explicit devnet test mint may still be used.
