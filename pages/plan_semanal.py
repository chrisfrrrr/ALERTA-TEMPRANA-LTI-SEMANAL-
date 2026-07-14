from __future__ import annotations

import hashlib
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
from services.runtime import get_database


page_header(
    "Plan semanal del curso",
    "Asigne manualmente cada actividad detectada en Canvas a la semana real del curso. Este plan será la fuente principal para calcular el avance esperado y las derivaciones.",
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


def _plan_signature(plan: pd.DataFrame) -> str:
    if plan.empty:
        return "sin_plan"
    cols = [col for col in ["canvas_assignment_id", "activity_name", "week_number", "include_in_risk", "is_required"] if col in plan.columns]
    if not cols:
        return "sin_columnas"
    payload = plan[cols].fillna("").astype(str).sort_values(cols).to_csv(index=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:10]


def _build_editor_df(assignments: list[dict[str, Any]], plan: pd.DataFrame) -> pd.DataFrame:
    """Construye la tabla editable.

    Cuando no existe plan guardado, no se asignan semanas automáticamente. Esto evita
    que actividades de semana 1 aparezcan sugeridas en semanas 2, 3 o posteriores.
    La distribución uniforme se deja únicamente como acción opcional del usuario.
    """
    plan_by_id: dict[str, dict[str, Any]] = {}
    plan_by_name: dict[str, dict[str, Any]] = {}
    if not plan.empty:
        for row in plan.to_dict(orient="records"):
            row_id = str(row.get("canvas_assignment_id") or "").strip()
            row_name = AnalysisService._normalize_activity_name(row.get("activity_name") or row.get("Actividad") or row.get("name") or "")
            if row_id:
                plan_by_id[row_id] = row
            if row_name:
                plan_by_name[row_name] = row

    rows: list[dict[str, Any]] = []
    for index, assignment in enumerate(assignments, start=1):
        assignment_id = str(assignment.get("id") or "")
        assignment_name = assignment.get("name") or f"Actividad {assignment_id}"
        existing = plan_by_id.get(assignment_id) or plan_by_name.get(AnalysisService._normalize_activity_name(assignment_name)) or {}

        week_value = AnalysisService._plan_week(existing.get("week_number") or existing.get("Semana"), config.course_weeks) if existing else None
        include = AnalysisService._plan_bool(existing.get("include_in_risk", True), True) if existing else False
        week_label = f"Semana {week_value}" if include and week_value else "No incluir"

        rows.append(
            {
                "Orden": index,
                "Semana": week_label,
                "Actividad": assignment_name,
                "Tipo": existing.get("activity_type") or _activity_type(assignment),
                "Puntos": float(assignment.get("points_possible") or 0),
                "Fecha límite Canvas": assignment.get("due_at") or "Sin fecha",
                "Obligatoria": AnalysisService._plan_bool(existing.get("is_required", True), True) if existing else True,
                "Observación": existing.get("manual_note") or "",
                "ID Canvas": assignment_id,
            }
        )
    return pd.DataFrame(rows)


def _editor_to_records(editor_df: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in editor_df.to_dict(orient="records"):
        week_label = str(row.get("Semana") or "No incluir").strip()
        week_number = AnalysisService._plan_week(week_label, config.course_weeks)
        include = week_number is not None
        due_at = row.get("Fecha límite Canvas")
        if due_at in ("Sin fecha", "", None):
            due_at = None
        records.append(
            {
                "canvas_assignment_id": str(row.get("ID Canvas") or "").strip(),
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


def _apply_uniform_distribution(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    included_indexes = result.index.tolist()
    total = len(included_indexes)
    for position, index in enumerate(included_indexes, start=1):
        # Distribución acumulativa uniforme como propuesta opcional; no se usa por defecto.
        week = min(config.course_weeks, max(1, int(((position - 1) * config.course_weeks) // max(total, 1) + 1)))
        result.loc[index, "Semana"] = f"Semana {week}"
    return result


def _summary(records: list[dict[str, Any]]) -> pd.DataFrame:
    data = []
    cumulative = 0
    for week_number in range(1, config.course_weeks + 1):
        week_records = [record for record in records if record.get("include_in_risk") and record.get("week_number") == week_number]
        cumulative += len(week_records)
        data.append(
            {
                "Semana": f"Semana {week_number}",
                "Actividades de la semana": len(week_records),
                "Meta acumulada": cumulative,
                "Puntos": round(sum(float(record.get("points_possible") or 0) for record in week_records), 2),
                "Ejemplos": ", ".join(record["activity_name"] for record in week_records[:3]) + ("..." if len(week_records) > 3 else ""),
            }
        )
    data.append(
        {
            "Semana": "No incluir",
            "Actividades de la semana": sum(1 for record in records if not record.get("include_in_risk")),
            "Meta acumulada": "—",
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
        "Este curso aún no tiene plan guardado. Las actividades aparecen inicialmente como 'No incluir' para que usted asigne únicamente las que corresponden a cada semana."
    )
else:
    included_existing = int(existing_plan.get("include_in_risk", pd.Series(dtype=bool)).fillna(False).sum())
    st.success(f"Plan semanal existente cargado: {included_existing} actividades incluidas en riesgo.")

base_df = _build_editor_df(assignments, existing_plan)
week_options = ["No incluir"] + [f"Semana {week_number}" for week_number in range(1, config.course_weeks + 1)]

# Evita que Streamlit reutilice una tabla editada de otro curso o de otra carga.
revision_key = f"activity_plan_editor_revision_{course_id}_{include_zero_point}_{'demo' if demo_mode else 'real'}"
st.session_state.setdefault(revision_key, 0)
editor_key = f"activity_plan_editor_{course_id}_{include_zero_point}_{_plan_signature(existing_plan)}_{st.session_state[revision_key]}"

st.markdown("#### Asignación de actividades")
st.caption(
    "Seleccione la semana real de cada actividad. Las filas en 'No incluir' no cuentan para el riesgo, la meta acumulada ni las derivaciones."
)

a1, a2, a3 = st.columns([1.2, 1.25, 1.55])
with a1:
    if st.button("Limpiar asignación", use_container_width=True):
        base_df["Semana"] = "No incluir"
        st.session_state[revision_key] += 1
        st.session_state[f"course_activity_plan_records_{course_id}"] = _editor_to_records(base_df)
        st.rerun()
with a2:
    if st.button("Proponer distribución uniforme", use_container_width=True):
        base_df = _apply_uniform_distribution(base_df)
        st.session_state[revision_key] += 1
        st.session_state[f"course_activity_plan_records_{course_id}"] = _editor_to_records(base_df)
        # Guardamos un dataframe temporal para que el siguiente render lo use como base.
        st.session_state[f"activity_plan_temp_df_{course_id}_{include_zero_point}"] = base_df
        st.rerun()
with a3:
    st.caption("Tip: para su caso actual, asigne manualmente las actividades porque Canvas aún no tiene fechas de entrega.")

temp_key = f"activity_plan_temp_df_{course_id}_{include_zero_point}"
if temp_key in st.session_state:
    base_df = st.session_state.pop(temp_key)

edited = st.data_editor(
    base_df,
    use_container_width=True,
    hide_index=True,
    num_rows="fixed",
    disabled=["Orden", "Actividad", "Tipo", "Puntos", "Fecha límite Canvas", "ID Canvas"],
    column_config={
        "Orden": st.column_config.NumberColumn("#", width="small"),
        "Semana": st.column_config.SelectboxColumn("Semana", options=week_options, required=True, width="medium"),
        "Obligatoria": st.column_config.CheckboxColumn("Obligatoria"),
        "Observación": st.column_config.TextColumn("Observación", width="medium"),
        "ID Canvas": st.column_config.TextColumn("ID Canvas", width="small"),
    },
    key=editor_key,
)

records = _editor_to_records(edited)
# Mantiene el plan editado en la sesión activa. Así el análisis puede usarlo de inmediato.
st.session_state[f"course_activity_plan_records_{course_id}"] = records
st.session_state[f"course_activity_plan_name_{course_id}"] = course_name
summary_df = _summary(records)

st.markdown("#### Resumen del plan")
st.dataframe(summary_df, use_container_width=True, hide_index=True)

included_count = sum(1 for record in records if record.get("include_in_risk"))
week_one_count = sum(1 for record in records if record.get("include_in_risk") and record.get("week_number") == 1)
without_week = sum(1 for record in records if not record.get("include_in_risk"))

c1, c2, c3, c4 = st.columns(4)
c1.metric("Actividades detectadas", len(assignments))
c2.metric("Incluidas en riesgo", included_count)
c3.metric("Semana 1", week_one_count)
c4.metric("Sin incluir", without_week)

if included_count == 0:
    st.warning("Todavía no hay actividades asignadas a semanas. Si ejecuta el análisis así, la app usará distribución uniforme de respaldo.")
else:
    st.success(
        "El próximo análisis usará este plan de la sesión. Para conservarlo en futuras sesiones y en las derivaciones, presione Guardar plan semanal."
    )

st.divider()
if st.button("Guardar plan semanal del curso", type="primary", use_container_width=True, disabled=not db.connected):
    try:
        st.session_state[f"course_activity_plan_records_{course_id}"] = records
        st.session_state[f"course_activity_plan_name_{course_id}"] = course_name
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
                "included_in_risk": included_count,
                "weekly_distribution": summary_df.to_dict(orient="records"),
            },
        )
        st.session_state[revision_key] += 1
        st.success(f"Plan semanal guardado correctamente para {saved} actividades. Semana 1 quedó con {week_one_count} actividad(es) esperadas.")
    except DatabaseError as exc:
        st.error(str(exc))

with st.expander("Cómo se usará este plan en el análisis"):
    st.markdown(
        """
        Cuando ejecute el análisis semanal, la aplicación revisará primero el plan configurado en esta pestaña.
        La meta acumulada se calculará sumando las actividades incluidas desde la semana 1 hasta la semana analizada.

        Ejemplo: si asigna 4 actividades a Semana 1 y analiza Semana 1, la meta será 4. Si el estudiante completó las 4, el avance será 4/4.

        La distribución uniforme solo se usará cuando no exista ninguna actividad asignada en el plan.
        """
    )
