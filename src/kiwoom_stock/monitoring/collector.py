import logging
import re
from typing import List, Dict
from ..api.parser import clean_numeric

logger = logging.getLogger(__name__)

class MarketDataCollector:
    """[Module] API 통신 및 데이터 원본 수집 전문 담당"""
    def __init__(self, client):
        self.client = client

    def fetch_stock_basic(self, code: str) -> Dict:
        """[ka10001] 주식기본정보요청"""
        try:
            item = self.client.market.get_stock_basic_info(code)
            return{
                key: clean_numeric(value) for key, value in item.items()
                if not re.search(r'[가-힣a-zA-Z]', str(value))
            }
        except Exception as e:
            logger.error(f"[{code}] 기본 정보 수집 실패: {e}")
            return {}

    def fetch_tick_strength(self, code: str) -> List[Dict]:
        """[ka10046] 체결강도추이시간별요청 수집"""
        try:
            items = self.client.market.get_tick_strength(code)
            return [
                {
                    key: clean_numeric(value) for key, value in item.items()
                    if not re.search(r'[가-힣a-zA-Z]', str(value))
                }
                for item in items
            ]
        except Exception as e:
            logger.error(f"[{code}] 체결강도 수집 실패: {e}")
            return []

    def fetch_minute_chart(self, code: str, tic: str = "1") -> List[Dict]:
        """범용 분봉 데이터 수집 (1, 5, 60분 등)"""
        try:
            items = self.client.market.get_minute_chart(code, tic=tic)        
            return [
                {
                    key: clean_numeric(value) for key, value in item.items()
                    if not re.search(r'[가-힣a-zA-Z]', str(value))}
                for item in items
            ]
            
        except Exception as e:
            logger.error(f"[{code}] {tic}분봉 수집 실패: {e}")
            return []

    def fetch_program_trade(self) -> Dict[str, Dict]:
        """프로그램 매매 벌크 데이터 수집 및 맵핑"""
        try:
            items = self.client.market.get_program_trade()
            return {
                item['stk_cd']: {
                    k: clean_numeric(v) for k, v in item.items()
                    if not re.search(r'[가-힣a-zA-Z]', str(v))}
                for item in items if item.get('stk_cd')
            }
        except Exception as e:
            logger.error(f"프로그램 매매 데이터 수집 실패: {e}")
            return {}

    def fetch_foreign_window_trade(self) -> Dict[str, Dict]:
        """외국계 창구 매매 벌크 데이터 수집 및 맵핑"""
        try:
            items = self.client.market.get_foreign_window_total()
            return {
                item['stk_cd']: {
                    "netprps_prica": clean_numeric(item.get("netprps_prica", "0")),
                    "trde_prica": clean_numeric(item.get("trde_prica", "1")), # 분모(0) 방어
                } for item in items if item.get("stk_cd")
            }
        except Exception as e:
            logger.error(f"외국계 창구 데이터 수집 실패: {e}")
            return {}