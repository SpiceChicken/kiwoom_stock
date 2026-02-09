import logging
import math
from datetime import datetime, time, timedelta
from typing import Dict, Tuple, Optional, List, Any

from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core import scoring
from kiwoom_stock.core.types import MarketRegime

logger = logging.getLogger(__name__)

class TradingStrategy:
    """
    [Strategy] 트레이딩 전략 (v2.5 Modularized)
    - 역할: Analyzer가 수집한 데이터를 바탕으로 매수/매도/관망 여부를 '판단'
    - 특징 1: 점수 계산(Calculation) 로직은 'scoring' 모듈로 위임하여 코드를 경량화
    - 특징 2: 'SupplyData' 데이터 클래스를 사용하여 타입 안전성 확보
    - 특징 3: 차등 진입 전략(Primary Driver) 및 과열 필터 적용
    """
    
    def __init__(self, strategy_config: Dict):
        self.settings = strategy_config
        self.momentum_threshold = strategy_config.get("momentum_threshold", 10.0)
        self.debug_mode = strategy_config.get("debug_mode", False)

        # 시간 설정 (장 마감 3분 전 강제 청산 등)
        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()

        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config: Dict[str, Any] = {}
        
        # [Thresholds] 진입 임계값 초기화 (87.0/82.0 이원화)
        self.curr_strict_th = 87.0  # Trend/Alpha 주도 시 (엄격)
        self.curr_supply_th = 82.0  # Supply 주도 시 (완화)
        self.curr_alert_th = 75.0   # 관심 종목 등록 기준
        self.curr_interest_th = 65.0  # 절대 이탈 기준선 (기본값)

        # 매매 규칙 로드 (익절, 손절, 점수 감쇠율)
        self.decay_rate = strategy_config.get("score_decay_rate", 0.25)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.03)
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.03)

        self.history: Dict[str, List[float]] = {}
        self.total_loss_limit: float = float(strategy_config.get("total_loss_limit", -5))
        deadline_time_str = strategy_config.get("entry_deadline", "15:00")
        self.deadline_time = time.fromisoformat(deadline_time_str)

        if self.debug_mode:
            logger.warning("🚨 [DEBUG MODE] Strategy initialized in TEST mode. (Time checks bypassed)")

    def update_context(self, regime: MarketRegime):
        """
        [Context Update] 시장 레짐에 따라 임계값 동적 조정
        - 디버그 모드일 경우: 설정 파일의 'debug_thresholds'를 강제 적용
        - 일반 모드일 경우: 레짐(강세/약세)에 맞는 설정 로드
        """
        if self.debug_mode:
            if self._current_regime != "DEBUG_MODE":
                self._current_regime = "DEBUG_MODE"
                debug_th = self.settings.get("debug_thresholds", {})
                self.curr_strict_th = debug_th.get("strong", 50.0)
                self.curr_supply_th = debug_th.get("strong_supply", 50.0)
                self.curr_alert_th = debug_th.get("alert", 40.0)
                logger.warning(f"🚨 [DEBUG] Thresholds Fixed: {self.curr_strict_th}/{self.curr_supply_th}")
            return

        regime_val = regime.value if hasattr(regime, 'value') else str(regime)
        
        if self._current_regime != regime_val:
            self._current_regime = regime_val
            regimes = self.settings.get("regimes", {})
            self._cached_config = regimes.get(regime_val, regimes.get("default", {}))
            
            # 설정 파일 값 우선 적용 (없으면 기본값 87/82 유지)
            config_th = self._cached_config.get("thresholds", {})
            self.curr_strict_th = config_th.get('strong', 87.0)
            self.curr_supply_th = config_th.get('strong_supply', 82.0)
            self.curr_alert_th = config_th.get('alert', 75.0)
            self.curr_interest_th = config_th.get('interest', 65.0)
            
            logger.info(f"Strategy Updated: {regime_val} | Strict: {self.curr_strict_th}, Supply: {self.curr_supply_th}")

    # --- Time & Risk Checks ---
    def is_monitoring_time(self) -> bool:
        """장 운영 시간 체크 (디버그 모드 시 무조건 True)"""
        if self.debug_mode: return True
        now = datetime.now()
        if now.weekday() >= 5: return False
        return time(8, 30) <= now.time() <= self.exit_time_obj

    def is_trading_window(self) -> bool:
        """신규 진입 가능 시간 체크 (15:00 마감)"""
        if self.debug_mode: return True
        return datetime.now().time() < self.deadline_time

    def is_kill_switch_activated(self, total_pnl: float) -> bool:
        """계좌 전체 손실 한도 초과 여부 확인"""
        return total_pnl <= self.total_loss_limit

    def get_exit_reason(self, pos: Position, strong_threshold: float) -> Optional[str]:
        """
        [Exit Logic] 청산 조건 판별
        1. Time Cut: 15:27 강제 청산 (디버그 모드 제외)
        2. Stop Loss: 손절선 이탈
        3. Take Profit: 목표가 도달 (단, 점수가 높으면 홀딩하여 수익 극대화)
        4. Score Decay: 점수 급락 시 청산 (수익 중일 땐 더 민감하게 반응)
        """
        profit_rate = (pos.sell_price / pos.buy_price - 1)
        # 종목의 변동성(ATR)에 비례하여 손절폭 부여 (기본 1.5배)
        current_atr = getattr(pos, 'atr_percent', 1.5)
        
        # 1. 시간 청산
        if datetime.now().time() >= self.forced_exit_time:
            return "Day Trade Close (3m Early)"
            
        # 2. [Dynamic Stop Loss] ATR 기반 동적 손절매

        # 목표: ATR * 1.5
        dynamic_stop = -(current_atr * 1.5) / 100
        # 안전장치: 최소 -1.0% 보장 (노이즈 방지), 최대 제한 (stop_loss_rate)
        final_stop = max(min(dynamic_stop, -0.01), self.stop_loss_rate)

        if profit_rate <= final_stop:
            return f"Stop Loss ({profit_rate*100:.1f}%)"
            
        # 3. [Dynamic Take Profit] ATR 기반 동적 익절
        
        # 목표: ATR * 3.0 (손절폭의 2배)
        dynamic_target = (current_atr * 3.0) / 100
        
        # 최소한 기본 목표가(target_profit_rate)는 넘어야 함 (너무 낮은 목표 방지)
        final_target = max(dynamic_target, self.target_profit_rate)

        if profit_rate >= final_target:
            # 목표가를 달성했더라도, 점수가 여전히 강력하면 더 보유 (Trend Following)
            if pos.current_score >= strong_threshold:
                return None 
            return f"Take Profit (+{profit_rate*100:.1f}%)"
        
        # 4. 점수 하락 감지 (Score Decay)
        current_decay = self.decay_rate  # 기본값 (예: 0.25)
        
        # 수익권에서는 민감도를 높여서(Decay 감소) 이익을 빠르게 확정
        # 기존 (*= 1.5) -> 수정 (*= 0.5): 25% 하락 허용 -> 12.5% 하락만 허용
        if profit_rate >= 0.01:
            current_decay *= 0.5 
            
        relative_threshold = pos.buy_score * (1 - current_decay)
        
        # 절대 기준선을 Alert(75) -> Interest Threshold로 완화
        # 잦은 조기 털림 방지
        absolute_threshold = self.curr_interest_th

        # 둘 중 더 낮은 값을 적용하여 웬만하면 버티되, 
        # 수익권이거나 점수가 심각하게 망가지면 매도
        final_sell_threshold = min(relative_threshold, absolute_threshold)

        if pos.current_score < final_sell_threshold:
            return f"Score Decay (-{current_decay*100:.1f}%)"
        return None
    
    def _get_momentum(self, stock_code: str, current_score: float) -> float:
        """점수 변화량(모멘텀) 측정"""
        scores = self.history.get(stock_code, [])
        if not scores:
            self.history[stock_code] = [current_score]
            return 0.0
        avg_prev_score = sum(scores) / len(scores)
        momentum = round(current_score - avg_prev_score, 1)
        scores.append(current_score)
        self.history[stock_code] = scores[-5:] # 최근 5개 점수만 유지
        return momentum
        
    def evaluate(self, metrics: SupplyData) -> Dict:
        """
        [Final Verdict] 통합 평가 (Analyzer가 전달한 SupplyData 기반)
        1. 점수 및 모멘텀 산출
        2. 주 동인(Primary Driver) 분석
        3. 차등 진입 기준 적용 (Supply vs Trend)
        4. 필터링 (과열, 하락세 등) 후 최종 매수 신호 생성
        """
        stock_code = metrics.stock_code
        score, score_detail = metrics.total_score, metrics.score_detail
        momentum = self._get_momentum(stock_code, score)
        
        status = "관망"
        is_buy_signal = False
        
        # 주 동인 식별
        # max 함수의 key를 람다로 명시하여 타입 에러 방지
        primary_driver = max(score_detail, key=lambda k: score_detail[k])

        # [Logic] 차등 진입 전략
        # - Supply 주도: 완화된 기준 (82.0) 적용 -> 기회 포착
        # - Trend 주도: 엄격한 기준 (87.0) 적용 -> 고점 추격 방지
        if primary_driver == 'supply':
            effective_threshold = self.curr_supply_th
        else:
            effective_threshold = self.curr_strict_th

        # 최종 판정
        if score >= effective_threshold:
            if momentum < 0:
                status = "⚠️고점경계" # 점수는 높지만 꺾이는 중
                is_buy_signal = False
            elif score_detail['trend'] >= 90.0:
                status = "⚠️추세과열" # [Filter] Trend 과열 필터 (평균회귀 위험)
                is_buy_signal = False
            else:
                status = "🔥강력추천"
                is_buy_signal = True
        elif score >= self.curr_alert_th:
            if momentum >= self.momentum_threshold:
                status = "🚀수급폭발" # 점수 부족하나 기세가 좋음
                is_buy_signal = False
            else:
                status = "👀관심"
                is_buy_signal = False

        return {
                "score": score,
                "momentum": momentum,
                "status": status,
                'score_detail': score_detail,
                "regime": self._current_regime,
                "is_buy_signal": is_buy_signal,
                "primary_driver": primary_driver,
                # Engine 호환성을 위해 필요한 필드들 전달
                "price": metrics.price,
                "stock_code": stock_code,
                "atr_percent": metrics.atr_percent
            }