import os
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

class S3Manager:
    def __init__(self, bucket_name: str):
        self.bucket_name = bucket_name
        # 💡 EC2 인스턴스에 Attach된 IAM Role을 통해 자동으로 자격 증명을 획득합니다.
        self.s3_client = boto3.client('s3')

    def upload_file(self, local_path: str, s3_key: str) -> bool:
        try:
            self.s3_client.upload_file(local_path, self.bucket_name, s3_key)
            return True
        except ClientError as e:
            logger.error(f"[S3 Upload] 실패 {local_path}: {e}")
            return False

    def sync_daily_outputs(self, target_date: str, source_dir: str):
        if not os.path.exists(source_dir):
            logger.warning(f"[S3 Sync] 소스 디렉토리를 찾을 수 없습니다: {source_dir}")
            return

        # S3 내부의 경로를 연/월/일 등으로 우아하게 관리 (예: daily/20260322/)
        s3_prefix = f"daily/{target_date}/"
        upload_count = 0
        
        logger.info(f"[{target_date}] ☁️ S3 데이터 백업 파이프라인 가동...")
        for filename in os.listdir(source_dir):
            if filename.endswith(".csv") and target_date in filename:
                local_path = os.path.join(source_dir, filename)
                s3_key = f"{s3_prefix}{filename}"
                
                if self.upload_file(local_path, s3_key):
                    upload_count += 1
                    
        logger.info(f"[S3 Sync] ✅ 완료. 총 {upload_count}개의 파일을 S3({self.bucket_name})에 백업했습니다.")