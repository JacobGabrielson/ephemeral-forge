# Requirements — ephemeral-forge v1

## Overview

ephemeral-forge launches fleets of cheap, preemptible cloud
instances, runs a workload, and tears everything down. v1
delivers a working AWS implementation behind a provider
abstraction that GCP and Azure can plug into without
rewriting the core.

## Functional Requirements

### FR-1: Launch a spot fleet

Given a count and optional flags (provider, region, GPU,
instance types), the tool must:

1. Probe spot prices across all available regions (or a
   user-specified subset) in parallel.
2. Select the cheapest viable region/zone.
3. Resolve the correct base image (Ubuntu 24.04; for GPU:
   Deep Learning AMI with NVIDIA drivers).
4. Create all prerequisites (SSH key pair, security
   group / firewall rule, launch template / network).
5. Launch N preemptible instances using the cloud's batch
   fleet API (CreateFleet for AWS).
6. Wait until all instances are running and have public IPs.
7. Save fleet state to disk for later teardown.
8. Print a summary table of instances (ID, type, zone, IP).

### FR-2: Destroy a fleet

Given a run ID (or `--all`), the tool must:

1. Load saved fleet state from disk.
2. Terminate all instances.
3. Delete all prerequisites created during launch (launch
   template, security group, key pair, etc.).
4. Handle partial state gracefully (e.g., instances already
   terminated).

### FR-3: Show fleet status

List all tracked fleets with their run ID, provider, region,
instance count, and state (running / terminated / unknown).

### FR-4: Configuration via TOML

All account-specific settings live in `ephemeral-forge.toml`
(gitignored). The tool must:

- Load config from `ephemeral-forge.toml` in the current
  directory or a path given via `--config`.
- Fall back to sensible defaults if keys are missing.
- Support per-provider sections (`[aws]`, `[gcp]`,
  `[azure]`) and a `[general]` section.

### FR-5: SSH key generation without shell-out

Generate Ed25519 SSH key pairs using the `cryptography`
library. Never call `ssh-keygen`. Inject the public key via
the provider's native mechanism (AWS key pair API, GCP
instance metadata, Azure OS profile).

### FR-6: Parallel region probing

Probe all regions worldwide by default using
`asyncio.to_thread` to parallelize synchronous SDK calls.
Users can restrict to specific regions in config.

For AWS: call `ec2.describe_regions()` to get the full
region list, then `describe_spot_price_history()` across all
of them concurrently.

### FR-7: Resource tagging and cleanup

Tag all created resources with:
- `Purpose` = value from config (default `ephemeral-forge`)
- `RunID` = unique run identifier

Always clean up on exit via `try/finally`. No orphaned
resources, ever.

### FR-8: Reusable infrastructure

Distinguish between **per-run resources** (instances, launch
templates, key pairs) and **persistent infrastructure** (VPCs,
subnets, firewall rules, IAM roles) that costs nothing to
keep around.

On first launch, create any missing persistent infra and tag
it with `Purpose=ephemeral-forge`. On subsequent launches,
discover and reuse existing infra by tag. On teardown, only
destroy per-run resources — leave persistent infra in place
so future launches are faster.

Provide an explicit `ef infra setup [--provider aws|gcp|azure]`
command that pre-creates all persistent infrastructure, and
`ef infra teardown` to remove it when no longer needed.

What counts as persistent vs per-run:

| Resource           | AWS           | GCP              | Azure             |
|--------------------|---------------|------------------|-------------------|
| **Persistent**     | VPC, subnets, IAM role | Firewall rules | VNet, subnet, NSG |
| **Per-run**        | Key pair, SG, launch template, instances | SSH metadata, instances | Resource group (contains everything per-run) |

For AWS specifically: the default VPC works, but a dedicated
`ephemeral-forge` security group with SSH-from-anywhere rules
can be reused across runs rather than recreated each time.
Only the key pair and instances are truly per-run.

### FR-9: Launch time tracking and provider scoring

Record timing data for every fleet launch:

- **Timestamps**: API call start, fleet created, all
  instances running, first instance SSH-ready, all
  instances SSH-ready.
- **Metadata**: provider, region, zone, instance types
  requested, count requested vs fulfilled.

Store this history in `~/.ephemeral-forge/history.json`
(append-only log of past launches).

Use the history to adjust provider/region selection. When
comparing options during region probing:

1. Compute a **time-adjusted cost** for each candidate:
   `effective_cost = spot_price + (median_launch_seconds / 3600) * spot_price`
   (i.e., you're paying for the time the instance is
   booting but not yet usable).
2. If a provider/region is consistently slower by more than
   a configurable threshold (default: 60 seconds), penalize
   it in ranking — but only if the faster alternative is
   within some cost margin (default: 20% more expensive).
   A provider that's 30 seconds slower but 50% cheaper
   still wins.

The `ef status` command should include launch time stats.
`ef history` should show past launches with timings.

## Non-Functional Requirements

### NFR-1: Python only

All code is Python. No bash scripts in the main package.
The `reference/` directory contains bash scripts for design
reference only.

### NFR-2: No shell-outs

Use native Python libraries for all operations:
- `boto3` for AWS (not `aws` CLI)
- `cryptography` for key generation (not `ssh-keygen`)
- `paramiko` for SSH (not `ssh` command)

### NFR-3: Spot only

Never launch on-demand instances. Never fall back to
on-demand. If there is no spot capacity, fail with a clear
error message.

### NFR-4: Batch fleet APIs only

Always use the cloud's batch fleet API:
- AWS: `CreateFleet` with `Type=instant`
- GCP: `bulkInsert` (future)
- Azure: VMSS Flex (future)

Never use single-instance launch APIs (e.g., `RunInstances`).

### NFR-5: Fail fast

If no capacity is available, fail immediately with a clear
error. Do not retry indefinitely or fall back to expensive
alternatives.

### NFR-6: Code quality

- All code passes `ruff format` and `ruff check`.
- Type hints on all function signatures.
- No `# noqa` without an explanatory comment.

## Out of Scope for v1

- GCP provider implementation (stub only).
- Azure provider implementation (stub only).
- SSH into instances (`ef ssh` command).
- Workload execution (setup, run, collect results).
- GPU-specific image resolution and quota checks.
- Cross-provider cost comparison ("cheapest anywhere").
- `ef run` one-shot command (launch + execute + teardown).
