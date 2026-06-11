"""
Course Registration API - Phase 2
Student Profile Ingestion & Data Normalization
"""

import re
from typing import Any
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI(title="Course Registration API - Phase 2")

# Per-student state: { student_id: { "history": [...], "plan": [...] } }
students: dict[str, dict[str, list]] = {}


# ---------------------------------------------------------------------------
# HTML Transcript Parsing
# ---------------------------------------------------------------------------

VALID_STATUSES = {"completed", "in-progress", "attempted"}


def _grade_priority(grade: str) -> int:
    """Higher = more informative. Numeric > letter > P/blank."""
    g = grade.strip()
    if not g or g == "P":
        return 0
    if re.match(r'^[A-Fa-f][+-]?$', g):
        return 1
    try:
        float(g)
        return 2
    except ValueError:
        return 1


def _parse_transcript(html_bytes: bytes) -> list[dict]:
    """
    Parse student transcript HTML.
    Headers: Status · Course · (title) · Grade · Term · Credits
    Rules:
    - Status in {Completed, In-Progress, Attempted}
    - Term cell must be non-empty
    - Deduplicate by (course_code, term): keep most informative grade, then higher credits
    """
    soup = BeautifulSoup(html_bytes, "html.parser")
    raw_rows: list[dict] = []

    for table in soup.find_all("table"):
        # Find header row
        header_row = table.find("tr")
        if not header_row:
            continue

        headers = [th.get_text(strip=True).lower()
                   for th in header_row.find_all(["th", "td"])]

        # Check this table has the right columns
        if "status" not in headers or "course" not in headers:
            continue

        # Map column names to indices
        try:
            status_idx = headers.index("status")
            course_idx = headers.index("course")
            term_idx   = next(i for i, h in enumerate(headers) if "term" in h)
            credits_idx = next(i for i, h in enumerate(headers) if "credit" in h)
            grade_idx  = next(i for i, h in enumerate(headers) if "grade" in h)
        except (ValueError, StopIteration):
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])

            def cell(idx):
                return cells[idx].get_text(strip=True) if idx < len(cells) else ""

            status  = cell(status_idx)
            course  = cell(course_idx)
            term    = cell(term_idx)
            grade   = cell(grade_idx)
            credits_raw = cell(credits_idx)

            # Apply canonical rule
            if status.lower() not in VALID_STATUSES:
                continue
            if not term:
                continue
            if not course:
                continue

            try:
                credits_earned = int(float(credits_raw))
            except (ValueError, TypeError):
                credits_earned = 0

            raw_rows.append({
                "course_code":    course,
                "term":           term,
                "credits_earned": credits_earned,
                "status":         status,
                "grade":          grade,
            })

    # Deduplicate by (course_code, term)
    best: dict[tuple, dict] = {}
    for row in raw_rows:
        key = (row["course_code"], row["term"])
        if key not in best:
            best[key] = row
        else:
            existing = best[key]
            ep = _grade_priority(existing["grade"])
            np = _grade_priority(row["grade"])
            if np > ep or (np == ep and row["credits_earned"] > existing["credits_earned"]):
                best[key] = row

    # Return clean records (drop internal grade field)
    result = []
    for row in best.values():
        result.append({
            "course_code":    row["course_code"],
            "term":           row["term"],
            "credits_earned": row["credits_earned"],
            "status":         row["status"],
        })

    return result


# ---------------------------------------------------------------------------
# History Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...)):
    """Parse HTML transcript and store as student's history."""
    html_bytes = await file.read()
    if not html_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    courses = _parse_transcript(html_bytes)

    if student_id not in students:
        students[student_id] = {"history": [], "plan": []}

    students[student_id]["history"] = courses

    return JSONResponse(
        status_code=201,
        content={
            "status": "success",
            "past_courses_imported": len(courses),
        }
    )


@app.put("/api/v1/students/{student_id}/history")
async def update_history(student_id: str, body: dict):
    """Overwrite student's history with provided JSON array."""
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")

    history = body.get("history", [])
    students[student_id]["history"] = history

    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
async def delete_history(student_id: str):
    """Clear student's history."""
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")

    students[student_id]["history"] = []
    return {"status": "success", "message": "Academic history cleared."}


# ---------------------------------------------------------------------------
# Plan Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/students/{student_id}/plan")
async def create_plan(student_id: str, body: dict):
    """Store a course plan for an existing student."""
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")

    planned = body.get("planned_courses", [])
    students[student_id]["plan"] = planned

    return {
        "status": "success",
        "planned_courses_saved": len(planned),
    }


@app.put("/api/v1/students/{student_id}/plan")
async def update_plan(student_id: str, body: dict):
    """Replace student's plan entirely."""
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")

    planned = body.get("planned_courses", [])
    students[student_id]["plan"] = planned

    return {
        "status": "success",
        "planned_courses_saved": len(planned),
        "message": "Plan updated successfully.",
    }


@app.delete("/api/v1/students/{student_id}/plan")
async def delete_plan(student_id: str):
    """Clear student's plan."""
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")

    students[student_id]["plan"] = []
    return {"status": "success", "message": "Plan cleared."}


# ---------------------------------------------------------------------------
# Profile Endpoint
# ---------------------------------------------------------------------------

@app.get("/api/v1/students/{student_id}/profile")
async def get_profile(student_id: str):
    """Return unified student profile."""
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")

    s = students[student_id]
    return {
        "student_id": student_id,
        "history":    s["history"],
        "plan":       s["plan"],
    }


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"message": "Course Registration API Phase 2 running", "docs": "/docs"}