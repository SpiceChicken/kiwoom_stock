# services/market.py
from ..parser import parse_chart_item
from typing import Dict, List

class MarketService:
    def __init__(self, base):
        self.base = base

    def get_minute_chart(self, code, tic="5"):
        data = self.base.request("/api/dostk/chart", "ka10080", {"stk_cd": code, "tic_scope": tic, "upd_stkpc_tp": "1"})
        return [parse_chart_item(i) for i in data.get("stk_min_pole_chart_qry", [])]

    def get_top_trading_value(self, market_tp: str = "001") -> List[Dict]:
        """
        거래대금 상위 종목 조회 (API ID: ka10032)
        
        Args:
            market_tp: 시장구분 (001: 코스피, 101: 코스닥)
            
        Returns:
            List[Dict]: 거래대금 상위 종목 리스트 (trde_prica_upper)
        """
        endpoint = "/api/dostk/rkinfo"
        api_id = "ka10032"
        
        payload = {
            "mrkt_tp": market_tp,       # 시장구분
            "mang_stk_incls": "0",      # 관리종목 포함 여부 (0: 포함)
            "stex_tp": "1"              # 거래소구분 (1: 전체)
        }
        
        response = self.base.request(endpoint, api_id, payload)
        # 응답 Body에서 종목 리스트 추출
        return response.get("trde_prica_upper", [])

    def get_investor_supply(self, market_tp: str = "001", investor_tp: str = "6") -> List[Dict]:
        """
        장중 투자자별 매매 현황 조회 (API ID: ka10063)
        
        Args:
            market_tp: 시장구분 (001: 코스피, 101: 코스닥)
            investor_tp: 투자자구분 (6: 외국인, 7: 기관)
            
        Returns:
            List[Dict]: 종목별 순매수 현황 리스트 (stk_invsr_smtm_netprps_qry)
        """
        endpoint = "/api/dostk/mrkcond"
        api_id = "ka10063"
        
        payload = {
            "mrkt_tp": market_tp,       # 시장구분
            "amt_qty_tp": "1",          # 금액수량구분 (1: 수량)
            "invsr": investor_tp,       # 투자자구분
            "frgn_all": "1" if investor_tp == "6" else "0", # 외국계전체여부
            "smtm_netprps_tp": "0",     # 장중순매수타입 (0: 전체)
            "stex_tp": "3"              # 거래소구분
        }
        
        response = self.base.request(endpoint, api_id, payload)
        # 응답 Body에서 수급 리스트 추출
        return response.get("stk_invsr_smtm_netprps_qry", [])