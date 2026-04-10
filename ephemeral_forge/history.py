"""Launch time tracking for provider/region scoring."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HISTORY_DIR = Path.home() / ".ephemeral-forge"
HISTORY_FILE = HISTORY_DIR / "history.json"


@dataclass
class LaunchRecord:
    run_id: str
    provider: str
    region: str
    zone: str
    instance_types: list[str]
    count_requested: int
    count_fulfilled: int = 0
    ts_probe_start: float = 0.0
    ts_api_call: float = 0.0
    ts_fleet_created: float = 0.0
    ts_all_running: float = 0.0
    ts_first_ssh: float = 0.0
    ts_all_ssh: float = 0.0
    spot_price: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def launch_duration(self) -> float:
        """Seconds from API call to all instances running."""
        if self.ts_all_running > 0 and self.ts_api_call > 0:
            return self.ts_all_running - self.ts_api_call
        return 0.0


def save_record(record: LaunchRecord) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    records = load_history()
    records.append(record)
    with open(HISTORY_FILE, "w") as f:
        json.dump([asdict(r) for r in records], f, indent=2)


def load_history() -> list[LaunchRecord]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE) as f:
        data = json.load(f)
    return [LaunchRecord(**r) for r in data]


def get_median_launch_time(
    provider: str,
    region: str,
    limit: int = 10,
) -> float | None:
    """Median launch duration for a (provider, region) pair,
    using the last `limit` records."""
    records = [
        r
        for r in load_history()
        if r.provider == provider and r.region == region and r.launch_duration() > 0
    ]
    records = records[-limit:]
    if not records:
        return None
    durations = sorted(r.launch_duration() for r in records)
    mid = len(durations) // 2
    if len(durations) % 2 == 0:
        return (durations[mid - 1] + durations[mid]) / 2
    return durations[mid]
