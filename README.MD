# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Pure Kinematics Edition v2.6)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동($F=ma$)으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

**🔥 Major Update v2.6: Net Force Lock & Auto LLM Post-Mortem**

V2.6 업데이트는 엔진의 마지막 사각지대였던 '합력 역전 현상'을 수학적으로 차단하고, 장 마감 후 시스템 스스로 매매를 복기하는 LLM 기반 자동 부검 파이프라인을 구축한 완전 자율주행 마일스톤입니다.

1. **진입 통제 종결: Net Force 하드 록 (비행기 딜레마 해결)**
   * 아무리 수급(Thrust)이 폭발해도 매물대(Drag)와 고점 중력(Gravity)의 저항이 더 커서 **최종 합력(`Net Force`)이 0 미만으로 역전되는 경우**, 즉시 진입을 거부합니다. 
   * 이로써 거래량은 터지지만 윗꼬리를 달고 추락하는 캔들에서의 뇌동매매가 완전히 멸종되었습니다.
2. **생명주기 기반 AI 자동 부검 파이프라인 (Lifecycle-based LLM Reporter)**
   * 장 마감 후 메인 엔진(무한 루프)이 정상 종료(Graceful Shutdown)되면, 시스템이 자동으로 당일 매매 종목의 1분봉 데이터를 추출하고 물리 역학 로그를 교차 검증(`trade_validator`)합니다.
   * 검증된 통계와 로그는 Gemini API로 전송되며, '수석 아키텍트 AI'의 날카로운 분석 총평과 함께 Slack으로 일일 부검 리포트가 발송됩니다.
3. **프롬프트 외부화 (Prompt Externalization)**
   * LLM 제어용 프롬프트를 파이썬 코드에서 완전히 분리하여 `prompts/` 디렉토리 내 `.md` 파일로 관리합니다. 비개발자도 코드 수정 없이 AI의 페르소나와 분석 룰(예: 환각 방지)을 유연하게 제어할 수 있습니다.

---

## 🧠 Core Engine: Physics-based Quant Scoring

### 🎯 물리학적 스코어링 수식 (Physical Dynamics)

주가의 현재 상태(Score)는 물체의 '현재 속도($V_t$)'를 Sigmoid 함수로 정규화하여 산출됩니다.

* **가속도 (Net Force):** $F_{net} = F_{thrust} + F_{gravity} + F_{drag} + F_{magnetic} + F_{jerk}$
* **현재 속도 (Velocity):** $V_t = V_{t-1} + F_{net} + Impulse$ (이전 속도 유지 = 관성)

### 🔬 6대 물리적 힘 (The 6 Physical Forces)

1. **추진력 (Thrust):** 체결강도 100% 초과분을 기본 엔진으로 삼고, 당일 평균 대비 거래량(Volume Ratio)을 '질량/부스터'로 곱하여 가속도를 증폭시킵니다.
2. **중력 (Gravity):** 주가가 당일 평균가(VWAP)에서 멀어질수록 강한 탄성 저항력(0.0 ~ -1.0)으로 작용하여 과열 매수 및 데드캣 바운스(가짜 반등) 진입을 차단합니다.
3. **항력 (Drag):** RSI 과열 지표를 마찰열로 계산합니다. 급등 시 차익실현 매물을 시뮬레이션하여 속도의 무한 발산을 막습니다. 
4. **자기력 (Magnetic Force):** 매도 호가창에 물량이 압도적으로 쌓여있을 때 발생하는 위쪽 호가로의 '진공 흡입력'을 수치화합니다.
5. **져크 (Jerk / 가속도의 미분):** 5분 전 체결강도와 현재 체결강도를 비교하여 수급이 가속 중인지 감속 중인지 파악해 킬 스위치(Bail-out)의 척도로 삼습니다.
6. **충격량 (Impulse):** 단일 틱 체결대금이 1억 원 이상 터질 때 타격을 가하며, 음봉 체결 대금은 매도 폭탄으로 간주해 감점합니다.

---

## ⚡ 시스템 아키텍처 (System Architecture)

### 1. 3중 진입 락 (Triple Entry Lock)
* **Thrust Minimum Lock:** 시장의 돈이 들어오지 않으면(`Thrust < 0.8`) 절대 사지 않습니다.
* **Net Force Lock (v2.6):** 맞바람에 밀려 추락하는 비행기(`Net Force < 0.0`)에는 절대 타지 않습니다.
* **Stall Shield:** 초고점(`Gravity <= -0.9`)에서 수급이 말라가는(`Thrust < 1.0`) 실속 구간 진입을 차단합니다.

### 2. 동적 청산 및 이익 보존 전략 (Kinematic Exit & Retention)
* **Universal Shield:** 단일 방어선(`-(Up-ATR * 3.0)`)으로 하락장에서는 바짝 당겨 목을 치고, 상승장에서는 넉넉히 룸을 주는 자가-수축형 1차 방어막입니다.
* **Profit Lock-in (2.0% 이상):** 수익 2.0% 궤도에 오르면, 엔진 출력 상실 시 즉시 탈출하는 `이중 잠금 스나이퍼(Dual-Trigger)`와 번 돈의 절반(50%)을 무조건 지키는 `에너지 보존 비율`이 가동됩니다.

### 3. 상태 관리 제로화 (State-Free Architecture) & 파이프라인
* **L1 Cache (In-Memory Engine):** 트레이딩 메인 스레드는 시간이나 타이머에 의존하지 않고, 메모리(`dict`)에 존재하는 $V_{t-1}$ 상태값만 참조하여 실시간 연산(Zero Latency)합니다.
* **L2 Backup (Asynchronous DB Logging):** 계산 결과는 백그라운드 워커(`ThreadPoolExecutor`)를 통해 비동기로 DB에 로깅됩니다.
* **L3 Auto Post-Mortem:** 엔진 셧다운 후, 추출기 ➔ 밸리데이터 ➔ LLM 클라이언트 ➔ Slack 알림으로 이어지는 단방향 자동 부검 파이프라인이 구동됩니다.

---

## 📂 디렉토리 구조 (Directory Structure)

```text
Kiwoom-Stock/
├── config/                 
│   ├── config.json         # 계좌 및 API 시스템 설정
│   └── scoring_config.json # 마찰 계수, 감쇠율 등 물리 모델 하이퍼파라미터
├── prompts/                # 외부화된 LLM 시스템/유저 프롬프트 (v2.6)
│   ├── daily_postmortem_system.md
│   └── daily_postmortem_user.md
├── src/
│   └── kiwoom_stock/
│       ├── core/
│       │   ├── physics_engine.py  # 6대 물리적 힘 및 Net Force 산출
│       │   ├── state_manager.py   # 관성 상태 캐싱 (L1 Cache)
│       │   └── indicators.py      # Tri-ATR 수학적 파이프라인
│       ├── monitoring/
│       │   ├── strategy.py        # Triple Entry Lock & 동적 이익 보존망
│       │   ├── reporter.py        # 장 마감 자동 부검 컨트롤러
│       │   ├── notifier.py        # Slack 알림
│       │   └── manager.py         # 포지션 관리 및 비동기 처리
│       └── utils/
│           └── gemini_client.py   # LLM 연동 및 프롬프트 동적 매핑
├── logs/                   # 실행 로그
├── data/                   # 분석 데이터 (SQLite DB)
├── tools/                  # 거래 검증 및 1분봉 추출 등 사후 분석 툴킷
└── main.py                 # 프로그램 진입점 및 Lifecycle 관리
```

---

## 🚀 시작하기 (Getting Started)

본 프로젝트는 `pyproject.toml`을 사용하여 의존성을 관리합니다.

### 1. 환경 설정 및 설치

```bash
poetry install
# 또는
pip install .
```

### 2. 실행

```bash
poetry run python main.py
```

*(실행 후 메인 루프가 정상 종료되면 자동으로 일일 부검 리포트가 Slack으로 발송됩니다.)*

---

## 📊 사후 분석 및 검증 (Backtest Reporting)

매일 장 마감 후 슬랙으로 요약 리포트가 전송되며, 심층 분석이 필요한 경우 `tools/trade_validator.py` 및 각종 분석 스크립트를 통해 DB에 저장된 물리 엔진의 상세 벡터 추이, `Net Force` 차단 내역, 이익 보존망 작동 시점의 `Jerk` 변화 등을 정밀하게 사후 부검할 수 있습니다.