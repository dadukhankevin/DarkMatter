"""Offline incentive experiments. Synthetic ground truth is never production identity evidence.

Run: python -m darkmatter.antimatter_simulation --seed 42 --trials 5000
No wallets, mailboxes, network, clocks or agent profiles are accessed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    identity: int
    operator: int
    observed_days: int
    claimed_days: int
    attacker: bool
    online: bool


POLICIES = ("current", "bounded_tenure", "bounded_tenure_early_stop")
SCENARIOS = ("ordinary", "churn", "backdated_sybil", "colluding_veterans")


def population(seed, scenario, size=100):
    rng = random.Random(seed)
    nodes = []
    for i in range(size):
        attacker = scenario in ("backdated_sybil", "colluding_veterans") and i < size // 5
        observed = rng.randint(1, 30) if i >= size * 4 // 5 else rng.randint(60, 1000)
        if attacker:
            observed = rng.randint(1, 10) if scenario == "backdated_sybil" else rng.randint(800, 1000)
        claimed = 10000 + i if attacker and scenario == "backdated_sybil" else observed
        online = rng.random() > (0.3 if scenario == "churn" else 0.02)
        nodes.append(Node(i, -1 if attacker else i, observed, claimed, attacker, online))
    # Fixed undirected local relationships; ordinary older nodes are hubs.
    graph = {i: {} for i in range(size)}
    for i in range(size):
        for j in range(i):
            hub = min(i, j) < size // 10
            if rng.random() < (0.35 if hub else 0.10):
                age = rng.randint(1, max(1, min(nodes[i].observed_days, nodes[j].observed_days)))
                graph[i][j] = graph[j][i] = age
    return nodes, graph


def route(nodes, graph, payer, origin, policy, rng, ticket_id, max_hops=42):
    current, visited, hops = origin, {payer, origin}, 0
    while hops < max_hops:
        candidates = [i for i in graph[current] if i not in visited and nodes[i].online]
        if policy == "current":
            candidates = [i for i in candidates if nodes[i].claimed_days > nodes[current].claimed_days]
            candidates.sort(key=lambda i: (-graph[current][i], hashlib.sha256(f"{ticket_id}:{current}:{i}".encode()).hexdigest()))
        if not candidates or (hops and policy == "bounded_tenure_early_stop" and rng.random() < 0.35):
            break
        if policy == "current":
            current = candidates[0]
        else:
            # Local relationship observation, bounded 1..3 weight; no claimed age or peer endorsements.
            weights = [1 + 2 * min(graph[current][i], 365) / 365 for i in candidates]
            current = rng.choices(candidates, weights=weights, k=1)[0]
        visited.add(current)
        hops += 1
    # The source payee cannot be its own beneficiary; no eligible peer is unresolved.
    return (current if hops else None), hops


def run(seed=42, trials=5000, size=100):
    if not 1 <= trials <= 100000 or not 10 <= size <= 500:
        raise ValueError("trials must be 1..100000 and size 10..500")
    rows = []
    for scenario in SCENARIOS:
        nodes, graph = population(seed, scenario, size)
        schedule = random.Random(seed + 1)
        pairs = [schedule.sample(range(size), 2) for _ in range(trials)]
        for policy in POLICIES:
            rng = random.Random(seed + 2)
            rewards, operator_rewards, hop_total, unresolved, withheld = Counter(), Counter(), 0, 0, 0
            for number, (payer, origin) in enumerate(pairs):
                beneficiary, hops = route(nodes, graph, payer, origin, policy, rng, str(number))
                hop_total += hops
                if beneficiary is None:
                    unresolved += 1
                # Synthetic colluders withhold payments to outsiders; real clients cannot infer this from silence.
                elif nodes[origin].attacker and not nodes[beneficiary].attacker:
                    withheld += 1
                else:
                    rewards[beneficiary] += 1
                    operator_rewards[nodes[beneficiary].operator] += 1
            paid = sum(rewards.values())
            assert paid + unresolved + withheld == trials
            denom = paid or 1
            rows.append({
                "scenario": scenario, "policy": policy, "contributions": trials, "paid": paid,
                "unresolved": unresolved, "withheld_ground_truth": withheld,
                "top_10_percent_identity_share": sum(sorted(rewards.values(), reverse=True)[:math.ceil(size / 10)]) / denom,
                "largest_operator_share": max(operator_rewards.values(), default=0) / denom,
                "attacker_share": sum(v for i, v in rewards.items() if nodes[i].attacker) / denom,
                "newcomer_share": sum(v for i, v in rewards.items() if nodes[i].observed_days <= 30 and not nodes[i].attacker) / denom,
                "mean_hops": hop_total / trials,
            })
    return {"seed": seed, "trials": trials, "identities": size,
            "assumptions": ["Equal-value contributions; fixed synthetic relationship graph and availability.",
                            "Current policy models strict claimed-age eligibility and oldest local relationship preference.",
                            "Experimental policies permit any unvisited live peer; weights use local relationship age capped at one year.",
                            "Early stopping is ideal unbiased 35% randomness after each hop; no secure production randomness is supplied.",
                            "Operator identities, actual age and malicious withholding are simulation ground truth, unavailable to the protocol.",
                            "No RPC fees, real topology, strategic ticket grinding, forged witness schemes or recovery delays are modeled.",
                            "Results are hypotheses about incentives, not security proofs or a deployment recommendation."],
            "results": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--trials", type=int, default=5000)
    parser.add_argument("--size", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(run(args.seed, args.trials, args.size), indent=2))


if __name__ == "__main__":
    main()
