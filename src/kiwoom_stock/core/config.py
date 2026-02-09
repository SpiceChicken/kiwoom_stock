"""
[Core] Configuration Module (Dynamic Loader)
- 하드코딩된 기본값 없이, 'config' 디렉토리 내의 모든 .json 파일을 읽어옵니다.
- 파일명(대문자)이 곧 변수명이 됩니다.
  예: scoring_config.json -> SCORING_CONFIG 변수로 자동 생성
"""

import os
import json
import logging
import sys

logger = logging.getLogger(__name__)

def _load_config_files():
    """
    config 디렉토리의 모든 JSON 파일을 읽어 전역 변수로 등록하는 함수
    """
    # 1. config 디렉토리 위치 찾기
    # 우선순위: 1.실행위치/config  2.현재파일상위/config  3.프로젝트루트/config
    current_file_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_file_dir, '../../../../'))
    
    candidates = [
        os.path.join(os.getcwd(), 'config'),           # 실행 위치 기준
        os.path.join(project_root, 'config'),          # 프로젝트 루트 기준
        os.path.join(current_file_dir, 'config'),      # 현재 파일 기준 (없을 확률 높음)
        os.path.join(current_file_dir, '../../config') # src/kiwoom_stock/config (혹시 모를 위치)
    ]

    config_dir = None
    for path in candidates:
        if os.path.exists(path) and os.path.isdir(path):
            config_dir = path
            break
    
    if config_dir is None:
        logger.critical("[Config] 'config' directory not found! System may fail.")
        return

    logger.info(f"[Config] Loading configurations from: {config_dir}")

    # 2. 파일 스캔 및 로드
    loaded_count = 0
    
    for filename in os.listdir(config_dir):
        if filename.endswith('.json'):
            file_path = os.path.join(config_dir, filename)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 3. 동적 변수 생성 (Magic Logic)
                # 파일명: scoring_config.json -> 변수명: SCORING_CONFIG
                var_name = filename.replace('.json', '').upper()
                
                # globals()를 사용하여 현재 모듈의 전역 변수로 등록
                globals()[var_name] = data
                
                logger.debug(f"  - Loaded: {filename} -> config.{var_name}")
                loaded_count += 1
                
            except json.JSONDecodeError as e:
                logger.error(f"  - Failed to parse JSON {filename}: {e}")
            except Exception as e:
                logger.error(f"  - Error loading {filename}: {e}")

    if loaded_count == 0:
        logger.warning("[Config] No JSON config files loaded. Check directory content.")
    else:
        logger.info(f"[Config] Successfully loaded {loaded_count} config files.")

# 모듈이 import 될 때 즉시 실행
_load_config_files()