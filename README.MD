# 🚀 Kiwoom All-Weather Intelligent Monitoring System (v2.6)

본 프로젝트는 키움증권 REST API를 기반으로 시장의 상태(Regime)를 실시간 진단하고, **가중 기하평균 기반의 승산형 시너지 모델**과 **경량화된 병렬 분석 엔진**을 통해 주도주를 포착하는 차세대 주식 모니터링 시스템입니다.

**v2.6 업데이트 (Logic Enhancement)**: 구조적 안정성(v2.5) 위에 **ATR 기반 동적 리스크 관리**와 **수급 질적 평가** 로직을 더해 실전 매매의 승률과 안정성을 극대화했습니다.

---

## 🛠 핵심 전략 및 알고리즘 (Core Strategy)

### 1. 승산형 시너지 모델 (Multiplicative Synergy Model)
단순 합산 방식의 한계를 극복하기 위해 **가중 기하평균(Weighted Geometric Mean)** 모델을 도입했습니다.
- **철학**: "하나라도 부족한 종목은 잡지 않는다."
- **수학적 구조**: $Total Score = (Alpha^{w1} \times Supply^{w2} \times VWAP^{w3} \times Trend^{w4})$
- **효과**: 특정 지표가 과락일 경우 총점이 급격히 하락하여, 모든 조건이 조화로운 '진짜 주도주'만 선별합니다.

### 2. 스마트 리스크 관리 (Smart Risk Management) [New]
획일적인 손절/익절 기준을 탈피하고, 종목별 변동성(ATR)에 맞춘 **동적 대응 시스템**을 구축했습니다.
- **동적 손절 (Dynamic Stop Loss)**: 종목의 변동폭(ATR)을 반영하여 **-1.0% ~ -3.0%** 사이에서 유연하게 손절선을 설정합니다. (노이즈에 의한 잦은 손절 방지)
- **동적 익절 (Dynamic Take Profit)**: 변동성이 큰 종목은 목표가를 **ATR × 3배**로 자동 상향하여, 추세를 끝까지 즐기며 수익을 극대화합니다.
- **수익 보전 (Profit Lock-in)**: 수익권(+1% 이상) 진입 시, 점수 하락에 대한 민감도를 2배 높여(`decay *= 0.5`) 이익을 반납하지 않고 빠르게 확정합니다.

### 3. 수급 질적 평가 (Supply Quality Filter) [New]
단순 거래량 급증에 속지 않기 위해 **수급 지속성 지수(Persistence Index)**를 도입했습니다.
- **로직**: 거래량이 터졌으나(`Vol Ratio > 2.0`) 실질적인 순매수(외인/기관) 유입이 미미할 경우, 가중치를 50% 삭감하여 **'가짜 수급(Fake Signal)'**을 걸러냅니다.

### 4. 차등 진입 및 과열 방지
- **Primary Driver Logic**: 수급 주도 시 **82점**(완화), 추세 주도 시 **87점**(엄격)으로 진입 장벽을 이원화했습니다.
- **Trend Overheat Filter**: `Trend Score >= 90.0`인 경우 진입을 차단하여 고점 추격 매수를 원천 봉쇄합니다.

---

## 🏗 시스템 아키텍처 (System Architecture)

### 1. 명시적 데이터 구조 (Type-Safe Data Structure)
거대하고 불명확한 딕셔너리(`Dict`) 대신, `dataclass`를 도입하여 데이터의 무결성을 보장합니다.
- **SupplyData**: 종목별 지표, 가격, 수급 정보를 담는 핵심 객체. 필드 정의가 명확하여 IDE 자동완성 및 타입 체크가 가능합니다.

### 2. ATR 데이터 파이프라인 (Data Pipeline)
`Analyzer`에서 계산된 실시간 ATR(변동성) 정보가 `Strategy`의 매매 판단 로직과 `Manager`의 잔고 관리 시스템으로 끊김 없이 전달되어 정교한 리스크 관리를 가능케 합니다.

### 3. 로직의 모듈화 (Modularization)
- **Scoring Module (`core/scoring.py`)**: 전략적 판단과 수학적 계산을 분리하여 테스트 용이성을 확보했습니다.
- **Lightweight Engine**: 복잡한 판단은 각 모듈에 위임하고, 엔진은 전체 흐름과 병렬 처리만을 관장합니다.

---

## 💻 개발 및 테스트 환경 (Dev Experience)

### 디버그 모드 (Debug Mode)
`strategy_config.json` 설정을 통해 장외 시간에도 로직을 검증할 수 있습니다.

- **`debug_mode: true`**: 장 운영 시간 체크 우회.
- **`debug_thresholds`**: 테스트용 낮은 진입 장벽 설정 지원.

```json
// strategy_config.json 예시
{
    "strategy": {
        "debug_mode": true,
        "debug_thresholds": {
            "strong": 50.0,
            "interest": 30.0
        }
    }
}

```

---

## 📊 성능 최적화 (Performance)

* **병렬 처리**: `ThreadPoolExecutor`를 사용하여 수십 개 종목 동시 분석.
* **잔상 효과 (Memory Effect)**: 일시적 호가 공백으로 인한 점수 왜곡 방지 (Decay 로직).
* **Fail-Fast**: 킬스위치 발동 시 즉각적인 전량 매도 및 알림 전송.

```

```