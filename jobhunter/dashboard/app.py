"""
JobHunter Streamlit Dashboard.

Views:
  - New jobs (scored, not yet saved/rejected)
  - High-match jobs (score >= threshold)
  - Saved jobs (bookmarked for application)
  - In-progress applications
  - All jobs (with full filter panel)
  - Database stats
"""
from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap path so the package is importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st
from sqlalchemy.orm import Session

from jobhunter.database import SessionLocal, init_db
from jobhunter.models.job import Job, JobStatus
from jobhunter.state_machine import StateMachine


# ── Page config ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="JobHunter",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Initialise DB ─────────────────────────────────────────────────────────
init_db()


# ── Session helpers ───────────────────────────────────────────────────────
@st.cache_resource
def get_db_session() -> Session:
    return SessionLocal()


def get_db() -> Session:
    return SessionLocal()


# ── Sidebar navigation ────────────────────────────────────────────────────
st.sidebar.title("🎯 JobHunter")
st.sidebar.caption("Internship discovery assistant")

page = st.sidebar.radio(
    "View",
    ["🆕 New Jobs", "⭐ High Match", "💾 Saved", "📋 In Progress", "🔍 All Jobs", "📊 Stats", "🖊 Apply"],
)

st.sidebar.divider()

# Filter controls (used by All Jobs and New Jobs views)
st.sidebar.subheader("Filters")
min_score = st.sidebar.slider("Min score", 0, 100, 0)
sources = st.sidebar.multiselect(
    "Source",
    options=["bosch_careers", "eures", "profession_hu", "email_linkedin", "email_indeed", "email_glassdoor"],
    default=[],
)
work_modes = st.sidebar.multiselect("Work mode", ["remote", "hybrid", "onsite", "unknown"])
exclude_hungarian = st.sidebar.checkbox("Exclude Hungarian-mandatory roles", value=False)

st.sidebar.divider()
# ── CSV export ────────────────────────────────────────────────────────
if st.sidebar.button("📥 Download CSV", use_container_width=True):
    from jobhunter.export import Exporter
    _db = get_db()
    try:
        _csv = Exporter(_db).to_csv_string()
        st.sidebar.download_button(
            "⬇ Save applications.csv",
            data=_csv,
            file_name="applications.csv",
            mime="text/csv",
            use_container_width=True,
        )
    finally:
        _db.close()

if st.sidebar.button("🔄 Run ingestion", use_container_width=True):
    import asyncio
    from jobhunter.pipeline import Pipeline
    with st.spinner("Running job discovery pipeline..."):
        result = asyncio.run(Pipeline().run())
    st.sidebar.success(
        f"Done: {result.new_jobs} new, {result.updated_jobs} updated, "
        f"{result.rejected} rejected"
    )
    st.rerun()


# ── Query helpers ─────────────────────────────────────────────────────────
def build_query(db: Session, statuses: list[str] | None = None):  # type: ignore[no-untyped-def]
    q = db.query(Job)
    if statuses:
        q = q.filter(Job.status.in_(statuses))
    if min_score > 0:
        q = q.filter(Job.relevance_score >= min_score)
    if sources:
        q = q.filter(Job.source.in_(sources))
    if work_modes:
        q = q.filter(Job.work_mode.in_(work_modes))
    if exclude_hungarian:
        q = q.filter(Job.hungarian_mandatory.is_(False))
    return q.order_by(Job.relevance_score.desc().nullslast(), Job.discovered_at.desc())


def score_badge(score: int | None) -> str:
    if score is None:
        return "⚪ ?"
    if score >= 75:
        return f"🟢 {score}"
    if score >= 50:
        return f"🟡 {score}"
    return f"🔴 {score}"


def render_job_card(job: Job, db: Session, expanded: bool = False) -> None:
    """Render a single job card with action buttons."""
    score_str = score_badge(job.relevance_score)
    header = f"{score_str} — **{job.title}** @ {job.company}"
    if job.location:
        header += f" • 📍{job.location}"

    with st.expander(header, expanded=expanded):
        col1, col2 = st.columns([2, 1])

        with col1:
            st.markdown(f"**Source:** `{job.source}`")
            st.markdown(f"**Type:** {job.job_type} | **Mode:** {job.work_mode}")
            if job.salary_raw:
                st.markdown(f"**Salary:** {job.salary_raw}")
            if job.posted_at:
                st.markdown(f"**Posted:** {job.posted_at.date()}")
            if job.deadline:
                st.markdown(f"**Deadline:** {job.deadline.date()}")
            if job.score_explanation:
                st.info(job.score_explanation)
            if job.description:
                with st.expander("Job description"):
                    st.text(job.description[:3000] + ("..." if len(job.description or "") > 3000 else ""))

        with col2:
            st.markdown(f"**Status:** `{job.status}`")

            if job.application_url:
                st.link_button("🔗 Apply", job.application_url, use_container_width=True)

            sm = StateMachine(db)
            available = sm.available_transitions(job)

            if available:
                new_status = st.selectbox(
                    "Move to",
                    ["(keep current)"] + available,
                    key=f"status_{job.id}",
                )
                if new_status != "(keep current)":
                    if st.button("Apply", key=f"apply_{job.id}", use_container_width=True):
                        sm.transition(job, new_status, changed_by="user")
                        db.commit()
                        st.success(f"Moved to {new_status}")
                        st.rerun()

            if job.notes:
                st.markdown(f"**Notes:** {job.notes}")

            note = st.text_area("Add note", key=f"note_{job.id}", height=60)
            if st.button("Save note", key=f"savenote_{job.id}"):
                job.notes = (job.notes or "") + f"\n{note}".strip()
                db.commit()
                st.rerun()


# ── Page renderers ────────────────────────────────────────────────────────
def page_new_jobs() -> None:
    st.title("🆕 New Jobs")
    db = get_db()
    try:
        jobs = build_query(db, [JobStatus.SCORED]).limit(50).all()
        st.caption(f"{len(jobs)} new scored jobs")
        if not jobs:
            st.info("No new jobs yet. Run ingestion from the sidebar.")
            return
        for job in jobs:
            render_job_card(job, db)
    finally:
        db.close()


def page_high_match() -> None:
    st.title("⭐ High Match Jobs")
    from jobhunter.config import get_search_config
    threshold = get_search_config().min_score_to_notify
    db = get_db()
    try:
        jobs = (
            db.query(Job)
            .filter(
                Job.relevance_score >= threshold,
                Job.status.notin_([JobStatus.REJECTED_BY_FILTER, JobStatus.EXPIRED, JobStatus.WITHDRAWN]),
            )
            .order_by(Job.relevance_score.desc())
            .limit(50)
            .all()
        )
        st.caption(f"{len(jobs)} jobs scoring ≥{threshold}/100")
        for job in jobs:
            render_job_card(job, db)
    finally:
        db.close()


def page_saved() -> None:
    st.title("💾 Saved Jobs")
    db = get_db()
    try:
        jobs = build_query(db, [JobStatus.SAVED]).all()
        st.caption(f"{len(jobs)} saved jobs")
        for job in jobs:
            render_job_card(job, db)
    finally:
        db.close()


def page_in_progress() -> None:
    st.title("📋 In-Progress Applications")
    in_progress_statuses = [
        JobStatus.APPLICATION_STARTED,
        JobStatus.AWAITING_USER_INFO,
        JobStatus.AWAITING_LOGIN,
        JobStatus.FORM_PARTIALLY_COMPLETED,
        JobStatus.READY_FOR_FINAL_REVIEW,
        JobStatus.MANUALLY_SUBMITTED,
        JobStatus.INTERVIEW,
        JobStatus.OFFER,
    ]
    db = get_db()
    try:
        jobs = db.query(Job).filter(Job.status.in_(in_progress_statuses)).all()
        st.caption(f"{len(jobs)} active applications")
        for job in jobs:
            render_job_card(job, db, expanded=True)
    finally:
        db.close()


def page_all_jobs() -> None:
    st.title("🔍 All Jobs")
    db = get_db()
    try:
        jobs = build_query(db).limit(100).all()
        st.caption(f"Showing {len(jobs)} jobs (max 100)")
        for job in jobs:
            render_job_card(job, db)
    finally:
        db.close()


def page_stats() -> None:
    st.title("📊 Statistics")
    db = get_db()
    try:
        import sqlalchemy as sa

        total = db.query(Job).count()
        by_status = db.query(Job.status, sa.func.count(Job.id)).group_by(Job.status).all()
        by_source = db.query(Job.source, sa.func.count(Job.id)).group_by(Job.source).all()
        avg_score = db.query(sa.func.avg(Job.relevance_score)).scalar()
        high_score_count = db.query(Job).filter(Job.relevance_score >= 75).count()

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total jobs", total)
        col2.metric("High-match (≥75)", high_score_count)
        col3.metric("Avg score", f"{avg_score:.0f}" if avg_score else "N/A")
        col4.metric(
            "Saved",
            db.query(Job).filter(Job.status == JobStatus.SAVED).count()
        )

        st.divider()

        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("By status")
            for status, count in sorted(by_status, key=lambda x: -x[1]):
                pct = count / total * 100 if total else 0
                st.text(f"{status:<30} {count:>4}  ({pct:.0f}%)")

        with col_b:
            st.subheader("By source")
            for source, count in sorted(by_source, key=lambda x: -x[1]):
                st.text(f"{source:<30} {count:>4}")

    finally:
        db.close()


# ── Router ────────────────────────────────────────────────────────────────
if page == "🆕 New Jobs":
    page_new_jobs()
elif page == "⭐ High Match":
    page_high_match()
elif page == "💾 Saved":
    page_saved()
elif page == "📋 In Progress":
    page_in_progress()
elif page == "🔍 All Jobs":
    page_all_jobs()
elif page == "📊 Stats":
    page_stats()
elif page == "🖊 Apply":
    page_application_review()


# ── Application review section ────────────────────────────────────────────
def page_application_review() -> None:
    st.title("🖊 Application Review")
    st.info(
        "Select a saved job and start browser-assisted application filling. "
        "The system will fill safe fields and pause before submission."
    )

    db = get_db()
    try:
        saved_jobs = db.query(Job).filter(Job.status == JobStatus.SAVED).all()
        if not saved_jobs:
            st.warning("No saved jobs. Save a job first from the dashboard.")
            return

        job_options = {f"{j.title} @ {j.company} [{j.relevance_score}/100]": j for j in saved_jobs}
        selected_label = st.selectbox("Select job", list(job_options.keys()))
        selected_job = job_options[selected_label]

        st.markdown(f"**Application URL:** {selected_job.application_url or 'N/A'}")
        st.markdown(f"**Status:** `{selected_job.status}`")

        col1, col2 = st.columns(2)
        with col1:
            cv_path = st.text_input("Approved CV path", placeholder="data/documents/CV_Robotics.docx")
        with col2:
            adapter_name = st.selectbox(
                "ATS adapter",
                ["workday", "greenhouse", "auto-detect"],
            )

        if st.button("🚀 Start application assistance", use_container_width=True):
            if not selected_job.application_url:
                st.error("No application URL for this job.")
                return

            st.warning(
                "⚠️ The system will fill fields automatically but will NEVER submit. "
                "You must review and submit manually."
            )

            import asyncio
            from jobhunter.application.adapters.workday import WorkdayAdapter

            profile = {
                "full_name": "Noureldeen Elshawaf",
                "email": "",  # loaded from profile in production
                "phone": "",
                "city": "Budapest",
                "university": "University of Debrecen",
                "degree": "Mechatronics Engineering BSc",
                "linkedin_url": "https://linkedin.com/in/nourelshawaf",
                "github_url": "https://github.com/nourelshawaf",
            }

            cv = Path(cv_path) if cv_path else None
            adapter = WorkdayAdapter(profile=profile, cv_path=cv)

            with st.spinner("Running application assistance (simulation mode)..."):
                session = asyncio.run(
                    adapter.run(job_id=selected_job.id, url=selected_job.application_url)
                )

            st.subheader("📋 Session Summary")
            summary = session.to_summary()

            if summary["fields_auto_filled"]:
                st.success(f"✅ {len(summary['fields_auto_filled'])} fields auto-filled")
                for f in summary["fields_auto_filled"]:
                    st.text(f"  {f['label']}: {f['value']} (from: {f['from']})")

            if summary["fields_needing_user_input"]:
                st.warning(f"⚠️ {len(summary['fields_needing_user_input'])} fields need your input")
                for f in summary["fields_needing_user_input"]:
                    st.text(f"  {f['label']} ({f['reason']})")

            if summary["required_fields_missing"]:
                st.error(f"❌ {len(summary['required_fields_missing'])} required fields missing")
                for label in summary["required_fields_missing"]:
                    st.text(f"  {label}")

            with st.expander("Audit log"):
                for entry in summary["audit_log"]:
                    st.text(entry)

            st.info(
                "✋ **Manual action required.** "
                "Open the application URL, review all fields, and submit manually."
            )

    finally:
        db.close()
