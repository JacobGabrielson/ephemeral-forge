"""AWS provider — CreateFleet + spot instances via boto3."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import boto3

from ephemeral_forge.config import AWSConfig
from ephemeral_forge.provider import (
    FleetConfig,
    FleetResult,
    Instance,
    ProviderBase,
    SpotPrice,
)

logger = logging.getLogger(__name__)

PERSISTENT_SG_NAME = "ephemeral-forge-sg"


class AWSProvider(ProviderBase):
    def __init__(self, config: AWSConfig) -> None:
        self.config = config
        self._session = boto3.Session(profile_name=config.profile)

    def _client(self, service: str, region: str) -> object:
        return self._session.client(service, region_name=region)

    # ── probe_spot_prices ────────────────────────────────────

    def probe_spot_prices(
        self,
        instance_types: list[str],
        regions: list[str] | None = None,
    ) -> list[SpotPrice]:
        if regions is None:
            ec2 = self._client("ec2", "us-east-1")
            resp = ec2.describe_regions(AllRegions=False)
            regions = [r["RegionName"] for r in resp["Regions"]]

        return asyncio.run(self._probe_all_regions(instance_types, regions))

    async def _probe_all_regions(
        self,
        instance_types: list[str],
        regions: list[str],
    ) -> list[SpotPrice]:
        async def probe_one(region: str) -> list[SpotPrice]:
            return await asyncio.to_thread(
                self._probe_region_sync, region, instance_types
            )

        results = await asyncio.gather(
            *[probe_one(r) for r in regions],
            return_exceptions=True,
        )
        prices: list[SpotPrice] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("Probe failed for a region: %s", result)
                continue
            prices.extend(result)
        return prices

    def _probe_region_sync(
        self,
        region: str,
        instance_types: list[str],
    ) -> list[SpotPrice]:
        ec2 = self._client("ec2", region)
        prices: list[SpotPrice] = []
        try:
            resp = ec2.describe_spot_price_history(
                InstanceTypes=instance_types,
                ProductDescriptions=["Linux/UNIX"],
                StartTime=datetime.now(UTC),
                MaxResults=200,
            )
            # Keep only the latest price per (type, zone) pair
            seen: set[tuple[str, str]] = set()
            for item in resp["SpotPriceHistory"]:
                key = (item["InstanceType"], item["AvailabilityZone"])
                if key in seen:
                    continue
                seen.add(key)
                price = float(item["SpotPrice"])
                if self.config.max_spot_price and price > self.config.max_spot_price:
                    continue
                prices.append(
                    SpotPrice(
                        region=region,
                        zone=item["AvailabilityZone"],
                        instance_type=item["InstanceType"],
                        price_per_hour=price,
                    )
                )
        except Exception as e:
            logger.debug("Spot price probe failed for %s: %s", region, e)
        return prices

    # ── resolve_image ────────────────────────────────────────

    def resolve_image(self, region: str, gpu: bool = False) -> str:
        ec2 = self._client("ec2", region)
        if gpu:
            filters = [
                {
                    "Name": "name",
                    "Values": ["Deep Learning AMI GPU PyTorch *Ubuntu 22.04*"],
                },
                {"Name": "state", "Values": ["available"]},
            ]
            owners = ["amazon"]
        else:
            filters = [
                {
                    "Name": "name",
                    "Values": [
                        "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
                    ],
                },
                {"Name": "state", "Values": ["available"]},
            ]
            owners = ["099720109477"]  # Canonical

        resp = ec2.describe_images(Owners=owners, Filters=filters)
        images = sorted(resp["Images"], key=lambda i: i["CreationDate"], reverse=True)
        if not images:
            raise RuntimeError(f"No suitable AMI found in {region} (gpu={gpu})")
        return images[0]["ImageId"]

    # ── persistent infrastructure ────────────────────────────

    def _ensure_security_group(self, region: str, purpose_tag: str) -> tuple[str, str]:
        """Find or create the persistent security group.

        Returns (sg_id, vpc_id).
        """
        ec2 = self._client("ec2", region)

        # Look for existing SG
        resp = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [PERSISTENT_SG_NAME]},
            ]
        )
        if resp["SecurityGroups"]:
            sg = resp["SecurityGroups"][0]
            logger.info("Reusing security group %s", sg["GroupId"])
            return sg["GroupId"], sg["VpcId"]

        # Need to create — get default VPC first
        vpc_id, vpc_cidr = self._get_default_vpc(ec2)

        sg_resp = ec2.create_security_group(
            GroupName=PERSISTENT_SG_NAME,
            Description="ephemeral-forge fleet instances",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": PERSISTENT_SG_NAME},
                        {"Key": "Purpose", "Value": purpose_tag},
                    ],
                }
            ],
        )
        sg_id = sg_resp["GroupId"]

        # SSH from anywhere (ephemeral instances) + all internal
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
                },
                {
                    "IpProtocol": "-1",
                    "IpRanges": [
                        {
                            "CidrIp": vpc_cidr,
                            "Description": "VPC internal",
                        }
                    ],
                },
            ],
        )
        # Also allow all traffic from the SG itself (fleet-to-fleet)
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "-1",
                    "UserIdGroupPairs": [
                        {
                            "GroupId": sg_id,
                            "Description": "Self",
                        }
                    ],
                }
            ],
        )
        logger.info("Created security group %s in %s", sg_id, region)
        return sg_id, vpc_id

    @staticmethod
    def _get_default_vpc(ec2: object) -> tuple[str, str]:
        """Return (vpc_id, vpc_cidr) for the default VPC."""
        resp = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
        if not resp["Vpcs"]:
            raise RuntimeError("No default VPC found")
        vpc = resp["Vpcs"][0]
        return vpc["VpcId"], vpc["CidrBlock"]

    def _get_subnets(
        self,
        ec2: object,
        vpc_id: str,
        zone: str | None = None,
    ) -> list[str]:
        """Get subnet IDs in the default VPC."""
        filters = [{"Name": "vpc-id", "Values": [vpc_id]}]
        if zone:
            filters.append({"Name": "availability-zone", "Values": [zone]})
        resp = ec2.describe_subnets(Filters=filters)
        return [s["SubnetId"] for s in resp["Subnets"]]

    # ── launch_fleet ─────────────────────────────────────────

    def launch_fleet(
        self,
        config: FleetConfig,
        run_id: str,
        region: str,
        zone: str | None = None,
    ) -> FleetResult:
        ec2 = self._client("ec2", region)
        purpose_tag = config.tags.get("Purpose", "ephemeral-forge")

        # SSH key — use CreateKeyPair (AWS generates the key)
        key_name = f"ef-{run_id}"
        key_resp = ec2.create_key_pair(
            KeyName=key_name,
            KeyType="ed25519",
            TagSpecifications=[
                {
                    "ResourceType": "key-pair",
                    "Tags": [
                        {"Key": "Purpose", "Value": purpose_tag},
                        {"Key": "RunID", "Value": run_id},
                    ],
                }
            ],
        )
        private_pem = key_resp["KeyMaterial"]

        # Security group (persistent)
        sg_id, vpc_id = self._ensure_security_group(region, purpose_tag)

        # Image
        image_id = config.image or self.resolve_image(region)

        # Subnets
        subnets = self._get_subnets(ec2, vpc_id, zone)
        if not subnets:
            raise RuntimeError(
                f"No subnets in VPC {vpc_id}" + (f" zone {zone}" if zone else "")
            )

        # Launch template
        lt_name = f"ef-{run_id}"
        instance_tags = [
            {"Key": "Name", "Value": f"ef-{run_id}"},
            *[{"Key": k, "Value": v} for k, v in config.tags.items()],
        ]
        lt_resp = ec2.create_launch_template(
            LaunchTemplateName=lt_name,
            LaunchTemplateData={
                "ImageId": image_id,
                "KeyName": key_name,
                "SecurityGroupIds": [sg_id],
                "BlockDeviceMappings": [
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": config.disk_gb,
                            "VolumeType": "gp3",
                        },
                    }
                ],
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": instance_tags,
                    },
                    {
                        "ResourceType": "volume",
                        "Tags": instance_tags,
                    },
                ],
            },
            TagSpecifications=[
                {
                    "ResourceType": "launch-template",
                    "Tags": [
                        {"Key": "Purpose", "Value": purpose_tag},
                        {"Key": "RunID", "Value": run_id},
                    ],
                }
            ],
        )
        lt_id = lt_resp["LaunchTemplate"]["LaunchTemplateId"]

        # Overrides: instance_type × subnet (Karpenter pattern)
        overrides = [
            {"InstanceType": itype, "SubnetId": sid}
            for itype in config.instance_types
            for sid in subnets
        ]

        # CreateFleet — spot only, price-capacity-optimized
        fleet_req: dict[str, object] = {
            "Type": "instant",
            "TargetCapacitySpecification": {
                "TotalTargetCapacity": config.count,
                "DefaultTargetCapacityType": "spot",
            },
            "SpotOptions": {
                "AllocationStrategy": "price-capacity-optimized",
            },
            "LaunchTemplateConfigs": [
                {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": lt_id,
                        "Version": "$Default",
                    },
                    "Overrides": overrides,
                }
            ],
        }

        logger.info(
            "CreateFleet: %d instances, %d type×subnet overrides",
            config.count,
            len(overrides),
        )
        fleet_resp = ec2.create_fleet(**fleet_req)

        # Extract instance IDs
        instance_ids: list[str] = []
        for inst_set in fleet_resp.get("Instances", []):
            instance_ids.extend(inst_set.get("InstanceIds", []))

        errors: list[str] = []
        for err in fleet_resp.get("Errors", []):
            errors.append(f"{err.get('ErrorCode')}: {err.get('ErrorMessage')}")

        if not instance_ids:
            # Clean up key + LT before raising
            self._cleanup_key_and_lt(ec2, key_name, lt_id)
            raise RuntimeError(f"No instances launched. Errors: {'; '.join(errors)}")

        logger.info("Launched %d/%d instances", len(instance_ids), config.count)
        return FleetResult(
            provider="aws",
            run_id=run_id,
            region=region,
            instances=[
                Instance(id=iid, instance_type="", zone="") for iid in instance_ids
            ],
            errors=errors,
            private_key_pem=private_pem,
            teardown_handles={
                "key_name": key_name,
                "launch_template_id": lt_id,
                "instance_ids": instance_ids,
                "region": region,
            },
        )

    # ── wait_until_ready ─────────────────────────────────────

    def wait_until_ready(
        self,
        result: FleetResult,
        timeout: int = 300,
    ) -> FleetResult:
        ec2 = self._client("ec2", result.region)
        instance_ids = [i.id for i in result.instances]

        waiter = ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=instance_ids,
            WaiterConfig={
                "Delay": 5,
                "MaxAttempts": max(timeout // 5, 1),
            },
        )

        # Collect details
        resp = ec2.describe_instances(InstanceIds=instance_ids)
        instances: list[Instance] = []
        for reservation in resp["Reservations"]:
            for inst in reservation["Instances"]:
                instances.append(
                    Instance(
                        id=inst["InstanceId"],
                        instance_type=inst["InstanceType"],
                        zone=inst["Placement"]["AvailabilityZone"],
                        public_ip=inst.get("PublicIpAddress"),
                        private_ip=inst.get("PrivateIpAddress", ""),
                    )
                )

        result.instances = instances
        return result

    # ── teardown ─────────────────────────────────────────────

    def teardown(self, result: FleetResult) -> None:
        handles = result.teardown_handles
        region = handles.get("region", result.region)
        ec2 = self._client("ec2", region)

        # Terminate instances
        instance_ids = handles.get("instance_ids", [])
        if instance_ids:
            logger.info("Terminating %d instances...", len(instance_ids))
            ec2.terminate_instances(InstanceIds=instance_ids)
            try:
                waiter = ec2.get_waiter("instance_terminated")
                waiter.wait(
                    InstanceIds=instance_ids,
                    WaiterConfig={"Delay": 5, "MaxAttempts": 60},
                )
            except Exception:
                logger.warning("Timeout waiting for termination, continuing")

        self._cleanup_key_and_lt(
            ec2,
            handles.get("key_name"),
            handles.get("launch_template_id"),
        )
        logger.info("Teardown complete for %s", result.run_id)

    @staticmethod
    def _cleanup_key_and_lt(
        ec2: object,
        key_name: str | None,
        lt_id: str | None,
    ) -> None:
        if lt_id:
            logger.info("Deleting launch template %s", lt_id)
            try:
                ec2.delete_launch_template(LaunchTemplateId=lt_id)
            except Exception as e:
                logger.warning("Failed to delete launch template: %s", e)
        if key_name:
            logger.info("Deleting key pair %s", key_name)
            try:
                ec2.delete_key_pair(KeyName=key_name)
            except Exception as e:
                logger.warning("Failed to delete key pair: %s", e)
