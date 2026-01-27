# src/kiwoom_stock/api/services/market.py
import datetime
from typing import Dict, List, TypedDict, Optional
from ..parser import parse_chart_item, clean_numeric

class MarketService:
    def __init__(self, base):
        self.base = base

    # --- [기존 유지] 시장 탐색 및 차트 지표 ---
    def get_top_trading_value(self, market_tp: str = "001") -> List[Dict]:
        """거래대금 상위 종목 조회 (ka10032)"""
        data = self.base.request("/api/dostk/rkinfo", "ka10032", {
            "mrkt_tp": market_tp,
            "mang_stk_incls": "0",
            "stex_tp": "1"
        })
        return data.get("trde_prica_upper", [])

    def get_stock_basic_info(self, stock_code: str) -> List[Dict]:
        """
        주식기본정보요청 (ka10001)

        """
        data = self.base.request("/api/dostk/stkinfo", "ka10001", {
            "stk_cd": stock_code
        })
        return data

    def get_minute_chart(self, stock_code: str, tic: str = "5") -> List[Dict]:
        """
        주식 분봉 차트 조회 (ka10080)

        """
        data = self.base.request("/api/dostk/chart", "ka10080", {
            "stk_cd": stock_code,    # 종목코드
            "tic_scope": tic,  # 틱범위
            "upd_stkpc_tp": "1"      # 수정주가구분 (1: 적용)
        })

        items = data.get("stk_min_pole_chart_qry", [])
        
        # parse_chart_item은 {'close': 1200.0, 'open': 1100.0, ...} 형태의 딕셔너리를 반환합니다.
        return [parse_chart_item(item) for item in items]

    # --- [신규/개선] 실시간 수급 지표 (ka10063 대체) ---

    def get_tick_strength(self, stock_code: str) -> float:
        """
        주식 체결강도 추이 조회 (ka10046)
        매수세의 실시간 공격성을 측정하는 Base 지표입니다. (100% 기준)
        """
        data = self.base.request("/api/dostk/mrkcond", "ka10046", {
            "stk_cd": stock_code
        })
        items = data.get("cntr_str_tm", [])
        if not items:
            return 100.0
        # 최신 틱의 체결강도 반환
        return clean_numeric(items[0].get("cntr_str", "100.0"))

    def get_program_trade(self) -> Dict[str, float]:
        """
        종목별 프로그램 매매 현황 조회 (ka90004)
        외인/기관 수급의 실시간 대용치(Proxy)로 활용됩니다.
        """

        # 1. 오늘 날짜 가져오기
        today = datetime.date.today()

        data = self.base.request("/api/dostk/stkinfo", "ka90004", {
            "dt": today.strftime('%Y%m%d'),
            "mrkt_tp": "P00101",
            "stex_tp": "1"
        })

        return data.get("stk_prm_trde_prst", [])

    def get_foreign_window_total(self, market_tp: str = "001") -> float:
        """
        외국계 창구 매매 상위 조회 (ka10037)
        외국계 증권사 합계 순매수량을 반환하여 수급의 질을 판정합니다.
        """
        data = self.base.request("/api/dostk/rkinfo", "ka10037", {
            "mrkt_tp": market_tp,
            "dt": "0",
            "trde_tp": "1",
            "sort_tp": "1",
            "stex_tp": "1",
        })
        
        return data.get("frgn_wicket_trde_upper", [])