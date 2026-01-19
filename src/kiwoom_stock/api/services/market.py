# src/kiwoom_stock/api/services/market.py
from typing import Dict, List, TypedDict
from ..parser import clean_numeric

# 반환 데이터 구조 정의 (우수성: 타입 안전성 확보)
class TradingValueItem(TypedDict):
    stk_cd: str
    stk_nm: str
    trde_prica: float
    cur_prc: float

class MarketService:
    def __init__(self, base):
        self.base = base

    def get_top_trading_value(self, market_tp: str = "001") -> List[TradingValueItem]:
        """거래대금 상위 종목 조회 (ka10032)"""
        # 파라미터 검증 추가 (우수성: 에러 사전 차단)
        if market_tp not in ["001", "101"]:
            raise ValueError("market_tp는 '001'(코스피) 또는 '101'(코스닥)이어야 합니다.")

        data = self.base.request("/api/dostk/rkinfo", "ka10032", {
            "mrkt_tp": market_tp,
            "mang_stk_incls": "0",
            "stex_tp": "1"
        })
        
        items = data.get("trde_prica_upper", [])
        # 서비스 단에서 수치 데이터 정제 (우수성: 엔진 로직 간소화)
        return [{
            "stk_cd": i.get("stk_cd", ""),
            "stk_nm": i.get("stk_nm", ""),
            "trde_prica": clean_numeric(i.get("trde_prica")),
            "cur_prc": clean_numeric(i.get("cur_prc"))
        } for i in items]

    def get_investor_supply(self, market_tp: str = "001", investor_tp: str = "6") -> List[Dict]:
        """장중 투자자별 매매 현황 조회 (ka10063)"""
        data = self.base.request("/api/dostk/mrkcond", "ka10063", {
            "mrkt_tp": market_tp,
            "amt_qty_tp": "1",
            "invsr": investor_tp,
            "frgn_all": "1" if investor_tp == "6" else "0",
            "smtm_netprps_tp": "0",
            "stex_tp": "3"
        })
        
        items = data.get("stk_invsr_smtm_netprps_qry", [])
        # netprps_qty 등을 미리 숫자로 변환하여 반환
        return [{
            **i,
            "netprps_qty": clean_numeric(i.get("netprps_qty")),
            "netprps_amt": clean_numeric(i.get("netprps_amt"))
        } for i in items]