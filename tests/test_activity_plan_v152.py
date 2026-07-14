import pandas as pd

from models.config import RiskConfig
from services.analysis_service import AnalysisService


class DummyCanvas:
    pass


def test_plan_week_can_read_spanish_week_column():
    assignments = [
        {"id": 1, "name": "Foro Semana 1", "published": True, "points_possible": 10, "submission_types": ["discussion_topic"]},
        {"id": 2, "name": "Quiz Semana 1", "published": True, "points_possible": 10, "submission_types": ["online_quiz"]},
        {"id": 3, "name": "Actividad Semana 2", "published": True, "points_possible": 10, "submission_types": ["online_upload"]},
    ]
    plan = pd.DataFrame([
        {"canvas_assignment_id": "1", "activity_name": "Foro Semana 1", "Semana": "Semana 1", "include_in_risk": True},
        {"canvas_assignment_id": "2", "activity_name": "Quiz Semana 1", "Semana": "Semana 1", "include_in_risk": True},
        {"canvas_assignment_id": "3", "activity_name": "Actividad Semana 2", "Semana": "Semana 2", "include_in_risk": True},
    ])
    resolved = AnalysisService(DummyCanvas(), RiskConfig())._resolve_activity_plan(assignments, week=1, activity_plan=plan)
    assert resolved["uses_saved_plan"] is True
    assert len(resolved["expected_assignments"]) == 2
    assert resolved["weekly_distribution"][:2] == [2, 1]


def test_activity_plan_name_match_ignores_accents_and_punctuation():
    assignments = [
        {"id": 10, "name": "Evaluacion modulo 1", "published": True, "points_possible": 10, "submission_types": ["online_quiz"]},
    ]
    plan = pd.DataFrame([
        {"canvas_assignment_id": "old", "activity_name": "Evaluación módulo 1", "week_number": 1, "include_in_risk": True},
    ])
    resolved = AnalysisService(DummyCanvas(), RiskConfig())._resolve_activity_plan(assignments, week=1, activity_plan=plan)
    assert resolved["uses_saved_plan"] is True
    assert len(resolved["expected_assignments"]) == 1
