import json
import subprocess
from pathlib import Path


NETWORK = Path("deploy/ec2/network-spec.json.example")
LAUNCH = Path("deploy/ec2/launch-spec.json.example")
APPLY = Path("deploy/ec2/apply_clean_rebuild.sh")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_network_contract_is_minimal_and_https_only():
    spec = _read(NETWORK)
    assert spec["vpc"] == {
        "cidrBlock": "10.77.0.0/20",
        "enableDnsSupport": True,
        "enableDnsHostnames": True,
    }
    assert spec["subnet"] == {
        "cidrBlock": "10.77.0.0/24",
        "availabilityZone": "ap-northeast-2a",
        "mapPublicIpOnLaunch": False,
    }
    assert spec["securityGroup"]["ingress"] == []
    assert spec["securityGroup"]["egress"] == [
        {
            "ipProtocol": "tcp",
            "fromPort": 443,
            "toPort": 443,
            "cidrIpv4": "0.0.0.0/0",
        }
    ]
    assert all(value == 0 for value in spec["prohibitedResources"].values())


def test_network_has_one_igw_default_route_and_separate_eip():
    spec = _read(NETWORK)
    assert spec["internetGateway"] == {"count": 1, "attachToVpc": True}
    assert spec["routeTable"]["routes"] == [
        {"destinationCidrBlock": "0.0.0.0/0", "target": "internetGateway"}
    ]
    assert spec["elasticIp"]["count"] == 1
    association = spec["elasticIp"]["association"]
    assert association == "precreatedPrimaryEniBeforeLaunch"


def test_launch_contract_is_t3_micro_standard_and_hardened():
    spec = _read(LAUNCH)
    assert spec["imageId"] == "ami-05fa22e12f2cb12aa"
    assert spec["instanceType"] == "t3.micro"
    assert spec["creditSpecification"] == {"cpuCredits": "standard"}
    assert spec["keyPair"] == "omitted"
    assert spec["network"]["associatePublicIpAddress"] is False
    assert spec["metadataOptions"]["httpTokens"] == "required"
    assert spec["metadataOptions"]["httpPutResponseHopLimit"] == 1


def test_launch_has_exact_root_volume_and_one_separate_eip():
    spec = _read(LAUNCH)
    assert len(spec["blockDeviceMappings"]) == 1
    ebs = spec["blockDeviceMappings"][0]["ebs"]
    assert ebs == {
        "volumeType": "gp3",
        "volumeSizeGiB": 8,
        "encrypted": True,
        "deleteOnTermination": True,
        "iops": 3000,
        "throughputMiBps": 125,
    }
    assert spec["elasticIp"]["count"] == 1
    assert spec["elasticIp"]["associateToPrecreatedEniBeforeLaunch"] is True
    assert spec["automaticApplicationStart"] is False


def test_specs_contain_no_secret_or_automatic_order_activation():
    combined = NETWORK.read_text() + LAUNCH.read_text()
    forbidden_values = (
        "KIWOOM_APP_KEY",
        "KIWOOM_SECRET_KEY",
        "docker compose up",
    )
    for forbidden in forbidden_values:
        assert forbidden not in combined


def test_executable_artifact_defaults_to_local_check_only():
    result = subprocess.run(
        [str(APPLY), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "no AWS calls or writes" in result.stdout


def test_apply_artifact_revokes_default_egress_and_checks_postcondition():
    text = APPLY.read_text(encoding="utf-8")
    revoke = text.index("revoke-security-group-egress")
    authorize = text.index("authorize-security-group-egress")
    read_back = text.index("describe-security-groups")
    launch = text.index("run-instances")
    assert revoke < authorize < read_back < launch
    assert '"IpProtocol":"-1"' in text
    assert "group.get(\"IpPermissions\") != []" in text
    assert "group.get(\"IpPermissionsEgress\") != expected" in text


def test_apply_artifact_preassociates_eip_to_eni_before_launch():
    text = APPLY.read_text(encoding="utf-8")
    create_eni = text.index("create-network-interface")
    allocate_eip = text.index("allocate-address")
    associate_eip = text.index("associate-address")
    verify_eip = text.index("describe-addresses")
    launch = text.index("run-instances")
    assert create_eni < allocate_eip < associate_eip < verify_eip < launch
    network_interface_arg = (
        '--network-interfaces "DeviceIndex=0,NetworkInterfaceId=$ENI_ID"'
    )
    assert network_interface_arg in text


def test_apply_artifact_has_exact_launch_and_safe_failure_contract():
    text = APPLY.read_text(encoding="utf-8")
    required = (
        "--instance-type t3.micro",
        "--credit-specification CpuCredits=standard",
        '"VolumeType":"gp3"',
        '"VolumeSize":8',
        '"Encrypted":true',
        "HttpTokens=required",
        "HttpPutResponseHopLimit=1",
        "--user-data",
        "no automatic cleanup was attempted",
        "--region",
        "--confirm-network-write",
        "--confirm-eip-cost",
        "--confirm-ec2-cost",
    )
    for value in required:
        assert value in text
    assert "--key-name" not in text
    assert "docker compose up" not in text
