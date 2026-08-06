# 로컬 AWS 접근 운영 가이드

이 문서는 개인 개발 PC에서 AWS root 계정을 일상적으로 사용하지 않고, MFA로
인증한 IAM 사용자와 최소 권한 역할을 통해 지정 EC2에 접근하는 방법을 설명한다.
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
  └─ kiwoom-local-operator 역할만 AssumeRole

kiwoom-local-operator (IAM role)
  ├─ 지정 EC2의 기본 Session Manager shell
  ├─ EC2/SSM 상태와 기존 command 결과 조회
  ├─ 임시 credential에서만 AssumeRole 허용
  └─ IAM 변경, Parameter Store 값 조회, 임의 SendCommand 권한 없음

GitHub OIDC roles
  ├─ 기존 배포·production-check·shadow activation 경계 유지
  └─ 별도 shadow rollout role로 worker/document pair만 갱신
```

`aws login` 세션은 영구 로그인이 아니다. 최대 12시간인 임시 세션이므로 만료 시
IAM 사용자로 다시 로그인한다. 운영 역할은 role chaining 제한에 맞춰 1시간씩
발급되며 source 로그인이 유효한 동안 CLI가 다시 발급한다. 중요한 개선점은 이때
root가 아니라 권한이 제한된 사용자를 이용하고, Access Key/Secret Key를 PC에
저장하지 않는다는 것이다.

일상적인 shadow worker rollout과 activation은 로컬 CLI 작업이 아니다. protected
`production-shadow` GitHub Environment의 required reviewer 승인 뒤 각 workflow가
OIDC 단기 credential을 새로 발급받는다. 따라서 bootstrap이 검증된 뒤에는 이 두
작업을 위해 `kiwoom-aws-login`을 반복할 필요가 없다. 로컬 profile은 임의 진단과
Session Manager에만 유지하며, rollout 실패를 로컬 `send-command`나 장기 Access
Key로 우회하지 않는다.

## 1. 저장소 템플릿 렌더링

원본 템플릿:

- `deploy/iam/local-user-assume-role-policy.json.example`
- `deploy/iam/local-operator-trust-policy.json.example`
- `deploy/iam/local-operator-policy.json.example`

렌더링 파일은 Git 저장소 밖의 임시 디렉터리에 둔다. 아래 값만 치환한다.

| placeholder | 의미 | 현재 환경에서 찾는 명령 |
|---|---|---|
| `<AWS_ACCOUNT_ID>` | 12자리 AWS 계정 ID | root bootstrap 세션의 `aws sts get-caller-identity --query Account --output text` |
| `<AWS_REGION>` | EC2 리전 | `ap-northeast-2` |
| `<EC2_INSTANCE_ID>` | 허용할 EC2 한 대 | `aws ec2 describe-instances ...`로 확인 |

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
8. 사용자로 처음 로그인해 비밀번호를 변경하고 MFA를 등록한다.
9. root에서 로그아웃한다.

비밀번호나 MFA 코드를 CLI 인자, shell history, 문서, GitHub Secret에 입력하지 않는다.

정책 적용 전에는 최소한 다음 검사를 수행한다.

```bash
python3 -m json.tool rendered-local-user-policy.json
python3 -m json.tool rendered-local-operator-trust-policy.json
python3 -m json.tool rendered-local-operator-policy.json

aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-local-user-policy.json
aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-local-operator-policy.json
```

Access Analyzer 호출은 정책 정적 검사이며 리소스를 생성하거나 과금을 발생시키지
않는다. trust policy는 `python3 -m json.tool`과 IAM 적용 후 read-back으로 검증한다.

## 3. 로컬 profile 사용

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
```

처음에는 `kiwoom-login`에 `login_session` 항목이 없다. 다음 명령으로 IAM 사용자
로그인을 완료하면 AWS CLI가 console identity에 해당하는 값을 기록한다.

```bash
kiwoom-aws-login
```

그 후 모든 일상 명령은 `kiwoom-local` profile을 쓴다.

```bash
aws sts get-caller-identity --profile kiwoom-local
aws ssm describe-instance-information \
  --filters Key=PingStatus,Values=Online \
  --region ap-northeast-2 \
  --profile kiwoom-local
aws ssm start-session \
  --target <EC2_INSTANCE_ID> \
  --region ap-northeast-2 \
  --profile kiwoom-local
```

정상 caller ARN은 다음 형태다.

```text
arn:aws:sts::<AWS_ACCOUNT_ID>:assumed-role/kiwoom-local-operator/kiwoom-local
```

`arn:aws:iam::<AWS_ACCOUNT_ID>:root`가 나오면 잘못된 profile이므로 즉시 중단한다.

## 4. 허용·거부 검증

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

Session Manager 연결은 실제 주문·외부 API 호출을 하지 않는 shell 연결만 확인하고
즉시 `exit`한다. 기존 Run Command 결과는 알고 있는 command ID에 대해서만
`get-command-invocation`으로 조회한다.

거부 검증은 값을 출력하지 않는 요청으로 수행한다. 아래 작업은 모두
`AccessDenied`여야 한다.

- `aws iam list-users --profile kiwoom-local`
- `aws ssm get-parameter --name /kiwoom-stock/prod/oauth/app-key --with-decryption ...`
- 다른 EC2 instance를 대상으로 한 `ssm start-session`
- `ssm send-command`

Parameter Store 거부 검사에서 오류 메시지만 확인하고 응답이나 shell trace를
artifact에 저장하지 않는다.

## 5. 일상 사용과 장애 대응

- 세션이 유효하면 AWS CLI가 cache된 임시 credential을 재사용한다.
- 세션이 만료되면 `kiwoom-aws-login`을 한 번 실행한다.
- 브라우저에는 root가 아니라 `kiwoom-local-user`로 로그인한다.
- role assumption이 거부되면 IAM 사용자 MFA 로그인 여부, `kiwoom-login` caller
  identity, `aws login` 임시 session 여부를 확인한다.
- `kiwoom-login` 자체로 운영 명령을 실행하지 않는다.
- GitHub Actions 배포 실패를 로컬 role의 권한 확대로 우회하지 않는다.

## 6. 롤백

로컬 이관 전 Windows-mounted `.aws` symlink는 timestamp가 붙은 이름으로 보존한다.
롤백이 필요하면 새 Linux `~/.aws` 디렉터리를 별도 이름으로 이동하고, 보존한
symlink를 `~/.aws`로 되돌린다. 오래된 root login cache를 새 경계로 복사하지 않는다.

AWS 쪽 롤백은 다음 순서다.

1. `kiwoom-local-user`에서 AssumeRole inline policy를 제거한다.
2. 활성 local operator session이 없는지 확인한다.
3. role inline policy를 제거하고 role을 삭제한다.
4. IAM user의 MFA/login profile을 제거한 뒤 사용자를 삭제한다.

GitHub OIDC role, EC2 instance role, VPC/EIP/EBS는 이 작업의 대상이 아니므로 변경하거나
롤백하지 않는다.

## 공식 근거

- <https://docs.aws.amazon.com/IAM/latest/UserGuide/root-user-best-practices.html>
- <https://docs.aws.amazon.com/signin/latest/userguide/command-line-sign-in.html>
- <https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html>
- <https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-quickstart.html>
- <https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html>
