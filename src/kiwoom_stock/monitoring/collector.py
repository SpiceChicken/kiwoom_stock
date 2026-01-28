import logging
from typing import List, Dict
from ..api.parser import parse_chart_item, clean_numeric

logger = logging.getLogger(__name__)

class MarketDataCollector:
    """[Module] API 통신 및 데이터 원본 수집 전문 담당"""
    def __init__(self, client):
        self.client = client

    def fetch_stock_basic(self, code: str) -> Dict:
        """[ka10001] 주식기본정보요청"""
        try:
            return self.client.market.get_stock_basic_info(code)
        except Exception as e:
            logger.error(f"[{code}] 기본 정보 수집 실패: {e}")
            return {}

    def fetch_tick_strength(self, code: str) -> float:
        """[ka10046] 체결강도추이시간별요청 수집"""
        try:
            return self.client.market.get_tick_strength(code)
        except Exception as e:
            logger.error(f"[{code}] 체결강도 수집 실패: {e}")
            return 100.0

    def fetch_minute_chart(self, code: str, tic: str = "1") -> List[Dict]:
        """범용 분봉 데이터 수집 (1, 5, 60분 등)"""
        try:
            items = self.client.market.get_minute_chart(code, tic=tic)        
            return [
                {key: clean_numeric(value) for key, value in item.items()}
                for item in items
            ]
            
        except Exception as e:
            logger.error(f"[{code}] {tic}분봉 수집 실패: {e}")
            return []

    def fetch_program_trade(self) -> Dict[str, Dict]:
        """프로그램 매매 벌크 데이터 수집 및 맵핑"""
        try:
            pgm_list = self.client.market.get_program_trade()
            return {
                item['stk_cd']: {
                    "net_amt": clean_numeric(item.get("netprps_prica", "0")),
                    "ratio": clean_numeric(item.get("all_trde_rt", "0")),
                    "buy_amt": clean_numeric(item.get("buy_cntr_amt", "0")),
                    "sel_amt": clean_numeric(item.get("sel_cntr_amt", "0"))
                } for item in pgm_list if item.get("stk_cd")
            }
        except Exception as e:
            logger.error(f"프로그램 매매 데이터 수집 실패: {e}")
            return {}

    def fetch_foreign_window_trade(self) -> Dict[str, Dict]:
        """외국계 창구 매매 벌크 데이터 수집 및 맵핑"""
        try:
            frgn_list = self.client.market.get_foreign_window_total()
            return {
                item['stk_cd']: {
                    "netprps_prica": clean_numeric(item.get("netprps_prica", "0")),
                    "trde_prica": clean_numeric(item.get("trde_prica", "1")), # 분모(0) 방어
                } for item in frgn_list if item.get("stk_cd")
            }
        except Exception as e:
            logger.error(f"외국계 창구 데이터 수집 실패: {e}")
            return {}