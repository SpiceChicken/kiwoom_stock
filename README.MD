# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Pure Kinematics Edition v2.5)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동($F=ma$)으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

**🔥 Major Update v2.5: 순수 동역학 완전체 (Thrust Lock & Dual-Trigger Sniper)**

V2.5 업데이트는 시장의 레짐(Regime) 변화 속에서도 절대적인 생존을 보장하기 위해, 수급 빈곤 종목의 뇌동매매를 물리적으로 원천 차단하고, 초고도 수익 구간에서 대시세의 눌림목은 버티되 하락 변곡점에서는 예리하게 꼬리를 자르는 '이익 보존망'을 완성한 최종 마일스톤입니다.

1. **진입 통제 종결 (Entry Quality Control):**
   * **최소 수급 락 (Thrust Minimum Lock):** `Gravity(위치)`가 아무리 좋고 가속도가 붙어도, **`Thrust < 0.8`** (시장의 돈이 들어오지 않음)이면 가짜 펌핑으로 간주하여 매수를 즉각 거부하는 하드 록(Hard Lock)이 추가되었습니다.
   * **고공 실속막 (Stall Shield):** 주가가 VWAP에서 아득히 멀어졌으나(`Gravity <= -0.9`) 매수세가 고갈된(`Thrust < 1.0`) '추락 직전'의 고점 뇌동매매를 원천 차단합니다.
   
2. **비대칭 통합 스마트 방어망 (Universal Shield):**
   * **단일 방어선 통일:** 모든 포지션의 기본 방어막은 **`-(Up-ATR * 3.0)`** 단일 지표로 통합 제어됩니다.
   * **자가-수축형(Self-Tightening) 마법:** 진입 직후 음봉이 터져 하락하면 `Down-ATR`이 커지면서 `Up-ATR`이 급격히 축소됩니다. 주가가 내릴수록 방어선이 위로 바짝 당겨지며 지하실로 가기 전에 스스로 목을 자릅니다(손절 최소화).

3. **초고도 이익 보존망 (Profit Lock-in at 2.0% Altitude):**
   * **이중 잠금 스나이퍼 (Dual-Trigger Sniper):** 수익률이 **2.0%**를 돌파한 초고도 구간에서는, 엔진 출력 상실(`Jerk < 0.0`)과 실제 가격 이탈(`Up-ATR * 1.0` 하락)이 **동시에** 발생할 때만 즉시 스나이퍼 익절을 수행하여 꼭대기에서 탈출합니다.
   * **에너지 보존 법칙 (Kinetic Profit Retention):** 수익률이 **2.0%**를 넘어서면, 번 돈의 '절반(50%)'은 무조건 지키도록 방어선이 동적 수축합니다(`max(-(Up-ATR * 3.0), -(max_profit * 0.5))`). 변동성이 팽창해도 수익을 강제 수성(守城)합니다.

---

## 🧠 Core Engine: Physics-based Quant Scoring

엔진은 단순한 조건부 점수 합산이 아닌, 물리학의 속도 및 가속도 공식을 기반으로 동작합니다.

### 🎯 물리학적 스코어링 수식 (Physical Dynamics)

주가의 현재 상태(Score)는 물체의 '현재 속도($V_t$)'를 Sigmoid 함수로 정규화하여 산출됩니다.

* **가속도 (Net Force):** $F_{net} = F_{thrust} + F_{gravity} + F_{drag} + F_{magnetic} + F_{jerk}$
* **현재 속도 (Velocity):** $V_t = V_{t-1} + F_{net} + Impulse$ (이전 속도 유지 = 관성)

### 🔬 6대 물리적 힘 (The 6 Physical Forces)

1. **추진력 (Thrust):** 체결강도 100% 초과분을 기본 엔진으로 삼고, 당일 평균 대비 거래량(Volume Ratio)을 '질량/부스터'로 곱하여 가속도를 증폭시킵니다.
2. **중력 (Gravity / Absolute Deviation):** 주가가 당일 평균가(VWAP)에서 위아래로 멀어질수록 강한 탄성 저항력(0.0 ~ -1.0)으로 작용합니다. 위로 멀어지면 과열 매수를 방지하고, 아래로 멀어지면 데드캣 바운스(가짜 반등) 진입을 원천 차단합니다.
3. **항력 (Drag):** RSI 과열 지표를 마찰열로 계산합니다. 주가가 급등할수록 차익실현 매물이 거세져 속도의 무한 발산을 막습니다. 
4. **자기력 (Magnetic Force):** 매도 호가창에 물량이 압도적으로 쌓여있을 때 발생하는 위쪽 호가로의 '진공 흡입력'을 수치화합니다.
5. **져크 (Jerk / 가속도의 미분):** 5분 전 체결강도와 현재 체결강도를 비교하여 수급이 가속 중인지 감속 중인지 파악해 킬 스위치(Bail-out)의 척도로 삼습니다.
6. **충격량 (Impulse / 방향성 틱 체결대금):** 단일 틱 체결대금이 1억 원 이상 터질 때 타격을 가합니다. 방향성이 없는(음봉) 대량 체결은 매도 폭탄으로 간주하여 필터링합니다.

---

## ⚡ 시스템 아키텍처 (System Architecture)

### 1. 동적 청산 및 이익 보존 전략 (Kinematic Exit & Retention)

방어선 하나로 모든 상황을 커버하며, 잔파도는 버티고 변곡점은 즉시 잘라냅니다. 시간 제약이나 타이머 없이 오직 실시간 물리량으로만 구동됩니다.

* **Universal Shield:** 하락장에서는 바짝 당겨 목을 치고, 상승장에서는 넉넉히 룸을 주는 1차 방어막.
* **Profit Lock-in (2.0% 이상):** 수익 궤도에 오르면 `이중 잠금 스나이퍼`와 `에너지 보존 비율(50%)`이 가동되어 수익을 강제로 지켜냅니다.
* **Engine Dead:** 속도($V_t$)가 마이너스로 추락하여 상승 관성이 완전히 소멸했을 때 기계적 탈출.

### 2. 상태 관리 제로화 (State-Free Architecture) & L2 로깅

* **L1 Cache (In-Memory Engine):** 트레이딩 메인 스레드는 시간 유예, 타이머 등 쓸모없는 상태를 일절 추적하지 않습니다. 오직 메모리(`dict`)에 존재하는 $V_{t-1}$ 상태값과 최고점(`max_price`)만 참조하여 즉각적으로 점수를 연산(Zero Latency)합니다.
* **L2 Backup (Asynchronous DB Logging):** 계산이 완료된 결과는 전용 워커(`ThreadPoolExecutor`)를 통해 백그라운드에서 SQLite DB로 비동기 저장(Fire-and-Forget)됩니다.

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
│       │   ├── physics_engine.py  # 절대 이격 중력, 가속도 산출 엔진
│       │   ├── state_manager.py   # 관성 상태 캐싱 (L1 Cache)
│       │   └── indicators.py      # Tri-ATR (General, Down, Up) 수학적 파이프라인
│       ├── monitoring/
│       │   ├── strategy.py        # Thrust Lock, Universal Shield & 이중 잠금 스나이퍼 로직
│       │   └── manager.py         # 포지션 관리 및 비동기 처리
├── logs/                   # 실행 로그
├── data/                   # 분석 데이터 (SQLite DB)
├── tools/                  # 거래 검증 및 1분봉 추출 등 사후 분석 툴킷
└── main.py                 # 프로그램 진입점