# IAM Setup for EC2 Spot Builder

Follow these steps in the AWS Console. Total time: ~5 minutes.

## 1. Create the IAM Policy

1. Go to **IAM** in the AWS Console
   - URL: https://console.aws.amazon.com/iam/
2. Click **Policies** in the left sidebar
3. Click **Create policy**
4. Click the **JSON** tab
5. Delete the default content and paste the contents of
   `aws-iam-policy.json` from this directory
6. Click **Next**
7. Name it: `EphemeralForgeSpot` (or whatever you prefer)
8. Click **Create policy**

## 2. Create the IAM User

1. Click **Users** in the left sidebar
2. Click **Create user**
3. Pick a username (e.g., `ephemeral-forge`)
4. Do NOT check "Provide user access to the AWS Management
   Console"
5. Click **Next**
6. Select **Attach policies directly**
7. Search for the policy you created and check the box
8. Click **Next**
9. Click **Create user**

## 3. Create Access Keys

1. Click on the user you just created
2. Click the **Security credentials** tab
3. Scroll down to **Access keys**
4. Click **Create access key**
5. Select **Command Line Interface (CLI)**
6. Check the confirmation checkbox at the bottom
7. Click **Next**, then **Create access key**
8. **IMPORTANT**: Copy both values now (you won't see the
   secret again):
   - Access key ID (looks like: `AKIA...`)
   - Secret access key (looks like: `wJalr...`)

## 4. Configure Credentials

```bash
aws configure --profile YOUR_PROFILE_NAME
```

When prompted, enter:
```
AWS Access Key ID:     <paste the access key ID>
AWS Secret Access Key: <paste the secret access key>
Default region name:   us-east-1
Default output format: json
```

Then set the profile name in `ephemeral-forge.toml`:
```toml
[aws]
profile = "YOUR_PROFILE_NAME"
```

## 5. Verify It Works

```bash
aws --profile YOUR_PROFILE_NAME ec2 describe-regions \
    --query 'Regions[].RegionName' --output table
```

You should see a list of AWS regions.

## 6. GPU Quota (if needed)

GPU spot instance quota defaults to 0 vCPUs. To launch GPU
instances, request an increase:

1. Go to Service Quotas → EC2
2. Search "All G and VT Spot Instance Requests"
3. Request increase to at least 4 vCPUs

---

## Policy Document (for reference)

See `aws-iam-policy.json` in this directory. Key permissions:

- Describe: regions, AZs, spot prices, images, instances,
  VPCs, subnets, security groups, key pairs
- Launch: CreateFleet, CreateLaunchTemplate,
  DeleteLaunchTemplate
- Manage: CreateSecurityGroup, DeleteSecurityGroup,
  AuthorizeSecurityGroupIngress, CreateKeyPair,
  DeleteKeyPair, CreateTags
- Terminate: TerminateInstances

## Cleanup

1. IAM > Users > your user > Delete
2. IAM > Policies > your policy > Delete
3. Remove the profile from `~/.aws/credentials`
