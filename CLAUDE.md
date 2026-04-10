# ephemeral-forge

Spin up massive compute fleets in seconds, run your workload,
tear them down. Cheap, fast, disposable infrastructure.

## Language

**Python only.** No bash scripts. All tooling, CLI, and
library code must be Python. The `reference/` directory
contains the original bash scripts for design reference
only — they must be rewritten in Python.

Use `click` or `typer` for CLI. Use `boto3` for AWS,
`google-cloud-compute` for GCP, `azure-mgmt-compute` for
Azure. Use `asyncio` where concurrency helps (e.g., parallel
SSH).

**Prefer native libraries over shelling out.** Use `boto3`
instead of calling `aws` CLI. Use `paramiko` or `asyncssh`
instead of shelling out to `ssh`. Use `cryptography` instead
of calling `ssh-keygen`. If a Python library exists for the
task, use it. Only shell out as a last resort.

## Configuration

All account-specific settings (AWS profile, GCP project ID,
Azure subscription, instance types, regions) live in
`ephemeral-forge.toml`. This file is gitignored — never
check it in.

A `ephemeral-forge.example.toml` should be checked in as a
template.

## Multi-Cloud

AWS is the first implementation. The design should
accommodate GCP, Azure, OCI, and others. Keep cloud-specific
code behind a provider abstraction so adding new clouds
doesn't require rewriting the core.

## Global Region Probing

**Probe all regions worldwide by default.** Don't hardcode a
small set of candidate regions — probe every available region
in parallel and pick the cheapest. Users can restrict to
specific regions in `ephemeral-forge.toml` if they want.

For AWS: call `ec2.describe_regions()` to get the full list,
then `describe_spot_price_history()` across all of them in
parallel (use `asyncio.to_thread` since boto3 is sync).

For GCP: list all zones via `compute_v1.ZonesClient`, probe
spot availability across all of them.

For Azure: query the Retail Prices API
(`prices.azure.com`) for all regions.

## EC2 / AWS (first provider)

- **Always use CreateFleet**, never RunInstances. No
  fallbacks. No exceptions.
- **Always use spot**, never on-demand. No fallbacks. Better
  to fail and try another region than silently pay 3x.
- Use `price-capacity-optimized` allocation strategy.
- Wide instance type pool + all subnets (Karpenter pattern).
- Tag all resources with `Purpose=ephemeral-forge` and a
  unique `RunID` for cleanup.
- Always clean up on exit (`try/finally` or `atexit`).
- SDK: `boto3`.
- AWS profile and credentials are configured in
  `ephemeral-forge.toml` and the local AWS credentials file.
- GPU spot quota: "All G and VT Spot Instance Requests"
  service quota defaults to 0 vCPUs — must request increase
  via AWS console before launching GPU instances.
- IAM policy: see `reference/aws-iam-policy.json` and
  `reference/IAM_SETUP.md` for the required permissions.

## GCP (second provider)

- **Always use Spot VMs** (`provisioningModel: SPOT`), never
  regular VMs. No fallbacks.
- Use `bulkInsert` for launching multiple instances.
  `bulkInsert` only accepts a single machine type — use
  sequential fallback across types if the first choice has
  no capacity.
- Spot prices are fixed per (machine_type, zone), not
  auction-based like AWS. Probe availability, not price.
- SSH keys via instance metadata (`ssh-keys` field), not a
  key pair API.
- Firewall rules are project-global and tag-based.
- Cleanup: delete instances, delete firewall rules. No
  resource group concept.
- SDK: `google-cloud-compute`.
- Auth: `gcloud auth application-default login` creates
  ambient credentials the Python SDK picks up automatically.

## Azure (third provider)

- **Always use Spot VMs** (`priority: Spot`,
  `evictionPolicy: Delete`), never regular VMs. No
  fallbacks.
- Use **VMSS Flex** (Virtual Machine Scale Sets, Flexible
  orchestration mode) for launching fleets. Supports
  `vmSizesProfile` for multiple VM sizes in one call.
- Use `CapacityOptimized` allocation strategy.
- Create a **dedicated resource group per run** — deleting
  the resource group at teardown cleans up everything
  (VMs, disks, NICs, NSG, VNet, public IPs). This is the
  Azure cleanup superweapon.
- Azure requires explicit VNet + Subnet creation (no usable
  default networking like AWS/GCP).
- Spot prices from the Azure Retail Prices REST API
  (`prices.azure.com`).
- SDK: `azure-mgmt-compute`, `azure-mgmt-network`,
  `azure-mgmt-resource`, `azure-identity`.

## Design Principles

- **Preemptible only, always.** Spot (AWS), Spot VMs (GCP),
  Spot priority (Azure). On-demand is never acceptable.
- **Batch fleet APIs, always.** CreateFleet (AWS), bulkInsert
  (GCP), VMSS (Azure). Single-instance launch APIs are
  never acceptable.
- **Clean up everything.** Instances, launch templates, key
  pairs, security groups, firewall rules, resource groups.
  No orphaned resources, ever.
- **Cost-aware.** Probe prices globally before launching. Log
  estimated cost. Use the cheapest viable option anywhere
  in the world.
- **Fail fast.** If no capacity, fail immediately with a
  clear error. Don't retry forever or fall back to
  expensive alternatives.
- **Wide instance pools.** Offer many instance types and all
  zones. Let the cloud's allocation strategy pick the best
  combo.

## Provider Abstraction

The core abstraction (`ProviderBase` ABC) defines this
lifecycle:

1. `probe_spot_prices(regions, instance_types)` — find
   cheapest region/zone globally
2. `resolve_image(region, image_spec)` — get the right
   AMI / image / URN (default: Ubuntu 24.04; for GPU:
   Deep Learning image with NVIDIA drivers)
3. `launch_fleet(config, run_id, region, zone)` — create
   all prerequisites (keys, firewall, template) then
   launch N preemptible instances
4. `wait_until_ready(result)` — poll until running, collect
   IPs
5. `teardown(result)` — destroy everything

Each provider implements these five methods using its native
APIs. The cloud-agnostic orchestrator (`fleet.py`) calls them
in sequence.

Key data types:
- `FleetConfig` — what the user wants (count, instance types,
  image, disk size, tags)
- `Instance` — a running instance (ID, type, zone, IPs)
- `FleetResult` — launch result (instances, errors, resource
  handles for teardown)

## Project Structure

```
ephemeral_forge/           # Python package
  cli.py                   # CLI entry point
  config.py                # Load ephemeral-forge.toml
  fleet.py                 # Cloud-agnostic orchestrator
  provider.py              # ProviderBase ABC + dataclasses
  ssh.py                   # SSH/SCP helpers (paramiko/asyncssh)
  providers/
    __init__.py            # get_provider() factory
    aws.py                 # AWSProvider (CreateFleet + spot)
    gcp.py                 # GCPProvider (bulkInsert + Spot VMs)
    azure.py               # AzureProvider (VMSS Flex + Spot)
ephemeral-forge.toml       # Local config (gitignored)
ephemeral-forge.example.toml  # Template (checked in)
reference/                 # Original bash scripts (read-only)
tests/                     # Tests
```

## Python

Always use a venv. Never install into system Python.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Code Quality

All Python code must:

- **Pass `ruff format`** (formatter). Run before every commit.
- **Pass `ruff check`** (linter). Fix all warnings before
  committing. No `# noqa` suppressions without a comment
  explaining why.
- Use type hints on all function signatures.

## Markdown Standards

Word wrap paragraphs/prose at 80 chars. Align table columns.
Do not wrap text inside triple backtick blocks.

## Key Patterns

See `reference/ec2-fleet-strategies.md` for detailed notes on:

- CreateFleet vs RunInstances
- price-capacity-optimized allocation
- Wide instance type pools (Karpenter pattern)
- Region/AZ selection via spot price probing
- Tagging and cleanup conventions
- Cost control strategies

See `reference/IAM_SETUP.md` and `reference/aws-iam-policy.json`
for AWS IAM setup.

## GPU Instance Types by Cloud

### AWS

| Type          | GPU       | VRAM  | Spot ~$/hr |
|---------------|-----------|-------|------------|
| g4dn.xlarge   | 1x T4     | 16 GB | $0.13      |
| g4dn.2xlarge  | 1x T4     | 16 GB | $0.23      |
| g5.xlarge     | 1x A10G   | 24 GB | $0.45      |
| g6.xlarge     | 1x L4     | 24 GB | $0.22      |

### GCP

| Type              | GPU    | VRAM  | Spot ~$/hr |
|-------------------|--------|-------|------------|
| g2-standard-4     | 1x L4  | 24 GB | $0.28      |
| n1-standard-4+T4  | 1x T4  | 16 GB | $0.18      |
| a2-highgpu-1g     | 1x A100| 40 GB | $1.10      |

### Azure

| Type                    | GPU    | VRAM  | Spot ~$/hr |
|-------------------------|--------|-------|------------|
| Standard_NC4as_T4_v3    | 1x T4  | 16 GB | $0.13      |
| Standard_NV36ads_A10_v5 | 1x A10 | 24 GB | $0.45      |
| Standard_NC24ads_A100_v4| 1x A100| 80 GB | $1.00      |
