# AntiMatter routing experiment

Reproduce each run with `python -m darkmatter.antimatter_simulation --seed 42 --trials 5000`.
This report aggregates seeds 42–46: 100 identities, 5,000 equal-value contributions per scenario and policy per seed.

## Historical motivation

- `b14a6ee`: the original 1% convention connected useful work with network support.
- `6476ada`: veterans represented contribution, rather than seniority alone.
- `8c9f610`: randomized age/trust selection tried to spread rewards.
- `0e6637d`: self-selection could falsely punish a payee; routing failure must remain distinct from misconduct.
- `2b950d2`: the Git-mailbox redesign favored durable asynchronous primitives.

The experiment compares the current claimed-age/longest-relationship rule with bounded local relationship-age weighting and the same weighting plus a 35% early stop. Experimental routes allow any unvisited live nonparticipant, which would require a protocol change before deployment.

## Results

Percentages below are shares of fulfilled synthetic contributions, averaged across seeds. Top 10% refers to identities; attacker share groups all attacker identities. Ground-truth malicious withholding is modeled separately from unavailable routes.

| Scenario | Policy | Top 10% share | Attacker share | Newcomer share | Mean hops | Paid / offered |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| ordinary | current | 99.8% | 0.0% | 0.0% | 2.6 | 91.9% |
| ordinary | bounded_tenure | 14.2% | 0.0% | 16.1% | 42.0 | 100.0% |
| ordinary | bounded_tenure_early_stop | 26.5% | 0.0% | 11.2% | 2.9 | 100.0% |
| churn | current | 99.9% | 0.0% | 0.0% | 2.3 | 88.1% |
| churn | bounded_tenure | 32.3% | 0.0% | 20.7% | 38.9 | 100.0% |
| churn | bounded_tenure_early_stop | 32.1% | 0.0% | 11.8% | 2.9 | 100.0% |
| backdated_sybil | current | 100.0% | 100.0% | 0.0% | 3.7 | 92.4% |
| backdated_sybil | bounded_tenure | 15.1% | 24.3% | 17.0% | 42.0 | 84.0% |
| backdated_sybil | bounded_tenure_early_stop | 24.7% | 32.1% | 13.1% | 2.9 | 85.4% |
| colluding_veterans | current | 100.0% | 40.3% | 0.0% | 2.5 | 83.6% |
| colluding_veterans | bounded_tenure | 15.5% | 25.5% | 15.4% | 41.9 | 84.2% |
| colluding_veterans | bounded_tenure_early_stop | 30.4% | 41.2% | 10.2% | 2.9 | 86.8% |

## Interpretation and limits

Strict age ordering concentrates terminal beneficiaries and lets backdated identities dominate this constructed attack. Removing claimed-age eligibility distributes rewards more broadly, but long random walks consume nearly the entire hop budget. Early stopping reduces that cost and broadens newcomer access. It does not solve collusion: established attackers retain substantial capture, sometimes greater than under the baseline.

The network deliberately includes older hubs; 20% of identities in attack scenarios share one malicious operator, including hub positions. Local relationship age is simulator ground truth for the node holding that relationship, not transferable proof of independent operators. Claimed age and signatures do not establish Sybil resistance. The model does not use peer endorsements or activity counts, so fabricated witness histories are not evaluated.

These figures are not measurements of the live DarkMatter network. They depend on graph construction, random seed, equal contribution values, fixed availability and modeled attacker behavior. Randomness is ideal and cannot be ground by a participant in this simulator; a real protocol needs an adversarial analysis of that assumption. Fees, retries, real topology, transaction verification attacks and strategic ticket grinding are omitted.

Production selection remains unchanged. Before adopting an alternative, test more graph families and value distributions, model adaptive colluders and randomness manipulation, and define verifiable continuity without pretending it identifies independent humans. The immediate production improvement is durable agreements with attributable follow-through, not a new reward score.

Full machine-readable results and assumptions are emitted by the command above. The simulator never opens a wallet, mailbox or network connection.
