# ephemeral-forge

Spin up massive compute fleets in seconds, run your workload,
tear them down. Cheap, fast, disposable infrastructure.

## Language

**Python only.** No bash scripts. All tooling, CLI, and
library code must be Python. The `reference/` directory
contains the original bash scripts from iron-proxy perfspace
for design reference only — they must be rewritten in Python.

Use `click` or `typer` for CLI. Use `boto3` for AWS. Use
`asyncio` where concurrency helps (e.g., parallel SSH).

## Multi-Cloud

AWS is the first implementation. The design should
accommodate GCP, Azure, OCI, and others. Keep
cloud-specific code behind a provider abstraction so
adding new clouds doesn't require rewriting the core.

## EC2 / AWS (first provider)

- **Always use CreateFleet**, never RunInstances. No fallbacks.
  No exceptions.
- **Always use spot**, never on-demand. No fallbacks. Better
  to fail and try another region than silently pay 3x.
- Use `price-capacity-optimized` allocation strategy.
- Wide instance type pool + all subnets (Karpenter pattern).
- Tag all resources with `Purpose=ephemeral-forge` and a
  unique `RunID` for cleanup.
- Always clean up on exit (trap equivalent in Python:
  `try/finally` or `atexit`).

## Design Principles

- **Spot-only, always.** On-demand is never acceptable.
- **CreateFleet, always.** RunInstances is never acceptable.
- **Clean up everything.** Instances, launch templates, key
  pairs, security groups. No orphaned resources, ever.
- **Cost-aware.** Probe spot prices before launching. Log
  estimated cost. Use the cheapest viable option.
- **Fail fast.** If no spot capacity, fail immediately with
  a clear error. Don't retry forever or fall back to
  expensive alternatives.
- **Wide instance pools.** Offer many instance types and all
  AZs. Let EC2's allocation strategy pick the best combo.

## Project Structure

```
ephemeral_forge/        # Python package
  cli.py                # CLI entry point
  fleet.py              # CreateFleet wrapper
  ssh.py                # SSH/SCP helpers
  cleanup.py            # Resource cleanup
  pricing.py            # Spot price probing
reference/              # Original bash scripts (read-only)
tests/                  # Tests
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

## Key Patterns from iron-proxy perfspace

See `reference/ec2-fleet-strategies.md` for detailed notes on:

- CreateFleet vs RunInstances
- price-capacity-optimized allocation
- Wide instance type pools (Karpenter pattern)
- Region/AZ selection via spot price probing
- Tagging and cleanup conventions
- Cost control strategies
