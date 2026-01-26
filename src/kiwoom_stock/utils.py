import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler

# --- [로깅 시스템 고도화 설정] ---

# 에러 로그를 제외하기 위한 필터 클래스 정의
class ExcludeErrorFilter(logging.Filter):
    def filter(self, record):
        # ERROR(40) 레벨보다 낮은 로그(DEBUG, INFO, WARNING)만 허용합니다.
        return record.levelno < logging.ERROR
        
def setup_structured_logging():
    """로그 폴더 생성 및 핸들러 설정 (에러 분리 필터 적용)"""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # 콘솔 핸들러 (기존 유지)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))

    # 2. trading.log 핸들러 설정 (필터 적용)
    file_format = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(module)s", "message": "%(message)s"}'
    )
    file_handler = TimedRotatingFileHandler(
        filename=f"{log_dir}/trading.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(file_format)
    
    # [핵심] 필터를 추가하여 ERROR 이상의 로그가 trading.log에 기록되는 것을 방지합니다.
    file_handler.addFilter(ExcludeErrorFilter())

    # 3. error.log 핸들러 (에러만 수집 - 기존 유지)
    error_handler = TimedRotatingFileHandler(
        filename=f"{log_dir}/error.log",
        when="D",
        interval=1,
        backupCount=90,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.addHandler(error_handler)