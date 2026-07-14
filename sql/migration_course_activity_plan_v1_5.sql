
-- Plan semanal de actividades por curso. Es independiente de las tablas históricas
-- y permite que el avance esperado no dependa de dividir uniformemente entre 5 semanas.
create table if not exists public.course_activity_plan (
    id uuid primary key default gen_random_uuid(),
    canvas_course_id text not null,
    course_name text,
    canvas_assignment_id text not null,
    activity_name text not null,
    activity_type text,
    due_at timestamptz,
    week_number integer check (week_number between 1 and 5),
    include_in_risk boolean not null default true,
    is_required boolean not null default true,
    points_possible numeric(10,2),
    manual_note text,
    configured_by_name text,
    configured_by_canvas_user_id text,
    configured_by_email text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(canvas_course_id, canvas_assignment_id)
);
create index if not exists idx_course_activity_plan_course on public.course_activity_plan(canvas_course_id, week_number);
create index if not exists idx_course_activity_plan_assignment on public.course_activity_plan(canvas_assignment_id);

alter table public.course_activity_plan enable row level security;
