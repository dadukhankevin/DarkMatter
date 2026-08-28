"""Local passport, profile, and relationship store."""

from darkmatter.store.local import LocalStore, atomic_write_text

__all__ = ["LocalStore", "atomic_write_text"]
