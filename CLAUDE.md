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

## Multi-Cloud

AWS is the first implementation. The design should
accommodate GCP, Azure, OCI, and others. Keep cloud-specific
code behind a provider abstraction so adding new clouds
doesn't require rewriting the core.

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
- **Cost-aware.** Probe prices before launching. Log
  estimated cost. Use the cheapest viable option.
- **Fail fast.** If no capacity, fail immediately with a
  clear error. Don't retry forever or fall back to
  expensive alternatives.
- **Wide instance pools.** Offer many instance types and all
  zones. Let the cloud's allocation strategy pick the best
  combo.

## Project Structure

```
ephemeral_forge/           # Python package
  cli.py                   # CLI entry point
  fleet.py                 # Cloud-agnostic orchestrator
  provider.py              # ProviderBase ABC + dataclasses
  ssh.py                   # SSH/SCP helpers (paramiko/asyncssh)
  providers/
    __init__.py            # get_provider() factory
    aws.py                 # AWSProvider (CreateFleet + spot)
    gcp.py                 # GCPProvider (bulkInsert + Spot VMs)
    azure.py               # AzureProvider (VMSS Flex + Spot)
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
