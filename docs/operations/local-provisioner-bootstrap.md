# 로컬 AWS provisioner 1회 bootstrap

반복적인 EC2 재생성·네트워크 적용을 매번 수동 콘솔에서 수행하지 않도록
`kiwoom-local-provisioner` 역할을 한 번 만든다. 이 역할은 일반 운영 role이나
IAM 관리자 role이 아니다.

## 권한 경계

허용 범위:

- 서울 리전의 reviewed clean-rebuild contract에 필요한 EC2/VPC/ENI/EIP/SG
  생성·구성·read-back;
- Canonical 고정 AMI, `t3.micro`, IMDSv2 required 조건의 instance launch;
- 정확한 `kiwoom-stock-ec2-role`에 대한 `iam:PassRole`;
- 기존 AWS command 결과와 SSM Agent 상태의 조회가 아니라, EC2 재생성 자체에
  필요한 control-plane API만 수행.

금지 범위:

- IAM role/policy/trust 변경;
- EC2 terminate/delete, EIP release, VPC 삭제;
- SSM Session Manager, `ssm:SendCommand`, Parameter Store 값 조회;
- Kiwoom credential, 주문·취소·계좌 capability.

따라서 IAM 정책 교체 같은 계정 권한 변경은 계속 관리자 1회 작업으로 남긴다.
자동화 범위는 인프라 적용이며, 파괴적 철거와 IAM privilege escalation을
자동화하지 않는다.

## 관리자 1회 적용

관리자 권한이 실제로 있는 세션에서만 수행한다. 현재 `kiwoom-admin-bootstrap`
프로필이 `kiwoom-local-user`를 반환하면 관리자 세션이 아니므로 중단한다.

```bash
aws --profile <ADMIN_PROFILE> sts get-caller-identity

.venv/bin/python deploy/bootstrap_local_provisioner.py \
  --profile <ADMIN_PROFILE> \
  --account-id 380648615401 \
  --update-reviewed-policy
```

bootstrap은 다음을 create-or-reuse하고 exact read-back한다.

- trust: `deploy/iam/local-provisioner-trust-policy.json.example`
- role: `kiwoom-local-provisioner`
- inline policy: `KiwoomLocalProvisioner`
- `kiwoom-local-user`의 기존 AssumeRole inline policy에 provisioner role을 추가

AWS 관리형 `SignInLocalDevelopmentAccess` 하나는 `aws login`에 필요한 정책
문서가 exact read-back되는 경우에만 유지한다. 그 외 managed policy, IAM group,
복수 inline policy가 있으면 권한 합산을 자동 판단하지 않고 중단한다. 기존 단일
inline policy가 저장소 템플릿의 허용 statement 부분집합일 때만 provisioner
statement를 포함한 exact 정책으로 갱신한다. 그 외 drift는 관리자 검토 대상이다.

기존 trust/policy가 다르면 자동 덮어쓰지 않고 drift로 중단한다.

## IAM 사용자 AssumeRole 허용

위 bootstrap이 다음 저장소 템플릿을 `kiwoom-local-user`의 단일 inline policy에
적용한다.

[`local-user-assume-role-policy.json.example`](../../deploy/iam/local-user-assume-role-policy.json.example)

허용 대상은 정확히 다음 다섯 역할이다.

- `kiwoom-local-operator`
- `kiwoom-local-observer`
- `kiwoom-local-provisioner`
- `kiwoom-cstar-document-deployer`
- `kiwoom-cstar-release-rotator`

적용 후 bootstrap이 `get-user-policy` read-back에서 exact 다섯 role ARN과
same-device login action만 남는지 확인한다. C* 문서 배포 역할은 별도
`cstar-document-deployer-*.json.example`의 trust/policy를 사용하며, EC2 재생성
권한과 섞지 않는다. 실패하면 기존 권한을 임의로 삭제하거나 덮어쓰지 않는다.

관측 역할은 별도 [C* observer bootstrap](aws-local-access.md#3-비관리자-c-observer-1회-bootstrap)으로
생성한다. provisioner는 observer 정책을 대신 생성하지 않으며, observer에는
IAM·EC2 write·SSM 실행 권한을 추가하지 않는다.

## 로컬 profile

`~/.aws/config`에 다음 profile을 추가한다.

```ini
[profile kiwoom-provisioner]
role_arn = arn:aws:iam::380648615401:role/kiwoom-local-provisioner
source_profile = kiwoom-login
role_session_name = kiwoom-provisioner
duration_seconds = 3600
region = ap-northeast-2
output = json
```

read-back:

```bash
aws --profile kiwoom-provisioner sts get-caller-identity
```

정상 ARN은 다음 형태다.

```text
arn:aws:sts::380648615401:assumed-role/kiwoom-local-provisioner/kiwoom-provisioner
```

## EC2 재생성 실행

현재 live host에는 실행하지 않는다. 별도 승인된 재생성 창에서만 exact key pair와
현재 관리자 PC의 `/32`를 넣는다.

```bash
./deploy/ec2/apply_clean_rebuild.sh \
  --apply \
  --profile kiwoom-provisioner \
  --region ap-northeast-2 \
  --instance-profile kiwoom-stock-ec2-role \
  --key-pair-name kiwoom-ec2-ssh-20260815 \
  --ssh-admin-cidr <CURRENT_ADMIN_IPV4>/32 \
  --state-file /absolute/path/rebuild-state.txt \
  --confirm-network-write \
  --confirm-eip-cost \
  --confirm-ec2-cost
```

실행 전후에 다음을 확인한다.

- state file mode `0600` 및 exact resource IDs;
- SG ingress TCP 22 current admin `/32` 하나, egress TCP 443 하나;
- instance key pair, SSH 신규 연결, `sshd -t`와 cloud-init completion;
- SSM Agent `Online`은 GitHub 자동화 의존성으로만 확인;
- parameter, credential, application, worker를 자동으로 시작하지 않음.

실행 도중 launch 전 단계에서 실패하면 스크립트가 자동 삭제하지 않고 state file에
생성된 ID를 기록한다. 모든 ID와 SG/EIP 상태를 read-back한 뒤 같은 인자에
`--resume`을 추가하면 기존 VPC/ENI/EIP를 재사용하고 instance launch만 재개한다.
state file의 대상·관리 `/32`·key pair가 현재 인자와 다르면 resume하지 않는다.

이 문서는 current host 변경을 승인하지 않는다. 현재 host와 release tuple은
[current-state.md](current-state.md)를 기준으로 한다.
