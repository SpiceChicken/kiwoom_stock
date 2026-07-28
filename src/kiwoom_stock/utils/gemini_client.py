import importlib.resources as pkg_resources
import logging
from typing import Any, Dict, Optional, Protocol, cast

genai: Any
try:  # Prefer the supported SDK; keep a compatibility fallback for existing installs.
    from google import genai as _modern_genai

    genai = _modern_genai
    _SDK_KIND = "modern"
except ImportError:  # pragma: no cover - exercised only in legacy environments
    try:
        import google.generativeai as _legacy_genai

        genai = _legacy_genai
        _SDK_KIND = "legacy"
    except ImportError:  # dependency is optional until an API key is configured
        genai = None
        _SDK_KIND = "none"


class _GeminiResponse(Protocol):
    text: Optional[str]


class _ModernFiles(Protocol):
    def upload(self, *, file: str, config: Dict[str, str]) -> Any: ...


class _ModernModels(Protocol):
    def generate_content(
        self,
        *,
        model: str,
        contents: Any,
    ) -> _GeminiResponse: ...


class _ModernClient(Protocol):
    files: _ModernFiles
    models: _ModernModels


class _ModernSDK(Protocol):
    def Client(self, *, api_key: str) -> _ModernClient: ...


class _LegacyModel(Protocol):
    def generate_content(self, contents: Any) -> _GeminiResponse: ...


class _LegacySDK(Protocol):
    def configure(self, *, api_key: str) -> None: ...

    def GenerativeModel(self, model_name: str) -> _LegacyModel: ...

    def upload_file(self, *, path: str, mime_type: str) -> Any: ...


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
        self.model: Optional[Any] = None
        self.client: Optional[_ModernClient] = None
        self._legacy_model: Optional[_LegacyModel] = None
        self._legacy_sdk: Optional[_LegacySDK] = None
        
        if self.api_key and genai is not None:
            if hasattr(genai, "Client"):
                modern_sdk = cast(_ModernSDK, genai)
                self.client = modern_sdk.Client(api_key=self.api_key)
                self.model = self.client
                self._sdk_kind = "modern"
            else:
                self._legacy_sdk = cast(_LegacySDK, genai)
                self._legacy_sdk.configure(api_key=self.api_key)
                self._legacy_model = self._legacy_sdk.GenerativeModel(
                    self.model_name
                )
                self.model = self._legacy_model
                self._sdk_kind = "legacy"
            logger.info(f"✅ Gemini Native Engine 점화 완료 (Model: {self.model_name})")
        else:
            self.model = None
            self._sdk_kind = "none"
            logger.warning("⚠️ Gemini SDK가 설치되지 않았거나 API Key가 주입되지 않았습니다.")

    @staticmethod
    def _extract_response_text(response: _GeminiResponse) -> str:
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise ValueError("Gemini response did not include text")
        return text.strip()

    def _require_modern_client(self) -> _ModernClient:
        if self.client is None:
            raise RuntimeError("Gemini modern client is not initialized")
        return self.client

    def _require_legacy_model(self) -> _LegacyModel:
        if self._legacy_model is None:
            raise RuntimeError("Gemini legacy model is not initialized")
        return self._legacy_model

    def _require_legacy_sdk(self) -> _LegacySDK:
        if self._legacy_sdk is None:
            raise RuntimeError("Gemini legacy SDK is not initialized")
        return self._legacy_sdk

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
                    client = self._require_modern_client()
                    uploaded_file = client.files.upload(file=file_path, config={"mime_type": "text/csv"})
                    response = client.models.generate_content(
                        model=self.model_name, contents=[uploaded_file, prompt]
                    )
                else:
                    uploaded_file = self._require_legacy_sdk().upload_file(
                        path=file_path,
                        mime_type="text/csv",
                    )
                    response = self._require_legacy_model().generate_content(
                        [uploaded_file, prompt]
                    )
                
                # The SDK-specific call above is the single request.  Do not
                # replay it through the legacy model (the modern client has no
                # such model and a duplicate request would be costly).
                
            # 💡 텍스트만 있는 경우 (기존과 동일)
            else:
                response = (
                    self._require_modern_client().models.generate_content(
                        model=self.model_name,
                        contents=prompt,
                    )
                    if self._sdk_kind == "modern"
                    else self._require_legacy_model().generate_content(prompt)
                )
                
            return {
                "success": True,
                "output": self._extract_response_text(response),
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
                    client = self._require_modern_client()
                    uploaded_file = client.files.upload(file=csv_path, config={"mime_type": "text/csv"})
                    response = client.models.generate_content(
                        model=self.model_name, contents=[uploaded_file, full_prompt]
                    )
                else:
                    uploaded_file = self._require_legacy_sdk().upload_file(
                        path=csv_path,
                        mime_type="text/csv",
                    )
                    response = self._require_legacy_model().generate_content(
                        [uploaded_file, full_prompt]
                    )
            else:
                response = (
                    self._require_modern_client().models.generate_content(
                        model=self.model_name,
                        contents=full_prompt,
                    )
                    if self._sdk_kind == "modern"
                    else self._require_legacy_model().generate_content(
                        full_prompt
                    )
                )
                
            return {
                "success": True,
                "output": self._extract_response_text(response),
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
