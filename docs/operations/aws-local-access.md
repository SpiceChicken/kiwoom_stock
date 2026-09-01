# 로컬 AWS 접근 운영 가이드

이 문서는 개인 개발 PC에서 AWS root 계정을 일상적으로 사용하지 않고, MFA로
인증한 IAM 사용자와 최소 권한 역할을 통해 지정 EC2를 조회·복구하는 방법을
설명한다. 현재 사람의 EC2 shell 접속은 직접 SSH로 수행하며, GitHub Actions의
자동화 명령에만 SSM command plane을 사용한다.
추가 유료 서비스, 장기 Access Key, IAM Identity Center/Organizations는 사용하지
않는다.

## 최종 구조

```text
root 계정
  └─ 계정·결제·root 전용 복구 작업에만 사용

kiwoom-local-user (IAM console user)
  ├─ console password + MFA
  ├─ access key 없음
  ├─ 서울 리전 same-device `aws login` OAuth 권한
  └─ kiwoom-local-operator·kiwoom-local-observer·kiwoom-local-provisioner·kiwoom-cstar-document-deployer·kiwoom-cstar-release-rotator만 AssumeRole

kiwoom-local-operator (IAM role)
  ├─ EC2/SSM 상태와 기존 command 결과 조회
  ├─ SSH bootstrap에 필요한 EC2 read-back
  ├─ 임시 credential에서만 AssumeRole 허용
  └─ IAM 변경, Parameter Store 값 조회, 임의 SendCommand 권한 없음

kiwoom-local-observer (IAM role, 관측 전용)
  ├─ C* 원장·증적 bucket·스케줄·SSM 결과·EC2 상태·알람·DLQ만 조회
  ├─ exact C* table/index, evidence prefix, schedule와 DLQ에만 결속
  ├─ 임시 credential에서만 AssumeRole 허용
  └─ DynamoDB/S3 변경, SSM 실행·shell, Parameter Store/Secrets Manager, IAM 변경 권한 없음

kiwoom-local-provisioner (IAM role, 관리자 1회 bootstrap 후 사용)
  ├─ reviewed EC2 clean-rebuild의 서울 리전 인프라 생성·구성·read-back
  ├─ 정확한 kiwoom-stock-ec2-role만 iam:PassRole
  └─ IAM 변경, 삭제/종료/release, SSM shell/command, Parameter Store 조회 권한 없음

kiwoom-cstar-document-deployer (IAM role, 문서 배포 전용)
  ├─ C* evidence SSM 문서 1개의 조회·버전 생성·default 전환
  ├─ C* start/stop/reconciliation schedule 3개의 조회
  └─ EC2, SendCommand, Parameter Store, Logs, IAM 변경 권한 없음

kiwoom-cstar-release-rotator (IAM role, release pointer 교정 전용)
  ├─ C* 원장 table의 GetItem·PutItem·UpdateItem·TransactWriteItems만 허용
  ├─ C* start/stop schedule의 GetSchedule·UpdateSchedule만 허용
  ├─ Scheduler가 기존 실행 역할을 보존하도록 exact iam:PassRole만 허용
  └─ SSM, EC2, Parameter Store, Logs, 그 밖의 IAM 변경 권한 없음

GitHub OIDC roles
  ├─ production-check·shadow rollout·shadow activation의 보호된 SSM command
  └─ 별도 shadow rollout role로 worker/document pair만 갱신
```

### 현재 사람용 접속 경계

- 대상: 서울 리전의 단일 운영 EC2; exact instance ID와 주소는
  [현재 운영 기준선](current-state.md) 대신 AWS/private operator inventory에서
  read-back한다.
- 접속 도구: [`tools/ssh-direct-shell.sh`](../../tools/ssh-direct-shell.sh)
- 개인키: repository 밖 `/home/pc/.ssh/kiwoom-recovery`, mode `0600`
- 네트워크: 현재 PC의 관리용 TCP 22 `/32`만 허용하며 `0.0.0.0/0`은 허용하지
  않는다.
- SSH 정책: password/kbd-interactive/root login 금지, public-key login만 허용
- 사람용 `aws ssm start-session`은 사용하지 않는다. SSM Agent와 SSM IAM은
  GitHub 자동화·상태 확인을 위해 남아 있을 수 있으므로, 이를 “SSM 완전 제거”로
  해석하지 않는다.
- 저장소의 `local-operator-policy.json.example`은 사람용 SSM session 권한을
  포함하지 않는다. 2026-08-24 KST에 AWS의 `kiwoom-local-operator`도 이
  canonical read-only 정책 하나로 교체했고, 종료된 host용 `StartSession`과
  session recovery/data-channel 권한이 있던 두 inline policy 및 임시 목록 정책을
  삭제했다. 실제 live target의 `start-session`은 AccessDenied이며 EC2 inventory와
  SSM managed-node health read만 계속 허용된다.

현재 키는 EC2 console의 `KeyName` launch metadata와 별개로 `ubuntu`의
`authorized_keys`에 설치돼 있다. 키를 교체할 때는 새 키로 별도 SSH 연결을
확인한 뒤 기존 키를 제거한다.

`aws login` 세션은 영구 로그인이 아니다. 최대 12시간인 임시 세션이므로 만료 시
IAM 사용자로 다시 로그인한다. 운영 역할은 role chaining 제한에 맞춰 1시간씩
발급되며 source 로그인이 유효한 동안 CLI가 다시 발급한다. 중요한 개선점은 이때
root가 아니라 권한이 제한된 사용자를 이용하고, Access Key/Secret Key를 PC에
저장하지 않는다는 것이다.

일상적인 shadow worker rollout과 activation은 로컬 CLI 작업이 아니다. protected
`production-shadow` GitHub Environment의 required reviewer 승인 뒤 각 workflow가
OIDC 단기 credential을 새로 발급받는다. 따라서 bootstrap이 검증된 뒤에는 이 두
작업을 위해 `kiwoom-aws-login`을 반복할 필요가 없다. 로컬 profile은 AWS
read-only 진단, SG/instance read-back 및 승인된 SSH bootstrap 확인에만 사용한다.
rollout 실패를 로컬 `send-command`, 사람용 Session Manager 또는 장기 Access Key로
우회하지 않는다.

## 1. 저장소 템플릿 렌더링

원본 템플릿:

- `deploy/iam/local-user-assume-role-policy.json.example`
- `deploy/iam/local-operator-trust-policy.json.example`
- `deploy/iam/local-operator-policy.json.example`
- `deploy/iam/local-observer-trust-policy.json.example`
- `deploy/iam/local-observer-policy.json.example`
- `deploy/iam/local-provisioner-trust-policy.json.example`
- `deploy/iam/local-provisioner-policy.json.example`
- `deploy/iam/cstar-document-deployer-trust-policy.json.example`
- `deploy/iam/cstar-document-deployer-policy.json.example`
- `deploy/iam/cstar-release-rotator-trust-policy.json.example`
- `deploy/iam/cstar-release-rotator-policy.json.example`

렌더링 파일은 Git 저장소 밖의 임시 디렉터리에 둔다. 아래 값만 치환한다.

| placeholder | 의미 | 현재 환경에서 찾는 명령 |
|---|---|---|
| `<AWS_ACCOUNT_ID>` | 12자리 AWS 계정 ID | root bootstrap 세션의 `aws sts get-caller-identity --query Account --output text` |
| `<AWS_REGION>` | EC2 리전 | `ap-northeast-2` |
| `<EC2_INSTANCE_ID>` | 허용할 EC2 한 대 | `aws ec2 describe-instances ...`로 확인 |
| `<CSTAR_TABLE_NAME>` | C* release ledger table | CloudFormation C* stack output으로 확인 |
| `<EVIDENCE_BUCKET_NAME>` | C* evidence Object Lock bucket | CloudFormation C* stack output으로 확인 |
| `<SUBMITTER_DLQ_NAME>` 등 3개 | C* Scheduler DLQ physical name | `aws sqs list-queues`로 prefix를 확인 |
| `<CSTAR_SCHEDULER_ROLE_NAME>` | 두 schedule이 사용하는 execution role 이름 | 두 schedule의 `Target.RoleArn`에서 확인 |

템플릿에는 비밀값이 없다. 계정 ID와 instance ID도 credential은 아니지만, 실제
렌더링 결과는 운영 환경별 산출물이므로 repository에 commit하지 않는다.

## 2. root로 최초 bootstrap 한 번만 수행

root 로그인은 이 단계에서만 사용한다. IAM console에서 다음 순서로 만든다.

1. IAM 사용자 `kiwoom-local-user`를 만든다.
2. AWS Management Console access를 켜고 임시 비밀번호를 설정한다.
3. Access Key는 만들지 않는다.
4. 사용자에게 렌더링한 `local-user-assume-role-policy`만 inline policy로 연결한다.
   이 정책은 exact 역할 AssumeRole과 서울 리전 same-device `aws login`에 필요한
   `signin:AuthorizeOAuth2Access`, `signin:CreateOAuth2Token`만 포함한다.
5. 렌더링한 trust policy로 IAM 역할 `kiwoom-local-operator`를 만든다. trust는
   exact IAM 사용자와 임시 credential에만 있는 `aws:TokenIssueTime`을 모두
   요구하므로 장기 Access Key로는 역할을 맡을 수 없다.
6. 역할의 Maximum session duration을 `12 hours`로 설정한다.
7. 역할에 렌더링한 `local-operator-policy`만 inline policy로 연결한다.
8. 렌더링한 `cstar-document-deployer-trust-policy`로
   `kiwoom-cstar-document-deployer`를 만들고, `cstar-document-deployer-policy`만
   inline policy로 연결한다. 이 역할은 `KiwoomStock-ShadowEvidenceExport` 문서의
   version update/default 전환과 C* schedule read-back만 허용한다.
9. `local-user-assume-role-policy`의 exact third-role statement가 위 역할을
   가리키는지 read-back한다.
10. 사용자로 처음 로그인해 비밀번호를 변경하고 MFA를 등록한다.
11. root에서 로그아웃한다.

비밀번호나 MFA 코드를 CLI 인자, shell history, 문서, GitHub Secret에 입력하지 않는다.

정책 적용 전에는 최소한 다음 검사를 수행한다.

```bash
python3 -m json.tool rendered-local-user-policy.json
python3 -m json.tool rendered-local-operator-trust-policy.json
python3 -m json.tool rendered-local-operator-policy.json
python3 -m json.tool rendered-local-observer-trust-policy.json
python3 -m json.tool rendered-local-observer-policy.json

aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-local-user-policy.json
aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-local-operator-policy.json
aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-local-observer-policy.json
```

Access Analyzer 호출은 정책 정적 검사이며 리소스를 생성하거나 과금을 발생시키지
않는다. trust policy는 `python3 -m json.tool`과 IAM 적용 후 read-back으로 검증한다.

## 3. 비관리자 C* observer 1회 bootstrap

observer 역할 생성과 `kiwoom-local-user`의 AssumeRole statement 추가만 관리자
세션에서 1회 수행한다. 이후 C* 실행 모니터링과 증적 확인에는 root/Admin을
사용하지 않는다. 아래 명령은 역할·정책·사용자 inline policy의 exact read-back을
수행하며, 기존 drift가 있으면 덮어쓰지 않고 중단한다.

```bash
./.venv/bin/python deploy/bootstrap_local_observer.py \
  --profile <ADMIN_PROFILE> \
  --account-id <AWS_ACCOUNT_ID> \
  --table-name <CSTAR_TABLE_NAME> \
  --evidence-bucket-name <EVIDENCE_BUCKET_NAME> \
  --instance-id <EC2_INSTANCE_ID> \
  --submitter-dlq-name <SUBMITTER_DLQ_NAME> \
  --observer-dlq-name <OBSERVER_DLQ_NAME> \
  --reconciliation-dlq-name <RECONCILIATION_DLQ_NAME> \
  --check
```

read-only check가 통과한 동일 명령에 `--apply --update-reviewed-policy`를
사용한다. 역할 이름은 `kiwoom-local-observer`, inline policy 이름은
`KiwoomLocalObserver`다. 이 bootstrap 자체만 IAM role 생성·정책 연결을 위해
관리자 권한을 사용할 수 있으며, observer 정책에는 IAM 권한을 넣지 않는다.

## 4. 로컬 profile 사용

WSL의 `~/.aws/config`는 다음 역할을 갖는다.

```ini
[default]
region = ap-northeast-2
output = json

[profile kiwoom-login]
region = ap-northeast-2
output = json

[profile kiwoom-local]
role_arn = arn:aws:iam::<AWS_ACCOUNT_ID>:role/kiwoom-local-operator
source_profile = kiwoom-login
role_session_name = kiwoom-local
duration_seconds = 3600
region = ap-northeast-2
output = json

[profile kiwoom-cstar-deployer]
role_arn = arn:aws:iam::<AWS_ACCOUNT_ID>:role/kiwoom-cstar-document-deployer
source_profile = kiwoom-login
role_session_name = kiwoom-cstar-deployer
duration_seconds = 3600
region = ap-northeast-2
output = json

[profile kiwoom-cstar-observer]
role_arn = arn:aws:iam::<AWS_ACCOUNT_ID>:role/kiwoom-local-observer
source_profile = kiwoom-login
role_session_name = kiwoom-cstar-observer
duration_seconds = 3600
region = ap-northeast-2
output = json
```

처음에는 `kiwoom-login`에 `login_session` 항목이 없다. 다음 명령으로 IAM 사용자
로그인을 완료하면 AWS CLI가 console identity에 해당하는 값을 기록한다.

```bash
kiwoom-aws-login
```

그 후 EC2 inventory와 SSH 전제조건은 `kiwoom-local`, C* 원장·증적·스케줄·DLQ
확인은 `kiwoom-cstar-observer` profile을 쓴다.

```bash
aws sts get-caller-identity --profile kiwoom-local
aws ssm describe-instance-information \
  --filters Key=PingStatus,Values=Online \
  --region ap-northeast-2 \
  --profile kiwoom-local

# 사람용 shell은 SSM이 아니라 repository helper를 사용한다.
./tools/ssh-direct-shell.sh
```

`describe-instance-information`은 GitHub 자동화용 SSM Agent health 확인일 뿐
사람용 접속 경로가 아니다. helper는 고정된 host/user를 다시 확인하고 private
key를 출력하지 않는다.

정상 caller ARN은 다음 형태다.

```text
arn:aws:sts::<AWS_ACCOUNT_ID>:assumed-role/kiwoom-local-operator/kiwoom-local
```

C* evidence 문서 배포가 필요한 경우에만 별도 profile을 사용한다.

```bash
aws sts get-caller-identity --profile kiwoom-cstar-deployer
aws ssm describe-document \
  --name KiwoomStock-ShadowEvidenceExport \
  --region ap-northeast-2 \
  --profile kiwoom-cstar-deployer
```

정상 deployer caller ARN은 다음 형태다.

```text
arn:aws:sts::<AWS_ACCOUNT_ID>:assumed-role/kiwoom-cstar-document-deployer/kiwoom-cstar-deployer
```

observer 확인:

```bash
aws sts get-caller-identity --profile kiwoom-cstar-observer
aws dynamodb scan \
  --table-name <CSTAR_TABLE_NAME> \
  --filter-expression 'session_date_kst = :d' \
  --expression-attribute-values '{":d":{"S":"YYYY-MM-DD"}}' \
  --region ap-northeast-2 \
  --profile kiwoom-cstar-observer
```

정상 observer ARN은 다음 형태다.

```text
arn:aws:sts::<AWS_ACCOUNT_ID>:assumed-role/kiwoom-local-observer/kiwoom-cstar-observer
```

`arn:aws:iam::<AWS_ACCOUNT_ID>:root`가 나오면 잘못된 profile이므로 즉시 중단한다.

EC2 재생성까지 로컬에서 자동화해야 하면 관리자 세션에서
[로컬 provisioner 1회 bootstrap](local-provisioner-bootstrap.md)을 먼저 수행한다.
현재 `kiwoom-local`과 `kiwoom-admin-bootstrap`은 `kiwoom-local-user` 및
`kiwoom-local-operator`를 사용하고, C* 문서 배포는 별도
`kiwoom-cstar-deployer` profile을 사용한다. provisioner bootstrap은 별도
`kiwoom-provisioner` profile을 사용하고, 일상 진단에는 기존 `kiwoom-local`을
계속 사용한다.

## 5. 허용·거부 검증

허용 검증:

```bash
aws ec2 describe-instances \
  --instance-ids <EC2_INSTANCE_ID> \
  --region ap-northeast-2 \
  --profile kiwoom-local

aws ssm get-connection-status \
  --target <EC2_INSTANCE_ID> \
  --region ap-northeast-2 \
  --profile kiwoom-local
```

SSH 연결은 실제 주문·외부 API 호출을 하지 않는 shell 연결만 확인하고 필요한
점검 후 `exit`한다. 기존 SSM Run Command 결과는 GitHub workflow가 만든 알고
있는 command ID에 대해서만 `get-command-invocation`으로 조회한다.

거부 검증은 값을 출력하지 않는 요청으로 수행한다. 아래 작업은 모두
`AccessDenied`여야 한다.

- `aws iam list-users --profile kiwoom-local`
- `aws ssm get-parameter --name /kiwoom-stock/prod/oauth/app-key --with-decryption ...`
- 사람용 `ssm start-session` 사용
- `ssm send-command`

observer profile에서도 위 거부 목록과 DynamoDB/S3/스케줄 변경 요청은 모두
`AccessDenied`여야 한다. observer는 C* 검증을 위한 읽기 전용 역할이며, 기존
`kiwoom-local` operator의 권한을 넓히지 않는다.

`kiwoom-cstar-deployer`는 위 거부 목록의 예외로 evidence 문서 version update와
default 전환만 허용하며, worker activation이나 evidence command 실행 권한은
갖지 않는다.

Immutable release hash 교정이 필요한 경우에는 `kiwoom-cstar-rotator` profile만
사용한다. 이 profile은 C* 원장 pointer와 start/stop schedule 상태를 교정하는
동안에만 사용하며, 기존 release item을 수정·삭제하지 않는다.

Parameter Store 거부 검사에서 오류 메시지만 확인하고 응답이나 shell trace를
artifact에 저장하지 않는다.

## 6. 일상 사용과 장애 대응

- 세션이 유효하면 AWS CLI가 cache된 임시 credential을 재사용한다.
- 세션이 만료되면 root가 아닌 `kiwoom-local-user`로 `kiwoom-aws-login`을 한 번
  실행한다.
- 브라우저에는 root가 아니라 `kiwoom-local-user`로 로그인한다.
- role assumption이 거부되면 IAM 사용자 MFA 로그인 여부, `kiwoom-login` caller
  identity, `aws login` 임시 session 여부를 확인한다.
- `kiwoom-login` 자체로 운영 명령을 실행하지 않는다.
- GitHub Actions 배포 실패를 로컬 role의 권한 확대로 우회하지 않는다.
- SSH host/user/key helper의 고정 계약을 임의로 바꾸지 않는다.
- 디스크 부족 시 `docker system prune --volumes`를 실행하지 않는다. 먼저
  [운영 runbook](runbook.md)의 label-scoped 정리 절차를 따른다.

## 7. 롤백

로컬 이관 전 Windows-mounted `.aws` symlink는 timestamp가 붙은 이름으로 보존한다.
롤백이 필요하면 새 Linux `~/.aws` 디렉터리를 별도 이름으로 이동하고, 보존한
symlink를 `~/.aws`로 되돌린다. 오래된 root login cache를 새 경계로 복사하지 않는다.

AWS 쪽 롤백은 다음 순서다.

1. `kiwoom-local-user`에서 AssumeRole inline policy를 제거한다.
2. 활성 local operator/observer session이 없는지 확인한다.
3. observer와 operator role inline policy를 제거하고 role을 삭제한다.
4. IAM user의 MFA/login profile을 제거한 뒤 사용자를 삭제한다.

GitHub OIDC role, EC2 instance role, VPC/EIP/EBS는 이 작업의 대상이 아니므로 변경하거나
롤백하지 않는다.

SSH 접근 자체를 폐기할 때는 다음을 별도 승인된 변경으로 취급한다.

1. 새 키를 `authorized_keys`에 추가하고 새 SSH 연결을 확인한다.
2. Security Group의 새 관리 `/32`를 추가·read-back한다.
3. 기존 키와 기존 `/32`를 제거한다.
4. `sshd -t`, daemon reload/restart, 새 연결과 기존 연결 종료를 확인한다.

EC2 재생성 시에는 `apply_clean_rebuild.sh --apply`에 기존 AWS EC2 key pair 이름과
현재 관리 PC의 정확한 IPv4 `/32`를 전달한다. 스크립트는 key pair 존재 여부와 SG
ingress/egress를 read-back한 뒤에만 instance를 시작하고, 현재 live host에는
절대 재실행하지 않는다. 적용 전·후 SSH 신규 연결을 별도로 확인한다.

## 공식 근거

- <https://docs.aws.amazon.com/IAM/latest/UserGuide/root-user-best-practices.html>
- <https://docs.aws.amazon.com/signin/latest/userguide/command-line-sign-in.html>
- <https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html>
- <https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-quickstart.html>
- <https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html>
