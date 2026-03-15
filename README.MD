# 🚀 Kiwoom All-Weather Intelligent Monitoring System (Pure Kinematics Edition v2.6.1)

본 프로젝트는 키움증권 REST API를 기반으로 주가의 움직임을 질량을 가진 물체의 운동($F=ma$)으로 치환하여 분석하는 **차세대 물리적 퀀트 트레이딩 시스템(Physics-based Quant System)**입니다.

**🔥 Major Update v2.6.1: Kinematic Entry Control & Breakout Override**

V2.6.1 업데이트는 파편화되어 있던 진입/방어 로직을 **'3단계 동역학 진입 통제 센터'**로 우아하게 재조립하고, 거대한 매물대를 막대한 자본으로 부수고 올라가는 주도주를 놓치지 않기 위한 **'진성 돌파(Breakout Override)'** 하이패스 로직을 완성한 최종 마일스톤입니다.

---

## ⚡ 시스템 아키텍처 (System Architecture)

### 1. 3단계 동역학 진입 통제 (3-Stage Kinematic Entry Control)
복잡한 뇌동매매 방어 조건들을 물리적 인과관계에 따라 3단계로 완벽하게 재설계했습니다.

* **[Stage 1] 진성 돌파 하이패스 (Breakout Override):** 고점 중력(Gravity)과 매물대 항력(Drag)에 의해 최종 합력(`Net Force`)이 마이너스로 역전된 악조건 속에서도, 막대한 자본(`Impulse >= 3.0`)과 가속도(`Jerk >= 0.5`), 그리고 충분한 수급(`Thrust >= 1.0`)이 동반되면 저항선을 뚫어낸 '진성 돌파'로 간주하여 최우선적으로 탑승합니다.
* **[Stage 2] 물리적 5대 하드 록 (Hard Locks):**
  하이패스를 통과하지 못한 일반 타점들에 대해 가장 엄격한 물리 법칙을 적용하여 뇌동매매를 차단합니다.
  * `수급 빈곤 (Thrust Low)`: 시장의 돈이 들어오지 않으면(`Thrust < 0.8`) 절대 사지 않습니다.
  * `합력 역전 (Net Force < 0)`: 맞바람에 밀려 추락하는 비행기(`Net Force < 0.0`)에는 타지 않습니다.
  * `고점 과열 (Climax Shield)`: 초고점(`Gravity <= -0.9`)에서 수급이 과도하게 폭발(`Thrust >= 1.5`)하면 세력의 피날레(설거지)로 간주하여 차단합니다.
  * `고공 실속 (Stall Shield)`: 초고점(`Gravity <= -0.9`)에서 수급이 마르면(`Thrust < 1.0`) 추락 직전으로 간주하여 차단합니다.
  * `빈 껍데기 가속 (Fake Breakout)`: 수급(`Thrust >= 1.0`)은 좋아 보이나 실제 뭉칫돈(`Impulse < 1.0`)이 없으면 가짜 반등으로 간주하여 차단합니다.
* **[Stage 3] 정상 궤도 가동 (Standard Entry):**
  위의 깐깐한 5대 락(Lock)을 모두 무사히 통과한 종목 중, 엔진의 가속 페달(`Jerk > 0.0`)을 밟은 경우에만 최종적으로 매수를 집행합니다.

### 2. 동적 청산 및 이익 보존 전략 (Kinematic Exit & Retention)
* **Universal Shield:** 단일 방어선(`-(Up-ATR * 3.0)`)으로 하락장에서는 바짝 당겨 목을 치고, 상승장에서는 넉넉히 룸을 주는 자가-수축형 1차 방어막입니다.
* **Profit Lock-in (2.0% 이상):** 수익 2.0% 궤도에 오르면, 엔진 출력 상실 시 즉시 탈출하는 `이중 잠금 스나이퍼(Dual-Trigger)`와 번 돈의 절반(50%)을 무조건 지키는 `에너지 보존 비율`이 가동됩니다.

### 3. 상태 관리 제로화 & 자동 부검 파이프라인 (Auto Post-Mortem)
* **L1 Cache:** 트레이딩 스레드는 시간/타이머에 의존하지 않고, 메모리의 $V_{t-1}$ 상태값만 참조하여 실시간 연산(Zero Latency)합니다.
* **L3 Auto Post-Mortem:** 메인 엔진 무한 루프가 정상 종료(Graceful Shutdown)되면, 시스템이 자동으로 당일 매매의 1분봉 추출 ➔ 물리 역학 검증(`trade_validator`) ➔ 1분봉 궤적 요약 생성 ➔ Gemini LLM을 통한 아키텍트 총평 작성 ➔ Slack 알림으로 이어지는 단방향 자동 부검 파이프라인을 구동합니다.

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
├── config/                 
│   ├── config.json         
│   └── scoring_config.json 
├── prompts/                # 외부화된 LLM 시스템/유저 프롬프트 (v2.6+)
│   ├── daily_postmortem_system.md
│   └── daily_postmortem_user.md
├── src/
│   └── kiwoom_stock/
│       ├── core/
│       │   ├── physics_engine.py  # 6대 물리적 힘 및 Net Force 연산
│       │   ├── state_manager.py   # 관성 상태 캐싱 (L1 Cache)
│       │   └── indicators.py      # Tri-ATR 수학적 파이프라인
│       ├── monitoring/
│       │   ├── strategy.py        # 3단계 진입 통제 (Breakout Override) & 이익 보존망
│       │   ├── reporter.py        # 장 마감 자동 부검 컨트롤러
│       │   ├── notifier.py        # Slack 알림 및 LLM 궤적 요약 주입
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
```

### 2. 실행

```bash
poetry run python main.py
```

*(실행 후 메인 루프가 정상 종료되면 자동으로 일일 부검 리포트가 Slack으로 발송됩니다.)*

---

## 📊 사후 분석 및 검증 (Backtest Reporting)

매일 장 마감 후 슬랙으로 요약 리포트가 전송되며, 심층 분석이 필요한 경우 `tools/trade_validator.py` 및 각종 분석 스크립트를 통해 DB에 저장된 물리 엔진의 상세 벡터 추이, `Net Force` 차단 내역, 이익 보존망 작동 시점의 `Jerk` 변화 등을 정밀하게 사후 부검할 수 있습니다.