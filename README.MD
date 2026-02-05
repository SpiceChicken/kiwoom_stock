# 🚀 Kiwoom All-Weather Intelligent Monitoring System (v2.5)

본 프로젝트는 키움증권 REST API를 기반으로 시장의 상태(Regime)를 실시간 진단하고, **가중 기하평균 기반의 승산형 시너지 모델**과 **경량화된 병렬 분석 엔진**을 통해 주도주를 포착하는 차세대 주식 모니터링 시스템입니다.

**v2.5 업데이트**: 파이썬의 개발 철학("Explicit is better than implicit")에 따라 데이터 구조를 명시화(`Dataclass`)하고, 핵심 로직을 모듈화하여 안정성과 확장성을 대폭 강화했습니다.

---

## 🛠 핵심 전략 및 알고리즘 (Core Strategy)

### 1. 승산형 시너지 모델 (Multiplicative Synergy Model)
단순 합산 방식의 한계를 극복하기 위해 **가중 기하평균(Weighted Geometric Mean)** 모델을 도입했습니다.
- **철학**: "하나라도 부족한 종목은 잡지 않는다."
- **수학적 구조**: $Total Score = (Alpha^{w1} \times Supply^{w2} \times VWAP^{w3} \times Trend^{w4})$
- **효과**: 특정 지표가 과락일 경우 총점이 급격히 하락하여, 모든 조건이 조화로운 '진짜 주도주'만 선별합니다.

### 2. 차등 진입 전략 (Primary Driver Logic)
점수를 견인한 **주 동인(Primary Driver)**에 따라 진입 장벽을 유연하게 적용합니다.
- **Supply(수급) 주도 시**: 진입 기준 **82.0점** (완화). 수급이 강력한 종목은 기회를 놓치지 않고 포착합니다.
- **Trend/Alpha 주도 시**: 진입 기준 **87.0점** (엄격). 이미 고점일 가능성이 높으므로, 확실한 종목만 선별하여 보합 거래를 필터링합니다.

### 3. 추세 과열 방지 (Trend Overheat Filter)
- **로직**: `Trend Score >= 90.0` 인 경우 **"⚠️추세과열"**로 판정하여 진입을 차단합니다.
- **효과**: 지나친 급등 후의 평균 회귀(Mean Reversion) 위험을 피하고 고점 추격 매수를 방지합니다.

---

## 🏗 시스템 아키텍처 (System Architecture)

### 1. 명시적 데이터 구조 (Type-Safe Data Structure) [New]
거대하고 불명확한 딕셔너리(`Dict`) 대신, `dataclass`를 도입하여 데이터의 무결성을 보장합니다.
- **SupplyData**: 종목별 지표, 가격, 수급 정보를 담는 핵심 객체. 필드 정의가 명확하여 IDE 자동완성 및 타입 체크가 가능합니다.
- **PgmData / ForeignData**: 프로그램 및 외인 수급 데이터를 구조화하여 관리합니다.

### 2. 로직의 모듈화 (Modularization) [New]
- **Scoring Module (`core/scoring.py`)**: 전략적 판단(`Strategy`)과 수학적 계산(`Calculation`)을 분리했습니다. 상태를 가지지 않는 **순수 함수(Pure Function)** 형태로 구현되어 테스트가 용이합니다.
- **Functional Indicators (`core/indicators.py`)**: RSI, ATR 등 보조지표 계산을 위한 범용 함수 모듈입니다.

### 3. 경량화된 실행 엔진 (Lightweight Engine)
`TradingEngine`은 복잡한 세부 판단을 각 모듈(`Manager`, `Strategy`)에 위임하고, 전체적인 **흐름(Flow Control)**과 **병렬 처리(Concurrency)**만을 관장하는 지휘자 역할에 집중합니다.

---

## 💻 개발 및 테스트 환경 (Dev Experience)

### 디버그 모드 (Debug Mode)
장외 시간이나 주말에도 전략 로직을 검증할 수 있도록 강력한 테스트 모드를 지원합니다. `strategy_config.json` 설정을 통해 활성화할 수 있습니다.

- **`debug_mode: true`**: 장 운영 시간(09:00~15:30) 체크 로직을 우회합니다.
- **`debug_thresholds`**: 테스트 시에는 진입 장벽을 낮춰(예: 50점) 매수 로직이 정상 작동하는지 쉽게 확인할 수 있습니다.

```json
// strategy_config.json 예시
{
    "strategy": {
        "debug_mode": true,
        "debug_thresholds": {
            "strong": 50.0,
            "strong_supply": 45.0
        }
    }
}

---

## 📊 성능 최적화 (Performance)
- 병렬 처리: ThreadPoolExecutor를 사용하여 수십 개 종목의 데이터를 동시에 분석합니다.

- 잔상 효과 (Memory Effect): 일시적인 호가 공백으로 인한 점수 왜곡을 방지하기 위해, 이전 점수의 일정 비율을 반영하는 감쇠(Decay) 로직을 적용했습니다.