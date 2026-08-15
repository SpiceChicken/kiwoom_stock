# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Pure Kinematics Edition v2.6.4)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동($F=ma$)으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

> 운영 기준: 현재는 **shadow-only / no-live-trading** 상태다. 실제 주문·취소·계좌
> 조회는 운영하지 않으며, 승인된 bounded shadow 실행과 설정·읽기 전용 검증만
> 허용한다. 현재 EC2·release tuple·SSH/SSM 경계는
> [현재 운영 기준선](docs/operations/current-state.md)을 기준으로 한다.

**🔥 Major Update v2.6.4: Zero-Price Shield & Fuel Exhaustion Shield**

V2.6.4 업데이트는 거래소의 VI(변동성 완화장치) 발동 시 발생하는 API 데이터 오염을 방어하고, 후행성 지표의 착시로 인한 휩쏘(Whipsaw) 장세의 뇌동매매를 '실시간 거래량 가속도'로 썰어버리는 무결점 방어 체계 완성 버전입니다.

---

## ⚡ 시스템 아키텍처 (System Architecture)

### 1. 6단계 동역학 진입 통제 (6-Stage Kinematic Entry Control)
시장 노이즈, 세력의 설거지, 그리고 API의 기만까지 완벽히 차단하는 다층 방어막입니다.

* **[Stage -1] VI 오염 방어 (Zero-Price Shield):** VI 발동으로 인해 현재가(`cur_prc`)가 0원으로 송출될 경우, 엔진의 수학적 패닉(분모 0 또는 -100% 손절 오판)을 막고 즉시 평가를 보류합니다.
* **[Stage 0] 절대 진입 금지 (Climax Shield):** 주가가 초고점(`Gravity <= -0.9`)인데 수급이 폭발(`Thrust >= 1.5`)하면, 세력의 피날레(설거지)로 간주하여 최우선으로 매수를 영구 차단합니다.
* **[Stage 1] 진성 돌파 하이패스 (Breakout Override):** 합력(`Net Force`)이 마이너스로 역전된 악조건 속에서도, 막대한 자본(`Impulse >= 3.0`)과 가속도(`Jerk >= 0.5`)가 동반되면 저항선을 뚫어낸 '진성 돌파'로 간주하여 프리패스로 탑승합니다.
* **[Stage 2] 물리적 하드 록 (Hard Locks):**
  * `수급 빈곤 (Thrust Low)`: 시장의 돈이 들어오지 않으면(`Thrust < 0.8`) 절대 사지 않습니다.
  * `합력 역전 (Net Force < 0)`: 맞바람에 밀려 추락하는 비행기(`Net Force < 0.0`)에는 타지 않습니다.
  * `고공 실속 (Stall Shield)`: 초고점(`Gravity <= -0.9`)에서 수급이 마르면(`Thrust < 1.0`) 추락 직전으로 차단합니다.
* **[Stage 2.5] 연료 고갈 방어막 (Fuel Exhaustion Shield):** 직전 1분 대비 현재 1분의 거래량이 반토막(`Ratio < 0.5`) 났다면, 가속도(`Jerk`) 지표가 아무리 높아도 과거 뭉칫돈에 의한 '후행 관성의 착시(휩쏘)'로 간주하여 탑승을 거부합니다.
* **[Stage 3] 정상 궤도 가동 (Standard Entry):** 위의 무시무시한 5겹의 락을 모두 통과한 종목 중, 가속 페달(`Jerk > 0.0`)을 밟은 경우에만 최종적으로 매수를 집행합니다.

### 2. 동적 청산 및 이익 보존 전략 (Kinematic Exit & Retention)
* **Zero-Price Panic Block:** 보유 종목이 VI에 걸려 0원 호가가 들어와도 시장가로 던지지 않고(Ghost Exit 차단) 침착하게 관성을 유지합니다.
* **Universal Shield:** 단일 방어선(`-(Up-ATR * 3.0)`)으로 하락장에서는 바짝 당겨 목을 치고, 상승장에서는 넉넉히 룸을 주는 자가-수축형 1차 방어막입니다.
* **Profit Lock-in (2.0% 이상):** 수익 2.0% 궤도에 오르면, 엔진 출력 상실 시 즉시 탈출하는 `이중 잠금 스나이퍼(Dual-Trigger)`와 번 돈의 절반(50%)을 무조건 지키는 `에너지 보존 비율`이 가동됩니다.

### 3. 상태 관리 제로화 & 자동 부검 파이프라인 (Auto Post-Mortem)
* **L1 Cache:** 트레이딩 스레드는 시간/타이머에 의존하지 않고, 메모리의 $V_{t-1}$ 상태값만 참조하여 실시간 연산(Zero Latency)합니다. 특히 V2.6.4부터는 **실시간 1분 누적 거래량 가속도**를 추적하여 연료 고갈을 판단합니다.
* **L3 Auto Post-Mortem:** 로컬/legacy 애플리케이션 경로에는 정상 종료 후 당일
  분석·Gemini·Slack 부검 흐름이 남아 있다. 현재 운영 호스트에서는 이 runtime
  경로와 worker를 활성화하지 않으며, shadow 상태 알림은 보호된 workflow에서
  별도로 관리한다.

---

## 🧠 Core Engine: Physics-based Quant Scoring

### 🎯 물리학적 스코어링 수식 (Physical Dynamics)
주가의 현재 상태(Score)는 물체의 '현재 속도($V_t$)'를 Sigmoid 함수로 정규화하여 산출됩니다.

* **가속도 (Net Force):** $F_{net} = F_{thrust} + F_{gravity} + F_{drag} + F_{magnetic} + F_{jerk}$
* **현재 속도 (Velocity):** $V_t = V_{t-1} + F_{net} + Impulse$ (이전 속도 유지 = 관성)

### 🔬 6대 물리적 힘 (The 6 Physical Forces)
1. **추진력 (Thrust):** 체결강도 100% 초과분을 기본 엔진으로 삼고, 당일 평균 대비 거래량(Volume Ratio)을 곱해 증폭시킵니다.
2. **중력 (Gravity):** 주가가 당일 평균가(VWAP)에서 멀어질수록 강한 탄성 저항력(0.0 ~ -1.0)으로 작용합니다.
3. **항력 (Drag):** RSI 과열 지표를 마찰열로 계산하여 속도의 무한 발산을 막습니다. 
4. **자기력 (Magnetic Force):** 매도 호가창에 물량이 쌓여있을 때 발생하는 위쪽 호가로의 '진공 흡입력'입니다.
5. **져크 (Jerk):** 5분 전 체결강도와 현재 체결강도를 비교하는 가속도의 미분값으로, 킬 스위치(Bail-out)의 척도입니다.
6. **충격량 (Impulse):** 단일 틱 체결대금이 1억 원 이상 터질 때 타격을 가하며, 진성 돌파(Breakout Override)를 식별하는 가장 중요한 핵심 키(Key)입니다.

---

## 📂 디렉토리 구조 (Directory Structure)

```text
Kiwoom-Stock/
├── src/
│   └── kiwoom_stock/
│       ├── application/    # composition root, lifecycle orchestration, ports
│       ├── domain/         # 순수 계산/모델
│       ├── infrastructure/ # DB 등 외부 adapter
│       ├── api/            # Kiwoom REST/auth client
│       ├── core/
│       │   ├── physics_engine.py  # 6대 물리적 힘 및 Net Force 연산
│       │   ├── state_manager.py   # 관성 상태 캐싱 및 repository port 사용
│       │   └── indicators.py
│       ├── monitoring/
│       │   ├── strategy.py        # 6단계 진입 통제 센터 & 이익 보존망
│       │   ├── reporter.py        # 장 마감 자동 부검 컨트롤러
│       │   ├── notifier.py        # Slack 알림
│       │   └── manager.py         # 포지션/감시 종목 관리
│       └── utils/
│           └── gemini_client.py
├── docs/                   # architecture, configuration, testing, container/deployment guides
├── tools/                  # 거래 검증 및 1분봉 추출 등 사후 분석 툴킷
└── main.py                 # process entrypoint
```

구조와 외부 경계는 [docs/architecture.md](docs/architecture.md), 핵심 규칙은 [docs/business-rules.md](docs/business-rules.md), 설정 표는 [docs/configuration.md](docs/configuration.md)를 기준으로 확인합니다. 운영 경계는 [현재 운영 기준선](docs/operations/current-state.md)과 [deployment boundary](docs/operations/deployment-boundary.md)를 따른다.

---

## 🚀 시작하기 (Getting Started)

본 프로젝트는 `pyproject.toml`을 의존성 SSOT로 사용하고, `venv`와 `pip`으로 개발 환경을 관리합니다.
Python은 3.11 이상을 지원하며, 개발/CI의 최신 기준은 Python 3.14 계열입니다.

### 1. 가상환경 생성 및 활성화

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell에서는 `.venv\Scripts\Activate.ps1`을 실행합니다.

### 2. 의존성 설치

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 3. 테스트

```bash
python -m pytest
```

전체 로컬 검증 명령은 [docs/testing.md](docs/testing.md)를 기준으로 실행합니다.
Docker 기반 dev/prod 분리와 현재 배포 경계는 [docs/container-development.md](docs/container-development.md) 및 [docs/operations/deployment-boundary.md](docs/operations/deployment-boundary.md)를 참고합니다.
수동 GHCR → EC2 설정 검사 절차는 [docs/operations/github-ec2-container-deployment.md](docs/operations/github-ec2-container-deployment.md)를 따릅니다.
EC2 사람용 관리 접속은 [SSH 접근 가이드](docs/operations/aws-local-access.md)와 [직접 SSH helper](tools/ssh-direct-shell.sh)를 사용하며, GitHub 자동화의 보호된 SSM command 경계와 구분합니다.
키움 App/Secret Key는 대화·Git·`.env`·CI에 입력하지 말고 [자격증명 관리 정책](docs/security/kiwoom-credential-management.md)과 [회전·사고 대응 절차](docs/operations/credential-rotation.md)를 따릅니다.

### 4. 실행

```bash
python main.py
```

*(로컬 실행은 개발·검증 목적이다. 현재 운영 호스트에서 worker/매매 capability를
활성화하지 않는다. shadow 상태 알림은 보호된 GitHub workflow 경계를 통해서만
허용된다.)*

---

## 📊 사후 분석 및 검증 (Backtest Reporting)

매일 장 마감 후 슬랙으로 요약 리포트가 전송되며, 심층 분석이 필요한 경우 `tools/trade_validator.py` 및 각종 분석 스크립트를 통해 DB에 저장된 물리 엔진의 상세 벡터 추이, `Net Force` 차단 내역, 이익 보존망 작동 시점의 `Jerk` 변화 등을 정밀하게 사후 부검할 수 있습니다.
