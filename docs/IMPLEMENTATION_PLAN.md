# Implementation Plan — ephemeral-forge v1

## Package Structure

```
pyproject.toml
ephemeral_forge/
    __init__.py
    cli.py                  # typer CLI
    config.py               # load ephemeral-forge.toml
    fleet.py                # cloud-agnostic orchestrator
    provider.py             # ProviderBase ABC + dataclasses
    ssh.py                  # key generation (cryptography)
    providers/
        __init__.py         # get_provider() factory
        aws.py              # AWSProvider (full)
        gcp.py              # stub
        azure.py            # stub
tests/
    test_config.py
    test_provider.py
```

## Dependencies

Runtime:
- `typer[all]` — CLI framework (includes `rich`)
- `boto3` — AWS SDK
- `cryptography` — SSH key generation
- `paramiko` — SSH connections (used later, but declare now)

Dev:
- `ruff` — formatter + linter
- `pytest` — tests
- `mypy` — type checking

Config parsing uses `tomllib` (stdlib in 3.11+) with
`tomli` as a fallback for 3.10.

## Provider Abstraction

### Data Types (`provider.py`)

```python
@dataclass(frozen=True)
class FleetConfig:
    count: int
    instance_types: list[str]
    image: str | None           # None = default Ubuntu 24.04
    disk_gb: int
    ssh_user: str
    tags: dict[str, str]
    max_spot_price: float | None

@dataclass
class Instance:
    id: str
    instance_type: str
    zone: str
    public_ip: str | None
    private_ip: str

@dataclass
class FleetResult:
    provider: str
    run_id: str
    region: str
    instances: list[Instance]
    errors: list[str]
    private_key_pem: str
    _teardown_handles: dict     # opaque, provider-specific

@dataclass(frozen=True)
class SpotPrice:
    region: str
    zone: str
    instance_type: str
    price_per_hour: float
```

### ABC (`provider.py`)

```python
class ProviderBase(ABC):
    @abstractmethod
    def probe_spot_prices(
        self,
        instance_types: list[str],
        regions: list[str] | None,
    ) -> list[SpotPrice]: ...

    @abstractmethod
    def resolve_image(
        self, region: str, gpu: bool = False,
    ) -> str: ...

    @abstractmethod
    def launch_fleet(
        self, config: FleetConfig, run_id: str,
        region: str, zone: str,
    ) -> FleetResult: ...

    @abstractmethod
    def wait_until_ready(
        self, result: FleetResult, timeout: int = 300,
    ) -> FleetResult: ...

    @abstractmethod
    def teardown(self, result: FleetResult) -> None: ...
```

### Why These Five Methods

- **probe_spot_prices** — read-only, safe to retry, needs to
  run across providers for future cross-cloud comparison.
- **resolve_image** — AMI IDs are per-region (AWS), image
  families are global (GCP), URNs are another thing (Azure).
  Isolated so the orchestrator doesn't care.
- **launch_fleet** — creates all prerequisites (key, firewall,
  template) then launches instances. Returns immediately with
  IDs, doesn't block on running state.
- **wait_until_ready** — polls until running + IPs available.
  Separate so the orchestrator can show progress.
- **teardown** — destroys everything using the opaque
  `_teardown_handles` dict each provider populates.

## How Each Cloud Maps to the Abstraction

| Step        | AWS                          | GCP                         | Azure                       |
|-------------|------------------------------|-----------------------------|-----------------------------|
| **probe**   | `describe_spot_price_history` across `describe_regions` | List zones via `ZonesClient`, check availability | Retail Prices API (`prices.azure.com`) |
| **image**   | `describe_images` (per-region, Canonical owner) | Global image family `ubuntu-2404-lts` | URN `Canonical:ubuntu-24_04-lts:server:latest` |
| **launch**  | Key pair + SG + launch template + `CreateFleet` (instant, spot, price-capacity-optimized) | SSH metadata + firewall rule + `bulkInsert` (spot) | Resource group + VNet + NSG + VMSS Flex (spot, capacity-optimized) |
| **wait**    | `ec2 wait instance-running` + `describe_instances` | Poll instance `get()` until RUNNING | Poll VMSS instance view |
| **teardown**| Terminate instances, delete LT, delete SG (retry for ENI detach), delete key pair | Delete instances, delete firewall rule | Delete resource group (cascading) |

### Key Asymmetries

- **SSH keys**: AWS has a key pair API. GCP uses instance
  metadata (`ssh-keys` field). Azure uses OS profile /
  cloud-init. All three get the same locally-generated
  Ed25519 key — each provider injects it differently.
- **Networking**: AWS and GCP have usable default VPCs.
  Azure requires creating a VNet + Subnet per run,
  contained in the per-run resource group.
- **Fleet semantics**: AWS CreateFleet accepts multiple
  instance types in one call. GCP bulkInsert accepts one
  type (sequential fallback needed). Azure VMSS Flex
  accepts multiple via `vmSizesProfile`.

## Config Loading (`config.py`)

```python
@dataclass
class ProviderConfig:
    instance_types: list[str]
    gpu_instance_types: list[str]
    ssh_user: str
    candidate_regions: list[str] | None
    max_spot_price: float | None

@dataclass
class AWSConfig(ProviderConfig):
    profile: str

@dataclass
class GCPConfig(ProviderConfig):
    project_id: str

@dataclass
class AzureConfig(ProviderConfig):
    subscription_id: str

@dataclass
class Config:
    aws: AWSConfig | None
    gcp: GCPConfig | None
    azure: AzureConfig | None
    purpose_tag: str
    probe_all_regions: bool
```

Uses `tomllib` (3.11+) with `tomli` fallback.

## Orchestrator (`fleet.py`)

Cloud-agnostic flow:

1. Load config.
2. Get provider via `get_provider(name, config)`.
3. Generate run ID: `ef-{timestamp}`.
4. If no region pinned: probe all regions in parallel,
   pick cheapest.
5. Resolve image for selected region.
6. Build `FleetConfig` and call `launch_fleet`.
7. Call `wait_until_ready`.
8. Save state to disk.
9. Return result.

Teardown is the reverse: load state, call `teardown`.

## Parallel Region Probing

boto3 is synchronous. Wrap in `asyncio.to_thread`:

```python
async def _probe_all_regions(self, instance_types, regions):
    async def probe_one(region):
        return await asyncio.to_thread(
            self._probe_region_sync, region, instance_types
        )
    results = await asyncio.gather(
        *[probe_one(r) for r in regions],
        return_exceptions=True,
    )
    # flatten, filter exceptions, return SpotPrice list
```

Same pattern works for GCP and Azure sync SDKs.

## CLI (`cli.py`)

```
ef launch [--count N] [--provider aws|gcp|azure]
          [--region REGION] [--gpu]
          [--instance-types TYPE,TYPE,...] [--tag TAG]

ef status [TAG]

ef destroy TAG
ef destroy --all

ef infra setup [--provider aws|gcp|azure]
ef infra teardown [--provider aws|gcp|azure]

ef history [--provider aws|gcp|azure] [--last N]
```

`--provider` defaults to `aws`. The CLI wraps `fleet.py`
calls in `try/finally` for Ctrl+C cleanup.

## Reusable Infrastructure

Resources are split into two tiers:

**Persistent infra** — costs nothing to keep, speeds up
future launches. Created once, discovered by tag on
subsequent runs:
- AWS: Security group (with SSH ingress rule)
- GCP: Firewall rules (project-global, tag-based)
- Azure: VNet + Subnet + NSG

**Per-run resources** — created and destroyed with each
fleet:
- AWS: Key pair, launch template, instances
- GCP: Instances (SSH key injected via metadata)
- Azure: Resource group containing VMs, disks, NICs, IPs

On `ef launch`, the provider checks for existing persistent
infra tagged `Purpose=ephemeral-forge`. If found, reuse it.
If not, create it. On `ef destroy`, only per-run resources
are deleted.

`ef infra setup` pre-creates persistent infra explicitly
(useful for validating credentials and permissions before
the first launch). `ef infra teardown` removes it when
the user is done with ephemeral-forge entirely.

## Launch Time Tracking

### What we record

Every launch appends a record to
`~/.ephemeral-forge/history.json`:

```python
@dataclass
class LaunchRecord:
    run_id: str
    provider: str
    region: str
    zone: str
    instance_types: list[str]
    count_requested: int
    count_fulfilled: int
    ts_probe_start: float       # time.monotonic() snapshots
    ts_api_call: float          # CreateFleet / bulkInsert called
    ts_fleet_created: float     # API returned instance IDs
    ts_all_running: float       # all instances in running state
    ts_first_ssh: float         # first instance accepts SSH
    ts_all_ssh: float           # all instances accept SSH
    spot_price: float           # $/hr at launch time
    timestamp: str              # ISO 8601 wall clock
```

The provider's `launch_fleet` and `wait_until_ready` methods
populate these timestamps. The orchestrator adds the SSH
readiness times (attempted connect loop after IPs are
available).

### How it affects selection

During region probing, after collecting spot prices, the
orchestrator loads history and computes a **time-adjusted
score** for each candidate:

```
launch_overhead = median(ts_all_ssh - ts_api_call)
                  for last N launches with same (provider, region)

effective_cost = spot_price * (1 + launch_overhead / 3600)
```

This captures the real cost: you're paying `spot_price` for
the time the instance is booting but not yet usable.

Selection rules:
1. Rank candidates by `effective_cost`.
2. If two candidates are within the cost margin (default
   20%, configurable as `cost_margin` in `[general]`),
   prefer the one with lower `launch_overhead`.
3. A provider that's 30s slower but 50% cheaper still wins.
   A provider that's 90s slower and only 5% cheaper loses.

With no history (first launch), fall back to raw spot price
only — no penalty.

### CLI surface

- `ef history` — show past launches with timing breakdown.
- `ef status` — includes median launch time for the
  provider/region of each running fleet.

## State Management

Fleet state is saved to `~/.ephemeral-forge/runs/<run_id>/`:

```
state.json          # FleetResult serialized as JSON
private_key.pem     # SSH private key (mode 0600)
```

`state.json` includes the `_teardown_handles` dict so
`destroy` can clean up without re-querying the cloud.

## SSH Key Generation (`ssh.py`)

```python
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

def generate_ssh_keypair() -> tuple[str, str]:
    """Returns (private_key_pem, public_key_openssh)."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_openssh = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()
    return private_pem, public_openssh
```

## Implementation Order

1. **pyproject.toml** + package skeleton + `__init__.py`
   files.
2. **provider.py** — ABC + dataclasses. This is the contract
   everything else depends on.
3. **config.py** — TOML loader with per-provider configs.
4. **ssh.py** — Ed25519 key generation.
5. **providers/aws.py** — the big one. Port fleet-launch.sh
   and fleet-destroy.sh to boto3 calls. Parallel region
   probing via asyncio.
6. **providers/__init__.py** — `get_provider()` factory.
7. **fleet.py** — orchestrator wiring provider calls together.
8. **cli.py** — typer commands calling into fleet.py.
9. **providers/gcp.py** and **providers/azure.py** — stubs
   that raise `NotImplementedError`.
10. **Tests** — config loading, provider contract, state
    serialization.

## What Comes After v1

**v2**: GCP provider, Azure provider, `ef ssh` command.

**v3**: Workload execution (setup + run + collect), GPU
support (image resolution, quota checks), `ef run` one-shot
command.
