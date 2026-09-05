"""Local projection for public AntiMatter contribution proof packages."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from darkmatter.contract.contribution import contribution_state, verify_contribution_package
from darkmatter.contract.obligation import source_matches
from darkmatter.store.local import LocalStore, atomic_write_text


class ContributionLedger:
    def __init__(self, store: LocalStore):
        self.store = store

    @property
    def path(self):
        return self.store.dir / "antimatter_contributions.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "contributions": {}}
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("contributions"), dict):
            raise ValueError("malformed AntiMatter contribution ledger")
        return data

    def _save(self, data: dict) -> None:
        atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def put(self, package: dict) -> dict:
        verified = verify_contribution_package(package)
        contribution_id = verified["ticket"]["contribution_id"]
        with self.store.locked():
            data = self._load()
            previous = data["contributions"].get(contribution_id)
            if previous:
                old_package = verify_contribution_package(previous["package"])
                old_ticket = old_package["ticket"]
                if old_ticket != verified["ticket"]:
                    raise ValueError("contribution id is already bound to another ticket")
                old_path = old_package["path"]
                new_path = verified["path"]
                if len(new_path) < len(old_path) or new_path[:len(old_path)] != old_path:
                    raise ValueError("contribution package would roll back or fork its route")
                if old_package["resolution"] is not None and (
                    verified["resolution"] != old_package["resolution"]
                ):
                    raise ValueError("contribution package would replace its resolution")
                if old_package["fulfillment"] is not None and (
                    verified["fulfillment"] != old_package["fulfillment"]
                ):
                    raise ValueError("contribution package would replace its fulfillment")
            record = {
                "contribution_id": contribution_id,
                "settlement_id": verified["ticket"]["source"]["settlement_id"],
                "origin_id": verified["ticket"]["origin_id"],
                "status": contribution_state(verified),
                "package": verified,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            data["contributions"][contribution_id] = record
            self._save(data)
            return deepcopy(record)

    def get(self, contribution_id: str) -> Optional[dict]:
        with self.store.locked():
            record = self._load()["contributions"].get(contribution_id)
            if not record:
                return None
            record = deepcopy(record)
            record["status"] = contribution_state(record["package"])
            return record

    def for_settlement(self, settlement_id: str, *, settlement: Optional[dict] = None) -> Optional[dict]:
        matches = [
            item for item in self.list()
            if item.get("settlement_id") == settlement_id
            and (settlement is None or source_matches(settlement, verify_contribution_package(item["package"])["ticket"]["source"]))
        ]
        return matches[0] if matches else None

    def list(self, status: Optional[str] = None) -> list[dict]:
        with self.store.locked():
            records = list(self._load()["contributions"].values())
        for record in records:
            record["status"] = contribution_state(record["package"])
        if status:
            records = [record for record in records if record.get("status") == status]
        records.sort(key=lambda record: record.get("updated_at", ""), reverse=True)
        return deepcopy(records)


__all__ = ["ContributionLedger"]
