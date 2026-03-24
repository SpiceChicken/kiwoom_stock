import logging
from typing import Dict, Optional
import importlib.resources as pkg_resources
import google.generativeai as genai

logger = logging.getLogger(__name__)

class GeminiClient:
    """
    [V3.1] Pure AI Communication Client
    - 레거시 파일 I/O(md 처리 등) 완전 제거
    - 오직 프롬프트 텍스트를 받아 통신하고 응답만 반환하는 초경량 모듈
    """
    
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash"):
        self.model_name = model
        self.api_key = api_key
        
        if self.api_key:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel(self.model_name)
            logger.info(f"✅ Gemini Native Engine 점화 완료 (Model: {self.model_name})")
        else:
            self.model = None
            logger.warning("⚠️ Gemini SDK가 설치되지 않았거나 API Key가 주입되지 않았습니다.")
    
    def generate_content(self, prompt: str, file_path: str = None) -> Dict:
        """[Core] Native SDK를 이용한 텍스트 및 파일(멀티모달) 처리"""
        if not self.model:
            return {"success": False, "output": None, "error": "Gemini 엔진 미초기화"}

        try:
            # 💡 첨부 파일이 있는 경우
            if file_path:
                logger.info(f"📎 첨부파일 업로드 중: {file_path}")
                # 구글 임시 서버에 파일 업로드 (보통 48시간 후 자동 삭제됨)
                uploaded_file = genai.upload_file(path=file_path, mime_type="text/csv")
                
                # 텍스트(prompt)와 파일 객체를 리스트([])로 묶어서 전송!
                response = self.model.generate_content([uploaded_file, prompt])
                
            # 💡 텍스트만 있는 경우 (기존과 동일)
            else:
                response = self.model.generate_content(prompt)
                
            return {
                "success": True,
                "output": response.text.strip(),
                "error": None
            }
        except Exception as e:
            logger.error(f"Gemini API 통신 오류: {e}")
            return {"success": False, "output": None, "error": str(e)}

    def generate_daily_report(self, stats: Dict, csv_path: Optional[str] = None) -> Dict:
        """외부 MD 파일에서 프롬프트를 읽어와 리포트 생성."""
        if not self.model:
            return {"success": False, "output": None, "error": "Gemini 엔진 미초기화"}

        try:
            # 'prompt' 패키지 안의 파일을 찾아 텍스트로 읽어옵니다.
            prompts_pkg = pkg_resources.files("prompt")
            
            system_prompt = prompts_pkg.joinpath("daily_postmortem_system.md").read_text(encoding='utf-8')
            user_prompt_template = prompts_pkg.joinpath("daily_postmortem_user.md").read_text(encoding='utf-8')
                
            # 2. 통계 데이터 포맷팅
            stats_str = (
                f"- 날짜: {stats.get('date', 'N/A')}\n"
                f"- 승률: {stats.get('win_rate', 'N/A')}\n"
                f"- 총 수익률: {stats.get('total_pnl', '0.00%')}\n"
                f"- 쉴드 방어 횟수: {stats.get('defense_count', 0)}건"
            )
            user_prompt = user_prompt_template.replace("{stats}", stats_str).replace("{logs}", "첨부된 CSV 파일 참조")
            
            full_prompt = f"{system_prompt}\n\n{user_prompt}"

            # 3. API 호출 (멀티모달)
            if csv_path:
                logger.info(f"📎 첨부파일 업로드 중: {csv_path}")
                uploaded_file = genai.upload_file(path=csv_path, mime_type="text/csv")
                response = self.model.generate_content([uploaded_file, full_prompt])
            else:
                response = self.model.generate_content(full_prompt)
                
            return {
                "success": True,
                "output": response.text.strip(),
                "error": None
            }
        except Exception as e:
            logger.error(f"Gemini API 통신 오류: {e}")
            return {"success": False, "output": None, "error": str(e)}
            
    def check_availability(self) -> bool:
        is_available = self.model is not None
        if not is_available:
            logger.error("❌ Gemini 클라이언트가 준비되지 않았습니다.")
        return is_available