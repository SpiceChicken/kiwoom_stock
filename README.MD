# Kiwoom All-Weather Stock Monitoring System (v5.0)

키움증권 REST API를 활용하여 시장 지수(Regime)에 따라 동적으로 매수 기준을 변경하는 지능형 주식 모니터링 시스템입니다.

## 📂 프로젝트 구조

```text
kiwoom_project/
├── src/
│   └── kiwoom_stock/
│       ├── main.py          # 실행 엔트리 포인트
│       ├── api/             # 인증 및 통신 계층 (Authenticator, BaseClient)
│       │   └── services/    # 도메인별 서비스 (Account, Market)
│       ├── core/            # 보조지표 계산 로직 (RSI, Bollinger Bands)
│       ├── monitoring/      # 실시간 감시 엔진 (MultiTimeframeRSIMonitor)
│       └── utils/           # 설정 및 유틸리티 (load_config)
├── tests/                  # 단위 및 통합 테스트 (pytest)
├── pyproject.toml          # 프로젝트 메타데이터 및 의존성 관리
├── config.json             # API 키 설정 (Local 전용, Git 제외 대상)
└── .gitignore              # Git 관리 제외 설정

```

## 🚀 주요 특징

* **멀티 타임프레임 분석**: 1시간 봉으로 추세를 확인하고 5분 봉으로 최적의 진입 시점을 포착합니다.
* **동적 임계값(Dynamic Threshold)**: KODEX 200(069500)을 활용한 시장 전체 RSI에 따라 매수 타점을 유동적으로 산출합니다.
* **상대적 강도(Alpha)**: 시장 지수 대비 종목의 강도를 측정하기 위해 종목 RSI와 시장 RSI의 편차(Alpha)를 분석합니다.
* **수급 및 VWAP 분석**: 외국인/기관의 장중 실시간 순매수 수량과 거래량 가중 평균 가격(VWAP) 돌파 여부를 종합적으로 판단합니다.
* **Wilder's Smoothing RSI**: 표준적인 지표 계산 방식을 통해 신뢰도 높은 데이터를 산출합니다.

## 🛠 설치 및 설정

1. **패키지 설치**: 프로젝트 루트 폴더에서 에디터블 모드로 설치합니다.
```bash
pip install -e .

```


2. **설정 파일 작성**: 루트 폴더에 `config.json` 파일을 생성하고 API 정보를 입력합니다.
```json
{
  "appkey": "YOUR_APP_KEY",
  "secretkey": "YOUR_SECRET_KEY"
}

```


* *참고: 운영 서버 도메인은 `https://api.kiwoom.com`을 기본값으로 사용합니다.*



## 📈 실행 방법

```bash
python -m kiwoom_stock.main

```

## 🧪 테스트 실행

단위 테스트와 통합 테스트를 통해 시스템의 안정성을 검증합니다.

```bash
pytest

```

---

### ⚠️ 주의사항

* `config.json` 및 `appkey.txt` 등 민감한 정보가 포함된 파일은 보안을 위해 절대 공용 저장소(GitHub 등)에 업로드하지 마십시오.
* 본 시스템은 코스피(KOSPI) 시장 데이터를 기반으로 최적화되어 있습니다.