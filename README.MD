# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Physical Dynamics Edition v2.0)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동($F=ma$)으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

파편화된 if-else 휴리스틱과 기계적 손절(Score Decay)의 한계를 극복하기 위해, '관성(Inertia)'과 '합력(Net Force)' 개념을 도입하여 세력의 개미털기(Shake-out) 구간을 버텨내고 진정한 주도주를 정밀 타격합니다.

**🔥 Major Update v2.3: Pure Hybrid Tri-ATR & Absolute Gravity**

1. **상태 관리 제로화 (State-Free Architecture):** 시간 유예(Grace Period)나 진입 시점의 대포알 위력(`max_impulse`) 등 과거의 데이터를 기억하고 추적하는 로직을 전면 폐기했습니다. 엔진은 오직 '현재의 가속도'와 '현재의 변동성'만으로 즉각적인 판단을 내립니다.
2. **Tri-ATR 시스템 (3차원 분리형 방어망):** 변동성을 목적에 따라 3가지로 완벽히 분해했습니다.
   * **`Down-ATR` (순수 하방 리스크):** 상승 허수가 제거된 핀셋 변동성. **초기 생존망(Stop Loss)**에 적용하여 기관의 폭포수 투매만 정확히 잘라냅니다. (`Down-ATR * 5.0`)
   * **`Up-ATR` (순수 상승 폭발력):** 전체 변동성에서 하방 리스크를 뺀 값(`일반 ATR - Down-ATR`). **이익 보존망(Trailing Stop)**에 적용되어, 추세의 질(Quality)이 좋을수록 방어막을 넉넉히 펼치고(대시세 탑승), 지저분할수록 타이트하게 조입니다. (발동: `Up-ATR * 3.5`, 방어: `Up-ATR * 3.0`)
3. **절대 이격 중력 (Absolute Deviation Gravity):** 당일 평균단가(VWAP) 아래에서 발생하는 가짜 반등(Dead-cat Bounce)에 낚이는 맹점을 해결했습니다. `if`문을 통한 분기 처리를 삭제하고, 이격도에 **절대값(`abs`)**을 씌워 VWAP에서 위/아래 어디로 멀어지든 강력한 탄성 저항력(0.0 ~ -1.0)이 연속적으로 산출되도록 물리학 공식을 고도화했습니다.
4. **순수 동역학 Bail-out:** 가속도(Jerk)가 수직 붕괴(-1.0)하거나 꺾이는(-0.5) 즉시, 시간 유예 없이 기계적으로 킬 스위치를 작동시켜 계좌의 피를 막습니다.

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