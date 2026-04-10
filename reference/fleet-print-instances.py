#!/usr/bin/env python3
"""Parse instance-table.txt and write IP files."""
import sys

state_dir = sys.argv[1]
with open(f"{state_dir}/instance-table.txt") as f:
    lines = [l.strip().split("\t") for l in f if l.strip()]

print(f"\n  {'ID':<22} {'Type':<12} {'Private IP':<16} {'Public IP'}")
print(f"  {'─'*22} {'─'*12} {'─'*16} {'─'*16}")
pub_ips, priv_ips = [], []
for parts in lines:
    iid, itype, priv, pub = parts[0], parts[1], parts[2], parts[3]
    pub_d = pub if pub != "None" else "—"
    print(f"  {iid:<22} {itype:<12} {priv:<16} {pub_d}")
    if pub != "None":
        pub_ips.append(pub)
    priv_ips.append(priv)

with open(f"{state_dir}/public-ips", "w") as f:
    f.write("\n".join(pub_ips) + "\n")
with open(f"{state_dir}/private-ips", "w") as f:
    f.write("\n".join(priv_ips) + "\n")
