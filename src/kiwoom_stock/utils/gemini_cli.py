import subprocess
import json
import os
from pathlib import Path
from typing import Dict, List
import datetime
from datetime import datetime


class GeminiCLI:
    """Gemini CLI를 사용하여 .md 파일의 프롬프트를 처리하는 클래스"""
    
    def __init__(self, model: str = "gemini-2.5-pro"):
        """
        Gemini CLI 초기화
        
        Args:
            model: 사용할 Gemini 모델명
        """
        self.model = model
    
    def _call_gemini_cli(self, prompt: str) -> Dict:
        """
        Gemini CLI를 호출하여 응답을 받는 내부 메서드
        
        Args:
            prompt: Gemini에게 보낼 프롬프트
            
        Returns:
            Gemini 응답 결과
        """
        try:
            cmd = ["gemini", "-m", self.model]
            
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding='utf-8',
                shell=True
            )
            
            if result.returncode != 0:
                return {
                    "success": False,
                    "output": None,
                    "error": f"Gemini CLI 실행 오류: {result.stderr}"
                }
            
            return {
                "success": True,
                "output": result.stdout.strip(),
                "error": None
            }
            
        except Exception as e:
            return {
                "success": False,
                "output": None,
                "error": f"예상치 못한 오류: {e}"
            }
    
    def _read_md_file(self, md_file_path: str) -> str:
        """
        .md 파일에서 프롬프트를 읽어오는 내부 메서드
        
        Args:
            md_file_path: .md 파일 경로
            
        Returns:
            파일 내용
        """
        try:
            with open(md_file_path, 'r', encoding='utf-8') as file:
                return file.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {md_file_path}")
        except Exception as e:
            raise Exception(f"파일 읽기 오류: {e}")
    
    def _save_result(self, result: Dict, company_name: str, md_file_path: str, output_dir: str) -> str:
        """
        처리 결과를 파일로 저장하는 내부 메서드
        
        Args:
            result: 처리 결과
            md_file_path: 원본 .md 파일 경로
            output_dir: 출력 디렉토리
            
        Returns:
            저장된 파일 경로 (실패시 빈 문자열)
        """
        try:
            Path(output_dir).mkdir(exist_ok=True)
            
            md_filename = Path(md_file_path).stem
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"{company_name}_{md_filename}_result_{timestamp}.txt"
            output_path = Path(output_dir) / output_filename
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(result['result']['output'])
            
            return str(output_path)
            
        except Exception as e:
            print(f"결과 저장 오류: {e}")
            return ""
    
    def process_md_file(self, md_file_path: str, company_name: str, output_dir: str) -> Dict:
        """
        단일 .md 파일을 처리
        
        Args:
            md_file_path: .md 파일 경로
            additional_context: 추가 컨텍스트
            output_dir: 출력 디렉토리
            
        Returns:
            처리 결과
        """

        additional_context = f"{company_name} 주식에 대해 다음 지침에 따라 분석해줘."

        try:
            prompt = self._read_md_file(md_file_path)
            
            if additional_context:
                prompt = f"{additional_context}\n\n{prompt}"
            
            result = self._call_gemini_cli(prompt)
            
            response = {
                "md_file": md_file_path,
                "prompt_length": len(prompt),
                "result": result
            }
            
            if result['success']:
                saved_path = self._save_result(response, company_name, md_file_path, output_dir)
                if saved_path:
                    response['saved_to'] = saved_path
                    print(f"결과 저장: {saved_path}")
            
            return response
            
        except Exception as e:
            return {
                "md_file": md_file_path,
                "prompt_length": 0,
                "result": {
                    "success": False,
                    "output": None,
                    "error": str(e)
                }
            }
    
    def process_md_directory(self, md_directory: str, company_name: str, output_dir: str = "output") -> List[Dict]:
        """
        디렉토리 내의 모든 .md 파일을 일괄 처리
        
        Args:
            md_directory: .md 파일들이 있는 디렉토리 경로
            company_name: 회사명
            output_dir: 출력 디렉토리
            
        Returns:
            처리 결과 리스트
        """
        md_dir = Path(md_directory)
        
        if not md_dir.exists():
            raise FileNotFoundError(f"디렉토리를 찾을 수 없습니다: {md_directory}")
        
        md_files = list(md_dir.glob("*.md"))
        
        if not md_files:
            print(f"디렉토리에 .md 파일이 없습니다: {md_directory}")
            return []
        
        print(f"총 {len(md_files)}개의 .md 파일을 처리합니다...")
        
        results = []
        for md_file in md_files:
            print(f"처리 중: {md_file.name}")
            result = self.process_md_file(str(md_file), company_name, output_dir)
            results.append(result)
        
        return results
    
    def save_results_summary(self, results: List[Dict], output_file: str = None) -> str:
        """
        처리 결과 요약을 JSON 파일로 저장
        
        Args:
            results: 처리 결과 리스트
            output_file: 출력 파일 경로 (None이면 자동 생성)
            
        Returns:
            저장된 파일 경로
        """
        if output_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"gemini_results_{timestamp}.json"
        
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            
            print(f"결과 요약 저장: {output_file}")
            return output_file
            
        except Exception as e:
            print(f"결과 요약 저장 오류: {e}")
            return ""
    
    def check_availability(self) -> bool:
        """
        Gemini CLI 사용 가능 여부 확인
        
        Returns:
            CLI 사용 가능 여부
        """
        try:
            result = subprocess.run(
                ["gemini", "--version"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                shell=True
            )
            
            if result.returncode == 0:
                print(f"✅ Gemini CLI 버전: {result.stdout.strip()}")
                return True
            else:
                print(f"❌ Gemini CLI 확인 실패: {result.stderr}")
                return False
                
        except FileNotFoundError:
            print("❌ Gemini CLI가 설치되지 않았거나 PATH에 없습니다.")
            print("설치 방법: https://ai.google.dev/gemini-api/docs/quickstart")
            return False
        except Exception as e:
            print(f"❌ CLI 확인 중 오류: {e}")
            return False
