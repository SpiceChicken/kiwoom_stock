# 키움 자격증명 관리

## 결론

현재 프로젝트는 키움 App Key와 Secret Key를 **개발자 PC, Git, 채팅, 문서, `.env`, 명령행 인자,
컨테이너 이미지, CI에 저장하지 않는다.** 개발과 CI는 `KIWOOM_API_MODE=disabled`만 사용한다.
실제 자격증명은 승인된 staging/prod-like 실행 호스트에서만 저장소 밖의 전용 파일 두 개로 전달한다.

이 문서는 값을 받거나 기록하는 장소가 아니다. Key 값을 이 문서, 이슈, PR, Slack, 이메일, 스크린샷,
터미널 명령문에 붙여 넣지 않는다.

## 환경 분리

| 환경 | API mode | 실제 key | endpoint | 현재 상태 |
|---|---|---:|---|---|
| 로컬 개발·dev Compose | `disabled` | 금지 | 없음 | config/test 전용 |
| CI | `disabled` | 금지 | 없음 | build/test/secret scan 전용 |
| 격리 staging | `mock` | mock 전용 pair만 | 코드가 mock endpoint 파생 | `NO_GO`, validator 전 |
| production-like/prod | `prod` | prod 전용 pair만 | 코드가 prod endpoint 파생 | `NO_GO`, 배포 대상 미정 |

mock과 prod는 서로 다른 발급 pair와 저장 위치를 사용한다. prod key를 mock 검증에 재사용하지 않는다.
Key가 존재해도 주문 권한은 생기지 않으며 시스템은 계속 `DISARMED`다. endpoint는 환경변수로 덮어쓸 수
없고 mode에서만 파생된다.

## 금지되는 전달 경로

값을 다음 경로로 전달하거나 복사하지 않는다.

- ChatGPT/Codex 대화, 이슈, PR comment, 메신저, 이메일;
- Git tracked/untracked 파일, 문서, test fixture, snapshot, 로그;
- `.env`, process environment, shell history, argv, command substitution;
- Dockerfile의 `ARG`/`ENV`/`RUN`, image layer, Compose interpolation 결과;
- GitHub Actions secret, artifact, cache, job output;
- 스크린샷, 화면 녹화, crash dump, hash, prefix 또는 suffix.

`KIWOOM_APP_KEY`, `KIWOOM_SECRET_KEY`, `KIWOOM_BASE_URL` process environment는 애플리케이션이
fail-fast로 거부한다. 파일 경로를 가리키는 비민감 설정과 실제 파일 내용은 구분한다.

Access-token expiry, refresh, retry, 401 replay, HTTP redaction, and typed
revoke-result semantics are owned by the
[credential lifecycle runbook](../auth/credential-lifecycle-runbook.md).

## 현재 저장 방식

배포 대상이 정해지기 전에는 특정 cloud secret manager를 선결정하지 않는다. 승인된 staging/prod-like
호스트에서는 repository 밖의 전용 absolute directory에 다음 이름의 파일만 둔다.

- `KIWOOM_APP_KEY`
- `KIWOOM_SECRET_KEY`

애플리케이션에는 디렉터리 경로만 `KIWOOM_CREDENTIALS_DIR`로 전달한다. provider는 startup에서 한 번
pair를 읽고 hot reload하지 않는다. token은 메모리에만 보관하며 파일, DB, cache, artifact에 저장하지
않는다.

파일 provider는 POSIX에서 다음 조건을 fail-closed로 검사한다.

- directory는 root 또는 effective UID 소유이고 group/world writable이 아님;
- file은 effective UID 소유 `0400`, 또는 root 소유·effective GID `0440`;
- regular file, hard link 수 1, symlink 없음, world bit·write·execute bit 없음;
- UTF-8, 8 KiB 이하, NUL/내부 개행/빈 값 없음;
- 두 파일의 읽기 전후 generation과 directory identity가 동일함.

한 파일씩 제자리에서 수정하지 않는다. 새 pair는 새 hardened directory에 함께 준비하고 process가
정지된 상태에서 directory reference를 전환한 뒤 재시작한다.

## Docker의 현재 차단점

Compose의 `${...:?}`는 host 변수가 있다는 사실만 확인한다. source path가 absolute인지, Git repository
밖인지, container의 UID/GID `10001:10001`이 target을 실제로 읽을 수 있는지, mount가 요구 mode를
보존하는지는 증명하지 못한다. Compose file-backed secret이 UID/GID/mode를 remap한다고 가정하지 않는다.

따라서 다음 실측 전 mock/prod container activation은 `BLOCKED / NO_GO / DISARMED`다.

1. 실제 target에서 effective UID/GID가 `10001:10001`인지 확인;
2. 두 target의 owner, mode, regular-file, link count와 읽기 가능 여부 확인;
3. host source가 absolute이며 repository 밖인지 host-side validator로 확인;
4. 실제 값이나 token을 출력하지 않는 config/preflight smoke 수행;
5. mock 전용 계정으로 별도 승인된 validator 수행.

Docker/배포 대상이 확정되면 중앙 secret manager를 선택하고 `CredentialProvider` adapter만 교체한다.
선택 기준은 workload identity, short-lived access, audit log, versioned rotation, least privilege, target
runtime의 file delivery 지원이다. 제품 key를 CI나 개발자에게 내려주는 구조는 선택하지 않는다.

## 자격증명 인벤토리

인벤토리는 secret과 분리된 보안 시스템에 두며 다음 비민감 metadata만 기록한다.

| 필드 | 예시 의미 |
|---|---|
| internal record ID | secret에서 파생하지 않은 임의 관리번호 |
| environment | mock 또는 prod |
| service/account scope | 사용할 키움 계정과 업무 범위 |
| owner / backup owner | 회전·폐기 책임자 |
| provider reference | secret manager의 논리적 reference 또는 승인된 host 위치 식별자 |
| issued / rotated / review date | 수명주기 날짜 |
| status | requested, active, rotation-pending, revoked |
| last validation evidence | 값 없는 validator artifact 경로 |

값, 암호화된 값의 복사본, hash, fingerprint, prefix, suffix, 길이, 일부 마스킹 표시는 기록하지 않는다.
secret-derived 식별자는 대조 편의를 제공하지만 유출 surface와 oracle을 늘리므로 사용하지 않는다.

## CI secret scan

CI는 full-SHA로 고정한 checkout 뒤 official Gitleaks `8.30.1` Linux x86_64 archive를 내려받는다.
Archive SHA-256을 압축 해제 전에, executable SHA-256을 첫 실행 전에 검증하며 mismatch와 다른 runner
architecture는 fail-closed다. Scanner는 GitHub token, PR API, Action license를 사용하지 않는다.
Default rule을 유지하면서 `.gitleaks.toml`의 Kiwoom assignment heuristic을 추가하고 finding 댓글,
summary, artifact를 만들지 않는다. 실제 key나 GitHub broker secret은 주입하지 않는다.

Checkout은 `fetch-depth: 0`을 사용하고 event pagination과 무관한 다음 redacted full-history scan 한 번을
실행한다.

```bash
gitleaks git --redact --config .gitleaks.toml --log-opts="--all HEAD" .
```

이 명령은 scanner가 설치된 격리 검증 환경에서만 실행한다. finding의 원문을 복사하지 않는다. custom
rule은 공식 key 형식 계약이 없는 heuristic이므로 scanner PASS만으로 유출이 없다고 단정하지 않는다.

### Repository governance prerequisite

Scanner policy와 검사 대상이 같은 PR에서 함께 바뀔 수 있으므로 다음 GitHub repository setting은 merge
전 외부 prerequisite다.

- `secret scan`을 required status check로 지정;
- `.gitleaks.toml`, `.github/workflows/ci.yml`, `tests/test_ci_workflow.py`,
  `tests/test_secret_scan_config.py`, 이 security 문서 변경에 보안 owner review 요구;
- required review를 우회하는 direct push와 branch rule bypass를 최소화.

이 저장소의 파일만으로 branch protection, ruleset, CODEOWNERS 적용 여부를 증명할 수 없다. 이 작업은
GitHub 외부 설정을 변경하지 않았으며 위 prerequisite가 현재 enforced라고 주장하지 않는다.

### Organization transfer prerequisite

현재 CI는 Gitleaks Action이 아니라 checksum-pinned MIT CLI를 사용하므로 personal→organization 이전이
scanner license secret이나 license-validation metadata 전송을 요구하지 않는다. 이전 전 owner는 GitHub
ruleset/required check 이름과 runner architecture, release-download network policy, checkout token 최소
권한을 다시 검증한다. Action 기반 경로로 되돌리는 변경은 license·법무·metadata boundary를 새로
검토하고 별도 승인받아야 한다.

승인 없이 `GITLEAKS_LICENSE`, `secrets.*`, Kiwoom credential을 workflow에 추가하지 않는다. 이전 결정과
검증 책임자·날짜는 secret이 아닌 repository governance record에 남긴다.

## 문서 SSOT 역할

- 이 문서: static App/Secret key custody, delivery, inventory, CI/repository governance;
- [credential rotation](../operations/credential-rotation.md): static pair rotation, 폐기, 유출 사고 대응;
- [credential lifecycle runbook](../auth/credential-lifecycle-runbook.md): in-memory token, HTTP, refresh/retry,
  typed revoke result.

Static pair의 회전·사고 순서는 이 문서에서 반복하지 않고 operations runbook을 따른다.

## 관련 문서

- 회전과 사고 대응: [credential-rotation.md](../operations/credential-rotation.md)
- token/HTTP lifecycle: [credential-lifecycle-runbook.md](../auth/credential-lifecycle-runbook.md)
- 설정 계약: [configuration.md](../configuration.md)
- 컨테이너 차단점: [container-development.md](../container-development.md)
- 배포 승인 경계: [deployment-boundary.md](../operations/deployment-boundary.md)
