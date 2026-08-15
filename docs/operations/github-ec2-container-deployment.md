# GitHub → EC2 container production-check 운영 가이드

이 문서는 현재 코드를 public GHCR에 올리고 EC2에서 **설정 검사만** 수행하는
절차를 설명한다. 실제 worker와 매매 기능은 시작하지 않는다.

현재 운영 기준선은
[`current-state.md`](current-state.md)에 있다. 사람이 수행한 최근 host
preflight/production-check는 직접 SSH transport로 완료했지만, GitHub protected
workflow의 canonical production-check 실행 backend는 여전히 exact SSM document다.
따라서 SSH 전환을 이유로 `ssm:SendCommand`, SSM document, OIDC role 또는
자동화 경계를 제거하지 않는다. 이 문서의 GitHub 절차를 로컬 `aws ssm
send-command`로 우회해서도 안 된다.

## 한눈에 보는 흐름

```text
manual candidate workflow
  → full 40-hex source SHA 입력
  → lint/type/test/package/container 검증
  → 새 tag만 ghcr.io/spicechicken/kiwoom_stock:sha-<40 hex> push
  → 기존 tag면 덮어쓰지 않고 remote digest 재사용
  → OCI digest/계약 익명 검사 + release-manifest JSON sealing
manual protected promotion workflow
  → approved source SHA + exact digest + build run ID 입력
  → trusted executor checkout + fixed tuple/audit preflight
  → Node 24 OIDC outputs로 authoritative run/job/artifact/Compose/image 검증
  → single exact SSM command + credential clear + evidence upload
  → i-0e42e09d6c087ba29에서 잠금/자원/secret metadata 검사
  → network/운영 volume/실제 key 없는 digest image로 일회성 --check-config
  → current/previous full release tuple을 하나의 JSON으로 기록
  → terminal success/failure/cancel 뒤 approval tuple 3개 삭제 및
    role-only/secrets 0/pending 0 read-back
```

GitHub에는 Kiwoom App Key와 Secret Key를 넣지 않는다. 두 값은 EC2의 Parameter
Store materializer가 `/run/kiwoom-stock/credentials/app-key`와 `secret-key`에
root 소유 `0400` 파일로 만든다. workflow와 SSM 출력은 값이나 OAuth token을 읽거나
출력하지 않는다. host command는 이 파일의 owner/mode/link/size/symlink
metadata만 검사한다. candidate container에는 별도의 정적 non-secret placeholder
두 개만 잠시 mount하며 종료 시 제거한다.

## 1. 한 번만 준비할 항목

### GitHub

1. repository가 public인지 확인한다.
2. Settings → Environments → `production`을 만든다.
3. required reviewer를 지정한다. 1인 프로젝트라면 운영 계정 본인을 지정한다.
4. Environment variable `KIWOOM_AWS_DEPLOY_ROLE_ARN`에 exact deploy role ARN을
   등록한다. 현재 허용값은
   `arn:aws:iam::380648615401:role/kiwoom-stock-github-production-check` 하나다.
5. 다음 세 Environment variable에 검토·승인한 release tuple을 등록한다.
   - `KIWOOM_APPROVED_SOURCE_SHA`: full 40-character lowercase source SHA
   - `KIWOOM_APPROVED_IMAGE_DIGEST`:
     `ghcr.io/spicechicken/kiwoom_stock@sha256:<64 hex>`
   - `KIWOOM_APPROVED_BUILD_RUN_ID`: candidate workflow의 numeric run ID
6. Kiwoom key, AWS access key, `.env` 내용은 GitHub Secret/Variable에 등록하지
   않는다.

첫 GHCR push 뒤 Packages에서 `kiwoom_stock` package를 public으로 설정해야 할 수
있다. workflow는 Docker 로그아웃과 빈 `DOCKER_CONFIG`를 사용한 digest pull이
실패하면 AWS OIDC 단계 전에 중단된다. 익명 pull이 성공하기 전 EC2 명령은 없다.

### AWS

1. GitHub OIDC provider의 URL은 `token.actions.githubusercontent.com`, audience는
   `sts.amazonaws.com`이어야 한다.
2. trust policy의 `sub`는 실제 repository OIDC customization read-back 결과와
   `production` Environment를 exact match한다. wildcard를 사용하지 않는다.
3. [GitHub deploy policy](../../deploy/iam/github-deploy-policy.json.example)는
   exact instance/document `ssm:SendCommand`와
   `ssm:GetCommandInvocation`만 허용한다.
4. [EC2 runtime policy](../../deploy/iam/ec2-runtime-policy.json.example)는 exact
   두 Parameter Store ARN만 읽는다. public GHCR pull에 ECR IAM 권한은 필요 없다.
5. Access Analyzer와 IAM simulation을 통과한 뒤에만 live role을 변경한다.

자세한 trust와 policy 적용 순서는
[OIDC/AWS bootstrap](github-oidc-aws-bootstrap.md)을 따른다.

## 2. candidate 생성과 production promotion

### 2.1 candidate 생성

1. Actions → `Production container check` → Run workflow를 연다.
2. `source_sha`에 full 40-character lowercase commit SHA를 입력한다. branch와
   tag 같은 mutable ref는 허용하지 않는다.
3. OIDC가 없는 `build_publish`와 `seal_release_manifest` job이 성공할 때까지
   기다린다.
4. artifact
   `release-manifest-<source_sha>-<build_run_id>`의 exact digest, image size,
   Compose hash, build run/job ID를 검토한다.

workflow는 다음 순서를 바꾸지 않는다.

1. complete-history Gitleaks scan
2. pip editable install, lint, mypy, 전체 테스트
3. package build, 설치된 wheel import/config smoke
4. Docker test/runtime build와 runtime image 검사
5. 새 full-SHA tag만 GHCR push; 기존 tag는 remote image 그대로 재사용
6. clean anonymous exact-digest pull과 revision/entrypoint/user/850 MiB 상한 검사
7. 최대 16 KiB strict release manifest sealing

The release manifest is the candidate workflow's only durable candidate artifact.
JUnit XML and a standalone runtime-image-inspect artifact are not retained.

`latest` tag는 만들지 않는다. EC2에는
`ghcr.io/spicechicken/kiwoom_stock@sha256:<64 hex>` 형식만 전달한다.
full-SHA tag가 이미 존재하면 remote image를 pull하며 새 local rebuild와 비교하거나
tag를 덮어쓰지 않는다. remote digest의 image contract가 source SHA와 다르면
manifest를 만들기 전에 실패한다.

### 2.2 protected digest promotion

1. release manifest를 검토한 뒤 `production` Environment의 승인 tuple 세 값을
   manifest와 동일하게 등록한다.
2. Actions → `Production digest promotion` → Run workflow를 연다.
3. `source_sha`, exact `image_digest`, numeric `build_run_id`를 입력한다.
4. Environment 승인 화면에서 같은 tuple인지 확인하고 승인한다.
5. workflow가 immutable workflow SHA의 trusted executor를 credential 없이 checkout하고,
   fixed tuple/audit preflight만 OIDC 전에 수행한다.
6. pinned Node 24 action의 OIDC credential outputs를 바로 다음 executor step에만
   전달한다.
7. executor가 원본 candidate run의 repository/path/event/head/ref/status,
   exact successful build job, unique non-expired artifact, strict ZIP/JSON,
   exact commit의 두 Compose byte hash, anonymous image revision/entrypoint/user/
   size를 검증한다.
8. 검증이 모두 끝난 뒤 exact custom SSM document를 한 번만 실행한다.
9. credential clear 뒤 `production-check-<source>-<promotion run>` evidence upload와
   terminal result를 확인한다.
10. success/failure/cancel 어느 terminal 결론이든 approval tuple 세 변수를 삭제하고,
    Environment가 role-only, secrets `0`, pending deployments `0`인지 read-back한다.

promotion job에는 trusted executor checkout이 있지만 setup-python, pip, candidate
build/push는 없다. Environment tuple/audit preflight 실패는 OIDC 전에 중단하고,
post-OIDC provenance, artifact, Compose 또는 image 검증 실패는 SSM 전에 중단한다.
tag나 arbitrary command는 입력 또는 SSM parameter로 전달하지 않는다.

### 2.3 Stage I legacy bootstrap 완료 이력

아래 값은 당시 release의 역사 기록이며 현재 release tuple이 아니다. 현재 tuple은
`current-state.md`의 read-back 값을 사용한다.

manifest 도입 전에 게시된 아래 tuple의 1회 production check는 Stage I에서 완료됐다.

- source SHA: `90b0f00f32e8db0b327d90aa3d053f520d2d3f1b`
- exact digest:
  `ghcr.io/spicechicken/kiwoom_stock@sha256:faa437771719203165c2de57bfd8f12299ddfcc1c5d014772f1af86b3c71093d`
- candidate run/job: `30544114256` / `90875823290`

승인 tuple은 성공 직후 제거됐고 Stage II에서 compatibility code와 무소비
`candidate-<source>` producer를 삭제했다. 위 cancelled run/artifact/old ZIP은 더는
승인 가능한 release가 아니며 재생성, rerun 또는 direct SSM으로 우회하지 않는다.
새 promotion은 successful run의 strict release manifest를 반드시 사용한다.

## 3. EC2에서 실제로 검사하는 것

[deploy_runtime_check.sh](../../deploy/ec2/deploy_runtime_check.sh)는 검증된
artifact를 root:root `0755`
`/usr/local/sbin/kiwoom-production-check`에 미리 설치한다. SSM의
[`KiwoomStock-ProductionCheck`](../../deploy/ssm/production-check-document.yaml)
document만 이 경로를 실행한다. document에는 임의 command/string parameter가
없다. host command는 다음을 fail-fast로 확인한다.

- root 실행과 `/run/lock/kiwoom-stock-deploy.lock`의 non-blocking `flock`;
- Docker 존재(EC2 host의 Docker Compose 설치는 필요하지 않음);
- IMDSv2 instance ID/region이 workflow의 exact target과 일치;
- Docker filesystem free space 1536 MiB 이상;
- `MemAvailable` 256 MiB 이상;
- secret directory가 root:root `0700`;
- 두 secret가 root:root `0400`, 1~8192 bytes, regular file, hard link 1개이며
  전체 path component에 symlink가 없음;
- source SHA와 두 Compose SHA256;
- image가 exact public repository의 sha256 digest.

그 다음 exact source SHA에서 두 Compose 파일을 내려받아 전달받은 hash와 대조해
release identity와 rollback record로만 저장한다. host는 이 파일을 파싱하거나
실행하지 않는다. bounded pull 뒤 OCI revision label, image entrypoint, image user를
검사하고 check 전용 directory와 placeholder key를 만든 뒤 root-owned fixed
`docker run` 한 번만 수행한다.

```text
docker run --rm --pull never --name kiwoom-check-<sha12>-<digest12> \
  --init --no-healthcheck --user 0:0 --network none --read-only \
  --cpus 0.75 --memory 536870912 --memory-swap 536870912 --pids-limit 128 \
  --cap-drop ALL --cap-add CHOWN --cap-add SETGID --cap-add SETUID \
  --security-opt no-new-privileges:true <fixed tmpfs/mount/env flags> \
  <exact image digest> \
  python -m kiwoom_stock --check-config
```

고정 flags는 network none, read-only rootfs, CPU/memory/PID 상한, capability
allowlist, no-new-privileges, exact tmpfs와 read-only ephemeral data bind를 강제한다.
실제 host key는 mount하거나 읽지 않고 non-secret placeholder 두 개만 read-only
bind한다. EXIT/ERR/TERM trap은 exact-name container를 `docker rm -f`하고 부재를
확인한 뒤 placeholder와 check data directory를 제거한다. Compose 실행, worker
override, restart, scale, volume prune는 없다.

## 4. 성공 증거

GitHub artifact `production-check-<source SHA>-<promotion run ID>`에는 비밀이 아닌
정보만 남긴다.

- source SHA
- OCI digest와 runner에서 측정한 image size
- instance ID와 SSM command ID
- terminal status와 response code
- 시작/종료 시각
- stdout/stderr byte 수
- GitHub secret 미사용 및 worker 미활성화 선언

원격 성공 후 `/opt/kiwoom-stock/deployments/release-state.json` 하나가 atomic
replace된다. `current`와 `previous` 각각은 source SHA, image digest, OCI revision,
두 Compose hash를 모두 가진다. 별도 파일 사이 partial update가 없다. secret 내용,
placeholder 내용, raw Kiwoom 응답, OAuth token은 evidence에 포함하지 않는다.

## 5. rollback check

rollback은 이전 worker를 재활성화하는 기능이 아니다. EC2 운영자가 별도 승인된 SSM
검증 창에서 동일 preinstalled script에 `--rollback-check`,
`--expected-instance-id`, `--region`만 전달한다. image/source/hash argument는
rollback에서 허용하지 않는다. recorded `previous` tuple의 image/source/hash를
읽고 저장된 Compose 두 파일의 hash identity만 재검증한 뒤, 동일한 root-owned fixed
`docker run`으로 `--check-config`를 다시 실행하며 state를 바꾸지 않는다.

previous image가 없거나 digest 형식이 다르면 실패한다. named volume과 host secret를
삭제하지 않는다.

## 6. 비용과 보존

이 설계는 private ECR을 사용하지 않는다. public GHCR은 익명 pull을 허용하며, 현재
GitHub 문서상 public package의 Container registry 저장·대역폭은 무료 정책에
속한다. public repository의 표준 GitHub-hosted Actions도 무료 정책 범위에서
사용한다. 정책은 바뀔 수 있으므로 release 전 공식 문서를 다시 확인한다.

- <https://docs.github.com/en/packages/learn-github-packages/introduction-to-github-packages>
- <https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility>
- <https://docs.github.com/en/actions/concepts/billing-and-usage>

private repository/package, paid runner, 장기 artifact retention, private registry,
NAT Gateway, 추가 EC2/EBS/CloudWatch 자원은 이 승인 범위 밖이다. artifact retention은
14일이며 image 정리는 exact digest와 rollback 보존 조건을 먼저 확인해야 한다.

## 7. 아직 RED인 항목

production-check 성공은 아래를 증명하지 않는다.

- 시장 시간대의 장시간 worker 안정성;
- SIGTERM이 `TradingEngine.close()`까지 전달되는 real path;
- named volume의 실제 SQLite create/reopen/recovery;
- 한 process/replica 강제;
- 계좌·주문·취소 권한;
- Slack/S3/Gemini;
- 실제 매매 성과와 trading readiness.

이 항목은 [deployment boundary](deployment-boundary.md)의 shadow/live activation
gate를 별도로 통과해야 한다.
