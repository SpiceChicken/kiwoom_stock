import os
import json
from typing import Dict

def load_config(config_name: str = "config.json") -> Dict[str, str]:
    """
    루트 폴더의 설정 파일에서 API 키 정보를 로드합니다.
    """
    # 1. 현재 실행 위치 기준으로 루트의 config.json 경로 찾기
    # 보통 프로젝트 루트에 위치하므로 현재 작업 디렉토리를 기준으로 합니다.
    config_path = os.path.join(os.getcwd(), config_name)
    
    # 2. 파일이 존재하는지 확인
    if not os.path.exists(config_path):
        # 만약 찾지 못했다면 한 단계 상위 디렉토리도 확인 (tests 폴더 등에서 실행 시)
        config_path = os.path.join(os.path.dirname(os.getcwd()), config_name)

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                
                # 필수 키 존재 확인
                if "appkey" in config and "secretkey" in config:
                    return config
                else:
                    raise ValueError("config.json에 appkey 또는 secretkey가 누락되었습니다.")
        except (json.JSONDecodeError, IOError) as e:
            raise ValueError(f"설정 파일을 읽는 중 오류 발생: {e}")

    # 3. 환경 변수 확인 (보안을 위한 2순위 대안)
    appkey = os.getenv("KIWOOM_APPKEY")
    secretkey = os.getenv("KIWOOM_SECRETKEY")
    
    if appkey and secretkey:
        return {"appkey": appkey, "secretkey": secretkey}

    raise ValueError(f"설정 파일({config_name})을 찾을 수 없거나 환경 변수가 설정되지 않았습니다.")

def get_base_url() -> str:
    """
    기본 API URL을 반환합니다. 
    이전 테스트 실패 원인이었던 잘못된 URL(api.kiwoom.com)을 수정합니다.
    """
    # 기본값을 api.kiwoom.com으로 설정해야 정상 동작합니다.
    return os.getenv("KIWOOM_BASE_URL", "https://api.kiwoom.com")