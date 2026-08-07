# GitHub OIDC · AWS 배포 bootstrap 가이드

이 문서는 GitHub Actions가 장기 AWS Access Key 없이 AWS에 배포 명령을 전달하기
위한 초기 구성 경계를 정의한다. Kiwoom App Key와 Secret Key는 GitHub가 읽지
않는다.

## 현재 확인된 상태

- GitHub repository: `SpiceChicken/kiwoom_stock`
- visibility: public
- default branch: `main`
- repository 생성일: 2026-01-19
- AWS GitHub OIDC provider: 생성됨
  - URL: `token.actions.githubusercontent.com`
  - audience: `sts.amazonaws.com`
- container registry: public GHCR
  `ghcr.io/spicechicken/kiwoom_stock`
  - candidate workflow는 새 full source SHA tag만 한 번 push하고 기존 tag는
    덮어쓰지 않으며 `latest`를 만들지 않는다.
  - package가 실제로 public인지와 익명 digest pull은 첫 SSM 명령 전에 검증한다.
  - 기존 빈 private ECR repository가 있더라도 이 경로는 사용하지 않는다.
- `/kiwoom-stock/prod/oauth/app-key`,
  `/kiwoom-stock/prod/oauth/secret-key`: Standard `SecureString` metadata 존재
  (값은 조회하지 않음)
- 기존 `/kiwoom/config`, `/kiwoom/strategy_config` parameter는 별도 legacy 설정이므로
  조회·변경·삭제하지 않는다.
- 최신 live read-only 검증에서 EC2 instance role의
  attached managed policies: `0`, inline policies: `2`로 확인됐다.
  - `KiwoomStockSsmCoreWithoutParameterRead`
  - `KiwoomStockRuntimeMinimal`
- 따라서 현재 live role에는 `AmazonSSMManagedInstanceCore`,
  `AmazonSSMReadOnlyAccess`, `AmazonS3FullAccess`가 연결돼 있지 않다. 아래 전환
  절차는 broad policy가 남아 있는 다른 role 또는 재구축 시 적용하는 일반 절차다.

## OIDC subject 확정

GitHub는 2026-07-15 이후 생성되었거나 별도로 opt-in한 repository에 immutable
owner/repository ID 기반 subject를 사용한다. 현재 repository는 그 날짜보다 먼저
생성됐지만 opt-in 여부를 확인하기 전에는 legacy subject를 확정값으로 사용하지
않는다.

확인 대상 API:

```text
GET /repos/SpiceChicken/kiwoom_stock/actions/oidc/customization/sub
```

공식 문서:

- <https://docs.github.com/en/actions/reference/security/oidc>
- <https://docs.github.com/en/rest/actions/oidc>
- <https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws>

확정한 subject는
[github-oidc-trust-policy.json.example](../../deploy/iam/github-oidc-trust-policy.json.example)의
`<GITHUB_OIDC_SUBJECT>` 하나에만 넣는다. wildcard와 legacy/immutable subject 동시
허용은 사용하지 않는다.

production environment를 사용하는 legacy 기본 형식은 다음과 같지만, 이는 조회
결과 확인 전 예시일 뿐이다.

```text
repo:SpiceChicken/kiwoom_stock:environment:production
```

## 권한 분리

### GitHub deploy role

허용:

- 정확한 EC2 instance에 account-owned
  `KiwoomStock-ProductionCheck` Command document 실행
- 실행한 command 결과 조회

금지:

- `ssm:GetParameter`, `ssm:GetParameters`, `ssm:GetParametersByPath`
- Kiwoom SecureString 조회
- IAM 수정
- EC2 생성·종료·보안 그룹 변경
- S3 전체 접근
- ECR 인증·push·pull
- `AWS-RunShellScript` 및 그 밖의 generic document

`ssm:GetCommandInvocation`은 AWS Service Authorization Reference에서 resource type을
지원하지 않으므로 `Resource: "*"`가 필요하다. 이 wildcard는 해당 read action
하나에만 별도 statement로 둔다.

공식 근거:

- <https://docs.aws.amazon.com/service-authorization/latest/reference/list_ssm.html>

custom document source는
[`deploy/ssm/production-check-document.yaml`](../../deploy/ssm/production-check-document.yaml)이다.
모든 caller parameter는 anchored `allowedPattern`과 `ENV_VAR` interpolation을
사용하며, 실행문은 root 소유로 미리 설치한
`/usr/local/sbin/kiwoom-production-check` 하나다. caller가 shell command나 script
본문을 전달하는 parameter는 없다.

### EC2 runtime role

허용:

- `ec2-ssm-core-no-parameter-read-policy.json.example`의 SSM managed-node,
  `ssmmessages`, `ec2messages` core 기능
- 정확한 두 production parameter의 `ssm:GetParameters`
- public GHCR image의 익명 HTTPS pull(별도 IAM 권한 없음)

금지:

- `AmazonSSMManagedInstanceCore`
- `AmazonSSMReadOnlyAccess`
- `ssm:GetParameter`, `ssm:GetParametersByPath`
- exact 두 ARN 밖의 `ssm:GetParameters`

GitHub deploy role과 EC2 runtime role을 같은 role로 합치지 않는다.

## 적용 전 검증

템플릿 placeholder를 실제 값으로 렌더링한 파일은 repository가 아닌 임시
디렉터리에 만든다. account ID 자체는 비밀이 아니지만 불필요하게 문서나 artifact에
고정하지 않는다.

적용 전 최소 검사:

```bash
python3 -m json.tool rendered-trust-policy.json
python3 -m json.tool rendered-github-policy.json
python3 -m json.tool rendered-ec2-policy.json
python3 -m json.tool rendered-ssm-core-policy.json
aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-github-policy.json
aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-ec2-policy.json
aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY \
  --policy-document file://rendered-ssm-core-policy.json
```

trust policy는 IAM role 생성 전에 별도로 JSON 구문과 exact `aud`/`sub`를 확인한다.

custom core는 AWS managed policy
`arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore`의 공개 default version
`v2` action set을 2026-07-26에 read-only 대조한 뒤
`ssm:GetParameter`, `ssm:GetParameters`만 제외한 fork다. AWS managed policy
변경 또는 SSM Agent upgrade 전에는 공식 v2/current action을 다시 조회해 diff를
reviewer/verifier가 검토하며, 차이를 자동 반영하지 않는다.

## broad policy에서 exact role로 전환하는 일반 절차

1. GitHub OIDC customization과 production environment 보호 규칙을 확인한다.
2. AWS OIDC provider를 `token.actions.githubusercontent.com`, audience
   `sts.amazonaws.com`으로 생성한다.
3. exact subject trust policy로 GitHub deploy role을 생성한다.
4. role 이름은 workflow allowlist와 같은
   `kiwoom-stock-github-production-check`로 만들고 최소 권한 deploy policy를
   연결한다.
5. custom Command document를 `KiwoomStock-ProductionCheck` 이름으로 생성하고,
   EC2에 검증한 같은 commit의 deploy script를 root:root `0755`
   `/usr/local/sbin/kiwoom-production-check`로 설치한다. 두 artifact의 hash와
   document version을 기록한다.
6. GitHub `production` Environment에 required reviewer를 설정하고
   `KIWOOM_AWS_DEPLOY_ROLE_ARN` 및 아래 승인 tuple variable을 등록한다.
   - `KIWOOM_APPROVED_SOURCE_SHA`
   - `KIWOOM_APPROVED_IMAGE_DIGEST`
   - `KIWOOM_APPROVED_BUILD_RUN_ID`
   세 tuple 값은 검토한 release manifest와 byte-for-byte 같아야 한다. promotion
   workflow input과도 exact match해야 하며 Kiwoom key는 등록하지 않는다.
7. GHCR package visibility를 public으로 확인하고 로그아웃한 clean Docker config에서
   exact digest pull을 확인한다. candidate build와 protected promotion을 분리하고,
   promotion은 trusted executor checkout과 fixed tuple/audit preflight 뒤 Node 24 OIDC
   outputs를 얻고, 원본 run/job/artifact/Compose/image를 authoritative하게 재검증한
   뒤에만 exact SSM command를 한 번 보낸다.
8. EC2 runtime exact policy와 parameter read가 없는 custom SSM core policy를
   함께 준비하고 JSON, Access Analyzer, custom policy 단독 simulation을 통과시킨다.
9. custom core와 runtime inline policy를 **먼저** role에 연결한다. 이 시점의 SSM
   Online은 기존 managed core가 가릴 수 있으므로 최종 증거로 사용하지 않는다.
10. 별도 승인으로 `AmazonSSMReadOnlyAccess`,
   `AmazonSSMManagedInstanceCore`, `AmazonS3FullAccess`를 모두 제거한다.
11. 제거 직후 attached managed policy `0`, inline policy가 위 두 개뿐인지
   read-back한다. broad policy가 하나라도 남으면 materializer를 시작하지 않는다.
12. 제한된 검증 창 안에 SSM Online, 새 Session Manager session, side effect 없는
    `/usr/bin/true` RunCommand와 `GetCommandInvocation=Success`를 확인한다.
13. exact 두 parameter의 `GetParameters` allow와 이웃/out-of-scope parameter
    deny를 IAM simulation 및 read-only validator로 확인한다.
14. Kiwoom SecureString은 운영자가 숨김 입력으로 생성하며 GitHub workflow가 값을
    읽지 못하는지 확인한다.

Stage I의 manifest 이전 release는 1회 검증을 완료했고 당시 승인 tuple은 제거 후
API로 read-back했다. Stage II는 exact compatibility code를 삭제했지만
`KIWOOM_APPROVED_SOURCE_SHA`, `KIWOOM_APPROVED_IMAGE_DIGEST`,
`KIWOOM_APPROVED_BUILD_RUN_ID` 이름은 modern manifest-backed release의 임시 protected
approval contract로 계속 사용한다. 전체 순서는 trusted executor checkout → fixed
tuple/audit preflight → Node 24 OIDC outputs → authoritative
run/job/artifact/Compose/image validation → single exact SSM → credential clear → evidence
upload다. terminal success/failure/cancel 어느 결론이든 즉시 tuple 세 값을 모두 삭제하고
Environment의 role-only, secrets `0`, pending deployments `0`을 read-back한다. 다음
release는 이전 tuple을 유지하거나 교체하지 않고 새 approval tuple을 등록한다. direct
`aws ssm send-command`, generic document, tag 기반 배포는 이 절차의 대안이 아니다.

`github-deploy-policy.json.example`은 SSM-only다. GHCR push는 AWS role이 아니라
GitHub의 job-scoped `GITHUB_TOKEN`과 workflow의 `packages: write` 권한을 사용한다.
EC2는 public package를 익명으로 pull하므로 `ec2-runtime-policy.json.example`에도
ECR action이 없다.

AWS가 현재 신뢰하는 CA로 GitHub OIDC TLS chain을 검증할 수 있으면 IAM이 provider
생성 시 certificate 정보를 가져온다. thumbprint 값을 오래된 예제에서 복사하지
않고 AWS 공식 절차로 확인한다.

공식 근거:

- <https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html>

## Shadow rollout role 1회 bootstrap

shadow host worker와 activation document를 갱신하는 role은 activation role과 합치지
않는다. trust는 audience `sts.amazonaws.com`과 subject
`repo:SpiceChicken/kiwoom_stock:environment:production-shadow`의 exact equality다.
템플릿은 `deploy/iam/github-shadow-rollout-{trust,policy}.json.example`, 고정 document
source는 `deploy/ssm/shadow-worker-rollout-document.yaml`이다.

admin credential이 있는 승인된 1회 세션에서만 create-only 도구를 사용한다.

```bash
python3 deploy/bootstrap_shadow_rollout.py --account-id <12_DIGIT_ACCOUNT_ID>
```

도구는 role `kiwoom-stock-github-shadow-rollout`, exact inline policy,
`KiwoomStock-ShadowWorkerRollout` v1만 생성한다. 기존 리소스가 exact하면 재사용하고
drift나 v1 외 document는 overwrite하지 않는다. role/trust와 고정 rollout document를
먼저 생성·read-back하고, upsert인 inline policy 쓰기를 마지막 mutation으로 둔다.
policy phase pre-read에 진입하기 전 실패만 이번 실행 소유가 확정된 document와 role을
역순 제거한다. 기존 provider, Environment protection, activation role/document는
항상 보존한다. strict parse, Access Analyzer, 적용 후
trust/policy/role ARN/document content/version read-back과 allow/deny simulation은
validator가 별도로 기록해야 한다.

role/document create는 정상 응답과 후속 exact read-back이 모두 있을 때만
`created-by-attempt` cleanup 소유권을 확정한다. Inline policy phase의 pre-read 직전이
commit boundary다. 이 경계 뒤에는 기존/concurrent exact policy 관측, drift/read 실패,
정상 write 응답이나 응답 유실 어느 경우에도 role/policy/document를 자동 삭제하지
않는다. 정상 응답 뒤
policy/trust/document exact final read-back만 PASS이며, 응답 유실 뒤 exact policy는
`ownership-uncertain`, absent/drift는 manual recovery가 필요한 FAIL이다. 다음
idempotent 실행은 exact existing state를 재사용한다. 기존 exact policy는 mutation 없이
final read-back한다. document는 bounded wait 안에
`Active`, default/latest `1`, exact YAML content가 모두 보여야 한다. cleanup은 한 삭제
실패가 뒤 role 정리를 막지 않도록 각 리소스를 독립 시도하고, original failure,
journal, cleanup 결과와 orphan 목록을 함께 보존한다. final PASS는 role ARN뿐 아니라
exact trust/policy/document를 모두 다시 읽은 뒤에만 출력한다.

Rollout policy의 legacy transition read는 AWS IAM resource-level 특성 때문에 별도
`Resource: "*"` Sid 두 개로만 허용한다. `ssm:ListCommands`는 exact instance와
document filter의 acceptance/aggregate history, `ssm:ListCommandInvocations`는 exact
instance/document의 node execution history 교차검증 전용이다. 두 action은 mutation이나
command output read 권한을 포함하지 않는다.

이 script는 GitHub token을 받지 않고 GitHub API나 `gh`를 호출하지 않는다. GitHub
Environment write는 AWS partial rollback과 원자적으로 묶을 수 없으므로 fail-closed
수동 경계다. AWS read-back 뒤 `production-shadow` required reviewer/main policy를
재확인하고 Environment variable `KIWOOM_AWS_SHADOW_ROLLOUT_ROLE_ARN`에 출력된 exact
ARN을 등록한 다음 API/화면에서 byte-for-byte read-back한다. Kiwoom secret이나 장기
AWS key는 등록하지 않는다. 이 read-back 전에는 rollout을 실행하지 않는다.

일상 순서는 protected exact-main rollout → 14일 audit 검토 → 별도 protected activation
승인이다. 실패 또는 `skew=true`면 activation을 중지하고 previous default 복원 뒤
attempt backup host pair를 확인한다. generic `AWS-RunShellScript`, direct local
`send-command`, arbitrary URL/path/command로 우회하지 않는다.

## 롤백

- rollout bootstrap의 inline policy phase pre-read boundary 전 실패만 journal에서
  `created-by-attempt`가 확인된 rollout document/role을 제거한다. policy write 시도 뒤
  실패는 자동 제거하지 않고 exact read-back과 수동 복구 대상으로 남긴다.
- OIDC provider는 다른 role이 참조하지 않는 것을 확인한 뒤 제거한다.
- managed core 제거 후 SSM Online, 새 Session, `/usr/bin/true` RunCommand 또는
  결과 조회 중 하나라도 실패하면 즉시 cutover 실패로 판정한다.
- 이 경우에만
  `arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore`를 role에 다시 연결하고
  SSM Online과 새 Session 복구를 확인한다.
  정확한 role과 rollback 승인을 다시 확인한 운영자만 다음 write를 실행한다.

  ```bash
  aws iam attach-role-policy \
    --role-name kiwoom-stock-ec2-role \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  ```

- rollback 중 `AmazonSSMReadOnlyAccess`나 `AmazonS3FullAccess`는 재연결하지
  않는다. managed core가 재연결된 동안 wildcard parameter read가 복구되므로
  materializer와 애플리케이션은 계속 중지하고 custom policy 누락을 교정한다.
- broad policy 제거 실패를 이유로 더 넓은 새 권한을 추가하지 않는다.
- 기존 `/kiwoom/config`, `/kiwoom/strategy_config` parameter를 롤백 대상으로
  사용하거나 변경하지 않는다.
