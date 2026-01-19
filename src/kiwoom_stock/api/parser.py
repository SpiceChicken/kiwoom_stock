"""
데이터 정제 및 파싱 모듈
API 응답 문자열을 수치 데이터로 변환하는 기능을 담당합니다.
"""

from typing import Any, Union


def clean_numeric(value: Any) -> float:
    """
    문자열 내 특수기호(+, -, ,)를 제거하고 float으로 변환합니다.
    
    Args:
        value: 변환할 값 (문자열, 정수, 실수 등)
        
    Returns:
        float: 변환된 숫자 (실패 시 0.0)
    """
    if value is None or value == "":
        return 0.0
    
    if isinstance(value, (int, float)):
        return float(value)
    
    try:
        # 문자열에서 콤마, 플러스, 마이너스 기호 제거 후 절대값 처리 (필요시)
        # 기존 코드의 abs() 로직을 포함하여 일관성 유지
        cleaned = str(value).replace(",", "").replace("+", "").replace("-", "")
        return float(cleaned)
    except (ValueError, TypeError):
        return 0.0


def to_int(value: Any) -> int:
    """
    문자열 데이터를 정수형으로 변환합니다.
    
    Args:
        value: 변환할 값
        
    Returns:
        int: 변환된 정수
    """
    return int(clean_numeric(value))


def parse_chart_item(item: dict) -> dict:
    """
    차트 데이터 아이템(봉 데이터)을 표준 형식으로 파싱합니다.
    
    Args:
        item: API 응답 내 개별 봉 데이터 딕셔너리
        
    Returns:
        dict: 정제된 데이터 (close, volume 등)
    """
    return {
        "close": clean_numeric(item.get("cur_prc", "0")),
        "open": clean_numeric(item.get("open_pric", "0")),
        "high": clean_numeric(item.get("high_pric", "0")),
        "low": clean_numeric(item.get("low_pric", "0")),
        "volume": clean_numeric(item.get("trde_qty", "0")),
        "date": item.get("date", ""),
        "time": item.get("time", "")
    }