import json
import logging
import importlib.resources as pkg_resources

logger = logging.getLogger(__name__)

def _load_config_files():
    """
    kiwoom_stock.config 패키지 내부의 모든 JSON 파일을 읽어 전역 변수로 등록
    """
    try:
        # 💡 [궁극의 경로 탐색] OS 폴더 경로가 아닌 '파이썬 모듈'로서 config 패키지를 스캔합니다.
        config_pkg = pkg_resources.files("kiwoom_stock.config")
    except ModuleNotFoundError:
        logger.critical("[Config] 'kiwoom_stock.config' 패키지를 찾을 수 없습니다. 폴더 위치와 __init__.py를 확인하세요.")
        return

    logger.info("[Config] Loading configurations from resource package: kiwoom_stock.config")

    loaded_count = 0
    
    # iterdir()을 통해 패키지 내부의 리소스들을 순회
    for file_resource in config_pkg.iterdir():
        if file_resource.name.endswith('.json'):
            try:
                # 💡 파일 경로(open) 대신 리소스 객체에서 직접 텍스트를 추출
                raw_text = file_resource.read_text(encoding='utf-8')
                data = json.loads(raw_text)
                
                # 동적 변수 생성 (파일명 -> 대문자 변수명)
                var_name = file_resource.name.replace('.json', '').upper()
                
                # globals()를 사용하여 현재 모듈의 전역 변수로 등록
                globals()[var_name] = data
                
                logger.debug(f"  - Loaded: {file_resource.name} -> config.{var_name}")
                loaded_count += 1
                
            except json.JSONDecodeError as e:
                logger.error(f"  - Failed to parse JSON {file_resource.name}: {e}")
            except Exception as e:
                logger.error(f"  - Error loading {file_resource.name}: {e}")

    if loaded_count == 0:
        logger.warning("[Config] No JSON config files loaded. Check 'kiwoom_stock.config' package.")
    else:
        logger.info(f"[Config] Successfully loaded {loaded_count} config files.")

# 모듈이 import 될 때 즉시 실행
_load_config_files()