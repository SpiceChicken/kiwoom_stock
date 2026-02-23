# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Physical Dynamics Edition)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동()으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

파편화된 if-else 휴리스틱과 기계적 손절(Score Decay)의 한계를 극복하기 위해, '관성(Inertia)'과 '합력(Net Force)' 개념을 도입하여 세력의 개미털기(Shake-out) 구간을 버텨내고 진정한 주도주를 포착합니다.

**Major Update: Integrated Physical Dynamics & Hybrid State Management**:

1. **Continuous Mathematical Model**: 점수가 즉시 0점이 되는 현상을 방지하고, 이전 속도(관성)에 현재의 힘을 누적하는 연속적 물리 모델 구축.
2. **Multi-Dimensional Forces**: 체결강도, VWAP, 호가잔량, 순간거래량 등을 추진력, 중력, 자기력, 항력 등의 물리적 벡터로 변환하여 입체적 타점 도출.
3. **Hybrid State Management**: 지연(Latency) 없는 In-Memory L1 캐시 연산과 비동기 DB 백업(L2)을 결합하여 속도와 안정성을 동시 확보.
4. **Time-Decayed Crash Recovery**: 시스템 재시작 시 멈춰있던 시간만큼 관성을 감쇠시켜 안전하게 상태를 복구하는 지수 감쇠 보정 알고리즘 탑재.

---

## 🧠 Core Engine: Physics-based Quant Scoring

엔진은 단순한 조건부 점수 합산이 아닌, 물리학의 속도 및 가속도 공식을 기반으로 동작합니다.

### 🎯 물리학적 스코어링 수식 (Physical Dynamics)

주가의 현재 상태(Score)는 물체의 '현재 속도()'를 Sigmoid 함수로 정규화하여 산출됩니다.

* **가속도 (Net Force):** 
* **현재 속도 (Velocity):**  (이전 속도 유지 = 관성)
* **최종 스코어 (Score):** 

### 🔬 6대 물리적 힘 (The 6 Physical Forces)

1. **추진력 (Thrust):** 체결강도 100% 초과분을 기본 엔진으로 삼고, 당일 평균 대비 거래량(Volume Ratio)을 '질량/부스터'로 곱하여 가속도를 증폭시킵니다.
2. **중력 (Gravity):** 주가가 VWAP에서 너무 멀어지면(ATR 기반 측정) 강한 하방 압력으로 작용하여 과열 매수를 방지합니다.
3. **항력 (Drag):** RSI 과열 지표를 마찰열로 계산합니다. 주가가 급등할수록 차익실현 매물(공기 저항)이 거세져 속도가 무한히 발산하는 것을 막습니다.
4. **자기력 (Magnetic Force):** 매도 호가창에 물량이 압도적으로 쌓여있을 때(매도벽) 발생하는 위쪽 호가로의 '진공 흡입력'을 수치화합니다.
5. **져크 (Jerk / 가속도의 미분):** 5분 전 체결강도와 현재 체결강도를 비교하여 수급이 가속 중인지 감속 중인지 추세를 파악합니다.
6. **충격량 (Impulse):** 특정 창구에서 순간적으로 터지는 대량 거래량을 '타격(망치)'으로 해석하여 속도 벡터에 즉각적인 스칼라 부스트(+5.0)를 가합니다.

---

## ⚡ 시스템 아키텍처 (System Architecture)

초당 수십 번의 틱(Tick) 데이터가 쏟아지는 실시간 트레이딩 환경에서, 연산 지연(Slippage)을 0에 가깝게 유지하면서도 데이터 유실을 막기 위한 아키텍처가 적용되었습니다.

### 1. 하이브리드 상태 관리 (Hybrid State Management)

* **L1 Cache (In-Memory Engine):** 트레이딩 메인 스레드는 오직 메모리(`dict`)에 존재하는  상태값만 참조하여 즉각적으로 점수를 뱉어냅니다.
* **L2 Backup (Asynchronous DB Logging):** 계산이 완료된 결과는 메인 스레드와 완전히 분리된 전용 워커(`ThreadPoolExecutor`)를 통해 백그라운드에서 SQLite DB로 비동기 저장(Fire-and-Forget)됩니다.

### 2. 크래시 복구 메커니즘 (Crash Recovery)

장중 시스템이 비정상 종료되더라도, 재시작 시 DB에서 마지막 속도()를 읽어옵니다. 단, 중단된 시간() 동안 시장의 관심이 식었음을 반영하여 **지수 감쇠(Time-Decayed Inertia)** 처리 후 메모리에 안전하게 적재합니다.

### 3. 동적 설정 로더 (Dynamic Configuration Loader)

파이썬 코드 수정 없이 `config/` 디렉토리 내의 `.json` 파일 수정만으로 알고리즘의 민감도와 마찰 계수 등을 런타임에 튜닝할 수 있습니다.

---

## 📂 디렉토리 구조 (Directory Structure)

```text
Kiwoom-Stock/
├── config/                 
│   ├── config.json         # 계좌 및 API 시스템 설정
│   └── scoring_config.json # 마찰 계수, 정규화 상수 등 물리 모델 하이퍼파라미터
├── src/
│   └── kiwoom_stock/
│       ├── core/
│       │   ├── physics_engine.py  # [New] 6대 물리적 힘 및 Sigmoid 스코어링 연산 로직
│       │   ├── state_manager.py   # [New] 관성 상태 캐싱, 비동기 DB 백업, 크래시 복구
│       │   └── config.py          # 동적 설정 로더
│       ├── monitoring/
│       │   └── strategy.py        # 엔진을 호출하여 실제 매수/매도 시그널 생성
├── logs/                   # 실행 로그 (상태 변화 및 매매 기록)
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

### 2. 설정 파일 준비

`config/` 폴더에 `scoring_config.json`을 위치시켜 물리 엔진의 마찰 계수(`friction_coefficient`), 정규화 상수 등을 기호에 맞게 세팅합니다.

### 3. 실행

```bash
poetry run python main.py

```

---

## 📊 사후 분석 및 검증 (Backtest Reporting)

`tools/backtest_reporter.py`를 실행하면, DB에 저장된 물리 엔진의 상세 점수 추이와  변화량을 바탕으로 MFE(최대 허용 수익) 및 MAE(최대 허용 손실)를 Rich 기반의 깔끔한 터미널 UI로 분석할 수 있습니다.

---