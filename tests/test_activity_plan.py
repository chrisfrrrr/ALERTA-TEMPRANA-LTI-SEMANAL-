import pandas as pd

from models.config import RiskConfig
from services.analysis_service import AnalysisService


class DummyCanvas:
    pass


def test_saved_activity_plan_sets_expected_by_week():
    assignments = [
        {"id": 1, "name": "A1", "published": True, "points_possible": 10, "submission_types": ["online_upload"]},
        {"id": 2, "name": "A2", "published": True, "points_possible": 10, "submission_types": ["online_upload"]},
        {"id": 3, "name": "A3", "published": True, "points_possible": 10, "submission_types": ["online_upload"]},
        {"id": 4, "name": "A4", "published": True, "points_possible": 10, "submission_types": ["online_upload"]},
    ]
    plan = pd.DataFrame([
        {"canvas_assignment_id": "1", "activity_name": "A1", "week_number": 1, "include_in_risk": True},
        {"canvas_assignment_id": "2", "activity_name": "A2", "week_number": 1, "include_in_risk": True},
        {"canvas_assignment_id": "3", "activity_name": "A3", "week_number": 1, "include_in_risk": True},
        {"canvas_assignment_id": "4", "activity_name": "A4", "week_number": 1, "include_in_risk": True},
    ])
    service = AnalysisService(DummyCanvas(), RiskConfig())
    resolved = service._resolve_activity_plan(assignments, week=1, activity_plan=plan)
    assert resolved["uses_saved_plan"] is True
    assert len(resolved["included_assignments"]) == 4
    assert len(resolved["expected_assignments"]) == 4
    assert resolved["weekly_distribution"][0] == 4


def test_activity_plan_can_match_by_name_when_id_changes():
    assignments = [
        {"id": 101, "name": "Foro 1", "published": True, "points_possible": 10, "submission_types": ["discussion_topic"]},
        {"id": 102, "name": "Quiz 1", "published": True, "points_possible": 10, "submission_types": ["online_quiz"]},
    ]
    plan = pd.DataFrame([
        {"canvas_assignment_id": "old-1", "activity_name": "Foro 1", "week_number": 1, "include_in_risk": True},
        {"canvas_assignment_id": "old-2", "activity_name": "Quiz 1", "week_number": 1, "include_in_risk": True},
    ])
    service = AnalysisService(DummyCanvas(), RiskConfig())
    resolved = service._resolve_activity_plan(assignments, week=1, activity_plan=plan)
    assert resolved["uses_saved_plan"] is True
    assert len(resolved["expected_assignments"]) == 2
