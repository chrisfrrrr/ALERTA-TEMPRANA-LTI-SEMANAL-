from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from components.ui import page_header
from models.config import RiskConfig
from services.analysis_service import AnalysisService
from services.auth_service import current_actor, get_valid_canvas_token, load_oauth_config
from services.canvas_service import CanvasAPIError, CanvasService
from services.database_service import DatabaseError
from services.demo_service import demo_courses
from services.risk_engine import expected_activities
from services.runtime import get_database


page_header(
    "Plan semanal del curso",
    "Asigne cada actividad detectada en Canvas a la semana real del curso. Este plan será la fuente principal para calcular el avance esperado.",
)

config = RiskConfig.from_dict(st.session_state.get("risk_config"))
db = get_database()
oauth_config = load_oauth_config()
demo_mode = bool(st.session_state.get("demo_mode", True))
canvas_url = st.session_state.get("canvas_url", oauth_config.canvas_url)
token = st.session_state.get("canvas_token", "")

if not demo_mode:
    try:
        token = get_valid_canvas_token(oauth_config)
        if st.session_state.get("auth_method") != "manual_token":
            canvas_url = oauth_config.canvas_url
    except Exception:
        token = st.session_state.get("canvas_token", "")


def _activity_type(assignment: dict[str, Any]) -> str:
    return AnalysisService._activity_type(assignment)


def _default_week(index: int, total: int, total_weeks: int) -> int:
    if total <= 0:
        return 1
    for week_number in range(1, total_weeks + 1):
        if index <= expected_activities(total, week_number, total_weeks):
            return week_number
    return total_weeks


def _demo_assignments(course_id: str | int) -> list[dict[str, Any]]:
    names = [
        "Foro de presentación",
        "Actividad 1.1",
        "Quiz módulo 1",
        "Lectura aplicada 1",
        "Caso práctico 1",
        "Foro módulo 2",
        "Actividad 2.1",
        "Quiz módulo 2",
        "Actividad 3.1",
        "Evaluación parcial",
        "Caso práctico 2",
        "Foro módulo 4",
        "Actividad 4.1",
        "Proyecto final",
        "Evaluación final",
        "Encuesta de cierre",
    ]
    assignments: list[dict[str, Any]] = []
    for idx, name in enumerate(names, start=1):
        assignments.append(
            {
                "id": int(course_id) * 100 + idx if str(course_id).isdigit() else f"{course_id}-{idx}",
                "name": name,
                "published": True,
                "points_possible": 0 if "Encuesta" in name else 10,
                "submission_types": ["online_quiz"] if "Quiz" in name or "Evaluación" in name else ["discussion_topic"] if "Foro" in name else ["online_upload"],
                "due_at": None,
                "position": idx,
            }
        )
    return assignments


def _load_canvas_assignments(course: dict[str, Any], include_zero_point: bool) -> list[dict[str, Any]]:
    if demo_mode:
        raw = _demo_assignments(course["id"])
    else:
        if not token:
            raise CanvasAPIError("Primero inicie sesión con Canvas o use token manual seguro.")
        raw = CanvasService(canvas_url, token).list_assignments(course["id"])
    return AnalysisService._valid_assignments(raw, include_zero_point=include_zero_point)


def _build_editor_df(assignments: list[dict[str, Any]], plan: pd.DataFrame) -> pd.DataFrame:
    plan_by_id: dict[str, dict[str, Any]] = {}
    if not plan.empty:
        for row in plan.to_dict(orient="records"):
            plan_by_id[str(row.get("canvas_assignment_id") or "")] = row

    total = len(assignments)
    rows: list[dict[str, Any]] = []
    for index, assignment in enumerate(assignments, start=1):
        assignment_id = str(assignment.get("id") or "")
        existing = plan_by_id.get(assignment_id, {})
        if existing:
            include = bool(existing.get("include_in_risk", True)) and existing.get("week_number") is not None
            try:
                week_value = int(existing.get("week_number")) if existing.get("week_number") is not None else None
            except (TypeError, ValueError):
                week_value = None
            week_label = f"Semana {week_value}" if include and week_value else "No incluir"
            is_required = bool(existing.get("is_required", True))
            manual_note = existing.get("manual_note") or ""
        else:
            include = True
            week_label = f"Semana {_default_week(index, total, config.course_weeks)}"
            is_required = True
            manual_note = ""

        rows.append(
            {
                "Incluir": include,
                "Semana": week_label,
                "Actividad": assignment.get("name") or f"Actividad {assignment_id}",
                "Tipo": existing.get("activity_type") or _activity_type(assignment),
                "Puntos": float(assignment.get("points_possible") or 0),
                "Fecha límite Canvas": assignment.get("due_at") or "Sin fecha",
                "Obligatoria": is_required,
                "Observación": manual_note,
                "ID Canvas": assignment_id,
            }
        )
    return pd.DataFrame(rows)


def _editor_to_records(editor_df: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in editor_df.to_dict(orient="records"):
        week_label = str(row.get("Semana") or "No incluir")
        include = bool(row.get("Incluir")) and week_label != "No incluir"
        week_number = None
        if include:
            try:
                week_number = int(week_label.replace("Semana", "").strip())
            except ValueError:
                week_number = None
                include = False
        due_at = row.get("Fecha límite Canvas")
        if due_at in ("Sin fecha", "", None):
            due_at = None
        records.append(
            {
                "canvas_assignment_id": str(row.get("ID Canvas") or ""),
                "activity_name": str(row.get("Actividad") or "Actividad sin nombre"),
                "activity_type": str(row.get("Tipo") or "Actividad"),
                "due_at": due_at,
                "week_number": week_number,
                "include_in_risk": include,
                "is_required": bool(row.get("Obligatoria", True)),
                "points_possible": row.get("Puntos"),
                "manual_note": str(row.get("Observación") or "").strip() or None,
            }
        )
    return records


def _summary(records: list[dict[str, Any]]) -> pd.DataFrame:
    data = []
    for week_number in range(1, config.course_weeks + 1):
        week_records = [record for record in records if record.get("include_in_risk") and record.get("week_number") == week_number]
        data.append(
            {
                "Semana": f"Semana {week_number}",
                "Actividades": len(week_records),
                "Puntos": round(sum(float(record.get("points_possible") or 0) for record in week_records), 2),
                "Ejemplos": ", ".join(record["activity_name"] for record in week_records[:3]) + ("..." if len(week_records) > 3 else ""),
            }
        )
    data.append(
        {
            "Semana": "No incluir",
            "Actividades": sum(1 for record in records if not record.get("include_in_risk")),
            "Puntos": round(sum(float(record.get("points_possible") or 0) for record in records if not record.get("include_in_risk")), 2),
            "Ejemplos": "",
        }
    )
    return pd.DataFrame(data)


courses = st.session_state.get("courses") or (demo_courses() if demo_mode else [])
if not courses:
    st.info("Primero cargue los cursos desde la pestaña Conexión y análisis.")
    st.stop()

if not db.connected:
    st.warning(
        "Supabase no está conectado. Puede revisar el plan en pantalla, pero para guardarlo y usarlo en futuros análisis necesita conectar la base histórica."
    )

course_options = {f"{course.get('name') or course.get('course_code')}  ·  ID {course.get('id')}": course for course in courses}
col1, col2 = st.columns([2.2, 1])
with col1:
    selected_label = st.selectbox("Curso", list(course_options.keys()))
    selected_course = course_options[selected_label]
with col2:
    include_zero_point = st.toggle(
        "Incluir actividades de 0 puntos",
        value=False,
        help="Active esta opción si una actividad obligatoria no tiene ponderación en Canvas.",
    )

course_id = selected_course["id"]
course_name = selected_course.get("name") or selected_course.get("course_code") or f"Curso {course_id}"
cache_key = f"activity_plan_assignments_{course_id}_{include_zero_point}_{'demo' if demo_mode else 'real'}"

try:
    if cache_key not in st.session_state:
        with st.spinner("Cargando actividades del curso..."):
            st.session_state[cache_key] = _load_canvas_assignments(selected_course, include_zero_point)
    assignments = st.session_state[cache_key]
except CanvasAPIError as exc:
    st.error(str(exc))
    st.stop()

if not assignments:
    st.info("No se encontraron actividades válidas para configurar. Revise si están publicadas o active actividades de 0 puntos si aplica.")
    st.stop()

existing_plan = db.get_course_activity_plan(course_id) if db.connected else pd.DataFrame()
if existing_plan.empty:
    st.info(
        "Este curso aún no tiene plan guardado. La tabla se propone con una distribución inicial uniforme para que pueda ajustarla manualmente."
    )
else:
    st.success(f"Plan semanal existente cargado: {len(existing_plan)} actividades configuradas.")

editor_df = _build_editor_df(assignments, existing_plan)
week_options = ["No incluir"] + [f"Semana {week_number}" for week_number in range(1, config.course_weeks + 1)]

st.markdown("#### Asignación de actividades")
st.caption(
    "Cambie la semana de cada actividad según el plan real del curso. Las actividades marcadas como 'No incluir' no afectarán el riesgo."
)

edited = st.data_editor(
    editor_df,
    use_container_width=True,
    hide_index=True,
    num_rows="fixed",
    disabled=["Actividad", "Tipo", "Puntos", "Fecha límite Canvas", "ID Canvas"],
    column_config={
        "Incluir": st.column_config.CheckboxColumn("Incluir", help="Cuenta para el cálculo de avance y riesgo."),
        "Semana": st.column_config.SelectboxColumn("Semana", options=week_options, required=True),
        "Obligatoria": st.column_config.CheckboxColumn("Obligatoria"),
        "Observación": st.column_config.TextColumn("Observación", width="medium"),
        "ID Canvas": st.column_config.TextColumn("ID Canvas", width="small"),
    },
    key=f"activity_plan_editor_{course_id}",
)

records = _editor_to_records(edited)
summary_df = _summary(records)

st.markdown("#### Resumen del plan")
st.dataframe(summary_df, use_container_width=True, hide_index=True)

c1, c2, c3 = st.columns(3)
c1.metric("Actividades detectadas", len(assignments))
c2.metric("Incluidas en riesgo", sum(1 for record in records if record.get("include_in_risk")))
c3.metric("Sin incluir", sum(1 for record in records if not record.get("include_in_risk")))

st.divider()
if st.button("Guardar plan semanal del curso", type="primary", use_container_width=True, disabled=not db.connected):
    try:
        saved = db.save_course_activity_plan(
            course_id=course_id,
            course_name=course_name,
            records=records,
            actor=current_actor(),
        )
        db.log_audit(
            action="course_activity_plan_saved",
            entity_type="course_activity_plan",
            entity_id=str(course_id),
            actor=current_actor(),
            payload={
                "course_id": str(course_id),
                "course_name": course_name,
                "records_saved": saved,
                "included_in_risk": sum(1 for record in records if record.get("include_in_risk")),
                "weekly_distribution": summary_df.to_dict(orient="records"),
            },
        )
        st.success(f"Plan semanal guardado correctamente para {saved} actividades.")
    except DatabaseError as exc:
        st.error(str(exc))

with st.expander("Cómo se usará este plan en el análisis"):
    st.markdown(
        """
        Cuando ejecute el análisis semanal, la aplicación revisará primero si existe un plan guardado para este curso.
        Si existe, la meta acumulada se calculará sumando las actividades incluidas desde la semana 1 hasta la semana analizada.
        Si no existe plan, la aplicación usará la distribución uniforme como respaldo temporal.
        """
    )
