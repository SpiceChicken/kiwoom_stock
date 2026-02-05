# 🚀 Kiwoom All-Weather Intelligent Monitoring System (v2.4)

본 프로젝트는 키움증권 REST API를 기반으로 시장의 상태(Regime)를 실시간 진단하고, **가중 기하평균 기반의 승산형 시너지 모델**과 **경량화된 병렬 분석 엔진**을 통해 주도주를 포착하는 차세대 주식 모니터링 시스템입니다.

---

## 🛠 핵심 고도화 사항 (Technical Highlights)

### 1. 승산형 시너지 모델 (Multiplicative Synergy Model)

단순 합산(Additive) 방식의 허점을 보완하기 위해 **가중 기하평균(Weighted Geometric Mean)** 모델을 도입했습니다.

- **철학**: "하나라도 부족한 종목은 잡지 않는다."
- **수학적 구조**: $Total Score = (Alpha^{w1} \times Supply^{w2} \times VWAP^{w3} \times Trend^{w4})$
- **효과**: 특정 지표가 과락(예: 수급 부재)일 경우 총점이 지수적으로 하락하여, 모든 조건이 완벽하게 정렬된 '진짜 주도주'만 선별합니다.

### 2. 차등 진입 전략 (Primary Driver Logic) [New]

점수를 견인한 **주 동인(Primary Driver)**에 따라 진입 장벽을 다르게 적용하여 수익 기회와 리스크 관리를 동시에 달성합니다.

- **Supply(수급) 주도 시**: 진입 기준 **82.0점** (완화). 수급이 뒷받침된 종목은 적극적으로 매수하여 수익 기회를 포착합니다.
- **Trend/Alpha 주도 시**: 진입 기준 **87.0점** (엄격). 이미 가격이 많이 오른 상태일 수 있으므로, 확실한 종목만 선별하여 '보합/손실' 거래를 필터링합니다.

### 3. 추세 과열 방지 (Trend Overheat Filter) [New]

데이터 분석 결과, 추세 점수가 지나치게 높을수록 오히려 수익률이 하락하는 역설적 현상(Mean Reversion)을 방지합니다.

- **로직**: `Trend Score >= 90.0` 인 경우, **"⚠️추세과열"** 상태로 판정하여 매수 신호를 차단합니다.
- **효과**: 고점 추격 매수로 인한 손실 가능성을 원천 봉쇄합니다.

---

## 🏗 시스템 아키텍처 (System Architecture)

### 1. 경량화된 실행 엔진 (Lightweight Trading Engine)
`engine.py`는 복잡한 판단 로직을 제거하고, 전체적인 흐름(Orchestration)만 관장하는 지휘자 역할로 재설계되었습니다.
- **Workflow**: `Check -> Prepare -> Evaluate (Parallel) -> Decision -> Report`
- **역할 분담**: 매매 판단은 `Strategy`와 `Manager`에게 위임하여 유지보수성을 극대화했습니다.

### 2. 순수 함수형 지표 모듈 (Functional Indicators)
`indicators.py`를 클래스 방식에서 **순수 함수(Pure Function)** 모듈로 리팩토링했습니다.
- **특징**: 상태(State)를 가지지 않으며, 어떤 모듈에서든 `ind.calculate_rsi(...)` 형태로 가볍게 호출하여 사용할 수 있습니다.
- **표준 준수**: RSI(14), ATR(14) 등 기술적 분석의 표준 주기를 기본값으로 채택했습니다.

### 3. 디버그 및 테스트 모드 (Debug Mode)
장외 시간이나 주말에도 전략을 검증할 수 있도록 테스트 지원 기능을 강화했습니다.
- **Debug Mode**: `strategy_config.json`에서 `debug_mode: true` 설정 시, 장 운영 시간 체크를 우회합니다.
- **Debug Thresholds**: 테스트 시에는 낮은 점수(예: 50점)로도 매수가 발동되도록 임계값을 강제로 낮출 수 있습니다.

---

## 📊 성능 최적화 (Performance)

- **병렬 처리 (Concurrency)**: `ThreadPoolExecutor`를 사용하여 수십 개 종목의 전략 평가 및 데이터 계산을 동시에 수행, 분석 지연 시간을 최소화했습니다.
- **데이터 정합성**: 모든 지표 계산 시 데이터의 시간 순서(`[과거 -> 최신]`)를 엄격하게 관리하여 계산 오류를 방지했습니다.