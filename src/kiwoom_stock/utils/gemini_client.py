import logging
from typing import Any, Dict, Optional
import importlib.resources as pkg_resources
try:  # Prefer the supported SDK; keep a compatibility fallback for existing installs.
    from google import genai  # type: ignore
    _SDK_KIND = "modern"
except ImportError:  # pragma: no cover - exercised only in legacy environments
    try:
        import google.generativeai as genai  # type: ignore
        _SDK_KIND = "legacy"
    except ImportError:  # dependency is optional until an API key is configured
        genai = None  # type: ignore[assignment]
        _SDK_KIND = "none"

from kiwoom_stock.application.reporting import (
    DailyReportRequest,
    DailyReportStats,
    NarrationResult,
    ReportArtifact,
)

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
        self.model: Optional[Any]
        
        if self.api_key and genai is not None:
            if hasattr(genai, "Client"):
                self.client = genai.Client(api_key=self.api_key)
                self.model = self.client
                self._sdk_kind = "modern"
            else:
                genai.configure(api_key=self.api_key)
                self.model = genai.GenerativeModel(self.model_name)
                self.client = None
                self._sdk_kind = "legacy"
            logger.info(f"✅ Gemini Native Engine 점화 완료 (Model: {self.model_name})")
        else:
            self.model = None
            self.client = None
            self._sdk_kind = "none"
            logger.warning("⚠️ Gemini SDK가 설치되지 않았거나 API Key가 주입되지 않았습니다.")
    
    def generate_content(self, prompt: str, file_path: Optional[str] = None) -> Dict:
        """[Core] Native SDK를 이용한 텍스트 및 파일(멀티모달) 처리"""
        if not self.model:
            return {"success": False, "output": None, "error": "Gemini 엔진 미초기화"}

        try:
            # 💡 첨부 파일이 있는 경우
            if file_path:
                logger.info(f"📎 첨부파일 업로드 중: {file_path}")
                # 구글 임시 서버에 파일 업로드 (보통 48시간 후 자동 삭제됨)
                if self._sdk_kind == "modern":
                    uploaded_file = self.client.files.upload(file=file_path, config={"mime_type": "text/csv"})
                    response = self.client.models.generate_content(
                        model=self.model_name, contents=[uploaded_file, prompt]
                    )
                else:
                    uploaded_file = genai.upload_file(path=file_path, mime_type="text/csv")
                    response = self.model.generate_content([uploaded_file, prompt])
                
                # The SDK-specific call above is the single request.  Do not
                # replay it through the legacy model (the modern client has no
                # such model and a duplicate request would be costly).
                
            # 💡 텍스트만 있는 경우 (기존과 동일)
            else:
                response = (
                    self.client.models.generate_content(model=self.model_name, contents=prompt)
                    if self._sdk_kind == "modern"
                    else self.model.generate_content(prompt)
                )
                
            return {
                "success": True,
                "output": response.text.strip(),
                "error": None
            }
        except Exception as e:
            logger.error("Gemini API 통신 오류 (type=%s)", type(e).__name__)
            return {"success": False, "output": None, "error": str(e)}

    def generate_daily_report(self, stats: Dict, csv_path: Optional[str] = None) -> Dict:
        """외부 MD 파일에서 프롬프트를 읽어와 리포트 생성."""
        if not self.model:
            return {"success": False, "output": None, "error": "Gemini 엔진 미초기화"}

        try:
            # 설치된 package 안의 prompt resource를 읽습니다.
            prompts_pkg = pkg_resources.files("kiwoom_stock.resources.prompts")
            
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
                if self._sdk_kind == "modern":
                    uploaded_file = self.client.files.upload(file=csv_path, config={"mime_type": "text/csv"})
                    response = self.client.models.generate_content(
                        model=self.model_name, contents=[uploaded_file, full_prompt]
                    )
                else:
                    uploaded_file = genai.upload_file(path=csv_path, mime_type="text/csv")
                    response = self.model.generate_content([uploaded_file, full_prompt])
            else:
                response = (
                    self.client.models.generate_content(model=self.model_name, contents=full_prompt)
                    if self._sdk_kind == "modern"
                    else self.model.generate_content(full_prompt)
                )
                
            return {
                "success": True,
                "output": response.text.strip(),
                "error": None
            }
        except Exception as e:
            logger.error("Gemini API 통신 오류 (type=%s)", type(e).__name__)
            return {"success": False, "output": None, "error": str(e)}

    def narrate(
        self,
        *,
        request: DailyReportRequest,
        stats: DailyReportStats,
        trade_artifact: Optional[ReportArtifact],
    ) -> NarrationResult:
        """Adapt the legacy dictionary result to the reporting contract."""
        if not self.model:
            return NarrationResult.unavailable()

        csv_path = (
            trade_artifact.reference
            if trade_artifact is not None
            else None
        )
        result = self.generate_daily_report(
            stats={
                "date": request.report_date,
                "win_rate": stats.win_rate,
                "total_pnl": stats.total_pnl,
                "defense_count": stats.defense_count,
            },
            csv_path=csv_path,
        )
        if result.get("success"):
            output = result.get("output")
            if isinstance(output, str):
                return NarrationResult.succeeded(output)
            return NarrationResult.failed("invalid Gemini response output")

        return NarrationResult.failed("Gemini request failed")
            
    def check_availability(self) -> bool:
        is_available = self.model is not None
        if not is_available:
            logger.error("❌ Gemini 클라이언트가 준비되지 않았습니다.")
        return is_available
