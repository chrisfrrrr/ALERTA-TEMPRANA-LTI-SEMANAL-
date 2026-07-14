from __future__ import annotations

from collections import defaultdict
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from models.config import RiskConfig
from services.canvas_service import CanvasAPIError, CanvasService
from services.risk_engine import (
    RISK_ORDER,
    build_reasons,
    evaluate_access,
    evaluate_activity,
    evaluate_communication,
    evaluate_grade,
    evaluate_punctuality,
    expected_activities,
    intervention_priority,
    overall_risk,
    weekly_distribution,
)
from utils.dates import hours_between, parse_datetime
from utils.ids import extract_carne


class AnalysisService:
    def __init__(self, canvas: CanvasService, config: RiskConfig | None = None) -> None:
        self.canvas = canvas
        self.config = config or RiskConfig()

    @staticmethod
    def _valid_assignments(
        assignments: list[dict[str, Any]],
        *,
        include_zero_point: bool,
    ) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        for assignment in assignments:
            if assignment.get("published") is False:
                continue
            submission_types = assignment.get("submission_types") or []
            if submission_types in (["none"], ["not_graded"]) or "not_graded" in submission_types:
                continue
            if not include_zero_point and float(assignment.get("points_possible") or 0) <= 0:
                continue
            valid.append(assignment)

        def sort_key(item: dict[str, Any]) -> tuple[Any, int, str]:
            due = parse_datetime(item.get("due_at"))
            due_value = due.timestamp() if due else float("inf")
            return (due_value, int(item.get("position") or 999999), str(item.get("name") or ""))

        return sorted(valid, key=sort_key)


    @staticmethod
    def _activity_type(assignment: dict[str, Any]) -> str:
        submission_types = assignment.get("submission_types") or []
        if isinstance(submission_types, list):
            values = {str(item).lower() for item in submission_types}
        else:
            values = {str(submission_types).lower()}
        if "discussion_topic" in values:
            return "Foro"
        if "online_quiz" in values or assignment.get("quiz_id"):
            return "Quiz"
        if "external_tool" in values:
            return "Herramienta externa"
        if "online_upload" in values or "online_text_entry" in values:
            return "Tarea"
        if "none" in values or "not_graded" in values:
            return "No evaluable"
        return "Actividad"

    @staticmethod
    def _assignment_id(assignment: dict[str, Any]) -> str:
        return str(assignment.get("id") or "")

    @staticmethod
    def _normalize_activity_name(value: Any) -> str:
        """Normaliza nombres para poder empatar el plan aunque Canvas cambie un ID."""
        text = str(value or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _plan_bool(value: Any, default: bool = True) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "sí", "si", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n", "none", "nan"}:
            return False
        return bool(value)

    @staticmethod
    def _plan_week(value: Any, total_weeks: int) -> int | None:
        if value in (None, "", "No incluir"):
            return None
        text = str(value).strip()
        if not text or text.lower() in {"none", "nan", "no incluir"}:
            return None
        text = text.replace("Semana", "").replace("semana", "").strip()
        try:
            week_number = int(float(text))
        except (TypeError, ValueError):
            return None
        return week_number if 1 <= week_number <= total_weeks else None

    @staticmethod
    def _plan_records(activity_plan: pd.DataFrame | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if activity_plan is None:
            return []
        if isinstance(activity_plan, pd.DataFrame):
            if activity_plan.empty:
                return []
            return activity_plan.to_dict(orient="records")
        return [row for row in activity_plan if isinstance(row, dict)]

    def _resolve_activity_plan(
        self,
        assignments: list[dict[str, Any]],
        *,
        week: int,
        activity_plan: pd.DataFrame | list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Define qué actividades cuentan y cuáles se esperan para la semana.

        Si existe un plan semanal guardado, se usa como fuente principal. Si no existe,
        se mantiene la distribución uniforme anterior para no bloquear el análisis.
        """
        safe_week = min(max(1, int(week)), int(self.config.course_weeks))
        assignment_by_id = {self._assignment_id(item): item for item in assignments if self._assignment_id(item)}
        assignment_id_by_name = {
            self._normalize_activity_name(item.get("name")): self._assignment_id(item)
            for item in assignments
            if self._assignment_id(item)
        }
        plan_rows = self._plan_records(activity_plan)
        plan_by_assignment: dict[str, dict[str, Any]] = {}
        for row in plan_rows:
            raw_assignment_id = str(row.get("canvas_assignment_id") or row.get("assignment_id") or row.get("id") or "").strip()
            activity_name = row.get("activity_name") or row.get("Actividad") or row.get("name") or ""
            assignment_id = raw_assignment_id if raw_assignment_id in assignment_by_id else ""
            if not assignment_id:
                assignment_id = assignment_id_by_name.get(self._normalize_activity_name(activity_name), "")
            if not assignment_id:
                continue

            week_number = self._plan_week(row.get("week_number"), self.config.course_weeks)
            include = self._plan_bool(row.get("include_in_risk", True), True) and week_number is not None
            plan_by_assignment[assignment_id] = {
                "week_number": week_number,
                "include_in_risk": include,
                "is_required": self._plan_bool(row.get("is_required", True), True),
                "manual_note": row.get("manual_note"),
            }

        planned_included = [
            assignment
            for assignment in assignments
            if plan_by_assignment.get(self._assignment_id(assignment), {}).get("include_in_risk")
        ]
        if planned_included:
            expected_set = [
                assignment
                for assignment in planned_included
                if int(plan_by_assignment[self._assignment_id(assignment)]["week_number"] or 99) <= safe_week
            ]
            distribution = []
            for week_number in range(1, self.config.course_weeks + 1):
                distribution.append(
                    sum(
                        1
                        for assignment in planned_included
                        if int(plan_by_assignment[self._assignment_id(assignment)]["week_number"] or 99) == week_number
                    )
                )
            return {
                "method": "Plan semanal guardado",
                "uses_saved_plan": True,
                "included_assignments": planned_included,
                "expected_assignments": expected_set,
                "weekly_distribution": distribution,
                "assignment_plan": plan_by_assignment,
                "planned_rows_matched": len(plan_by_assignment),
                "planned_rows_available": len(plan_rows),
            }

        total_activities = len(assignments)
        expected_count = expected_activities(total_activities, safe_week, self.config.course_weeks)
        expected_set = assignments[:expected_count]
        distribution = weekly_distribution(total_activities, self.config.course_weeks)
        fallback_plan: dict[str, dict[str, Any]] = {}
        for index, assignment in enumerate(assignments, start=1):
            assigned_week = next(
                (
                    week_number
                    for week_number in range(1, self.config.course_weeks + 1)
                    if index <= expected_activities(total_activities, week_number, self.config.course_weeks)
                ),
                self.config.course_weeks,
            )
            aid = self._assignment_id(assignment)
            fallback_plan[aid] = {
                "week_number": assigned_week,
                "include_in_risk": True,
                "is_required": True,
                "manual_note": None,
            }
        return {
            "method": "Distribución uniforme de respaldo",
            "uses_saved_plan": False,
            "included_assignments": assignments,
            "expected_assignments": expected_set,
            "weekly_distribution": distribution,
            "assignment_plan": fallback_plan,
            "planned_rows_matched": 0,
            "planned_rows_available": len(plan_rows),
        }

    @staticmethod
    def _flatten_submissions(payload: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
        """Devuelve user_id -> assignment_id -> submission para respuestas planas o agrupadas."""
        result: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for item in payload:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("submissions"), list):
                user_id = str(item.get("user_id") or item.get("id") or "")
                for submission in item["submissions"]:
                    sid = str(submission.get("user_id") or user_id)
                    aid = str(submission.get("assignment_id") or "")
                    if sid and aid:
                        result[sid][aid] = submission
            else:
                user_id = str(item.get("user_id") or "")
                assignment_id = str(item.get("assignment_id") or "")
                if user_id and assignment_id:
                    result[user_id][assignment_id] = item
        return dict(result)

    @staticmethod
    def _is_completed(submission: dict[str, Any] | None) -> bool:
        if not submission:
            return False
        if submission.get("excused") is True:
            return True
        state = str(submission.get("workflow_state") or "").lower()
        return bool(submission.get("submitted_at")) or state in {"submitted", "graded", "pending_review"}

    @staticmethod
    def _grade_from_enrollment(enrollment: dict[str, Any]) -> float | None:
        grades = enrollment.get("grades") or {}
        for key in ("current_score", "final_score", "unposted_current_score"):
            value = grades.get(key)
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _grade_from_submissions(
        expected_assignments: list[dict[str, Any]],
        submissions: dict[str, dict[str, Any]],
    ) -> float | None:
        earned = 0.0
        possible = 0.0
        for assignment in expected_assignments:
            assignment_id = str(assignment.get("id"))
            submission = submissions.get(assignment_id) or {}
            score = submission.get("score")
            points = assignment.get("points_possible")
            try:
                if score is not None and points is not None and float(points) > 0:
                    earned += float(score)
                    possible += float(points)
            except (TypeError, ValueError):
                continue
        if possible <= 0:
            return None
        return max(0.0, min(100.0, earned / possible * 100.0))

    @staticmethod
    def _course_window(
        course: dict[str, Any],
        week: int,
        analysis_date: date,
    ) -> tuple[datetime, datetime]:
        end = datetime.combine(analysis_date, time.max).replace(tzinfo=timezone.utc)
        course_start = parse_datetime(course.get("start_at"))
        if course_start:
            start = course_start + timedelta(days=7 * (week - 1))
            scheduled_end = start + timedelta(days=7)
            end = min(end, scheduled_end)
        else:
            start = end - timedelta(days=7)
        return start, end

    @staticmethod
    def _message_map(messages: pd.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
        if messages.empty:
            return {}
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in messages.to_dict(orient="records"):
            key = (str(row.get("canvas_user_id") or ""), str(row.get("course_id") or ""))
            if key not in result:
                result[key] = row
        return result

    @staticmethod
    def _previous_map(history: pd.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
        if history.empty:
            return {}
        history = history.copy()
        if "created_at" in history.columns:
            history = history.sort_values("created_at", ascending=False)
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in history.to_dict(orient="records"):
            key = (str(row.get("carne") or ""), str(row.get("course_id") or ""))
            if key not in result:
                result[key] = row
        return result

    @staticmethod
    def _evolution(current_risk: str, previous: dict[str, Any] | None) -> tuple[str, float | None]:
        if not previous:
            return "Primera medición", None
        previous_risk = str(previous.get("overall_risk") or "Sin datos")
        current_order = RISK_ORDER.get(current_risk, -1)
        previous_order = RISK_ORDER.get(previous_risk, -1)
        if current_order < previous_order:
            status = "Mejorando"
        elif current_order > previous_order:
            status = "Empeorando"
        else:
            status = "Sin cambio"
        try:
            current_grade = float(previous.get("_current_grade"))
        except (TypeError, ValueError):
            current_grade = None
        return status, current_grade

    def analyze_course(
        self,
        *,
        course: dict[str, Any],
        section_id: int | str | None,
        section_name: str,
        week: int,
        analysis_date: date,
        include_page_views: bool,
        include_zero_point: bool,
        latest_messages: pd.DataFrame | None = None,
        previous_history: pd.DataFrame | None = None,
        activity_plan: pd.DataFrame | list[dict[str, Any]] | None = None,
        progress_callback: Callable[[str, float], None] | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
        course_id = course["id"]
        course_name = course.get("name") or course.get("course_code") or f"Curso {course_id}"
        if progress_callback:
            progress_callback("Consultando inscripciones", 0.08)
        enrollments = self.canvas.list_enrollments(course_id, section_id)

        # Canvas suele devolver el SIS ID en el nivel superior del enrollment,
        # no necesariamente dentro del objeto user. Si faltan identificadores,
        # se consulta una sola vez el directorio del curso como respaldo.
        def enrollment_has_identity(item: dict[str, Any]) -> bool:
            user = item.get("user") or {}
            values = (
                item.get("sis_user_id"),
                item.get("login_id"),
                item.get("email"),
                user.get("sis_user_id"),
                user.get("login_id"),
                user.get("email"),
            )
            return any(extract_carne(value) for value in values)

        identity_coverage = sum(1 for enrollment in enrollments if enrollment_has_identity(enrollment))
        user_directory: dict[str, dict[str, Any]] = {}
        identity_lookup_error: str | None = None
        if enrollments and identity_coverage < len(enrollments):
            try:
                if progress_callback:
                    progress_callback("Vinculando carnés institucionales", 0.14)
                course_students = self.canvas.list_course_students(course_id)
                user_directory = {
                    str(user.get("id")): user
                    for user in course_students
                    if isinstance(user, dict) and user.get("id") is not None
                }
            except CanvasAPIError as exc:
                # El análisis puede continuar y luego intentar la coincidencia por nombre.
                identity_lookup_error = str(exc)

        if progress_callback:
            progress_callback("Consultando actividades", 0.20)
        assignments_raw = self.canvas.list_assignments(course_id)
        assignments = self._valid_assignments(assignments_raw, include_zero_point=include_zero_point)

        user_ids = [
            str(enrollment.get("user_id") or (enrollment.get("user") or {}).get("id") or "")
            for enrollment in enrollments
        ]
        user_ids = [value for value in user_ids if value and value != "None"]
        assignment_ids = [str(assignment.get("id")) for assignment in assignments if assignment.get("id")]

        if progress_callback:
            progress_callback("Preparando consulta de entregas", 0.30)

        def submission_progress(done: int, total: int) -> None:
            if progress_callback:
                progress_callback(
                    f"Consultando entregas ({done}/{max(total, 1)} lotes)",
                    0.30 + 0.30 * done / max(total, 1),
                )

        submissions_raw = self.canvas.list_submissions(
            course_id,
            section_id,
            student_ids=user_ids,
            assignment_ids=assignment_ids,
            progress_callback=submission_progress,
        )
        submissions_map = self._flatten_submissions(submissions_raw)

        plan_resolution = self._resolve_activity_plan(assignments, week=week, activity_plan=activity_plan)
        included_assignments = plan_resolution["included_assignments"]
        expected_set = plan_resolution["expected_assignments"]
        assignment_plan = plan_resolution["assignment_plan"]
        total_activities = len(included_assignments)
        expected_count = len(expected_set)
        week_distribution = plan_resolution["weekly_distribution"]
        included_assignment_ids = {self._assignment_id(assignment) for assignment in included_assignments}
        expected_assignment_ids = {self._assignment_id(assignment) for assignment in expected_set}

        start_time, end_time = self._course_window(course, week, analysis_date)
        sessions_map: dict[str, int | None] = {}
        page_view_errors: dict[str, str] = {}

        if include_page_views and user_ids:
            if progress_callback:
                progress_callback("Estimando ingresos a Canvas", 0.62)

            def page_progress(done: int, total: int) -> None:
                if progress_callback:
                    progress_callback("Estimando ingresos a Canvas", 0.62 + 0.20 * done / max(total, 1))

            sessions_map, page_view_errors = self.canvas.fetch_page_view_sessions(
                user_ids,
                start_time,
                end_time,
                course_id,
                progress_callback=page_progress,
            )

        messages_map = self._message_map(latest_messages if latest_messages is not None else pd.DataFrame())
        previous_map = self._previous_map(previous_history if previous_history is not None else pd.DataFrame())

        rows: list[dict[str, Any]] = []
        details: dict[str, Any] = {}
        cutoff_dt = datetime.combine(analysis_date, time.max).replace(tzinfo=timezone.utc)

        if progress_callback:
            progress_callback("Calculando indicadores", 0.84)

        for enrollment in enrollments:
            enrollment_user = enrollment.get("user") or {}
            canvas_user_id = str(enrollment.get("user_id") or enrollment_user.get("id") or "")
            if not canvas_user_id:
                continue

            directory_user = user_directory.get(canvas_user_id, {})
            user = dict(directory_user)
            user.update(
                {key: value for key, value in enrollment_user.items() if value not in (None, "")}
            )

            student_submissions = submissions_map.get(canvas_user_id, {})
            name = user.get("name") or user.get("sortable_name") or f"Estudiante {canvas_user_id}"
            email = (
                user.get("email")
                or enrollment.get("email")
                or user.get("login_id")
                or enrollment.get("login_id")
                or ""
            )
            sis_user_id = enrollment.get("sis_user_id") or user.get("sis_user_id") or ""
            login_id = enrollment.get("login_id") or user.get("login_id") or ""
            carne = (
                extract_carne(sis_user_id)
                or extract_carne(login_id)
                or extract_carne(email)
                or str(sis_user_id or "")
                or f"canvas-{canvas_user_id}"
            )

            completed_assignments = [
                assignment
                for assignment in included_assignments
                if self._is_completed(student_submissions.get(str(assignment.get("id"))))
            ]
            completed_expected_items = [
                assignment
                for assignment in expected_set
                if self._is_completed(student_submissions.get(str(assignment.get("id"))))
            ]
            pending_items = [
                assignment
                for assignment in expected_set
                if not self._is_completed(student_submissions.get(str(assignment.get("id"))))
            ]

            late_count = 0
            early_count = 0
            assignment_rows: list[dict[str, Any]] = []
            for index, assignment in enumerate(assignments, start=1):
                aid = str(assignment.get("id"))
                submission = student_submissions.get(aid) or {}
                completed = self._is_completed(submission)
                due_at = parse_datetime(assignment.get("due_at"))
                submitted_at = parse_datetime(submission.get("submitted_at"))
                late = bool(submission.get("late"))
                if completed and aid in expected_assignment_ids and late:
                    late_count += 1
                if completed and due_at and submitted_at and submitted_at <= due_at - timedelta(hours=24):
                    early_count += 1
                plan_meta = assignment_plan.get(aid, {})
                assigned_week = plan_meta.get("week_number")
                include_in_risk = bool(plan_meta.get("include_in_risk", aid in included_assignment_ids))
                assignment_rows.append(
                    {
                        "id": aid,
                        "actividad": assignment.get("name") or f"Actividad {aid}",
                        "semana_asignada": assigned_week,
                        "incluida_en_riesgo": include_in_risk,
                        "obligatoria": bool(plan_meta.get("is_required", True)),
                        "esperada_a_la_fecha": aid in expected_assignment_ids,
                        "completada": completed,
                        "tardia": late,
                        "fecha_limite": assignment.get("due_at"),
                        "fecha_entrega": submission.get("submitted_at"),
                        "puntaje": submission.get("score"),
                        "puntos_posibles": assignment.get("points_possible"),
                        "observacion_plan": plan_meta.get("manual_note"),
                    }
                )

            average = self._grade_from_enrollment(enrollment)
            if average is None:
                average = self._grade_from_submissions(expected_set, student_submissions)

            last_activity = enrollment.get("last_activity_at")
            inactivity_hours = hours_between(last_activity, cutoff_dt)
            weekly_sessions = sessions_map.get(canvas_user_id) if include_page_views else None

            latest_message = messages_map.get((canvas_user_id, str(course_id)))
            response_hours: float | None = None
            pending_hours: float | None = None
            has_message = latest_message is not None
            if latest_message:
                try:
                    response_hours = float(latest_message.get("response_hours")) if latest_message.get("response_hours") is not None else None
                except (TypeError, ValueError):
                    response_hours = None
                if response_hours is None:
                    pending_hours = hours_between(latest_message.get("sent_at"), cutoff_dt)

            previous = previous_map.get((str(carne), str(course_id)))
            trend_delta = None
            if previous and previous.get("average_grade") is not None and average is not None:
                try:
                    trend_delta = average - float(previous["average_grade"])
                except (TypeError, ValueError):
                    trend_delta = None

            # Para el cumplimiento semanal se evalúan únicamente las actividades esperadas
            # hasta la semana analizada. Las actividades futuras completadas no deben inflar
            # ni deformar el indicador (evita casos como 4/1 cuando no se cargó el plan).
            completed_for_progress = len(completed_expected_items)
            completed_total_in_plan = len(completed_assignments)
            activity_indicator = evaluate_activity(completed_for_progress, expected_count, self.config)
            grade_indicator = evaluate_grade(average, self.config, trend_delta)
            punctuality_indicator = evaluate_punctuality(
                late_count,
                len(completed_expected_items),
                0,
                self.config,
            )
            access_indicator = evaluate_access(weekly_sessions, inactivity_hours, self.config)
            communication_indicator = evaluate_communication(
                response_hours,
                pending_hours,
                has_message,
                self.config,
            )
            indicators = [
                activity_indicator,
                grade_indicator,
                punctuality_indicator,
                access_indicator,
                communication_indicator,
            ]
            overall = overall_risk(indicators)
            priority = intervention_priority(indicators, overall)
            pending_names = [str(item.get("name") or "Actividad sin nombre") for item in pending_items]
            reasons = build_reasons(indicators, pending_names)

            if previous:
                previous_risk = str(previous.get("overall_risk") or "Sin datos")
                if RISK_ORDER.get(overall, -1) < RISK_ORDER.get(previous_risk, -1):
                    evolution = "Mejorando"
                elif RISK_ORDER.get(overall, -1) > RISK_ORDER.get(previous_risk, -1):
                    evolution = "Empeorando"
                else:
                    evolution = "Sin cambio"
            else:
                evolution = "Primera medición"

            completion_percentage = (
                min(100.0, completed_for_progress / expected_count * 100.0) if expected_count else 0.0
            )
            row = {
                "canvas_user_id": canvas_user_id,
                "carne": str(carne),
                "student_name": name,
                "email": email,
                "canvas_sis_user_id": str(sis_user_id or ""),
                "canvas_login_id": str(login_id or ""),
                "career": "",
                "avatar_url": user.get("avatar_url"),
                "course_id": str(course_id),
                "course_name": course_name,
                "section_id": str(section_id or enrollment.get("course_section_id") or ""),
                "section_name": section_name,
                "week_number": week,
                "total_weeks": self.config.course_weeks,
                "total_activities": total_activities,
                "expected_activities": expected_count,
                "completed_activities": completed_for_progress,
                "completed_expected": len(completed_expected_items),
                "completed_total_in_plan": completed_total_in_plan,
                "pending_count": len(pending_items),
                "late_count": late_count,
                "early_count": early_count,
                "completion_percentage": round(completion_percentage, 2),
                "average_grade": round(average, 2) if average is not None else None,
                "weekly_sessions": weekly_sessions,
                "inactivity_hours": round(inactivity_hours, 1) if inactivity_hours is not None else None,
                "last_activity_at": last_activity,
                "activity_risk": activity_indicator.risk,
                "grade_risk": grade_indicator.risk,
                "punctuality_risk": punctuality_indicator.risk,
                "access_risk": access_indicator.risk,
                "communication_risk": communication_indicator.risk,
                "overall_risk": overall,
                "intervention_priority": priority,
                "pending_assignments": pending_names,
                "reasons": reasons,
                "evolution": evolution,
                "analysis_cutoff": cutoff_dt.isoformat(),
                "advisor_name": "Sin asignar",
                "canvas_page_views_available": weekly_sessions is not None,
            }
            rows.append(row)
            details[canvas_user_id] = {
                "student": row,
                "indicators": [indicator.as_dict() for indicator in indicators],
                "assignments": assignment_rows,
                "latest_message": latest_message,
            }

        dataframe = pd.DataFrame(rows)
        if not dataframe.empty:
            dataframe = dataframe.sort_values(
                ["overall_risk", "completion_percentage", "student_name"],
                ascending=[False, True, True],
            ).reset_index(drop=True)

        diagnostics = {
            "course_id": str(course_id),
            "course_name": course_name,
            "students": len(dataframe),
            "assignments_raw": len(assignments_raw),
            "assignments_analyzed": total_activities,
            "expected_activities": expected_count,
            "weekly_distribution": week_distribution,
            "advance_method": plan_resolution["method"],
            "uses_saved_activity_plan": bool(plan_resolution["uses_saved_plan"]),
            "activity_plan_rows_available": plan_resolution.get("planned_rows_available", 0),
            "activity_plan_rows_matched": plan_resolution.get("planned_rows_matched", 0),
            "analysis_week": week,
            "window_start": start_time.isoformat(),
            "window_end": end_time.isoformat(),
            "page_view_errors": page_view_errors,
            "identity_coverage_from_enrollments": identity_coverage,
            "identity_directory_records": len(user_directory),
            "identity_lookup_error": identity_lookup_error,
        }
        if progress_callback:
            progress_callback("Análisis completado", 1.0)
        return dataframe, details, diagnostics
