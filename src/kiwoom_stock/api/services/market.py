# src/kiwoom_stock/api/services/market.py
from typing import Dict, List, TypedDict
from ..parser import parse_chart_item, clean_numeric

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

    def get_market_breadth(self, market_tp: str = "001") -> Dict:
        """
        전업종지수요청을 통한 시장 폭(Breadth) 조회 (ka20003)
        상승, 하락, 보합 종목 수 데이터를 반환합니다.
        """
        # 업종코드 001(종합코스피) 또는 101(종합코스닥)
        data = self.base.request("/api/dostk/sect", "ka20003", {
            "mrkt_tp": market_tp,
            "inds_cd": "001" if market_tp == "001" else "101"
        })
        
        # API 문서상의 상승/하락 종목 수 필드 매핑
        return {
            "rising": clean_numeric(data.get("up_stk_qty")),      # 상승 종목 수
            "falling": clean_numeric(data.get("low_stk_qty")),    # 하락 종목 수
            "unchanged": clean_numeric(data.get("same_stk_qty")), # 보합 종목 수
            "upper_limit": clean_numeric(data.get("up_lmt_qty"))  # 상한가 종목 수
        }

    def get_minute_chart(self, stock_code: str, tic: str = "5") -> List[Dict]:
        """
        주식 분봉 차트 조회 (API ID: ka10080)
        
        Args:
            stock_code: 종목코드 (6자리)
            tic_scope: 분 단위 범위 ("1", "3", "5", "10", "30", "60")
            
        Returns:
            List[Dict]: 정제된 봉 데이터 리스트 (시가, 고가, 저가, 종가, 거래량 포함)
        """
        endpoint = "/api/dostk/chart"
        api_id = "ka10080"
        
        payload = {
            "stk_cd": stock_code,    # 종목코드
            "tic_scope": tic,  # 틱범위
            "upd_stkpc_tp": "1"      # 수정주가구분 (1: 적용)
        }
        
        # 1. API 요청
        response = self.base.request(endpoint, api_id, payload)
        
        # 2. 원본 데이터 리스트 추출
        raw_items = response.get("stk_min_pole_chart_qry", [])
        
        # 3. parser를 사용하여 모든 수치 데이터를 float으로 변환하여 반환 (우수 등급 로직)
        # parse_chart_item은 {'close': 1200.0, 'open': 1100.0, ...} 형태의 딕셔너리를 반환합니다.
        return [parse_chart_item(item) for item in raw_items]