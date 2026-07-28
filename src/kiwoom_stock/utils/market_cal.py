import logging
from datetime import date, datetime
import exchange_calendars as xcals

logger = logging.getLogger(__name__)


def is_krx_open_on(target_date: date) -> bool:
    """
    통신이 필요 없는 글로벌 표준 달력(exchange_calendars)을 사용하여
    지정한 날짜가 한국거래소(XKRX) 영업일인지 확인합니다.
    """
    try:
        krx_cal = xcals.get_calendar("XKRX")
        target_date_text = target_date.isoformat()

        # 네트워크 통신 없이 로컬 메모리에서 즉시 휴장일 여부 판별
        if krx_cal.is_session(target_date_text):
            return True
        else:
            logger.info("%s은 KRX 휴장일입니다. (오프라인 달력 기준)", target_date_text)
            return False

    except Exception as e:
        logger.error(f"[Market Cal] 로컬 캘린더 판별 중 오류 발생: {e}")
        # 예기치 않은 오류 발생 시, 보수적으로 휴장일로 간주하여 엔진을 보호합니다.
        return False


def is_krx_open_today() -> bool:
    """Check the system-local current date through the explicit-date adapter."""

    return is_krx_open_on(datetime.now().date())
