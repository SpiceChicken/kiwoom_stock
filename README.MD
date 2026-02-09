# 🚀 Kiwoom All-Weather Intelligent Monitoring System (v3.0)

본 프로젝트는 키움증권 REST API를 기반으로 시장의 상태(Regime)를 실시간 진단하고, **물리학적 합력 모델(Physics-based Net Force)**과 **동적 설정 아키텍처**를 통해 주도주를 포착하는 차세대 퀀트 트레이딩 시스템입니다.

**v3.0 Major Update**:

1. **Physics-based Alpha**: '추진력(Thrust)'과 '저항(Resistance)'의 합력으로 상승 강도를 계산하여 역배열 가짜 신호를 원천 차단.
2. **Dynamic Configuration**: 코드 수정 없이 JSON 파일만으로 알고리즘 민감도를 실시간 튜닝하는 'Hot-Reload' 아키텍처 구축.
3. **Unit-Corrected Supply**: 수급 데이터의 단위(Scale) 불일치 문제를 해결하여 '진짜 수급 주도주' 변별력 확보.

---

## 🛠 핵심 전략 및 알고리즘 (Core Strategy)

### 1. 물리학적 합력 모델 (Physics-based Net Force Model)

단순 가중치 합산 방식을 폐기하고, 주가를 움직이는 **가속도와 저항의 역학 관계**를 모델링했습니다.

* **추진력 (Thrust)**: 거래량(Log Scale), RSI, 체결강도가 만들어내는 상승 에너지.
* **중력 (Gravity)**: 주가가 **60일 이동평균선(분기 수급선)** 아래에 위치할 경우, 괴리율에 비례하여 강력한 하락 압력(Penalty)을 부과합니다.
* *효과: 역배열 하락 추세에서 발생하는 일시적 거래량 급증(Dead Cat Bounce)을 '가짜 신호'로 필터링.*


* **항력 (Drag)**: 주가가 **VWAP(거래량 가중 평균가)** 아래에 위치할 경우, 악성 매물대 저항을 계산하여 상승력을 차감합니다.

### 2. 가중 기하평균 시너지 (Multiplicative Synergy)

* **철학**: "하나라도 부족한 종목은 잡지 않는다."
* **수학적 구조**: 
* **효과**: 산술평균과 달리, 특정 지표가 0점에 가까우면 총점이 급격히 하락하여 모든 박자가 맞는 종목만 선별합니다.

### 3. 정밀 수급 분석 (Precision Supply Analysis)

* **Unit Normalization**: 거래대금(백만 원)과 순매수금액(원)의 단위 불일치를 해결하여, 수급 점수의 포화(Saturation) 현상을 제거했습니다.
* **Smart Money Weighting**: 외국인 수급에 1.2배 가중치를 부여하여 메이저 자금의 흐름을 추적합니다.

---

## 🏗 시스템 아키텍처 (System Architecture)

### 1. 동적 설정 로더 (Dynamic Configuration Loader)

하드코딩된 설정을 제거하고, **Dependency Injection(의존성 주입)** 패턴을 적용했습니다.

* **구조**: `config/` 디렉토리 내의 모든 `.json` 파일을 런타임에 자동으로 로드하여 전역 변수로 매핑합니다.
* **장점**: 파이썬 코드를 전혀 수정하지 않고도, `scoring_config.json` 수정만으로 알고리즘의 민감도를 튜닝할 수 있습니다.

### 2. 명시적 데이터 구조 (Type-Safe Data Structure)

거대하고 불명확한 딕셔너리(`Dict`) 대신, `dataclass`를 도입하여 데이터의 무결성을 보장합니다.

* **SupplyData**: 종목별 지표, 가격, 수급 정보를 담는 핵심 객체. 필드 정의가 명확하여 IDE 자동완성 및 타입 체크가 가능합니다.

### 3. 로직의 모듈화 (Modularization)

* **Scoring Module (`core/scoring.py`)**: 비즈니스 로직과 설정이 완전히 분리되었습니다. 설정 파일이 없으면 기본값(Fallback)으로 안전하게 구동됩니다.

---

## 📂 디렉토리 구조 (Directory Structure)

```text
Kiwoom-Stock/
├── config/                 # [New] 설정 파일 저장소 (JSON)
│   ├── config.json         # 시스템 및 계좌 설정
│   ├── strategy_config.json# 매수/매도 임계값 전략
│   └── scoring_config.json # 알고리즘 가중치 및 물리학 계수
├── src/
│   └── kiwoom_stock/
│       ├── core/
│       │   ├── config.py   # 동적 설정 로더
│       │   ├── scoring.py  # 물리학적 점수 산출 엔진
│       │   └── ...
├── logs/                   # 실행 로그
├── data/                   # 분석 데이터 (CSV)
├── pyproject.toml          # 프로젝트 의존성 관리
└── main.py                 # 프로그램 진입점

```

---

## 🚀 시작하기 (Getting Started)

본 프로젝트는 `pyproject.toml`을 사용하여 의존성을 관리합니다. (Poetry 사용 권장)

### 1. 환경 설정 및 설치

```bash
# Poetry를 사용하는 경우
poetry install

# 또는 pip를 사용하는 경우
pip install .

```

### 2. 설정 파일 준비

`config/` 폴더에 필요한 JSON 설정 파일들을 위치시킵니다.

* **`scoring_config.json`**: `w_resistance`(중력 계수)를 조절하여 역배열 종목에 대한 페널티 강도를 조절합니다.
* **`config.json`**: 계좌 번호 및 모의투자 여부를 설정합니다.

### 3. 실행

```bash
# Poetry 실행
poetry run python main.py

# 또는 직접 실행
python main.py

```

---

## 📊 대시보드 및 리포트

시스템은 실시간으로 `data/trade_analysis_YYYYMMDD.csv` 파일을 생성합니다.
이 CSV 파일에는 **Alpha, Supply, VWAP, Trend** 4대 지표의 점수와 최종 판단 근거가 기록되어 있어, 엑셀 등을 통해 사후 분석(Back-testing)이 가능합니다.