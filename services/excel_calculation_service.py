"""ExcelCalculationService — thin, named wrapper around rag/calculator.py.

Named separately from RAGService because it's a distinct capability in the
AI execution pipeline (deterministic structured-data calculation, not
vector search) and the AI Playground reports on it as its own stage/
component. The actual engine is unchanged — see rag/calculator.py.
"""
from typing import Optional, Dict
from rag.calculator import answer_calculation_question


class ExcelCalculationService:
    def try_answer(self, question: str) -> Optional[Dict]:
        return answer_calculation_question(question)


_instance = ExcelCalculationService()


def get_excel_calculation_service() -> ExcelCalculationService:
    return _instance
