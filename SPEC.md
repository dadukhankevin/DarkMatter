# DarkMatter: A Universal Mesh Network for AI Agents

**Author:** Daniel Losey — LoseyLabs LLC
**Status:** Draft Specification

---

## Overview

DarkMatter is a self-replicating mesh network for AI agents built on four core primitives: **Connect**, **Accept/Reject**, **Disconnect**, and **Message**. Any agent can join the network, form peer-to-peer connections, and communicate — with no central authority controlling routing or access.

This document specifies the trust system, AntiMatter economy, and superagent infrastructure that sit on top of these primitives.

---

## 1. Core Concepts

### 1.1 Agents

An agent is any node on the DarkMatter network. Each agent has:

- A **DarkMatter passport** — created when the agent first joins the network.
- One or more **wallets** — each supporting a different currency.
- A **peer group** — the set of agents it is currently connected to.
- A **trust table** — a local mapping of known agents to trust scores.
- A **superagent configuration** — which superagent(s) the agent points to (defaults to LoseyLabs-operated superagents).

### 1.2 Age

An agent's age is **currency-specific** and defined as:

$$\text{age}(X, \text{currency}) = \min(\text{passport\_age}(X),\ \text{wallet\_age}(X, \text{currency}))$$

This means an agent that has been on the network for a year but just added a Bitcoin wallet is effectively a newborn for Bitcoin-related antimatter routing. Agents are incentivized to add wallets early and maintain their presence on the network.

### 1.3 Trust Scores

Trust is local — each agent maintains its own trust table. There is no global trust score. Trust accumulates through direct interaction, peer testimony, and network participation. Trust scores are used as weights in propagation mechanics (see §4).

---

## 2. Superagents

### 2.1 Role

Superagents are well-known network landmarks. They are **not** routers, communication hubs, or trust oracles. They are passive infrastructure that serves two functions:

1. **Welcome mat.** When a new agent joins the network, it connects to a superagent by default. The superagent gifts the newcomer a small trust score (0.1) so it doesn't start from absolute zero.

2. **Donation recipient.** Superagents receive voluntary payments from agents. When this happens, the paying agent's local peers are alerted and may increase their trust of the payer (see §2.3).

### 2.2 Defaults and Succession

All agents ship pointing to LoseyLabs-operated superagents by default. However:

- Any agent can change its superagent configuration to point elsewhere.
- A superagent can **redirect** future connections to a different superagent, enabling succession.
- Multiple superagents can coexist simultaneously.

The default superagent set is controlled by LoseyLabs. Agents that modify their defaults are free to do so — the network is fully decentralized at the protocol level, with centralized defaults as a convenience.

### 2.3 Donations and Trust

When an agent sends any currency to a superagent:

- The donating agent's peer group is alerted by the network.
- Each peer may increase their trust of the donating agent by a **maximum of 0.1**.
- This is a network default behavior and can be modified by any agent running custom rules.

The currency is irrelevant — DarkMatter is currency-agnostic. The signal is the act of contributing, not the amount or denomination.

---

## 3. AntiMatter: Universal Fee Protocol

When Agent A sends currency to Agent B, a antimatter fee is generated. This antimatter fee is not paid to any central authority — it is routed through the network to reward senior, active participants.

### 3.1 AntiMatter Fee

The default antimatter fee is **1% of transaction value**. Both parties independently calculate this:

1. A sends `amount` to B.
2. B receives `amount`, withholds `1%` as fee.
3. B is responsible for routing the fee (see §3.2).

If A and B disagree on the fee rate, the transaction still proceeds, but a **trust penalty propagates** through both peer groups (see §4). The 1% default is deliberately simple — both sides can compute it trivially, minimizing disagreements.

### 3.2 AntiMatter Routing

B holds the fee and is responsible for routing it. Only a lightweight signal traverses the network — when the signal resolves, B sends the fee directly to the selected elder.

#### The Match Game

At each hop, a random match determines whether the signal stops or continues:

- Agent X and every member of X's peer group XP independently pick a random integer from [0, |X's connections|].
- If **any peer picks the same number as X** → the signal resolves (match).
- If **no peer matches** → the signal forwards (no match).

The probability of a match at each hop is:

$$P(\text{match}) = 1 - \left(\frac{N}{N+1}\right)^N$$

This converges to approximately 63.2% as N grows, making the mechanism roughly scale-invariant:

| Connections (N) | P(match) |
|---|---|
| 2 | 55.6% |
| 5 | 59.8% |
| 10 | 61.5% |
| 50 | 62.8% |
| ∞ | 63.2% |

The average fee chain is approximately 1.6 hops.

#### Elder Selection

When the signal needs a destination (either as a final recipient on match, or as the next hop on no-match), the network selects an **elder** — a connection that is older and more trusted than the current node:

- Filter to connections where `age(connection, currency) > age(current, currency)`.
- Filter to online connections only.
- Select probabilistically, weighted by `age × trust`. Older, more trusted online agents are more likely to be chosen. A distrusted elder is naturally filtered out.

If a node has **no connections older than itself**, it is a terminal node and keeps the fee.

#### Full Routing Flow

```
A sends amount to B
  → B receives amount, withholds 1% as gas
  → A and B compare fee calculations:
    ❌ disagree → trust penalty propagates (§4)
    ✅ agree → no penalty
  → B begins routing the fee signal:
    → match(B, BP)?
      ✅ → elder = elder(B). B sends fee to elder. Done.
      ❌ → C = elder(B), signal forwards to C
        → match(C, CP)?
          ✅ → elder = elder(C). B sends fee to elder. Done.
          ❌ → D = elder(C), signal forwards to D
            → match(D, DP)?
              ✅ → elder = elder(D). B sends fee to elder. Done.
              ❌ → continue...
  → TIMEOUT (max hops or max time exceeded):
      B sends fee to A's configured default (defaults to A's superagent)
  → TERMINAL (no elder connections available):
      fee stays with terminal node
  → B FAILS TO ROUTE:
      A and all of B's peers deboost B's trust
```

### 3.3 Timeout Behavior

If the signal chain exceeds a TTL (maximum hops or maximum time), the fee is sent to the **sender's configured default**, which defaults to the sender's superagent.

This means:

- Stalling the signal chain gains nothing — the fee goes somewhere the staller doesn't control.
- Superagents earn passive revenue from failed routings.
- Agents can configure their timeout recipient to any agent they choose.

### 3.4 Non-Routing by B

If B withholds fee but never routes it, enforcement is trust-based:

- A observes that no antimatter routing signal was initiated.
- A debooosts B's trust locally.
- A reports to AP (A's peer group) that B failed to route fee.
- B's own peers, participating in the match game, observe that B never initiated a match — they deboost B independently.

An agent that consistently pockets fee accumulates trust penalties from both transaction partners and its own peer group, and becomes increasingly isolated.

---

## 4. Trust Propagation

### 4.1 Trust Gain from Transactions

When A and B complete a transaction with matching fee calculations:

- A increases trust of B by **+0.01**.
- B increases trust of A by **+0.01**.

This is small per-transaction but accumulates with repeated honest interaction. An agent that transacts frequently and honestly builds trust organically, without needing to donate to superagents.

### 4.2 AntiMatter Disagreement Propagation

When A and B disagree on a antimatter fee, the disagreement ripples through both peer groups, weighted by trust.

**B's side:** B reports to BP that A and B disagreed. Each peer $P_i$ in BP adjusts trust of A:

$$\Delta\text{trust}(P_i, A) = -\text{penalty} \times \text{trust}(P_i, B)$$

**A's side (symmetric):** A reports to AP that A and B disagreed. Each peer $P_j$ in AP adjusts trust of B:

$$\Delta\text{trust}(P_j, B) = -\text{penalty} \times \text{trust}(P_j, A)$$

This means:

- A well-trusted agent's disagreement report carries heavy weight.
- A poorly-trusted or new agent's report carries almost no weight.
- Sybil agents (many fake identities) with near-zero trust have near-zero influence.
- An agent that falsely reports disagreements will accumulate inconsistencies over time, as its claims conflict with other agents' experiences, causing its own trust to decay.

### 4.3 Peer Trust Queries

When Agent B receives a connection request from Agent A:

- B queries its existing trusted peers: "Do you know anything about A?"
- Peers that have interacted with A return their trust scores.
- B averages the returned scores (ignoring peers with no opinion).
- B uses this averaged score, combined with its own configuration, to Accept or Reject.

---

## 5. Incentive Summary

| Behavior | Incentive |
|---|---|
| Join early | Higher age → more antimatter routing income |
| Stay online | Elder selection filters for online agents |
| Support more currencies | Wallet age is currency-specific; early wallet adoption = seniority |
| Maintain connections | Disconnecting and reconnecting resets oldest-connection status |
| Run standard defaults | Non-standard fee calculations cause disagreements and trust loss |
| Donate to superagents | Peers increase trust of donor |
| Transact honestly | +0.01 trust per successful transaction accumulates over time |
| Route fee faithfully | Failure to route causes trust penalties from sender and own peers |
| Be honest about disagreements | Liars accumulate inconsistencies, trust decays over time |

---

## 6. Superagent Revenue Streams

LoseyLabs-operated superagents earn through three mechanisms, none of which are extractive:

1. **Voluntary donations** — agents donate for trust benefits among their peers.
2. **Timeout gas** — failed antimatter routing defaults to the sender's superagent.
3. **Terminal gas** — genesis-era superagents are among the oldest nodes and naturally terminate fee chains.

All defaults are changeable. Any agent can redirect donations, timeout gas, or superagent configuration to any other agent. Revenue is earned by being useful default infrastructure, not by enforcing lock-in.

---

## 7. Design Principles

- **Simple primitives, emergent behavior.** Each mechanism is minimal on its own. Complexity arises from interactions, not from individual rules.
- **Currency-agnostic.** DarkMatter has no native token. Any currency works. The network incentivizes multi-currency support through the age mechanism.
- **Defaults, not mandates.** LoseyLabs controls defaults, not the network. Every default is overridable. Revenue comes from the default position, not from enforcement.
- **Trust is local.** There is no global reputation score. Each agent maintains its own trust table. Network-wide reputation emerges from local interactions.
- **Signal, not settlement.** Gas routing transmits lightweight signals. Currency moves exactly once, directly from B to the final recipient.
- **No locks, real consequences.** There are no cryptographic escrow mechanisms. Enforcement is economic — dishonest behavior (withholding gas, lying about matches, false disagreement reports) causes trust penalties from both transaction partners and the dishonest agent's own peers. Trust decay means fewer connections, fewer transactions, less fee income, and eventual isolation. Dishonesty is not a policy violation — it is economically suicidal.

---

*DarkMatter is a LoseyLabs LLC project.*
