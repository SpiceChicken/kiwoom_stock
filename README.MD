# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Physical Dynamics Edition)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동($F=ma$)으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

파편화된 if-else 휴리스틱과 기계적 손절(Score Decay)의 한계를 극복하기 위해, '관성(Inertia)'과 '합력(Net Force)' 개념을 도입하여 세력의 개미털기(Shake-out) 구간을 버텨내고 진정한 주도주를 정밀 타격합니다.

**🔥 Major Update: Trap Defense & Heavy Trailing Stop**:

1. **Bull-Trap Shield (진입 방어막)**: 고점 피날레(Climax) 구간이나 매도 폭탄이 떨어지는 가짜 돌파를 차단하기 위해 `Gravity`와 `Jerk`, 방향성 `Impulse`에 하드 록(Hard Lock) 제약을 걸었습니다.
2. **Heavy Trailing Stop (동적 청산 개편)**: 속도(Velocity)가 조금만 줄어도 냅다 팔아버리던 잦은 매매 로직을 폐기했습니다. 대신 **실제 가격의 고점(Max Price)**을 추적하며, 진짜 추세가 꺾일 때(-1.5%)만 기계적으로 이익을 실현하여 수수료 출혈을 막고 수익을 극대화합니다.
3. **Threshold-Free Architecture**: 고정된 점수 임계값(Threshold)에 의존하던 기존의 if-else 룰을 전면 폐기하고, 물리적 힘(Force)의 역전과 관성 감쇠 비율만으로 매매를 결정합니다.
4. **Physical Reason Logging**: 매도 및 관망 사유(`get_exit_reason`)를 "엔진 소진(V<0)", "연속 하방 압력(Breakdown)", "최고가 대비 하락(Trailing Stop)" 등 직관적인 물리적 상태로 개편하여 사후 분석의 퀄리티를 높였습니다.

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

### 1. 묵직한 동적 청산 전략 (Heavy Dynamic Exit)

변동성에 흔들리지 않고 진짜 추세를 끝까지 발라먹기 위해 3가지 조건 중 하나라도 만족 시 즉각 탈출합니다.

* **Engine Dead:** 속도($V_t$)가 마이너스로 추락하여 상승 관성이 완전히 소멸했을 때.
* **Kinetic Breakdown:** 최근 3분간 누적된 합력($F_{net}$)이 -3.0 이하로 강한 하방 매도세가 쏟아질 때.
* **Price-based Trailing Stop:** 진입 후 달성한 최고가(Max Price) 대비 현재가가 **-1.5% 이상 하락**하여 실제 가격 추세가 깨졌을 때.

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
│       │   ├── strategy.py        # Bull-Trap 방어막(Shield) 및 Heavy Trailing Stop 로직
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

`tools/backtest_reporter.py`를 실행하면, DB에 저장된 물리 엔진의 상세 벡터(Thrust, Gravity, Drag, Magnetic, Jerk, Impulse) 추이와 $V_t$ 변화량을 바탕으로 MFE(최대 허용 수익) 및 MAE(최대 허용 손실)를 터미널 UI로 분석할 수 있습니다. 매도 사유 또한 "엔진 소진", "연속 하방 압력" 등으로 명확히 표시되어 전략의 직관적 피드백이 가능합니다.