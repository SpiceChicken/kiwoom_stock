import pytest
from kiwoom_stock.core.schema import SupplyData, PgmData, ForeignData
from kiwoom_stock.core import scoring

class TestScoringEngine:
    """
    [Core] 2-Stage 단기 폭발 스코어링 엔진 테스트
    - 목적: Supply, VWAP, Trend 모듈이 의도한 물리적/수학적 로직(Tanh, Log)대로 작동하는지 검증
    """

    @pytest.fixture
    def base_data(self):
        """기본적인 횡보/보합장 상태의 데이터 (Base State)"""
        return SupplyData(
            stock_code="005930",
            cur_prc=10000,
            vwap=10000,
            strength=100.0,
            vol_ratio=100.0,
            trde_qty=100000,  # 거래대금: 10억 (1,000백만원)
            vol_factor=1.0,
            ema5=9900,
            ema20=9800,
            pgm_data=PgmData(netprps_prica=0),
            foreign_data=ForeignData(netprps_prica=0)
        )

    def test_scenario_1_perfect_pump(self, base_data):
        """
        [시나리오 1: 🚀 완벽한 급등 (Perfect Breakout)]
        - 조건: 체결강도 150%, 거래량 폭발(250%), 주포 10% 침투, VWAP 2% 위에서 가속
        - 기대: 모든 점수가 80점 이상 극상위권, Total Score 매우 높음
        """
        data = base_data
        data.strength = 150.0
        data.vol_ratio = 250.0
        data.vol_factor = 5.0
        
        # 거래 규모를 100억
        data.trde_qty = 1000000  # 100만 주
        data.vwap = 10000        # 평단가 1만 원 (총 거래대금 100억 보장)
        data.cur_prc = 10200     # 현재가
        
        # 100억 거래대금 중 10억(1000백만) 주포 순매수 -> 침투율 10% 유지
        data.pgm_data = PgmData(netprps_prica=1000.0)
        
        # 가파른 정배열 (이격도 2% 이상)
        data.ema5 = 10100
        data.ema20 = 9800

        # 계산
        supply = scoring.calculate_supply_score(data)
        vwap = scoring.calculate_vwap_score(data)
        trend = scoring.calculate_trend_score(data)
        
        weights = scoring.calculate_dynamic_weights(data)
        total = scoring.calculate_total_score({'supply': supply, 'vwap': vwap, 'trend': trend}, weights)

        # 검증 (Assert)
        assert supply > 80.0, f"폭발장 수급 점수 미달: {supply}"
        assert vwap > 80.0, f"VWAP 도약 점수 미달: {vwap}"
        assert trend > 80.0, f"가파른 추세 점수 미달: {trend}"
        assert total['total_score'] > 80.0, f"총점 부족: {total['total_score']}"

        print(f"\n[🚀 Perfect Pump] S:{supply:.1f} V:{vwap:.1f} T:{trend:.1f} -> Total: {total['total_score']:.1f}")

    def test_scenario_2_fake_pump(self, base_data):
        """
        [시나리오 2: 🤡 거래량 없는 가짜 상승 (Fake Pump)]
        - 조건: 체결강도 180%로 높지만, 거래량 비율이 40%로 바닥
        - 기대: 퀄리티 필터에 의해 Supply 점수가 반토막 나야 함
        """
        data = base_data
        data.strength = 180.0
        data.vol_ratio = 40.0  # 거래량 실종

        supply = scoring.calculate_supply_score(data)
        
        # 체결강도가 아무리 좋아도 거래량이 50% 미만이면 점수는 50점 이하로 억제되어야 함
        assert supply < 50.0, f"가짜 상승이 너무 높은 점수를 받음: {supply}"
        
        print(f"\n[🤡 Fake Pump] Supply Score: {supply:.1f} (반토막 페널티 정상 작동)")

    def test_scenario_3_submarine(self, base_data):
        """
        [시나리오 3: ⚓ 지하실 (Under VWAP)]
        - 조건: 주가가 VWAP 아래로 -1% 빠진 상태
        - 기대: 공격적 엔진이므로 VWAP 점수는 0점이 되어야 하고, Total 점수도 기하평균에 의해 급락
        """
        data = base_data
        data.vwap = 10000
        data.cur_prc = 9900  # VWAP 대비 -1%

        vwap = scoring.calculate_vwap_score(data)
        
        # 나머지 점수가 좋다고 가정
        weights = scoring.calculate_dynamic_weights(data)
        total = scoring.calculate_total_score({'supply': 90.0, 'vwap': vwap, 'trend': 90.0}, weights)

        assert vwap == 0.0, f"지하실에서 점수가 발생함: {vwap}"
        assert total['total_score'] < 30.0, f"기하평균 과락 시스템 작동 안함. Total: {total['total_score']}"
        
        print(f"\n[⚓ Submarine] VWAP: {vwap:.1f} / Total: {total['total_score']:.1f} (과락 정상 작동)")

    def test_scenario_4_trend_break(self, base_data):
        """
        [시나리오 4: 📉 생명선 이탈 (Trend Broken)]
        - 조건: EMA5 > EMA20 정배열이긴 하지만, 현재가가 EMA5를 아래로 깸
        - 기대: 단기 추세가 꺾인 것으로 간주, Trend 점수가 즉시 반토막 나야 함
        """
        data = base_data
        data.ema20 = 9500
        data.ema5 = 10000 # 이격도 5% (매우 좋음)
        data.cur_prc = 9900 # 그러나 현재가가 5일선을 깨버림 (음봉 꽂힘)

        trend = scoring.calculate_trend_score(data)

        # 이격도가 5%이므로 원래는 100점에 가깝지만, 현재가 이탈로 인해 50점 근방으로 떨어져야 함
        assert trend < 55.0, f"추세 이탈 시 점수 차감이 부족함: {trend}"

        print(f"\n[📉 Trend Break] Trend Score: {trend:.1f} (급브레이크 정상 작동)")