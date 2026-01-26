import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler

# --- [로깅 시스템 고도화 설정] ---

class ExcludeErrorFilter(logging.Filter):
    """ERROR(40) 레벨 이상의 로그를 제외하여 trading.log를 깨끗하게 유지"""
    def filter(self, record):
        return record.levelno < logging.ERROR

def setup_structured_logging():
    """Trading, Error, Status 로그를 분리하여 초기화합니다."""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 기본 포맷 설정
    standard_format = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')
    json_format = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(module)s", "message": "%(message)s"}'
    )

    # [1] Root Logger 설정 (전체 시스템용)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 콘솔 핸들러: 실시간 확인용 (INFO 레벨)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(standard_format)
    root_logger.addHandler(console_handler)

    # Trading Log 핸들러: 일반 운영 로그 (INFO~WARNING, 에러 제외)
    trading_handler = TimedRotatingFileHandler(
        filename=f"{log_dir}/trading.log", when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    trading_handler.setLevel(logging.INFO)
    trading_handler.setFormatter(json_format)
    trading_handler.addFilter(ExcludeErrorFilter()) # 에러는 여기서 제외
    root_logger.addHandler(trading_handler)

    # Error Log 핸들러: 장애 로그 (ERROR~CRITICAL만 기록)
    error_handler = TimedRotatingFileHandler(
        filename=f"{log_dir}/error.log", when="midnight", interval=1, backupCount=90, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(json_format)
    root_logger.addHandler(error_handler)

    # [2] Status Logger 설정 (50개 종목 상태 전용)
    # propagate=False를 설정하여 trading.log에 중복 기록되는 것을 방지합니다.
    status_logger = logging.getLogger("status")
    status_logger.setLevel(logging.INFO)
    status_logger.propagate = False 

    status_handler = TimedRotatingFileHandler(
        filename=f"{log_dir}/status.log", when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    # 상태 데이터는 표 형태이므로 날짜만 붙은 심플한 포맷 사용
    status_handler.setFormatter(logging.Formatter('%(message)s'))
    status_logger.addHandler(status_handler)