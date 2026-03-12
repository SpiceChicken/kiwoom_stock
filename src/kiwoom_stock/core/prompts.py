from typing import Dict, Any

class SystemPrompts:
    """시스템 전체에서 사용되는 AI 프롬프트 템플릿 저장소"""
    
    @staticmethod
    def build_daily_post_mortem(stats: Dict[str, Any]) -> str:
        """일일 자동 부검 프롬프트 생성"""
        return f"""
        당신은 퀀트 트레이딩 시스템의 수석 아키텍트입니다.
        오늘 봇이 수행한 매매 통계와, '첨부된 CSV 파일(오늘의 모든 매매 상세 내역)'을 바탕으로 
        전략의 장단점과 뇌동매매 방어 성과를 분석하는 총평을 4~5문장으로 작성해주세요.
        
        첨부된 CSV 데이터를 분석하여 어떤 종목에서 가장 큰 손익이 났는지, 
        어떤 청산 로직(예: Jerk Drop, Trailing Stop)이 유효했는지 구체적인 팩트를 포함하세요.
        문체는 냉철하고 분석적인 전문가(Quant)의 어조여야 합니다.
        
        [오늘의 통계]
        - 날짜: {stats.get('date', 'N/A')}
        - 승률: {stats.get('win_rate', 'N/A')}
        - 총 수익률: {stats.get('total_pnl', '0.00%')}
        - Thrust Lock 등 쉴드 방어 횟수: {stats.get('defense_count', 0)}건
        - 매매 상세 내역: {stats.get('raw_details', '내역 없음')}
        """
        
    @staticmethod
    def build_anomaly_detection(stock_code: str, forces: dict) -> str:
        """(예시) 향후 추가할 수 있는 실시간 이상징후 탐지 프롬프트"""
        return f"종목 {stock_code}에서 특이 물리량({forces})이 포착되었습니다. 분석해주세요."