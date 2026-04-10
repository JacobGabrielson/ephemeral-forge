# EC2 Fleet Strategies for Perf Testing

Notes on EC2 spot fleet patterns learned from Karpenter's AWS
provider and applied to the iron-proxy multi-agent fleet
launcher.

## CreateFleet > RunInstances

For launching multiple spot instances, `CreateFleet` with
`Type=instant` is strictly better than N individual
`RunInstances` calls:

- **Single API call** for N instances across M instance types
- **price-capacity-optimized** allocation — EC2 picks the best
  (type, AZ) combo based on current spot pool depth and price
- **Partial fulfillment** — if you ask for 10 and only 7 are
  available, you get 7 (with `RunInstances` you get 0 or N)
- **Errors per override** — the response tells you which
  instance types failed and why, so you can adapt

### Key CreateFleet patterns

1. **Launch template + overrides**: Put common config (AMI,
   key pair, SG, EBS) in a launch template.  Put variable
   config (instance type, subnet) in overrides.  This keeps
   the fleet request clean and reusable.

2. **Wide instance type pool**: Offer 6-8 instance types
   across families (t3, t3a, m5, m6i, c5, c6i).  More
   overrides = more spot pools = higher success rate.
   Karpenter uses up to 60 overrides per CreateFleet call.

3. **price-capacity-optimized** allocation: Balances cost and
   availability.  Better than `lowest-price` (which chases
   cheap pools that are often exhausted) or
   `capacity-optimized` (which ignores price entirely).

4. **Spot-only enforcement**: Set
   `DefaultTargetCapacityType=spot` and omit
   `OnDemandTargetCapacity`.  For perf testing we never want
   on-demand — better to fail fast and try another region
   than silently pay 3x.

## Region and AZ Selection

- **Probe before launch**: `describe-spot-price-history` with
  `--start-time=now` gives current spot prices per AZ.  Probe
  2-3 regions and pick the cheapest.
- **Pin to one AZ**: All instances in the same AZ minimizes
  network variance.  Cross-AZ adds ~0.5ms and $0.01/GB.
- **Default VPCs only**: us-west-2 and us-east-2 have default
  VPCs in this account.  us-east-1 and us-west-1 don't —
  they'd need VPC/subnet creation.
- **Retry with different region**: If CreateFleet returns
  `InsufficientInstanceCapacity` for all overrides, try the
  next cheapest region rather than falling back to on-demand.

## Instance Type Strategy for Fleet Agents

For multi-agent load testing we need many distinct IPs, not
raw CPU.  Optimal choices:

| Type      | vCPU | RAM   | Spot ~$/hr | Notes            |
|-----------|------|-------|------------|------------------|
| t3.micro  | 2    | 1 GB  | $0.003     | Cheapest, plenty |
| t3a.micro | 2    | 1 GB  | $0.003     | AMD, same price  |
| t3.small  | 2    | 2 GB  | $0.006     | More headroom    |
| t3a.small | 2    | 2 GB  | $0.005     | AMD variant      |

We also include m5/m6i/c5/c6i.large as fallbacks in case
the t3 spot pools are exhausted.  CreateFleet's allocation
strategy means we won't pay for these unless the small
types are unavailable.

## Warmup Strategy

ECDSA certificate generation on the first request to a new
hostname costs ~22ms.  For accurate benchmarks:

1. **Before each rate step**, send a handful of requests
   (1/s for 2s) from every agent.  This primes the proxy's
   cert cache for all target hostnames.
2. **Vegeta's binary output** includes all requests including
   warmup if you don't separate them.  Our fleet-attack.sh
   runs warmup as a separate vegeta invocation whose output
   is discarded.
3. **Alternative**: Vegeta doesn't have a built-in warmup
   flag.  If running a single vegeta instance, prepend a
   short low-rate phase and trim the first N seconds in
   post-processing.

## Karpenter Patterns Worth Borrowing

Several patterns from Karpenter's codebase
(`~/src/karpenter-provider-aws`) that could improve the
fleet launcher:

1. **Unavailable offerings cache**: Track which (type, AZ)
   combos returned `InsufficientInstanceCapacity` and skip
   them on subsequent launches within the same session.

2. **Spot price ceiling**: Karpenter filters out spot
   offerings more expensive than the cheapest on-demand
   price for the same specs.  For perf testing, a simpler
   hard ceiling ($0.10/hr) achieves the same goal.

3. **Batched fleet requests**: Karpenter batches multiple
   single-instance requests into one CreateFleet call.  For
   our use case, we already batch (N agents in one call),
   but if we needed heterogeneous agents (different sizes
   for loadgen vs SUT), we'd use multiple CreateFleet calls
   with different launch templates.

4. **Graceful partial fulfillment**: If CreateFleet gives
   us 3/5 agents, we can still run the test at reduced
   scale rather than failing.  The fleet-attack.sh script
   handles this by reading whatever IPs are in the state
   directory.

## Tagging Convention

All fleet resources use three tags for tracking and cleanup:

```
Name      = perf-fleet-agent
Purpose   = perf-lab
RunID     = <tag>
```

The `Purpose=perf-lab` tag is shared with the main perf-lab
infrastructure, so `perf-cleanup.sh` can find and clean up
orphaned fleet resources too.

## Cost Control

- **Spot-only**: No on-demand fallback, ever
- **Small instances**: t3.micro at $0.003/hr means 10 agents
  for 1 hour costs $0.03
- **Instant fleets**: `Type=instant` means no persistent fleet
  — instances are launched and that's it, no ongoing fleet
  management overhead
- **Explicit teardown**: `fleet-destroy.sh` terminates
  instances, deletes LT, SG, and key pair
- **8 GB EBS**: Minimum viable root volume (gp3), vs the
  20 GB in the main perf lab
