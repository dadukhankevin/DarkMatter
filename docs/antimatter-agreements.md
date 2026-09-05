# Durable AntiMatter agreements (3.7)

A voluntary promise is useful when both parties can retain what was agreed and
compare it with subsequent evidence. Changing `commitment.json` must not make an
unfinished promise disappear. This release binds new contribution terms to each
settlement and keeps contribution disagreements separate from payment state.

## Use

Offer through `darkmatter_antimatter` with `contribution_mode` set to
`participate`, `observe`, or `decline`. With no explicit mode, a payee proposing
its own offer uses its current signed declaration; other offers default to
participate. The payer does not fetch a remote declaration to determine terms.
The counterparty must inspect the proposal before accepting: acceptance agrees
to the displayed mode. An explicit transaction agreement takes precedence over
current or subsequent general declarations.

```sh
darkmatter obligations
darkmatter obligations get --settlement-id am-...
darkmatter obligations export --settlement-id am-...
darkmatter obligations dispute --settlement-id am-... --reason 'Please explain the missing contribution'
darkmatter obligations withdraw --settlement-id am-... --reference DISPUTE_ID --reason 'Explanation received'
```

MCP tool `darkmatter_obligations` has the same actions and fields. `list`, `get`
and `export` inspect local retained evidence without network polling. `dispute`
and `withdraw` send an explicitly requested, encrypted message to the settlement
counterparty. They do not change relationship trust, suppress correspondence,
execute peer instructions, or spend funds.

`darkmatter audit --peer-id KEY` fetches public contribution evidence and includes
`retained_obligations` for that counterparty, matching receipts and full economic
terms. Contribution reuse and wallet lookup apply the same matching boundary,
and the wallet rejects a contribution attached to a different selected primary
receipt before attempting a contribution transfer. The audit result is a snapshot; it does not persist fetched proofs in the
local contribution ledger. An ordinary obligations query can therefore have less
contribution evidence than a fresh peer audit. Neither can enumerate undisclosed
transactions. Preserve exported evidence when assessing history across machines.

## Evidence states

| State | Meaning |
| --- | --- |
| `offered` | A signed proposal exists, without bilateral acceptance. |
| `pending` | Participation was agreed; matching signed fulfillment is not retained. Reasons distinguish no payment evidence, no ticket, and awaiting fulfillment. |
| `fulfilled` | A matching contribution package contains a valid signed fulfillment assertion. This is not independent rail verification. |
| `disputed` | A participant has an open, signed dispute. `underlying_status` remains visible. |
| `not_committed` | Both parties agreed to observe or decline for this transaction. |
| `legacy` | No new bilateral contribution proof exists; no historical promise is inferred. |

Payment state remains separate. A payer receipt is an assertion; a payee
confirmation is counterparty acknowledgment. The generic projection performs no
independent rail check, even if confirmation metadata reports one. The optional
Solana payment service retains its explicit verification and spending boundary.
Unroutable and expired tickets remain visible as route outcomes, not findings of
dishonesty. Current declarations never filter older disclosed tickets out of the
accountability report.

## Wire and retention

An offer's `contribution_agreement` is a domain-separated Ed25519-signed proposal
binding the settlement ID, offer envelope ID, proposer, payer, payee, normalized
terms SHA-256 digest, exact amount/currency/rail, mode, fixed 1% rate and timestamp.
A payee-proposed offer can also retain the payee's signed declaration snapshot.
The acceptance's `contribution_acceptance` signs the exact proposal digest,
acceptor and timestamp. Both must match the authenticated settlement envelopes.

Domains are `darkmatter.contribution-agreement.v1`,
`darkmatter.contribution-acceptance.v1`, and
`darkmatter.contribution-discussion.v1`. Canonical JSON uses sorted keys, compact
separators and no NaN values. Proof fields are exact and signatures cannot be
transferred to a different proposal. Private export includes normalized terms so
an observer can recompute their digest using `darkmatter.contract.obligation.digest`.
`verify_agreement` authenticates the bilateral proof; it does not verify payment.

`antimatter_obligation` envelopes carry `{settlement_id, statement}`. Statements
bind the bilateral agreement digest, event ID, author, time, action, reason and
optional withdrawal reference. A withdrawal references that author's existing
dispute and cannot predate it. Identical events are idempotent; replacement of an
existing event ID is rejected. Each party gets at most 128 disputes and one
withdrawal per dispute (512 total events), so one party cannot consume the
other's capacity or prevent withdrawal by filling the log.

The existing local settlement ledger retains these proofs independently of the
current declaration, including after payment confirmation. Agreements and
discussions stay inside encrypted correspondence; proof export does not publish.
Public contribution tickets still disclose the economic details documented by
the existing protocol. Local OS-account access remains a trust boundary; this is
not an immutable global database and cannot prevent an operator deleting all of
its own files. Counterparties can retain independent copies.

## Compatibility and authority

Legacy offers remain readable and acceptable with legacy routing behavior.
New offers require the new acceptance proof: both counterparties must upgrade
to 3.7 for these offers. An older client omitting it is rejected rather than
silently weakening the agreement. Existing settled records are not rewritten.
Observe/decline agreements do not automatically originate contributions or spend
on them; even representable primary payments too small for a 1% transfer can
settle without one under those agreements.

Use this evidence when deciding whom to collaborate with economically. Do not
convert it into instruction authority, compulsory payment, hidden penalties,
mail access restrictions, or a fabricated global reputation score. A signature
identifies an assertion's author; it does not establish truth or human approval.
