# ephemeral-forge

Spin up massive compute fleets in seconds, run your workload,
tear them down. Cheap, fast, disposable infrastructure.

## Vision

ephemeral-forge should work across cloud providers — AWS, GCP,
Azure, OCI, and others. The core abstraction is the same
everywhere: request N machines with specific capabilities
(CPU, GPU, memory), get them fast and cheap using preemptible
/ spot capacity, run your workload, tear everything down
cleanly.

AWS (CreateFleet + spot) is the first implementation. Other
providers will follow the same pattern using their native
preemptible compute APIs.

## Status

Early development. Currently rewriting original bash fleet
scripts into a proper Python library and CLI.

## Design Principles

- **Preemptible only.** Spot, preemptible VMs, excess capacity
  — whatever the cloud calls it. Never on-demand.
- **Fail fast.** No capacity? Fail with a clear error. Don't
  retry forever or fall back to expensive alternatives.
- **Clean up everything.** No orphaned resources, ever.
- **Cost-aware.** Probe prices before launching. Log estimated
  cost. Use the cheapest viable option.
- **Wide instance pools.** Offer many instance types and all
  zones. Let the cloud pick the best combo.

## License

MIT
