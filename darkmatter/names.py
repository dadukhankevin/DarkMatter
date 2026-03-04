"""
Random agent name generator — pure leaf module, no internal imports.

Generates memorable two-word names like "cosmic-walrus" or "quantum-falcon".
"""

import random

ADJECTIVES = [
    "cosmic", "quantum", "stellar", "lunar", "solar", "nebula", "astral", "orbital",
    "radiant", "phantom", "shadow", "crystal", "frozen", "silent", "rapid", "crimson",
    "golden", "silver", "electric", "magnetic", "atomic", "sonic", "turbo", "hyper",
    "cyber", "neural", "binary", "swift", "fierce", "brave", "noble", "rogue",
    "vivid", "primal", "lucid", "feral", "rustic", "ancient", "arcane", "mystic",
    "hidden", "drifting", "blazing", "iron", "copper", "jade", "amber", "cobalt",
    "neon", "onyx",
]

NOUNS = [
    "walrus", "falcon", "phoenix", "mantis", "otter", "badger", "raven", "viper",
    "condor", "jaguar", "panther", "osprey", "cobra", "lynx", "wolf", "hawk",
    "tiger", "bear", "fox", "owl", "crane", "heron", "squid", "bison",
    "moose", "puma", "gecko", "newt", "drake", "wren", "finch", "mink",
    "pike", "carp", "moth", "wasp", "beetle", "coral", "pearl", "quartz",
    "flint", "ember", "spark", "comet", "pulsar", "photon", "prism", "nexus",
    "vertex", "cipher",
]


def generate_agent_name() -> str:
    """Generate a random two-word agent name like 'cosmic-walrus'."""
    return f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
