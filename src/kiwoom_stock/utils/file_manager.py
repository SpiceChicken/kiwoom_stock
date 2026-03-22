import os
import time
import logging
import shutil

logger = logging.getLogger(__name__)

def clean_old_csv_files(retention_days: int, target_dir: str):
    """
    지정된 디렉토리(예: output/) 하위를 순회하며 
    보존 기간(retention_days)이 지난 CSV 파일과 텅 빈 날짜 폴더를 안전하게 삭제합니다.
    (retention_days가 0이면 S3 백업이 끝난 당일 데이터를 즉시 파기합니다)
    """
    if not os.path.exists(target_dir):
        logger.warning(f"[Cleanup] 대상 디렉토리를 찾을 수 없습니다: {target_dir}")
        return

    # retention_days가 0이면 당일(미래 시간 포함) 파일도 모두 지우도록 설정
    if retention_days == 0:
        cutoff_time = time.time() + 86400  
    else:
        cutoff_time = time.time() - (retention_days * 86400) # 86400초 = 1일
        
    deleted_files = 0
    deleted_dirs = 0

    # 💡 [V3.1] os.walk를 사용하여 날짜별 하위 폴더(YYYYMMDD)까지 모두 탐색 (Bottom-up 방식)
    for root, dirs, files in os.walk(target_dir, topdown=False):
        # 1. 오래된 CSV 파일 삭제
        for name in files:
            if name.endswith('.csv'):
                file_path = os.path.join(root, name)
                
                # 파일의 마지막 수정 시간이 보존 기간을 넘겼는지 확인
                if os.path.getmtime(file_path) < cutoff_time:
                    try:
                        os.remove(file_path)
                        deleted_files += 1
                    except Exception as e:
                        logger.error(f"[Cleanup] 파일 삭제 실패 {file_path}: {e}")
        
        # 2. 내부 파일이 모두 지워져서 텅 빈 날짜 폴더(YYYYMMDD)가 있다면 폴더 자체도 삭제
        for name in dirs:
            dir_path = os.path.join(root, name)
            
            # 폴더가 비어있고, 폴더 생성일 역시 오래되었다면
            if not os.listdir(dir_path) and os.path.getmtime(dir_path) < cutoff_time:
                try:
                    os.rmdir(dir_path)
                    deleted_dirs += 1
                except Exception as e:
                    pass
                    
    if deleted_files > 0 or deleted_dirs > 0:
        logger.info(f"🧹 [Cleanup] {retention_days}일 보존 기준 초과 산출물 파기 완료 (파일 {deleted_files}개, 빈 폴더 {deleted_dirs}개 삭제)")