"""
Course Registration API - Phase 1 + 2 + 3
Adds: audit-report endpoint with full validation engine
"""

import re
from typing import Any
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI(title="Course Registration API - Phase 3")

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
# catalog: { normalized_code: { course_code, title, credits, prerequisites, cross_listed } }
catalog: dict[str, dict[str, Any]] = {}

# students: { student_id: { "history": [...], "plan": [...] } }
students: dict[str, dict[str, list]] = {}


# ---------------------------------------------------------------------------
# Code normalization (format-insensitive matching)
# ---------------------------------------------------------------------------

def _norm(code: str) -> str:
    """COSC-3506 = COSC 3506 = cosc3506 → COSC3506"""
    return re.sub(r"[\s\-]", "", code.strip().upper())


# ---------------------------------------------------------------------------
# Term ordering  W < SP < S < F
# ---------------------------------------------------------------------------

SEASON_ORDER = {"W": 0, "SP": 1, "S": 2, "F": 3}


def _term_key(term: str) -> tuple[int, int]:
    """Return (year, season_index) for sorting. Unknown terms sort last."""
    term = term.strip()
    m = re.match(r'^(\d{2})(W|SP|S|F)$', term, re.IGNORECASE)
    if not m:
        return (9999, 9999)
    year = int(m.group(1))
    season = m.group(2).upper()
    return (year, SEASON_ORDER.get(season, 9999))


def _term_before(t1: str, t2: str) -> bool:
    """Return True if t1 is strictly before t2."""
    return _term_key(t1) < _term_key(t2)


# ---------------------------------------------------------------------------
# Phase 1 — Catalog Parsing
# ---------------------------------------------------------------------------

COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,8}[\s\-]?\d{3,5}[A-Z]?)\b")


def _extract_codes(text: str) -> list[str]:
    return [_norm(m) for m in COURSE_CODE_RE.findall(text.upper())]


def _parse_prerequisites(raw: str) -> list[str]:
    if not raw or raw.strip().lower() in ("none", "n/a", "-", ""):
        return []
    return _extract_codes(raw)


def _parse_cross_listed(raw: str) -> list[str]:
    if not raw or raw.strip().lower() in ("none", "n/a", "-", ""):
        return []
    return _extract_codes(raw)


def _parse_catalog_html(html_bytes: bytes) -> dict[str, dict[str, Any]]:
    soup = BeautifulSoup(html_bytes, "html.parser")
    courses: dict[str, dict[str, Any]] = {}

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower()
                   for th in header_row.find_all(["th", "td"])]
        if not headers:
            continue

        col_map = _build_col_map(headers)
        if "code" not in col_map or "title" not in col_map:
            if not _table_has_course_codes(table):
                continue
            col_map = {"code": 0, "title": 1, "credits": 2,
                       "prerequisites": 3, "cross_listed": 4}

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            def cell_text(idx, cells=cells):
                return cells[idx].get_text(separator=" ", strip=True) if idx < len(cells) else ""

            raw_code = cell_text(col_map.get("code", 0))
            norm_code = _norm(raw_code)
            if not norm_code or not re.match(r'^[A-Z]{2,8}\d{3,5}[A-Z]?$', norm_code):
                continue

            credits_raw = cell_text(col_map.get("credits", 2))
            try:
                credits = int(re.search(r"\d+", credits_raw).group())
            except (AttributeError, ValueError):
                credits = 0

            prereq_raw  = cell_text(col_map.get("prerequisites", 3))
            cross_raw   = cell_text(col_map.get("cross_listed", 4))

            courses[norm_code] = {
                "course_code":    raw_code.strip(),
                "title":          cell_text(col_map.get("title", 1)),
                "credits":        credits,
                "prerequisites":  _parse_prerequisites(prereq_raw),
                "cross_listed":   _parse_cross_listed(cross_raw),
            }

    return courses


def _build_col_map(headers):
    col_map = {}
    keyword_map = {
        "code":          ["code", "course code", "course #", "course no"],
        "title":         ["title", "course title", "name", "course name"],
        "credits":       ["credits", "credit", "units", "hrs", "hours"],
        "prerequisites": ["prerequisites", "prerequisite", "prereq", "pre-req"],
        "cross_listed":  ["cross", "cross-listed", "crosslisted", "also listed"],
    }
    for i, h in enumerate(headers):
        for field, keywords in keyword_map.items():
            if field not in col_map and any(kw in h for kw in keywords):
                col_map[field] = i
    return col_map


def _table_has_course_codes(table):
    for row in table.find_all("tr")[1:4]:
        cells = row.find_all(["td", "th"])
        if cells and COURSE_CODE_RE.search(cells[0].get_text(strip=True).upper()):
            return True
    return False


# ---------------------------------------------------------------------------
# Phase 1 — Routes
# ---------------------------------------------------------------------------

@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    html_bytes = await file.read()
    if not html_bytes:
        raise HTTPException(status_code=400, detail="File is empty.")
    parsed = _parse_catalog_html(html_bytes)
    if not parsed:
        raise HTTPException(status_code=422, detail="No courses found.")
    catalog.clear()
    catalog.update(parsed)
    return JSONResponse(status_code=200, content={
        "message": "Catalog imported successfully.",
        "courses_imported": len(catalog),
        "course_codes": sorted(catalog.keys()),
    })


@app.get("/api/v1/catalog/courses/{course_code}")
async def get_course(course_code: str):
    course = catalog.get(_norm(course_code))
    if not course:
        raise HTTPException(status_code=404, detail=f"Course '{course_code}' not found.")
    return course


@app.get("/api/v1/catalog/courses")
async def list_courses():
    return {"total": len(catalog), "courses": list(catalog.values())}


# ---------------------------------------------------------------------------
# Phase 2 — Transcript Parsing
# ---------------------------------------------------------------------------

VALID_STATUSES = {"completed", "in-progress", "attempted"}


def _grade_priority(grade: str) -> int:
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
    soup = BeautifulSoup(html_bytes, "html.parser")
    raw_rows = []

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower()
                   for th in header_row.find_all(["th", "td"])]
        if "status" not in headers or "course" not in headers:
            continue
        try:
            status_idx  = headers.index("status")
            course_idx  = headers.index("course")
            term_idx    = next(i for i, h in enumerate(headers) if "term" in h)
            credits_idx = next(i for i, h in enumerate(headers) if "credit" in h)
            grade_idx   = next(i for i, h in enumerate(headers) if "grade" in h)
        except (ValueError, StopIteration):
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])

            def cell(idx, cells=cells):
                return cells[idx].get_text(strip=True) if idx < len(cells) else ""

            status  = cell(status_idx)
            course  = cell(course_idx)
            term    = cell(term_idx)
            grade   = cell(grade_idx)
            cr      = cell(credits_idx)

            if status.lower() not in VALID_STATUSES:
                continue
            if not term or not course:
                continue

            try:
                credits_earned = int(float(cr))
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
    best = {}
    for row in raw_rows:
        key = (_norm(row["course_code"]), row["term"])
        if key not in best:
            best[key] = row
        else:
            ep = _grade_priority(best[key]["grade"])
            np = _grade_priority(row["grade"])
            if np > ep or (np == ep and row["credits_earned"] > best[key]["credits_earned"]):
                best[key] = row

    return [{
        "course_code":    r["course_code"],
        "term":           r["term"],
        "credits_earned": r["credits_earned"],
        "status":         r["status"],
    } for r in best.values()]


# ---------------------------------------------------------------------------
# Phase 2 — Routes
# ---------------------------------------------------------------------------

@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...)):
    html_bytes = await file.read()
    if not html_bytes:
        raise HTTPException(status_code=400, detail="File is empty.")
    courses = _parse_transcript(html_bytes)
    if student_id not in students:
        students[student_id] = {"history": [], "plan": []}
    students[student_id]["history"] = courses
    return JSONResponse(status_code=201, content={
        "status": "success",
        "past_courses_imported": len(courses),
    })


@app.put("/api/v1/students/{student_id}/history")
async def update_history(student_id: str, body: dict):
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")
    students[student_id]["history"] = body.get("history", [])
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
async def delete_history(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")
    students[student_id]["history"] = []
    return {"status": "success", "message": "Academic history cleared."}


@app.post("/api/v1/students/{student_id}/plan")
async def create_plan(student_id: str, body: dict):
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")
    planned = body.get("planned_courses", [])
    students[student_id]["plan"] = planned
    return {"status": "success", "planned_courses_saved": len(planned)}


@app.put("/api/v1/students/{student_id}/plan")
async def update_plan(student_id: str, body: dict):
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")
    planned = body.get("planned_courses", [])
    students[student_id]["plan"] = planned
    return {"status": "success", "planned_courses_saved": len(planned),
            "message": "Plan updated successfully."}


@app.delete("/api/v1/students/{student_id}/plan")
async def delete_plan(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")
    students[student_id]["plan"] = []
    return {"status": "success", "message": "Plan cleared."}


@app.get("/api/v1/students/{student_id}/profile")
async def get_profile(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")
    s = students[student_id]
    return {"student_id": student_id, "history": s["history"], "plan": s["plan"]}


# ---------------------------------------------------------------------------
# Phase 3 — Audit Engine
# ---------------------------------------------------------------------------

def _build_completed_map(history: list[dict]) -> dict[str, list[dict]]:
    """
    Build { norm_code: [list of completed entries] }.
    Handles retakes: if same course completed multiple times, keep all terms.
    For credit counting, we count each norm_code once (highest credits from
    completed entries).
    """
    completed: dict[str, list[dict]] = {}
    for entry in history:
        if entry.get("status", "").lower() == "completed":
            nc = _norm(entry["course_code"])
            if nc not in completed:
                completed[nc] = []
            completed[nc].append(entry)
    return completed


def _total_earned(history: list[dict]) -> int:
    """
    Sum credits_earned for completed courses.
    Each unique course counted once (latest/best completion).
    """
    best: dict[str, int] = {}
    for entry in history:
        if entry.get("status", "").lower() == "completed":
            nc = _norm(entry["course_code"])
            credits = entry.get("credits_earned", 0)
            if nc not in best or credits > best[nc]:
                best[nc] = credits
    return sum(best.values())


@app.get("/api/v1/students/{student_id}/audit-report")
async def audit_report(student_id: str, strict: bool = False):
    if student_id not in students:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")

    s = students[student_id]
    history: list[dict] = s["history"]
    plan: list[dict]    = s["plan"]

    completed_map = _build_completed_map(history)

    # ---- Group plan by term ------------------------------------------------
    by_term: dict[str, list[dict]] = {}
    for pc in plan:
        t = pc.get("term", "")
        by_term.setdefault(t, []).append(pc)

    # ---- Timeline validation (prerequisite checking) -----------------------
    timeline_validation = []

    for term in sorted(by_term.keys(), key=_term_key):
        errors = []
        for pc in by_term[term]:
            pc_norm = _norm(pc["course_code"])
            catalog_entry = catalog.get(pc_norm)
            if not catalog_entry:
                continue

            for prereq_norm in catalog_entry.get("prerequisites", []):
                # Check: prereq must be Completed in a strictly earlier term
                completions = completed_map.get(prereq_norm, [])
                satisfied = any(
                    _term_before(c["term"], term)
                    for c in completions
                )
                if not satisfied:
                    # Find original prereq display code
                    prereq_display = catalog.get(prereq_norm, {}).get(
                        "course_code",
                        prereq_norm  # fallback
                    )
                    errors.append({
                        "course_code": pc["course_code"],
                        "type": "MISSING_PREREQUISITE",
                        "message": f"Missing prerequisite: {prereq_display}",
                    })

        if errors:
            timeline_validation.append({"term": term, "errors": errors})

    # ---- Cross-list violations ---------------------------------------------
    cross_list_violations = []

    for pc in plan:
        pc_norm = _norm(pc["course_code"])
        catalog_entry = catalog.get(pc_norm)
        if not catalog_entry:
            continue

        for cross_norm in catalog_entry.get("cross_listed", []):
            if cross_norm in completed_map:
                cross_display = catalog.get(cross_norm, {}).get(
                    "course_code", cross_norm
                )
                cross_list_violations.append({
                    "course_code": pc["course_code"],
                    "type": "CROSS_LIST_CONFLICT",
                    "message": f"Cross-listed with completed course {cross_display}",
                })

    # ---- Credit summary ----------------------------------------------------
    total_earned = _total_earned(history)

    total_planned = 0
    for pc in plan:
        pc_norm = _norm(pc["course_code"])
        cat = catalog.get(pc_norm)
        if cat:
            total_planned += cat.get("credits", 0)

    total_remaining = max(0, 120 - total_earned - total_planned)

    credit_summary = {
        "total_earned":                  total_earned,
        "total_planned":                 total_planned,
        "total_remaining_for_graduation": total_remaining,
    }

    # ---- Status ------------------------------------------------------------
    has_issues = bool(timeline_validation or cross_list_violations)
    if not has_issues:
        status = "ok"
    elif strict:
        status = "failed"
    else:
        status = "warning"

    return {
        "student_id":           student_id,
        "status":               status,
        "timeline_validation":  timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary":       credit_summary,
    }


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"message": "Course Registration API Phase 3 running", "docs": "/docs"}