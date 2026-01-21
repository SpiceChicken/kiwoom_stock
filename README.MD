# 🚀 Kiwoom All-Weather Intelligent Monitoring System

본 프로젝트는 키움증권 REST API를 활용하여 시장의 상태(Regime)를 실시간으로 진단하고, 각 상황에 최적화된 지표 가중치를 적용하여 종목을 탐색 및 관리하는 **지능형 주식 모니터링 엔진**입니다.

## 🛠 주요 특징 및 고도화 사항

### 1. 시장 레짐 기반 동적 전략 (Multi-Regime Strategy)
* **실시간 시장 진단**: RSI와 ATR 지표를 결합하여 현재 시장을 5가지 레짐(안정적 강세장, 변동성 강세장, 평온 구간, 조용한 하락장, 패닉 하락장)으로 분류합니다.
* **동적 가중치 적용**: 각 레짐에 최적화된 지표별(Alpha, 수급, VWAP 이격도, 추세) 가중치를 실시간으로 적용하여 정밀한 점수를 산출합니다.

### 2. 정밀 분석을 위한 데이터 구조
* **상세 지표 기록**: 매수 시점의 총점뿐만 아니라 각 구성 지표의 상세 점수(Alpha, Supply, VWAP, Trend Score)를 DB에 개별 기록하여 로직의 효용성을 사후 검증합니다.
* **구조화된 로깅 (Structured Logging)**: 운영 로그(`trading.log`)와 에러 로그(`error.log`)를 엄격히 분리하고, 날짜별 로테이션을 통해 관리 효율성을 높였습니다.

### 3. 시스템 안정성 및 유연성
* **네트워크 내구성**: API 요청 시 발생하는 타임아웃 및 네트워크 에러에 대응하기 위해 지수 백오프(Exponential Backoff) 기반의 자동 재시도 로직을 적용했습니다.
* **관심사 분리 (SoC)**: 엔진, 분석기, 전략, 관리 모듈을 완전히 분리하여 로직 수정 시 부수 효과를 최소화했습니다.

## 📂 프로젝트 구조

```text
kiwoom_stock/
├── src/kiwoom_stock/
│   ├── api/            # API 클라이언트 및 재시도 로직
│   ├── core/           # DB 스키마 및 기술적 지표 계산
│   └── monitoring/     # 핵심 트레이딩 엔진 및 시장 분석
├── tools/              # 분석 툴킷 (Reporter, Validator, DB Manager)
├── config/             # 시스템 및 전략 설정 (JSON)
└── logs/               # 구조화된 운영/에러 로그 파일

🚀 사용 방법
1. 설정 (Configuration)
config/ 폴더 내에 시스템 설정(config.json)과 전략 설정(strategy_config.json)을 준비합니다.

2. 실행 및 모니터링
실시간 엔진 구동: python main.py

로그 확인: tail -f logs/trading.log (Linux/Mac) 또는 메모장으로 확인

3. 사후 분석 (Analysis)
일별 성과 리포트: python tools/backtest_reporter.py 실행 시 Rich 라이브러리 기반의 시각화 표 출력.

로직 효용성 진단: python tools/trade_validator.py를 통해 개별 지표의 수익 기여도 확인.
