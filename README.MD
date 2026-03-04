# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Physical Dynamics Edition v2.0)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동($F=ma$)으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

파편화된 if-else 휴리스틱과 기계적 손절(Score Decay)의 한계를 극복하기 위해, '관성(Inertia)'과 '합력(Net Force)' 개념을 도입하여 세력의 개미털기(Shake-out) 구간을 버텨내고 진정한 주도주를 정밀 타격합니다.

**🛠️ Minor Update v2.1: Asymmetric Volatility (Down-ATR) Foundation**

차세대 듀얼 방어망(V2.2) 구축을 위한 핵심 데이터 파이프라인이자, 기존 일반 ATR의 수학적 모순(상승폭이 손절선을 넓히는 현상)을 해결하기 위한 **'비대칭 하락 변동성(Down-ATR)'** 코어 연산 모듈이 도입되었습니다.
1. **비대칭 하락 변동성(Down-ATR) 코어 지표 신설**: 상승장이나 급등 캔들(양봉)에서 발생하는 '수익 방향의 변동성(허수)'을 철저히 배제하고, 오직 주가가 하락할 때(음봉 및 아래꼬리) 발생하는 **'순수 하방 타격량(Downside Deviation)'**만을 정밀 계산하는 `Down-ATR` 파이프라인을 구축했습니다.
2. **수학적 트릭을 통한 최적화**: 기존 `calculate_atr` 코어 함수를 수정하여 중복 코드를 만들지 않고, 5분봉 데이터 배열에서 시가(Open)를 이전 종가(Close)로 치환하는 우아한 수학적 트릭을 사용하여 하방 벡터만 정확히 분리해 냈습니다.
3. **V2.2 듀얼 방어망을 위한 파이프라인 완성**: 시장 데이터 수집부(`_update_volatility_data`)에서 기존 `atr_percent`와 함께 `down_atr_percent`를 병렬로 산출하여 캐시에 주입함으로써, 향후 전략(Strategy) 모듈에서 즉각적으로 듀얼 변동성을 참조할 수 있는 기반을 다졌습니다.

**🔥 Major Update v2.0: Fully Dynamic Dual-Defense & Kinematic Paradigm Shift**

1. **쿨다운 철폐 및 순수 역학 엔진 복원 (Time-Constraint Free)**: 잦은 매매를 막기 위한 땜질식 처방이었던 '10분 진입 냉각기'를 전면 삭제했습니다. 방어망이 견고해짐에 따라, 물리적 힘(Force)이 조건에 부합한다면 시간에 구애받지 않고 진입/유지하는 순수 역학 트레이딩 원칙을 복원했습니다.
2. **완전 동적 이중 방어망 (Fully Dynamic Dual-Defense)**: 고정된 상수(-1.5%)가 대형주의 진동을 견디지 못하던 한계를 극복했습니다. 진입 직후에는 넉넉한 **`초기 생존망(-ATR*3.0)`**으로 세력의 흔들기를 버텨내고, 의미 있는 수익권 도달 시에만 **`이익 보존망(-ATR*1.5)`**이 켜지며 추세가 꺾일 때 동적으로 익절합니다.
3. **대포알 우대 정책 (Impulse-based Multiplier)**: 진입 시점에 틱 체결대금이 폭발하는 진짜 스마트 머니(`Impulse >= 3.0`)가 들어왔다면, 이익 보존망(Trailing Stop)의 폭을 `ATR * 1.5`에서 **`ATR * 2.0`**으로 확장하여 대시세의 흔들기를 끝까지 버텨냅니다.
4. **가짜 수급 조기 탈출 (Fake-Breakout Bail-out)**: 대규모 자금 없이 알고리즘 매수로 체결강도(Thrust)만 인위적으로 끌어올린 세력의 꼬시기 덫을 감지합니다. 진입 직후 `수익률 <= 0.0 AND Jerk <= -0.5 AND Thrust < 1.0` 달성 시, 초기 손절망까지 기다리지 않고 즉시 시장가로 탈출(Bail-out)하여 손익비를 극대화합니다.
5. **Bull-Trap Shield (진입 방어막)**: 고점 피날레(Climax) 구간이나 매도 폭탄이 떨어지는 가짜 돌파를 차단하기 위해 `Gravity`와 `Jerk`, 방향성 `Impulse`에 하드 록(Hard Lock) 제약을 걸었습니다.

---

## 🧠 Core Engine: Physics-based Quant Scoring

엔진은 단순한 조건부 점수 합산이 아닌, 물리학의 속도 및 가속도 공식을 기반으로 동작합니다.

### 🎯 물리학적 스코어링 수식 (Physical Dynamics)

주가의 현재 상태(Score)는 물체의 '현재 속도($V_t$)'를 Sigmoid 함수로 정규화하여 산출됩니다.

* **가속도 (Net Force):** $F_{net} = F_{thrust} + F_{gravity} + F_{drag} + F_{magnetic} + F_{jerk}$
* **현재 속도 (Velocity):** $V_t = V_{t-1} + F_{net} + Impulse$ (이전 속도 유지 = 관성)
* **최종 스코어 (Score):** $100 \times \text{Sigmoid}(V_t)$ (현재는 Score에 의존하지 않고 $V_t$ 자체를 진입/청산의 기준으로 삼습니다.)

### 🔬 6대 물리적 힘 (The 6 Physical Forces)

1. **추진력 (Thrust):** 체결강도 100% 초과분을 기본 엔진으로 삼고, 당일 평균 대비 거래량(Volume Ratio)을 '질량/부스터'로 곱하여 가속도를 증폭시킵니다.
2. **중력 (Gravity):** 주가가 VWAP에서 너무 멀어지면(ATR 기반 측정) 강한 하방 압력으로 작용하여 과열 매수를 방지합니다. **[Constraint] 엔진이 꺼져 있을 때 중력이 주가를 위로 당기는 현상은 수학적으로 차단됩니다.**
3. **항력 (Drag):** RSI 과열 지표를 마찰열로 계산합니다. 주가가 급등할수록 차익실현 매물이 거세져 속도의 무한 발산을 막습니다. **[Constraint] 주가가 하락 중($V_{t-1} \le 0$)일 때는 마찰력이 0으로 고정됩니다.**
4. **자기력 (Magnetic Force):** 매도 호가창에 물량이 압도적으로 쌓여있을 때(매도벽) 발생하는 위쪽 호가로의 '진공 흡입력'을 수치화합니다.
5. **져크 (Jerk / 가속도의 미분):** 5분 전 체결강도와 현재 체결강도를 비교하여 수급이 가속 중인지 감속 중인지 추세를 파악해 '가짜 돌파(Bull Trap)'를 방지합니다.
6. **충격량 (Impulse / 방향성 틱 체결대금):** 단일 틱 체결대금이 1억 원 이상 터질 때 타격을 가합니다. **[Constraint] 이전 틱 대비 가격이 하락(음봉)했다면 매도 폭탄으로 간주하여 충격량을 0으로 차단합니다.**

---

## ⚡ 시스템 아키텍처 (System Architecture)

### 1. 묵직한 동적 청산 전략 (Fully Dynamic Exit)

변동성에 흔들리지 않고 진짜 추세를 끝까지 발라먹기 위해 4가지 단계적 조건에 따라 즉각 탈출합니다.

* **Bail-out (조기 탈출):** 진입 직후 수익이 나지 않는 상태에서 속도와 추진력이 급감할 때 초기 손절망 전 즉시 컷오프.
* **Engine Dead:** 속도($V_t$)가 마이너스로 추락하여 상승 관성이 완전히 소멸했을 때.
* **Initial ATR Stop Loss (초기 생존망):** 진입 직후 세력의 흔들기를 버티는 구간으로, 매수가 대비 `-(ATR * 3.0)` 하락 시 강제 손절.
* **Dynamic ATR Trailing Stop (이익 보존망):** 최고가 갱신 후, 진짜 추세가 꺾일 때만(`-(ATR * 1.5 ~ 2.0)` 하락) 동적으로 이익을 실현하여 수수료 출혈을 막고 수익을 극대화.

### 2. 하이브리드 상태 관리 (Hybrid State Management)

* **L1 Cache (In-Memory Engine):** 트레이딩 메인 스레드는 오직 메모리(`dict`)에 존재하는 $V_{t-1}$ 상태값만 참조하여 즉각적으로 점수를 연산(Zero Latency)합니다.
* **L2 Backup (Asynchronous DB Logging):** 계산이 완료된 결과는 메인 스레드와 완전히 분리된 전용 워커(`ThreadPoolExecutor`)를 통해 백그라운드에서 SQLite DB로 비동기 저장(Fire-and-Forget)됩니다.

### 3. 크래시 복구 메커니즘 (Crash Recovery)

장중 시스템이 비정상 종료되더라도, 재시작 시 DB에서 마지막 속도를 읽어옵니다. 중단된 시간($\Delta t$) 동안 시장의 관심이 식었음을 반영하여 **지수 감쇠(Time-Decayed Inertia)** 처리 후 메모리에 안전하게 복구합니다.

---

## 📂 디렉토리 구조 (Directory Structure)

```text
Kiwoom-Stock/
├── config/                 
│   ├── config.json         # 계좌 및 API 시스템 설정
│   └── scoring_config.json # 마찰 계수, 감쇠율 등 물리 모델 하이퍼파라미터
├── src/
│   └── kiwoom_stock/
│       ├── core/
│       │   ├── physics_engine.py  # 6대 물리적 힘 및 하드 록(Hard Lock) 제약 조건 연산
│       │   ├── state_manager.py   # 관성 상태 캐싱, 이전 가격/거래량 추적을 통한 Impulse 파싱
│       │   └── config.py          # 동적 설정 로더
│       ├── monitoring/
│       │   ├── strategy.py        # 동적 이중 방어망, 대포알 우대 정책, Bail-out 로직 구현
│       │   └── manager.py         # 포지션 관리 및 Selective Overnight 처리
├── logs/                   # 실행 로그 (status.log - 물리적 Exit Reason 포함)
├── data/                   # 분석 데이터 (SQLite DB 및 CSV 사후 검증용)
├── pyproject.toml          # 프로젝트 의존성 관리
└── main.py                 # 프로그램 진입점

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

---

## 📊 사후 분석 및 검증 (Backtest Reporting)

`tools/backtest_reporter.py`를 실행하면, DB에 저장된 물리 엔진의 상세 벡터(Thrust, Gravity, Drag, Magnetic, Jerk, Impulse) 추이와 $V_t$ 변화량을 바탕으로 MFE(최대 허용 수익) 및 MAE(최대 허용 손실)를 터미널 UI로 분석할 수 있습니다. 매도 사유 또한 "엔진 소진", "연속 하방 압력", "Trailing Stop" 등으로 명확히 표시되어 전략의 직관적 피드백이 가능합니다.