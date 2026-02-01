# 🚀 Kiwoom All-Weather Intelligent Monitoring System (v2.0)

본 프로젝트는 키움증권 REST API를 기반으로 시장의 상태(Regime)를 실시간 진단하고, **가중 기하평균 기반의 승산형 시너지 모델**과 **병렬 분석 엔진**을 통해 주도주를 포착하는 차세대 주식 모니터링 시스템입니다.

---

## 🛠 핵심 고도화 사항 (Technical Highlights)

### 1. 승산형 시너지 모델 (Multiplicative Synergy Model)

단순 합산(Additive) 방식의 허점을 보완하기 위해 **가중 기하평균(Weighted Geometric Mean)** 모델을 도입했습니다.

- **철학**: "하나라도 부족한 종목은 잡지 않는다."
- **수학적 구조**: $Total Score = (Alpha^{w1} \times Supply^{w2} \times VWAP^{w3} \times Trend^{w4})$
- **효과**: 특정 지표가 과락(예: 수급 부재)일 경우 총점이 지수적으로 하락하여, 모든 조건이 완벽하게 정렬된 '진짜 주도주'만 80점(Strong)의 벽을 넘을 수 있습니다.

### 2. 계층형 고성능 필터링 (Tiered Filtering)

시스템 부하를 최소화하고 실시간성을 극대화하기 위해 연산 단계를 이원화했습니다.

- **Tier 1 (Fast Pass)**: 가격 위치(VWAP 상단), 이평선 정렬(EMA20 상단), 최소 수급 조건을 1차 검현하여 하위 80% 종목을 즉시 필터링.
- **Tier 2 (Deep Analysis)**: 1차 통과 종목에 대해서만 승산형 시너지 점수, ATR 비례 추세 가중치, 모멘텀을 정밀 계산.

### 3. 병렬 분석 엔진 (Parallel Execution Engine)

`ThreadPoolExecutor` 기반의 **Worker-Collector 아키텍처**를 채택하여 분석 레이턴시를 획기적으로 단축했습니다.

- **병렬 워커(Worker)**: 개별 종목의 지표 산출, 전략 평가, 로그 데이터 생성을 여러 스레드에서 동시 처리.
- **메인 컬렉터(Collector)**: 분석 결과를 취합하여 주문 집행 및 포지션 상태(sell_price 등) 업데이트를 순차적으로 수행 (Race Condition 방지 및 API 제한 준수).

### 4. 동적 추세 가중치 및 ATR 감쇄 로직

- **정렬 품질 반영**: 단기/중기/장기 이평선의 이격도와 정렬 상태에 따라 추세 가중치를 0.6~1.0 사이로 동적 조절합니다.
- **과열 방지 모델**: 주가가 EMA60 대비 ATR(평균 실질 변동폭)의 2.5배를 초과 확장할 경우, 추세 가중치를 강제로 삭감하여 고점 추격 매수를 원천 차단합니다.

---

## 🏗 시스템 아키텍처 (System Architecture)

| **모듈** | **주요 역할** | **최적화 기술** |
| --- | --- | --- |
| **Engine** | 전체 루프 제어 및 주문 집행 | ThreadPoolExecutor (8-Workers) |
| **Strategy** | 승산형 점수 산출 및 상태 판정 | 가중 기하평균, 멤버 변수 캐싱 |
| **Analyzer** | 시장 레짐 판정 및 수급 캐싱 | 지수 이동평균(EMA), ATR 지표 |
| **Manager** | 잔고 관리 및 포지션 트래킹 | Zero-Lock State Synchronization |

---

## 📈 운영 및 성과 분석

- **Structured Logging**: `status.log`를 통해 Tier 1을 통과한 유효 종목의 4대 지표 상세 점수와 모멘텀을 실시간 모니터링합니다.
- **Pure Logic Testing**: 매수 시점의 레짐 정보와 상세 점수를 DB에 기록하여, 어떤 상황에서 승산형 모델이 가장 높은 수익률을 기록했는지 사후 검증(Back-testing)할 수 있습니다.

---

## 🚀 시작하기

1. **환경 설정**: `strategy_config.json`에서 레짐별 임계값(Strong, Alert)을 설정합니다.
2. **엔진 가동**: `main.py`를 실행하면 병렬 엔진이 즉시 가동되어 시장을 감시합니다.
3. **모니터링**: `🔥강력추천` 상태가 전송될 때 시스템은 자동으로 주문 레이어로 진입합니다.