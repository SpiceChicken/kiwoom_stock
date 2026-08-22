# AWS EC2 수동 생성·복구 가이드

이 문서는 AWS 콘솔에서 Kiwoom 호스트를 **한 단계씩 직접 생성하거나 복구**하는
절차다. [`apply_clean_rebuild.sh`](../../deploy/ec2/apply_clean_rebuild.sh)를
사용하는 경우에는 이 콘솔 절차와 섞지 않는다. 각 단계가 끝날 때 생성된 ID를
기록하고, 같은 단계를 두 번 실행하지 않는다.

## 현재 live 호스트 우선 기준

현재 운영 대상은
[`현재 운영 기준선`](current-state.md)에 기록된
서울 리전의 단일 EC2다. exact instance ID와 주소는 AWS/private operator
inventory에서 확인한다. 사람용 접속은 `ubuntu`에 대한 직접 SSH이며,
다음 helper를 사용한다.

```bash
./tools/ssh-direct-shell.sh
```

현재 live 호스트에는 관리 PC의 TCP 22 `/32` inbound와 public-key SSH가
설정돼 있다. SSM Agent/SSM command는 GitHub 자동화와 health 확인 때문에 남아
있으며, 사람용 Session Manager shell은 사용하지 않는다.

이 문서의 아래 생성 절차와 [`apply_clean_rebuild.sh`](../../deploy/ec2/apply_clean_rebuild.sh)는
SSH key pair 주입, 관리 `/32`, cloud-init SSH hardening, SG read-back을 포함하는
현재 재생성 계약이다. `--check`는 로컬 검증만 수행하며, `--apply`는 별도 승인된
재생성 창에서만 실행한다. 현재 live host에는 재실행하지 않는다.

## 1. 재생성에서 만들 리소스

다음 리소스만 만든다.

| 리소스 | 이름 | 설정 |
|---|---|---|
| VPC | `kiwoom-prod-vpc` | `10.77.0.0/20` |
| Public subnet | `kiwoom-prod-public-2a` | `10.77.0.0/24`, Seoul `2a` |
| Internet Gateway | `kiwoom-prod-igw` | VPC에 1개 연결 |
| Route table | `kiwoom-prod-public-rt` | `0.0.0.0/0 → IGW` |
| Security Group | `kiwoom-prod-https-egress` | inbound TCP 22 관리 `/32`, outbound TCP 443 |
| Network Interface | `kiwoom-prod-primary-eni` | 위 subnet과 SG 사용 |
| Elastic IP | `kiwoom-prod-eip` | 위 ENI에 먼저 연결 |
| EC2 | `kiwoom-stock-prod` | Ubuntu 24.04, `t3.micro` |
| EBS | EC2 root volume | encrypted gp3 8 GiB |

다음 리소스는 만들지 않는다.

- NAT Gateway
- VPC Endpoint
- Load Balancer
- RDS
- Bastion host
- repository 안에서 private SSH key를 생성하거나 저장
- Snapshot
- 추가 EIP 또는 임시 public IPv4
- 유료 상세 모니터링과 CloudWatch Logs

비용 예외는 `t3.micro`, gp3 8 GiB, EIP 1개뿐이다. VPC, subnet, Internet
Gateway, route table, security group 자체에는 시간당 리소스 요금이 없다.
EIP는 할당하는 즉시 사용 여부와 관계없이 요금이 발생한다.

## 2. 시작하기 전에

### 2.1 리전을 서울로 고정

AWS 콘솔 오른쪽 위 리전을 다음 값으로 선택한다.

```text
Asia Pacific (Seoul) / ap-northeast-2
```

다른 리전에서 만든 리소스는 서울 리전의 EC2에서 사용할 수 없다. 이후 모든
화면에서 오른쪽 위 리전이 `서울`인지 확인한다.

### 2.2 생성 기록표 준비

아래 표를 메모장에 복사한다. 생성 직후 ID와 상태를 기록한다.

| 항목 | 생성된 값 | 완료 |
|---|---|---|
| VPC ID |  | [ ] |
| Subnet ID |  | [ ] |
| Internet Gateway ID |  | [ ] |
| Route Table ID |  | [ ] |
| Security Group ID |  | [ ] |
| Network Interface ID |  | [ ] |
| EIP Allocation ID |  | [ ] |
| 새 EIP 주소 |  | [ ] |
| Kiwoom 허용 IP 등록 |  | [ ] |
| EC2 Instance ID |  | [ ] |
| Root Volume ID |  | [ ] |
| SSM Online 확인 |  | [ ] |
| cloud-init 완료 확인 |  | [ ] |

오류가 나더라도 처음부터 다시 만들지 않는다. 이 표와 AWS 콘솔에서 이미 생성된
리소스를 확인한 뒤 실패한 단계부터 이어서 수행한다.

### 2.3 IAM instance profile 확인

1. AWS 콘솔에서 `IAM`을 연다.
2. 왼쪽 메뉴에서 `Roles`를 선택한다.
3. `kiwoom-stock-ec2-role`을 검색한다.
4. 다음 항목을 확인한다.
   - `AmazonSSMManagedInstanceCore`가 연결돼 있지 않음
   - 프로젝트에서 만든 `KiwoomStockSsmCoreWithoutParameterRead` inline policy 존재
   - 프로젝트에서 만든 `KiwoomStockRuntimeMinimal` inline policy 존재
   - `AmazonS3FullAccess` 없음
   - `AmazonSSMReadOnlyAccess` 없음
5. 역할이 없거나 이름이 다르면 여기서 중단한다.

App Key나 Secret Key는 이 과정에서 입력하지 않는다.

## 3. VPC 만들기

1. AWS 콘솔에서 `VPC`를 검색해 연다.
2. 왼쪽 메뉴에서 `Your VPCs`를 선택한다.
3. `Create VPC`를 누른다.
4. `Resources to create`에서 **VPC only**를 선택한다.
   - `VPC and more`를 선택하면 불필요한 NAT Gateway가 함께 생성될 수 있다.
5. 다음 값을 입력한다.

| 화면 항목 | 입력값 |
|---|---|
| Name tag | `kiwoom-prod-vpc` |
| IPv4 CIDR manual input | `10.77.0.0/20` |
| IPv6 CIDR block | `No IPv6 CIDR block` |
| Tenancy | `Default` |

6. 태그가 보이면 다음 값을 추가한다.

| Key | Value |
|---|---|
| `Project` | `kiwoom-stock` |
| `Environment` | `production` |
| `ManagedBy` | `manual-console` |

7. `Create VPC`를 누른다.
8. 생성된 `vpc-...` 값을 기록표에 적는다.

### 3.1 DNS 옵션 켜기

1. 생성한 `kiwoom-prod-vpc`를 선택한다.
2. `Actions → Edit VPC settings`를 누른다.
3. 다음 두 옵션을 모두 켠다.
   - `Enable DNS resolution`
   - `Enable DNS hostnames`
4. 저장한다.

두 옵션이 꺼져 있으면 SSM, Ubuntu 패키지 저장소, Kiwoom API 도메인을 정상적으로
찾지 못한다.

## 4. Public subnet 만들기

1. VPC 콘솔 왼쪽에서 `Subnets`를 선택한다.
2. `Create subnet`을 누른다.
3. VPC는 `kiwoom-prod-vpc`를 선택한다.
4. 다음 값을 입력한다.

| 화면 항목 | 입력값 |
|---|---|
| Subnet name | `kiwoom-prod-public-2a` |
| Availability Zone | `ap-northeast-2a` |
| IPv4 subnet CIDR block | `10.77.0.0/24` |

5. subnet을 생성하고 `subnet-...` 값을 기록한다.
6. 생성한 subnet을 선택한다.
7. `Actions → Edit subnet settings`를 누른다.
8. `Enable auto-assign public IPv4 address`가 **꺼져 있는지** 확인한다.
9. 저장한다.

EIP 하나만 사용해야 하므로 subnet의 임시 public IPv4 자동 할당은 끈다.

## 5. Internet Gateway 만들기

1. VPC 콘솔 왼쪽에서 `Internet gateways`를 선택한다.
2. `Create internet gateway`를 누른다.
3. 이름은 `kiwoom-prod-igw`로 입력한다.
4. 위 공통 태그 세 개를 추가한다.
5. 생성하고 `igw-...` 값을 기록한다.
6. 생성한 IGW를 선택한다.
7. `Actions → Attach to a VPC`를 누른다.
8. `kiwoom-prod-vpc`를 선택하고 연결한다.
9. IGW의 상태가 `Attached`인지 확인한다.

## 6. Public route table 만들기

1. VPC 콘솔 왼쪽에서 `Route tables`를 선택한다.
2. `Create route table`을 누른다.
3. 다음 값을 입력한다.

| 화면 항목 | 입력값 |
|---|---|
| Name | `kiwoom-prod-public-rt` |
| VPC | `kiwoom-prod-vpc` |

4. 생성하고 `rtb-...` 값을 기록한다.
5. 생성한 route table을 선택한다.
6. `Routes` 탭에서 `Edit routes`를 누른다.
7. `Add route`를 누르고 다음 값을 입력한다.

| Destination | Target |
|---|---|
| `0.0.0.0/0` | `Internet Gateway` → `kiwoom-prod-igw` |

8. 저장한다.
9. `Subnet associations` 탭을 연다.
10. `Edit subnet associations`를 누른다.
11. `kiwoom-prod-public-2a`만 선택하고 저장한다.

정상적인 route table에는 다음 두 경로가 보인다.

```text
10.77.0.0/20  → local
0.0.0.0/0     → kiwoom-prod-igw
```

### 6.1 Route table이 두 개 보이는 이유

VPC를 만들면 AWS가 main route table을 자동으로 하나 만든다. 이 가이드에서
public route table을 추가하면 일시적으로 두 개가 보이는 것이 정상이다.

main route table은 바로 삭제할 수 없다. 하나만 남기려면 다음 순서가 필요하다.

1. `kiwoom-prod-public-rt`를 새 main route table로 지정한다.
2. 기존 자동 생성 table이 더 이상 main이 아닌지 확인한다.
3. 기존 table에 subnet 또는 gateway association이 없는지 확인한다.
4. 기존 table을 삭제한다.

두 route table 자체에는 시간당 요금이 없으므로 삭제는 비용 절감이 아니라 구성
정리 목적이다. 향후 private subnet을 추가할 계획이라면 local route만 가진 기존
main table을 그대로 두는 방식이 더 안전할 수 있다.

### 6.2 `kiwoom-prod-public-rt`를 main으로 지정

1. VPC 콘솔 왼쪽에서 `Route tables`를 선택한다.
2. 이름이 `kiwoom-prod-public-rt`인 table을 선택한다.
3. 다음 route 두 개가 있는지 확인한다.

```text
10.77.0.0/20  → local
0.0.0.0/0     → kiwoom-prod-igw
```

4. `Actions → Set main route table`을 선택한다.
5. 확인창에 요구되는 단어를 입력하고 변경한다.
6. Route table 목록의 `Main` 열에서 `kiwoom-prod-public-rt`가 `Yes`인지 확인한다.
7. subnet `kiwoom-prod-public-2a`의 `Route table` 탭에서도 이 table이 적용되는지
   확인한다.

잘못된 table에 `Set main route table`을 실행하면 새 subnet의 인터넷 경로가
사라질 수 있다. 이름뿐 아니라 ID와 route를 함께 확인한다.

### 6.3 이전 자동 생성 route table 삭제

2026-07-26 read-only 확인 기준으로 현재 상태는 다음과 같다.

| 구분 | Route table ID | Main | Route | 처리 |
|---|---|---:|---|---|
| 유지 | `rtb-0a5bed4f305382094` | Yes | local + IGW | 삭제 금지 |
| 삭제 대상 | `rtb-0f622507cf42037c7` | No | local만 존재 | 삭제 가능 |

현재는 이미 `kiwoom-prod-public-rt`가 main이고, 삭제 대상 table에는 subnet 및
gateway association이 없다. 따라서 별도의 main 설정 해제 작업은 필요하지 않다.

삭제 절차:

1. VPC 콘솔의 `Route tables`에서 `rtb-0f622507cf42037c7`을 선택한다.
2. 이름이 없고 `Main=No`인지 확인한다.
3. `Routes` 탭에 `10.77.0.0/20 → local`만 있는지 확인한다.
4. `Subnet associations` 탭에 explicit association이 없는지 확인한다.
5. `Edge associations` 또는 gateway association이 없는지 확인한다.
6. `Actions → Delete route table`을 선택한다.
7. 삭제 확인창에서 ID가 `rtb-0f622507cf42037c7`인지 다시 확인한다.
8. 삭제한다.
9. 목록을 새로 고쳐 route table이 다음 한 개만 남았는지 확인한다.

```text
rtb-0a5bed4f305382094
Name: kiwoom-prod-public-rt
Main: Yes
Routes:
  10.77.0.0/20 → local
  0.0.0.0/0 → igw-07487a35c12c00901
```

삭제 버튼이 비활성화되거나 dependency 오류가 나오면 강제로 진행하지 않는다.
삭제 대상이 아직 main이거나 association이 남아 있다는 의미이므로 6.2절부터 다시
확인한다.

## 7. Security Group 만들기

현재 live 호스트와 재생성 계약 모두 사람용 SSH를 위해 관리 PC의 TCP 22 `/32`가
필요하다. 현재 SG 상태를 변경할 때는 [현재 운영 기준선](current-state.md)과
AWS `describe-security-groups` read-back을 기준으로 판단한다.

1. EC2 콘솔 또는 VPC 콘솔에서 `Security Groups`를 연다.
2. `Create security group`을 누른다.
3. 다음 값을 입력한다.

| 화면 항목 | 입력값 |
|---|---|
| Security group name | `kiwoom-prod-https-egress` |
| Description | `Kiwoom production SSH admin and HTTPS egress` |
| VPC | `kiwoom-prod-vpc` |

4. `Inbound rules`에는 다음 규칙을 정확히 하나만 추가한다.

| Type | Protocol | Port | Source |
|---|---|---:|---|
| SSH | TCP | 22 | 현재 관리 PC의 정확한 IPv4 `/32` |

   - `0.0.0.0/0` SSH는 추가하지 않는다.
   - HTTPS 443 inbound도 추가하지 않는다.
5. 기본 `Outbound rules`의 `All traffic / 0.0.0.0/0` 규칙을 삭제한다.
6. outbound 규칙을 정확히 하나만 추가한다.

| Type | Protocol | Port | Destination |
|---|---|---:|---|
| HTTPS | TCP | 443 | `0.0.0.0/0` |

7. 공통 태그 세 개를 추가하고 생성한다.
8. `sg-...` 값을 기록한다.
9. 생성 후 다시 열어 다음 상태를 확인한다.

```text
Inbound rules: TCP 22, current-admin-ip/32 한 개
Outbound rules: TCP 443, 0.0.0.0/0 한 개
```

규칙이 다르면 ENI와 EC2를 만들기 전에 수정한다.

### 7.1 잘못 연결한 default Security Group 교체 (historical legacy host)

Security Group은 EC2가 실행 중이어도 교체할 수 있다. 기존 group부터 삭제하지
말고, 올바른 group을 연결하는 작업과 기존 group을 제거하는 작업을 같은 변경
화면에서 수행한다.

2026-07-26 read-only 확인 기준의 이전 host snapshot:

이 표의 instance/ENI/default SG는 현재 live host의 상태가 아니다. 현재 live
호스트의 SG와 SSH `/32`는 AWS read-back과 [current-state.md](current-state.md)의
비공개 inventory 원칙을 기준으로 확인한다. exact 값은 공개 문서에 적지 않는다.

| 구분 | Security Group ID | Group name | 규칙 |
|---|---|---|---|
| 제거할 연결 | `sg-0db4906725eeaae6e` | `default` | inbound self, outbound all |
| 새로 연결 | `sg-0339fcff47697d77f` | `kiwoom-prod-https-egress` | inbound 0, outbound TCP 443 |
| EC2 | `i-0ed33d5f18e5542ed` | `kiwoom-stock-prod` | 현재 default group 연결 |
| Primary ENI | `eni-0001f440c3bfcdd7a` |  | EC2에 연결 |

두 group 모두 화면의 Name tag가 `kiwoom-prod-https-egress`로 보일 수 있으므로
이름만 보고 선택하지 않는다. 반드시 **Group ID**와 **Group name**을 함께
확인한다.

교체 절차:

1. EC2 콘솔에서 `인스턴스`를 선택한다.
2. `i-0ed33d5f18e5542ed / kiwoom-stock-prod`를 선택한다.
3. `작업 → 보안 → 보안 그룹 변경`을 선택한다.
4. 연결된 보안 그룹 목록에 `sg-0339fcff47697d77f`을 추가한다.
5. 기존 `sg-0db4906725eeaae6e / default`를 목록에서 제거한다.
6. 저장 직전 연결 목록이 다음 한 개뿐인지 확인한다.

```text
sg-0339fcff47697d77f
Group name: kiwoom-prod-https-egress
```

7. 저장한다.
8. EC2의 `보안` 탭을 새로 고쳐 새 group 한 개만 표시되는지 확인한다.
9. Session Manager 연결이 계속 가능한지 확인한다.

EC2 화면에서 변경 메뉴를 찾기 어렵다면 다음 경로를 사용한다.

```text
EC2
→ 네트워크 및 보안
→ 네트워크 인터페이스
→ eni-0001f440c3bfcdd7a 선택
→ 작업
→ 보안 그룹 변경
```

여기에서도 새 group만 남기고 저장한다. EC2와 ENI 화면에서 각각 한 번씩 변경할
필요는 없다. 둘은 같은 primary ENI의 Security Group 연결을 보여준다.

### 7.2 기존 default Security Group은 삭제할 수 없음

`sg-0db4906725eeaae6e`의 실제 Group name은 `default`다. VPC를 만들 때 AWS가
자동 생성한 default Security Group은 VPC가 존재하는 동안 삭제할 수 없다.

연결 교체 후 다음과 같이 정리한다.

1. `Security Groups`에서 `sg-0db4906725eeaae6e`을 선택한다.
2. `Group name=default`이고 연결된 ENI 또는 EC2가 없는지 확인한다.
3. 잘못 추가한 `Name=kiwoom-prod-https-egress` 태그를 삭제하거나
   `Name=kiwoom-prod-default-unused`로 바꾼다.
4. 사용하지 않을 default group의 inbound self-reference 규칙을 삭제한다.
5. outbound `All traffic → 0.0.0.0/0` 규칙도 삭제한다.
6. group 자체는 남겨 둔다.

default group은 존재만으로 비용이 발생하지 않는다. 나중에 VPC 전체를 삭제하면
default Security Group도 VPC와 함께 제거된다.

## 8. Network Interface를 먼저 만들기

EIP를 EC2보다 먼저 연결해 두면 EC2의 초기화 스크립트가 공인 IP 없이 실행되는
순서 경쟁을 방지할 수 있다.

1. EC2 콘솔 왼쪽에서 `Network Interfaces`를 선택한다.
2. `Create network interface`를 누른다.
3. 다음 값을 입력한다.

| 화면 항목 | 입력값 |
|---|---|
| Description | `kiwoom-prod-primary-eni` |
| Subnet | `kiwoom-prod-public-2a` |
| Private IPv4 address | 자동 선택 |
| Security groups | `kiwoom-prod-https-egress`만 선택 |

4. 공통 태그 세 개와 다음 Name 태그를 추가한다.

| Key | Value |
|---|---|
| `Name` | `kiwoom-prod-primary-eni` |

5. 생성하고 `eni-...` 값을 기록한다.
6. ENI 상태가 `Available`인지 확인한다.

## 9. 새 EIP 할당 및 ENI 연결

이 단계부터 EIP 요금이 발생한다. 중간에 작업을 멈출 예정이라면 EIP 할당을
나중에 수행한다.

1. EC2 콘솔 왼쪽에서 `Elastic IPs`를 선택한다.
2. `Allocate Elastic IP address`를 누른다.
3. 다음 값을 사용한다.
   - Public IPv4 address pool: `Amazon's pool of IPv4 addresses`
   - Network border group: 서울 리전 기본값
4. 공통 태그와 `Name=kiwoom-prod-eip`을 추가한다.
5. `Allocate`를 누른다.
6. 다음 두 값을 기록한다.
   - 새 공인 IPv4 주소
   - `eipalloc-...` Allocation ID
7. 방금 만든 EIP를 선택한다.
8. `Actions → Associate Elastic IP address`를 누른다.
9. 다음 값을 선택한다.

| 화면 항목 | 선택값 |
|---|---|
| Resource type | `Network interface` |
| Network interface | `kiwoom-prod-primary-eni` |
| Private IP address | ENI의 기본 private IPv4 |
| Allow this Elastic IP address to be reassociated | 끔 |

10. 연결한다.
11. EIP 상세 화면의 `Network interface ID`가 기록한 `eni-...`와 같은지 확인한다.

## 10. Kiwoom 관리 화면에 허용 IP 등록

EC2를 만들기 전에 새 EIP를 Kiwoom 허용 IP로 등록한다.

1. Kiwoom API 관리 화면에 로그인한다.
2. App Key가 속한 애플리케이션의 허용 IP 관리 화면을 연다.
3. 9단계에서 받은 새 EIP 주소를 등록한다.
4. mock App Key와 production App Key가 분리돼 있다면 사용할 환경에 맞는
   애플리케이션에 등록한다.
5. 저장 후 화면에 새 주소가 표시되는지 확인한다.
6. 등록 완료 여부를 기록표에 표시한다.

App Key와 Secret Key는 이 문서, 채팅, 터미널 명령, AWS 태그에 기록하지 않는다.
허용 IP 등록 전에는 토큰 발급이나 API 검증을 실행하지 않는다.

## 11. EC2 만들기

### 11.1 기본 설정

1. EC2 콘솔에서 `Instances`를 선택한다.
2. `Launch instances`를 누른다.
3. 다음 값을 입력한다.

| 화면 영역 | 입력값 |
|---|---|
| Name | `kiwoom-stock-prod` |
| AMI | `ami-05fa22e12f2cb12aa` |
| AMI 이름 확인 | `ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20260714` |
| AMI owner 확인 | Canonical `099720109477` |
| Architecture | `64-bit (x86)` |
| Instance type | `t3.micro` |
| Key pair | 기존 승인된 `<EC2_SSH_KEY_PAIR_NAME>` |

AMI 검색 결과의 owner가 Canonical이 아니거나 이름·아키텍처가 다르면 중단한다.

### 11.2 Network 설정

1. `Network settings`에서 `Edit`을 누른다.
2. VPC는 `kiwoom-prod-vpc`를 선택한다.
3. subnet은 `kiwoom-prod-public-2a`를 선택한다.
4. `Auto-assign public IP`는 `Disable`로 둔다.
5. 고급 network 설정에서 **기존 network interface**를 선택한다.
6. `kiwoom-prod-primary-eni`를 primary interface, device index `0`으로 선택한다.
7. 새 security group이나 두 번째 ENI를 추가하지 않는다.

기존 ENI를 primary interface로 선택하는 항목을 찾을 수 없다면 EC2를 생성하지
말고 중단한다. 임시 public IP로 대신 진행하지 않는다.

### 11.3 Root volume 설정

`Configure storage`에서 root volume을 다음과 같이 설정한다.

| 항목 | 값 |
|---|---|
| Size | `8 GiB` |
| Volume type | `gp3` |
| IOPS | `3000` |
| Throughput | `125 MiB/s` |
| Encrypted | 켬 |
| KMS key | 기본 AWS managed key |
| Delete on termination | 켬 |

추가 volume은 만들지 않는다.

### 11.4 Advanced details

`Advanced details`를 열고 다음 값을 확인한다.

| 항목 | 값 |
|---|---|
| IAM instance profile | `kiwoom-stock-ec2-role` |
| Shutdown behavior | `Stop` |
| Stop - Hibernate behavior | `Disable` |
| Termination protection | 필요 시 켬; 비용 중단 기능은 아님 |
| Detailed CloudWatch monitoring | 끔 |
| Credit specification | `Standard` |
| Metadata accessible | `Enabled` |
| Metadata version | `V2 only (token required)` |
| Metadata response hop limit | `1` |
| Allow tags in instance metadata | `Disabled` |

`t3.micro`의 기본 CPU 설정은 Unlimited일 수 있다. 반드시 `Standard`를 선택한다.
이 프로젝트는 컨테이너가 IMDS에 접근하지 않고 host의 SSM/materializer만
사용하므로 hop limit을 `1`로 둔다.

### 11.5 User data

`User data`에는 저장소의 다음 파일 **전체 내용**을 붙여 넣는다.

[cloud-init-ubuntu-24.04.sh](../../deploy/ec2/cloud-init-ubuntu-24.04.sh)

주의:

- 파일 경로나 파일 이름만 입력하면 안 된다.
- App Key, Secret Key, AWS key를 추가하면 안 된다.
- `docker compose up`이나 실제 애플리케이션 실행 명령을 추가하면 안 된다.
- 붙여 넣은 첫 줄이 `#!/usr/bin/env bash`인지 확인한다.

### 11.6 최종 요약 확인 후 생성

오른쪽 Summary에서 다음을 마지막으로 확인한다.

```text
Instances: 1
AMI: Canonical Ubuntu 24.04 amd64
Instance type: t3.micro
Key pair: 기존 승인된 EC2 SSH key pair
Existing primary ENI: kiwoom-prod-primary-eni
Public IP 자동 할당: 꺼짐
Root disk: encrypted gp3 8 GiB
Additional disk: 없음
```

모두 맞으면 `Launch instance`를 한 번만 누른다. 화면이 늦게 반응하더라도 다시
누르지 말고 `Instances` 목록에서 생성 여부를 먼저 확인한다.

생성된 `i-...` Instance ID와 `vol-...` Volume ID를 기록한다.

## 12. 생성 직후 확인

### 12.0 현재 live 호스트 점검

현재 호스트의 점검은 새 instance를 만들지 않고 SSH로 수행한다.

```bash
./tools/ssh-direct-shell.sh
cloud-init status --wait
sudo systemctl is-active docker.service
sudo systemctl is-active snap.amazon-ssm-agent.amazon-ssm-agent.service
df -h /
df -ih /
docker ps -a
docker image ls
```

SSM Online은 GitHub 자동화의 control-plane health로 별도 확인할 수 있지만,
사람용 shell을 위해 `aws ssm start-session`을 실행하지 않는다. 현재 host의
production-check와 shadow rollout은 SSH transport로 사전 검증됐고, GitHub
workflow의 canonical execution backend는 계속 SSM이다.

### 12.1 EC2 콘솔

EC2 instance 상세에서 다음 값을 확인한다.

| 확인 항목 | 정상값 |
|---|---|
| Instance state | `Running` |
| Instance type | `t3.micro` |
| Availability Zone | `ap-northeast-2a` |
| Public IPv4 | Kiwoom에 등록한 새 EIP |
| VPC | `kiwoom-prod-vpc` |
| Subnet | `kiwoom-prod-public-2a` |
| Security group | `kiwoom-prod-https-egress` |
| IAM role | `kiwoom-stock-ec2-role` |
| IMDSv2 | `Required` |
| Root volume | encrypted gp3 8 GiB |

값이 다르면 애플리케이션을 설치하지 않는다.

### 12.2 SSM Online 확인

1. AWS 콘솔에서 `Systems Manager`를 연다.
2. `Fleet Manager → Managed nodes`를 선택한다.
3. 새 EC2가 나타나는지 확인한다.
4. `Ping status` 또는 node 상태가 `Online`인지 확인한다.

부팅 후 표시까지 몇 분 걸릴 수 있다. 10분 후에도 나타나지 않으면 새 EC2를
추가 생성하지 말고 다음 항목을 확인한다.

- EIP가 primary ENI에 연결돼 있는지
- route table의 `0.0.0.0/0 → IGW`
- SG outbound TCP 443
- IAM role의 `KiwoomStockSsmCoreWithoutParameterRead` inline policy와
  `ssmmessages`/`ec2messages` channel 권한
- EC2 system log와 cloud-init log

### 12.3 Session Manager health (자동화 의존성)

사람용 shell 접속 단계가 아니다. Systems Manager의 managed-node가 `Online`인지
확인하는 자동화 의존성 점검으로만 남긴다. 현재 live host와 새 호스트의 사람용
접속은 앞의 12.0절 SSH 절차를 사용한다. SSM Online과 SSH 연결을 모두 확인하기
전에는 application이나 secret을 설치하지 않는다.

접속 후 다음 명령만 실행한다.

```bash
cloud-init status --wait
sudo test -f /var/lib/kiwoom-stock/cloud-init-complete
sudo systemctl is-active amazon-ssm-agent.service \
  || sudo systemctl is-active snap.amazon-ssm-agent.amazon-ssm-agent.service
sudo systemctl is-active docker.service
python3 --version
docker --version
docker compose version
df -h /
free -h
```

성공 기준:

- `cloud-init status`가 `done`
- completion marker 존재
- SSM Agent와 Docker가 `active`
- root disk 여유 공간이 최소 3 GiB 이상
- 메모리 또는 서비스 오류 없음

이 단계에서는 App Key/Secret Key, Parameter Store 값, Kiwoom token을 조회하지
않는다.

### 12.4 Root EBS가 암호화되지 않았을 때

AWS control-plane의 `Encrypted` 값이 `false`이면 host 명령이 모두 성공해도
운영 admission은 실패다. 기존 EBS volume의 암호화 여부는 실행 중에 제자리에서
바꿀 수 없다.

2026-07-26에 기록된 다음 값은 이전 암호화 교체 대상의 **historical record**다.
현재 live host의 ID와 혼동하지 않는다.

| 항목 | 실제값 | 판정 |
|---|---|---|
| EC2 | `i-0ed33d5f18e5542ed` | 교체 대상 |
| Root EBS | `vol-02c70033f4258a05b` | gp3 8 GiB |
| EBS encryption | `false` | BLOCKED |
| Root delete on termination | `true` | EC2 종료 시 자동 삭제 |
| Primary ENI | `eni-0001f440c3bfcdd7a` | 보존 |
| ENI delete on termination | `false` | EC2 종료 후 재사용 가능 |
| EIP allocation | `eipalloc-063b9f45f362b5b54` | ENI에 연결된 채 보존 |

아직 애플리케이션과 secret 파일을 설치하지 않았다면 snapshot 복사보다 EC2만
다시 만드는 방법이 가장 단순하다. snapshot은 만들지 않는다.

#### 서울 리전의 EBS 기본 암호화 켜기

1. EC2 콘솔 오른쪽 위 리전이 `서울`인지 확인한다.
2. 왼쪽 메뉴에서 `설정`을 연다.
3. `데이터 보호 및 보안` 탭을 선택한다.
4. `EBS 암호화` 영역에서 `관리`를 선택한다.
5. 기본 EBS 암호화를 `사용`으로 설정한다.
6. 기본 key는 AWS managed key `aws/ebs`를 사용한다.
7. 저장한다.

이 설정은 기존 volume을 변경하지 않고 이후 서울 리전에서 새로 만드는 EBS에만
적용된다.

#### 기존 EC2만 종료

1. Kiwoom API, materializer, 애플리케이션을 실행하지 않았는지 확인한다.
2. EIP를 disassociate하거나 release하지 않는다.
3. ENI를 삭제하지 않는다.
4. EC2 콘솔에서 `i-0ed33d5f18e5542ed`를 선택한다.
5. `인스턴스 상태 → 인스턴스 종료`를 선택한다.
6. instance가 `Terminated`가 될 때까지 기다린다.
7. root volume `vol-02c70033f4258a05b`가 삭제됐는지 확인한다.
8. ENI `eni-0001f440c3bfcdd7a`의 상태가 `Available`인지 확인한다.
9. 기존 EIP가 같은 ENI에 계속 연결돼 있는지 확인한다.

EIP 주소가 유지되므로 Kiwoom 허용 IP를 다시 등록할 필요는 없다.

#### 암호화된 EC2 다시 생성

11절을 다시 수행하되 다음 항목을 특히 확인한다.

- 기존 primary ENI `eni-0001f440c3bfcdd7a` 재사용
- 기존 EIP 유지
- 기존 Security Group `sg-0339fcff47697d77f` 유지
- IAM instance profile `kiwoom-stock-ec2-role`
- `t3.micro`, CPU credit `standard`
- root gp3 8 GiB
- storage Summary에서 `Encrypted=Yes`
- KMS key는 `aws/ebs`
- root `Delete on termination=Yes`
- IMDSv2 required, hop limit 1
- 승인된 EC2 SSH key pair가 launch request와 일치함
- 동일한 cloud-init user data

새 instance가 Running이 된 뒤 AWS 콘솔의 volume 상세에서 `암호화됨=예`를
확인한다. 이 값을 확인하기 전에는 Parameter Store materializer, Kiwoom OAuth,
API 검증 또는 애플리케이션 배포를 진행하지 않는다.

## 13. 다음 단계로 넘어가는 조건

아래 항목이 전부 충족돼야 키 전달과 읽기 전용 API 검증으로 넘어간다.

- [ ] 새 EIP가 EC2 primary ENI에 연결됨
- [ ] 새 EIP가 Kiwoom 허용 IP에 등록됨
- [ ] Security Group inbound TCP 22이 현재 관리 PC의 정확한 `/32` 하나
- [ ] Security Group outbound TCP 443 한 개
- [ ] `t3.micro`, CPU credit `standard`
- [ ] encrypted gp3 8 GiB 한 개
- [ ] IMDSv2 required
- [ ] SSM Online
- [ ] cloud-init 완료
- [ ] Docker active
- [ ] root disk 여유 3 GiB 이상

다음 단계의 key 입력은
[Parameter Store 비밀 materializer 운영 가이드](parameter-store-secret-materializer.md)를
따른다. 첫 Kiwoom 검증은 token 발급과 읽기 전용 시세 조회만 수행한다. 실제
주문, Slack, S3, Gemini는 계속 비활성화한다.

## 14. 문제가 발생했을 때

### EIP까지 만들었지만 EC2를 만들지 못함

- EIP 요금은 계속 발생한다.
- 같은 EIP를 새로 만들지 않는다.
- 기록한 EIP와 ENI를 그대로 두고 EC2 단계만 다시 확인한다.
- 작업을 포기한다면 Kiwoom 허용 IP에서 먼저 제거한 뒤 EIP 연결 해제와 release를
  수행한다.

### Launch 버튼을 눌렀지만 오류 또는 빈 화면이 나옴

- Launch 버튼을 다시 누르지 않는다.
- EC2 `Instances`에서 `kiwoom-stock-prod`를 검색한다.
- `Network Interfaces`에서 ENI가 instance에 연결됐는지 확인한다.
- instance가 실제로 없을 때만 설정을 교정해 다시 시도한다.

### EC2는 Running이지만 SSM이 Offline

- 새 EC2를 만들지 않는다.
- EIP, route, SG outbound, IAM role을 순서대로 확인한다.
- EC2 `Actions → Monitor and troubleshoot → Get system log`에서
  `cloud-init failed`를 찾는다.
- 원인이 확인될 때까지 key나 애플리케이션을 설치하지 않는다.

### 디스크 여유가 3 GiB 미만

- gp3 크기를 자동으로 늘리지 않는다.
- Docker image와 패키지 예상 크기를 먼저 확인한다.
- 8 GiB 운영이 불가능하면 비용 예외 변경 전까지 배포를 중단한다.

## 15. 전체 삭제 절차 (별도 철거 승인 전용)

현재 live host에는 이 절차를 적용하지 않는다. 실제 철거는 shadow/secret/image/
volume/EIP/SSH 영향 분석과 별도 승인을 받은 뒤 exact target read-back 후에만
수행한다.

완전히 철거할 때는 다음 순서를 지킨다.

1. 애플리케이션과 실제 API 호출을 중지한다.
2. Kiwoom 관리 화면에서 새 EIP를 허용 IP 목록에서 제거한다.
3. EC2 instance를 `Terminate`한다.
4. instance가 `Terminated`가 될 때까지 기다린다.
5. root EBS volume이 자동 삭제됐는지 확인한다.
6. EIP에서 `Disassociate Elastic IP address`를 실행한다.
7. EIP에서 `Release Elastic IP address`를 실행한다.
8. 남아 있는 `kiwoom-prod-primary-eni`를 삭제한다.
9. `kiwoom-prod-https-egress` security group을 삭제한다.
10. custom route table의 subnet association을 해제한다.
11. `0.0.0.0/0 → IGW` route를 삭제하고 custom route table을 삭제한다.
12. Internet Gateway를 VPC에서 detach한 뒤 삭제한다.
13. subnet을 삭제한다.
14. VPC를 삭제한다.
15. EC2, EBS, EIP, ENI, NAT Gateway가 모두 0개인지 확인한다.

EIP `Disassociate`만으로는 요금이 중단되지 않는다. 완전 철거라면 반드시
`Release`까지 수행해야 한다. Release한 EIP 주소는 복구할 수 없다.

## 16. 공식 AWS 참고 문서

- [VPC 만들기](https://docs.aws.amazon.com/vpc/latest/userguide/create-vpc.html)
- [Subnet 만들기](https://docs.aws.amazon.com/vpc/latest/userguide/create-subnets.html)
- [Route table 만들기](https://docs.aws.amazon.com/vpc/latest/userguide/create-vpc-route-table.html)
- [Network Interface 만들기](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/create-network-interface.html)
- [기존 Network Interface 연결](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/network-interface-attachments.html)
- [EIP 할당과 연결](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/working-with-eips.html)
- [EC2 launch wizard](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-launch-instance-wizard.html)
- [IMDSv2 설정](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-IMDS-new-instances.html)
- [T3 CPU Standard mode](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-standard-mode.html)
- [Session Manager instance profile](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-instance-profile.html)
