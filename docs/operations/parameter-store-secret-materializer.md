# Parameter Store 비밀 materializer 운영 가이드

이 문서는 EC2 host가 AWS Systems Manager Parameter Store의 SecureString 두 개를
컨테이너가 읽는 임시 파일로 바꾸는 절차를 정의한다. 이 단계에서는 GitHub
Actions가 Kiwoom 키를 읽지 않으며, 실제 주문도 활성화하지 않는다.

## 전달 경계

```text
Parameter Store SecureString
  -> EC2 instance profile
  -> materialize_kiwoom_secrets.py
  -> host /run/kiwoom-stock/credentials (root:root, directory 0700/files 0400)
  -> Docker read-only mount
  -> root container entrypoint staging copy (10001:10001, 0700/files 0400)
  -> StrictFileCredentialProvider
```

컨테이너에는 AWS 자격증명, IMDS 접근 권한 또는 평문 환경변수를 전달하지 않는다.
parameter 값은 명령행, shell history, systemd unit, GitHub 변수, stdout, journald에
기록하지 않는다.

## 설치 전 조건

- EC2는 SSM Session Manager로 접속 가능해야 한다.
- instance role에는 `AmazonSSMManagedInstanceCore`를 연결하지 않고
  `ec2-ssm-core-no-parameter-read-policy.json.example`의 custom core를 사용해야
  한다. managed policy의 wildcard parameter read와 exact runtime allow를 함께
  두면 IAM Allow 합집합 때문에 경로 제한이 성립하지 않는다.
- instance role은 다음 네 경로 중 해당 환경의 정확한 두 parameter에 대해서만
  `ssm:GetParameters`를 허용해야 한다.
- `/etc/kiwoom/kiwoom-secrets.conf`는 root 소유, `0640` 이하 권한이어야 한다.
- `/usr/local/libexec/materialize_kiwoom_secrets.py`는 root 소유이고 일반 사용자가
  수정할 수 없어야 한다.
- `/opt/kiwoom-stock/.venv/bin/python`은 검토된 pip 환경이며 `boto3`를 포함해야 한다.
- systemd의 `RuntimeDirectory=kiwoom-stock`이 격리 namespace를 만들기 전에
  `/run/kiwoom-stock`을 `root:root 0700`으로 생성한다.
- `/run/kiwoom-stock`은 systemd 서비스의 `ReadWritePaths` 범위 안에 있고,
  `RuntimeDirectoryPreserve=yes`로 oneshot 종료 뒤에도 credential 파일을
  컨테이너 시작 시점까지 유지한다.
- 실제 운영 설치 전에 disk 여유, Docker log rotation, snapshot/복구 지점을
  확인해야 한다.

## SecureString 초기 등록

먼저 값 없이 metadata만 확인한다.

```bash
.venv/bin/python tools/bootstrap_kiwoom_parameters.py \
  --profile kiwoom-local \
  --region ap-northeast-2 \
  --check
```

두 항목이 모두 `missing`인 경우에만 초기 등록을 실행한다.

```bash
.venv/bin/python tools/bootstrap_kiwoom_parameters.py \
  --profile kiwoom-local \
  --region ap-northeast-2
```

도구가 App Key와 Secret Key를 각각 숨김 입력으로 요청한다. 값을 chat, shell
argument, 환경변수 또는 임시 파일에 붙여 넣지 않는다. 도구는 AWS CLI login
profile에서 단기 자격증명을 메모리로 전달하고 장기 AWS Access Key를 만들지 않는다.

rotation은 두 parameter가 모두 존재할 때만 `--overwrite`로 실행한다. rotation
도중 오류가 나면 애플리케이션과 materializer를 재시작하지 않고 두 parameter
version 상태를 먼저 확인한다.

## 파일 설치

구현 파일을 검토된 release artifact에서 설치한다. EC2 checkout에서 `git pull`로
덮어쓰지 않는다.

```text
/usr/local/libexec/materialize_kiwoom_secrets.py
/etc/systemd/system/kiwoom-secrets.service
/etc/kiwoom/kiwoom-secrets.conf
```

`kiwoom-secrets.conf`에는 값이 아니라 parameter 이름, 대상 디렉터리, UID/GID와
비밀이 아닌 AWS region만 둔다. 예시는
[kiwoom-secrets.conf.example](../../deploy/ec2/kiwoom-secrets.conf.example)에 있다.

전용 venv는 pip 기준으로 설치한다.

```bash
sudo python3 -m venv /opt/kiwoom-stock/.venv
sudo /opt/kiwoom-stock/.venv/bin/python -m pip install --upgrade pip boto3
```

설치 후 권한을 확인한다.

```bash
sudo chown root:root /usr/local/libexec/materialize_kiwoom_secrets.py
sudo chmod 0750 /usr/local/libexec/materialize_kiwoom_secrets.py
sudo chown root:root /etc/kiwoom/kiwoom-secrets.conf
sudo chmod 0640 /etc/kiwoom/kiwoom-secrets.conf
sudo systemctl daemon-reload
sudo systemctl enable kiwoom-secrets.service
```

`systemctl start` 전에는 대상 parameter가 실제로 존재하는지와 IAM 정책이 정확히
제한되어 있는지 별도 read-only 점검을 완료한다.

## 실행 및 검증

```bash
sudo systemctl start kiwoom-secrets.service
sudo systemctl status kiwoom-secrets.service --no-pager
sudo stat -c '%U:%G %a %n' /run/kiwoom-stock/credentials/*
```

host producer 성공 조건은 디렉터리 `root:root 0700`, 두 파일
`root:root 0400`이다. root launcher의 `docker/validate_secret_paths.py`가 이
metadata를 허용한 뒤 Docker read-only secret mount로 전달한다. 컨테이너는 root
entrypoint가 source를 읽어 별도 tmpfs staging에 복사하고 그 staging 디렉터리와
두 파일만 `10001:10001`, `0700`/`0400`으로 바꾼 다음 UID/GID 10001로 권한을
낮춘다. host source를 UID 10001 소유로 만들거나 strict provider 계약을 완화하지
않는다.
파일 내용, 길이, 일부 문자열을 로그나 검증 출력에 포함하지 않는다. 서비스 실패 시
애플리케이션을 시작하지 않고 SSM command 결과와 비민감 오류만 수집한다.

## 교체와 롤백

1. Kiwoom에서 새 키 pair를 발급하고 기존 pair는 아직 폐기하지 않는다.
2. 승인된 secret manager 절차로 두 SecureString을 갱신한다.
3. `kiwoom-secrets.service`를 재실행하여 두 파일을 원자적으로 교체한다.
4. 애플리케이션 `--check-config`와 읽기 전용 validator를 실행한다.
5. 문제가 없을 때만 이전 키를 폐기한다.

유출이 의심되는 경우에는 이전 version을 복구하지 않는다. Kiwoom에서 pair를
즉시 폐기·재발급하고, Parameter Store version과 materializer 로그에는 값이 아닌
민감정보 없는 사건 ID만 남긴다.

## 복구

- 재부팅하면 `/run`이 비워지므로 systemd oneshot이 다시 materialize해야 한다.
- Parameter Store 접근이 실패하면 fail-closed하고 오래된 파일을 자동으로 사용하지
  않는다.
- image 배포가 실패하면 이전 immutable image digest로 되돌린다.
- IAM 정책을 복구할 때도 광범위한 `AmazonS3FullAccess`를 기본 복구 정책으로
  되살리지 않는다.

## 금지사항

- GitHub Secrets에 Kiwoom 값을 복제하지 않는다.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, Kiwoom 값을 workflow 환경변수로
  전달하지 않는다.
- `journalctl`, `docker inspect`, process argv/env, shell history에 값을 남기지
  않는다.
- 실제 주문 경로가 열려 있는 상태에서 이 materializer를 운영 검증에 사용하지
  않는다.
