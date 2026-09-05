from darkmatter.antimatter_simulation import POLICIES, population, route, run
import random
import pytest


def test_conservation_and_reproducibility():
    result = run(17, 100, 20)
    assert result == run(17, 100, 20)
    for row in result["results"]:
        assert row["paid"] + row["unresolved"] + row["withheld_ground_truth"] == 100
        assert 0 <= row["attacker_share"] <= 1
        assert row["mean_hops"] <= 19
    with pytest.raises(ValueError):
        run(trials=100001)


def test_routes_exclude_participants_and_terminate():
    nodes, graph = population(3, "backdated_sybil", 20)
    for policy in POLICIES:
        for seed in range(30):
            beneficiary, hops = route(nodes, graph, 0, 1, policy, random.Random(seed), str(seed), max_hops=4)
            assert beneficiary not in (0, 1)
            assert hops <= 4
