🚀 Kiwoom All-Weather Intelligent Monitoring System
본 프로젝트는 키움증권 REST API를 활용하여 시장의 상태(Regime)를 실시간으로 진단하고, 각 상황에 최적화된 지표 가중치를 적용하여 종목을 탐색 및 관리하는 지능형 주식 모니터링 엔진입니다.

🛠 주요 개선 사항 및 특징
1. 시장 레짐 기반 동적 전략 (Multi-Regime Strategy)
실시간 시장 진단: RSI와 ATR 지표를 결합하여 현재 시장을 5가지 레짐(안정적 강세장, 변동성 강세장, 평온 구간, 조용한 하락장, 패닉 하락장)으로 분류합니다.

가중치 최적화: 각 레짐에 따라 Alpha, 수급, VWAP 이격도, 추세 지표의 가중치를 동적으로 변경하여 시장 적응력을 극대화합니다.

2. 고도화된 데이터 관리 및 사후 검증
상세 지표 기록: 매수 시점의 총점뿐만 아니라 각 지표별 상세 점수(Alpha, Supply, VWAP, Trend)를 DB에 개별 기록하여 로직의 효용성을 정밀 분석합니다.

분석 툴킷 제공:

backtest_reporter.py: Rich 라이브러리 기반의 시각화된 일별/레짐별 성과 리포트 생성.

trade_validator.py: 개별 종목의 지표 기여도 분석 및 로직 타당성 검증.

3. 시스템 안정성 및 유연성
네트워크 내구성: API 타임아웃 및 통신 에러 발생 시 지수 백오프(Exponential Backoff) 기반의 자동 재시도 로직을 적용하였습니다.

관심사 분리 (SoC): 엔진, 분석기, 전략, 관리자, 알림 모듈을 완전히 분리하여 유지보수와 기능 확장이 용이합니다.

설정 파일 통합: 시스템 인프라 설정(config.json)과 매매 로직 설정(strategy_config.json)을 분리하여 관리합니다.

📂 프로젝트 구조
Plaintext

kiwoom_stock/
├── src/kiwoom_stock/
│   ├── api/            # 키움 REST API 클라이언트 및 예외 처리
│   ├── core/           # DB 관리 및 기술적 지표 계산
│   └── monitoring/     # 핵심 트레이딩 엔진 (MarketAnalyzer 등)
├── tools/              # 분석 및 관리 도구 (Reporter, Validator)
├── config/             # 시스템 및 전략 설정 파일
├── logs/               # 구조화된 운영 및 에러 로그
└── trades.db           # SQLite 기반 통합 거래 데이터베이스
🚀 시작하기
1. 환경 설정
config/ 폴더 내에 필요한 설정 파일을 생성합니다.

config.json: API 키 및 계좌 정보

strategy_config.json: 레짐별 가중치 및 매매 임계값

2. 실행 및 분석
엔진 실행: python main.py를 통해 실시간 감시를 시작합니다.

로그 확인: logs/trading.log에서 시스템 작동 상태를 확인합니다.

성과 분석: 장 종료 후 python tools/backtest_reporter.py를 실행하여 일일 성과 리포트를 확인합니다.
