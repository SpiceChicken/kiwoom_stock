#!/usr/bin/env bash
set -Eeuo pipefail

# Exact AWS CLI execution artifact for the reviewed clean-rebuild contract.
# Default/--check is local-only and performs no AWS calls. --apply is the only
# write mode and intentionally never performs automatic cleanup.
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REGION="ap-northeast-2"
readonly AZ="ap-northeast-2a"
readonly VPC_CIDR="10.77.0.0/20"
readonly SUBNET_CIDR="10.77.0.0/24"
readonly AMI_ID="ami-05fa22e12f2cb12aa"
readonly CANONICAL_OWNER_ID="099720109477"
readonly PROJECT_TAG="kiwoom-stock"
readonly ENV_TAG="production"

MODE=check
PROFILE=""
REQUESTED_REGION=""
INSTANCE_PROFILE=""
STATE_FILE=""
CONFIRM_NETWORK=0
CONFIRM_EIP=0
CONFIRM_EC2=0

usage() {
  echo "usage: $0 [--check] | --apply --profile NAME --region ap-northeast-2 --instance-profile NAME --state-file ABSOLUTE_PATH --confirm-network-write --confirm-eip-cost --confirm-ec2-cost" >&2
}

while (($#)); do
  case "$1" in
    --check) MODE=check ;;
    --apply) MODE=apply ;;
    --profile) shift; PROFILE="${1:-}" ;;
    --region) shift; REQUESTED_REGION="${1:-}" ;;
    --instance-profile) shift; INSTANCE_PROFILE="${1:-}" ;;
    --state-file) shift; STATE_FILE="${1:-}" ;;
    --confirm-network-write) CONFIRM_NETWORK=1 ;;
    --confirm-eip-cost) CONFIRM_EIP=1 ;;
    --confirm-ec2-cost) CONFIRM_EC2=1 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

local_check() {
  command -v bash >/dev/null
  test -r "$SCRIPT_DIR/cloud-init-ubuntu-24.04.sh"
  bash -n "$SCRIPT_DIR/cloud-init-ubuntu-24.04.sh"
  python3 -m json.tool "$SCRIPT_DIR/network-spec.json.example" >/dev/null
  python3 -m json.tool "$SCRIPT_DIR/launch-spec.json.example" >/dev/null
  echo "clean-rebuild check passed (no AWS calls or writes)"
}

if [[ "$MODE" == check ]]; then
  local_check
  exit 0
fi

[[ -n "$PROFILE" && -n "$REQUESTED_REGION" && -n "$INSTANCE_PROFILE" && -n "$STATE_FILE" ]] || {
  usage
  exit 2
}
[[ "$REQUESTED_REGION" == "$REGION" ]] || {
  echo "region must be exactly $REGION" >&2
  exit 2
}
[[ "$CONFIRM_NETWORK" == 1 && "$CONFIRM_EIP" == 1 && "$CONFIRM_EC2" == 1 ]] || {
  echo "all three separate network/EIP/EC2 confirmations are required" >&2
  exit 2
}
[[ "$STATE_FILE" == /* ]] || {
  echo "--state-file must be an absolute path" >&2
  exit 2
}
[[ -d "$(dirname "$STATE_FILE")" ]] || {
  echo "--state-file parent directory must already exist" >&2
  exit 2
}
command -v aws >/dev/null
command -v python3 >/dev/null
local_check

VPC_ID=""
SUBNET_ID=""
IGW_ID=""
ROUTE_TABLE_ID=""
ROUTE_ASSOCIATION_ID=""
SG_ID=""
EIP_ALLOCATION_ID=""
EIP_ASSOCIATION_ID=""
ENI_ID=""
INSTANCE_ID=""

write_evidence() {
  local result="$1"
  umask 077
  {
    printf 'result=%s\n' "$result"
    printf 'region=%s\n' "$REGION"
    printf 'vpc_id=%s\n' "$VPC_ID"
    printf 'subnet_id=%s\n' "$SUBNET_ID"
    printf 'internet_gateway_id=%s\n' "$IGW_ID"
    printf 'route_table_id=%s\n' "$ROUTE_TABLE_ID"
    printf 'route_association_id=%s\n' "$ROUTE_ASSOCIATION_ID"
    printf 'security_group_id=%s\n' "$SG_ID"
    printf 'eip_allocation_id=%s\n' "$EIP_ALLOCATION_ID"
    printf 'eip_association_id=%s\n' "$EIP_ASSOCIATION_ID"
    printf 'network_interface_id=%s\n' "$ENI_ID"
    printf 'instance_id=%s\n' "$INSTANCE_ID"
  } > "$STATE_FILE"
  chmod 0600 "$STATE_FILE"
}

on_error() {
  local line="$1"
  write_evidence "failed_at_line_${line}"
  echo "clean-rebuild failed; no automatic cleanup was attempted" >&2
  echo "review exact IDs in $STATE_FILE before approved reverse-order cleanup" >&2
}
trap 'on_error "$LINENO"' ERR

aws_ec2() {
  aws --profile "$PROFILE" --region "$REGION" ec2 "$@"
}

tag_resource() {
  aws_ec2 create-tags --resources "$1" --tags \
    "Key=Project,Value=$PROJECT_TAG" "Key=Environment,Value=$ENV_TAG" \
    "Key=ManagedBy,Value=deploy-ec2-clean-rebuild"
}

# Read-only AMI supply-chain and launch-contract gate.
AMI_FACTS="$(aws_ec2 describe-images --image-ids "$AMI_ID" --owners "$CANONICAL_OWNER_ID" \
  --query 'Images[0].[State,Architecture,VirtualizationType,RootDeviceType,RootDeviceName]' \
  --output text)"
[[ "$AMI_FACTS" == $'available\tx86_64\thvm\tebs\t/dev/sda1' ]] || {
  echo "AMI contract mismatch; launch refused" >&2
  exit 1
}

VPC_ID="$(aws_ec2 create-vpc --cidr-block "$VPC_CIDR" \
  --tag-specifications "ResourceType=vpc,Tags=[{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENV_TAG},{Key=ManagedBy,Value=deploy-ec2-clean-rebuild}]" \
  --query 'Vpc.VpcId' --output text)"
aws_ec2 wait vpc-available --vpc-ids "$VPC_ID"
aws_ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}'
aws_ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'

SUBNET_ID="$(aws_ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$SUBNET_CIDR" \
  --availability-zone "$AZ" --query 'Subnet.SubnetId' --output text)"
tag_resource "$SUBNET_ID"
aws_ec2 modify-subnet-attribute --subnet-id "$SUBNET_ID" \
  --no-map-public-ip-on-launch

IGW_ID="$(aws_ec2 create-internet-gateway \
  --query 'InternetGateway.InternetGatewayId' --output text)"
tag_resource "$IGW_ID"
aws_ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" \
  --vpc-id "$VPC_ID"

ROUTE_TABLE_ID="$(aws_ec2 create-route-table --vpc-id "$VPC_ID" \
  --query 'RouteTable.RouteTableId' --output text)"
tag_resource "$ROUTE_TABLE_ID"
aws_ec2 create-route --route-table-id "$ROUTE_TABLE_ID" \
  --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID" >/dev/null
ROUTE_ASSOCIATION_ID="$(aws_ec2 associate-route-table \
  --route-table-id "$ROUTE_TABLE_ID" --subnet-id "$SUBNET_ID" \
  --query 'AssociationId' --output text)"

SG_ID="$(aws_ec2 create-security-group --group-name kiwoom-stock-production \
  --description 'Kiwoom production HTTPS egress only' --vpc-id "$VPC_ID" \
  --query 'GroupId' --output text)"
tag_resource "$SG_ID"
# CreateSecurityGroup adds allow-all IPv4 egress. Revoke it before adding 443.
aws_ec2 revoke-security-group-egress --group-id "$SG_ID" \
  --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
aws_ec2 authorize-security-group-egress --group-id "$SG_ID" \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'

SG_JSON="$(aws_ec2 describe-security-groups --group-ids "$SG_ID" --output json)"
SG_READBACK_JSON="$SG_JSON" python3 - "$SG_ID" <<'PY'
import json
import os
import sys

group_id = sys.argv[1]
data = json.loads(os.environ["SG_READBACK_JSON"])
groups = data.get("SecurityGroups")
if not isinstance(groups, list) or len(groups) != 1:
    raise SystemExit(f"security group {group_id} read-back count mismatch")
group = groups[0]
expected = [{
    "IpProtocol": "tcp",
    "FromPort": 443,
    "ToPort": 443,
    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    "Ipv6Ranges": [],
    "PrefixListIds": [],
    "UserIdGroupPairs": [],
}]
if group.get("IpPermissions") != [] or group.get("IpPermissionsEgress") != expected:
    raise SystemExit(f"security group {group_id} is not ingress=0/egress=tcp443")
PY

# Eliminate the EIP/user-data race: pre-create ENI, associate EIP, verify the
# association, and only then attach that ENI while launching the instance.
ENI_ID="$(aws_ec2 create-network-interface --subnet-id "$SUBNET_ID" \
  --groups "$SG_ID" --description 'Kiwoom production primary ENI' \
  --query 'NetworkInterface.NetworkInterfaceId' --output text)"
tag_resource "$ENI_ID"
EIP_ALLOCATION_ID="$(aws_ec2 allocate-address --domain vpc \
  --query 'AllocationId' --output text)"
tag_resource "$EIP_ALLOCATION_ID"
EIP_ASSOCIATION_ID="$(aws_ec2 associate-address --allocation-id "$EIP_ALLOCATION_ID" \
  --network-interface-id "$ENI_ID" --query 'AssociationId' --output text)"
ASSOCIATED_ENI="$(aws_ec2 describe-addresses --allocation-ids "$EIP_ALLOCATION_ID" \
  --query 'Addresses[0].NetworkInterfaceId' --output text)"
[[ "$ASSOCIATED_ENI" == "$ENI_ID" ]] || {
  echo "EIP association read-back mismatch; launch refused" >&2
  false
}

INSTANCE_ID="$(aws_ec2 run-instances \
  --image-id "$AMI_ID" --instance-type t3.micro \
  --credit-specification CpuCredits=standard \
  --iam-instance-profile "Name=$INSTANCE_PROFILE" \
  --network-interfaces "DeviceIndex=0,NetworkInterfaceId=$ENI_ID" \
  --metadata-options "HttpEndpoint=enabled,HttpTokens=required,HttpPutResponseHopLimit=1,InstanceMetadataTags=disabled" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeType":"gp3","VolumeSize":8,"Encrypted":true,"DeleteOnTermination":true,"Iops":3000,"Throughput":125}}]' \
  --user-data "file://$SCRIPT_DIR/cloud-init-ubuntu-24.04.sh" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENV_TAG},{Key=ManagedBy,Value=deploy-ec2-clean-rebuild}]" \
  "ResourceType=volume,Tags=[{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENV_TAG},{Key=ManagedBy,Value=deploy-ec2-clean-rebuild}]" \
  --min-count 1 --max-count 1 --query 'Instances[0].InstanceId' --output text)"
aws_ec2 wait instance-running --instance-ids "$INSTANCE_ID"
write_evidence "instance_running"
trap - ERR
echo "instance launched; inspect $STATE_FILE and verify SSM/cloud-init before any application action"
