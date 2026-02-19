# 🚀 Kiwoom All-Weather Intelligent Monitoring System (v3.0)

본 프로젝트는 키움증권 REST API를 기반으로 시장의 상태(Regime)를 실시간 진단하고, **물리학적 합력 모델(Physics-based Net Force)**과 **동적 설정 아키텍처**를 통해 주도주를 포착하는 차세대 퀀트 트레이딩 시스템입니다.

**v3.0 Major Update**:

1. **Physics-based Alpha**: '추진력(Thrust)'과 '저항(Resistance)'의 합력으로 상승 강도를 계산하여 역배열 가짜 신호를 원천 차단.
2. **Dynamic Configuration**: 코드 수정 없이 JSON 파일만으로 알고리즘 민감도를 실시간 튜닝하는 'Hot-Reload' 아키텍처 구축.
3. **Unit-Corrected Supply**: 수급 데이터의 단위(Scale) 불일치 문제를 해결하여 '진짜 수급 주도주' 변별력 확보.
4. **Universal Filter**: 통계적 패배 공식을 사전 차단하는 4중 안전장치 도입.

---

## 🧠 Core Engine: Physics-based Quant Scoring
본 프로젝트의 심장부인 트레이딩 엔진은 단순한 휴리스틱(if-else)을 배제하고, 주가의 움직임을 유체역학과 운동 에너지로 해석하는 **물리학적/수학적 모델링**을 기반으로 작동합니다.

### 🎯 2-Stage 단기 폭발 모델 (Aggressive Short-term Model)
엔진은 '입장권 검사'와 '타점 트리거'의 완벽하게 분리된 2단계(2-Stage) 필터링 아키텍처를 가집니다.

#### Stage 1: Alpha Score (잠재 에너지 / Qualifier)
"이 종목이 오늘 뛸 자격이 있는가?"를 판별하는 문지기입니다.
* **Volume Thrust:** 거래량 폭발을 추진력(Thrust)으로 해석.
* **Momentum Heat:** RSI 등을 기반으로 주가의 열에너지(Heat) 측정.
* **통과 기준:** Alpha 점수 70점(관심선) 미만 시, 이후 타점이 아무리 좋아도 진입을 차단하여 불필요한 노이즈(가짜 신호)를 원천 봉쇄합니다.

#### Stage 2: S.V.T 3-Factor Model (운동 에너지 / Trigger)
Alpha 관문을 통과한 종목에 한해, **"지금 방아쇠를 당겨야 하는가?"**를 결정하는 3대 전술 지표입니다.

1. **Supply (수급 폭발력):** 유체역학의 압력(Pressure) 모델링.
   * 단순히 '외국인이 샀다'가 아니라, **'현재 거래대금 중 주포(기관/프로그램)의 자금 밀도(Penetration)'**를 측정합니다.
   * 체결강도 100% 이하는 가차 없이 0점 처리하며, 잉여 힘(Excess Power)에 대해서만 Tanh(S-Curve)로 비선형 점수를 부여합니다.
2. **VWAP (탄성 도약 / Trampoline Effect):** * '저가 매수(Mean Reversion)'가 아닌 '가속도 탑승(Momentum)'을 지향합니다.
   * 주가가 VWAP를 발판 삼아 **+1% ~ +3% 위로 솟구치는 순간(Sweet Spot)**을 가우스 곡선(Gaussian Peak)으로 포착하여 최고점을 부여합니다.
3. **Trend (발사각 / Launch Angle):**
   * 단순한 이평선 정배열(미인대회)을 보지 않습니다.
   * 단기 생명선(EMA5)이 중기선(EMA20)을 뚫고 치솟는 **발사 각도(Divergence Percentage)** 자체를 수치화하여 상승 관성(Inertia)에 올라탑니다. 단, 5일선 이탈 시 즉시 급브레이크(-50% 삭감)가 걸립니다.

### 🔬 수학적 정합성 (Mathematical Intuition)
* **Log-Geometric Mean (로그-기하 평균):** S.V.T 3가지 지표를 산술 평균으로 더하지 않습니다. 하나의 지표라도 치명적 결함(과락)이 발생하면 최종 확률이 기하급수적으로 폭락하여 시스템을 보호합니다.
* **Dynamic Weights (동적 가중치):** 거래량이 폭발하면 수급(Supply)의 가중치가 높아지고, 주가가 VWAP에서 너무 멀어지면 VWAP 지표의 신뢰도를 스스로 낮추는 유기적 신경망을 갖추고 있습니다.

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