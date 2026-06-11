"""
Course Registration API - Phase 1 + 2 + 3 + 4
Adds: JWT auth, BOLA, RBAC, rate limiting, recommendations engine
"""

import re
import time
import collections
from typing import Any, Optional

import bcrypt
import jwt
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

app = FastAPI(title="Course Registration API - Phase 4")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JWT_SECRET = "course-reg-super-secret-key-2026"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = 3600
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
catalog: dict[str, dict[str, Any]] = {}
students: dict[str, dict[str, list]] = {}
users: dict[str, dict[str, str]] = {}          # username -> {hashed_pw, role}
rate_limit_store: dict[str, list[float]] = {}  # key -> [timestamps]

# Seed admin account at startup
_admin_hash = bcrypt.hashpw(b"admin", bcrypt.gensalt())
users["admin"] = {"hashed_pw": _admin_hash.decode(), "role": "admin"}

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
security = HTTPBearer(auto_error=False)


def _create_token(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token.")


def _get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization header required.")
    return _decode_token(credentials.credentials)


def _require_owner_or_admin(student_id: str, user: dict):
    if user.get("role") == "admin":
        return
    if user.get("sub") != student_id:
        raise HTTPException(status_code=401, detail="Access denied.")


def _require_owner(student_id: str, user: dict):
    """BOLA: only the exact owner (not even admin) can import their own history."""
    if user.get("sub") != student_id:
        raise HTTPException(status_code=401, detail="Access denied.")


# ---------------------------------------------------------------------------
# Rate limiter (sliding window)
# ---------------------------------------------------------------------------
def _check_rate_limit(key: str):
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = rate_limit_store.get(key, [])
    timestamps = [t for t in timestamps if t > window_start]
    if len(timestamps) >= RATE_LIMIT_MAX:
        rate_limit_store[key] = timestamps
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    timestamps.append(now)
    rate_limit_store[key] = timestamps


# ---------------------------------------------------------------------------
# Code normalization
# ---------------------------------------------------------------------------
def _norm(code: str) -> str:
    return re.sub(r"[\s\-]", "", code.strip().upper())


# ---------------------------------------------------------------------------
# Term ordering  W < SP < S < F
# ---------------------------------------------------------------------------
SEASON_ORDER = {"W": 0, "SP": 1, "S": 2, "F": 3}


def _term_key(term: str) -> tuple:
    m = re.match(r'^(\d{2})(W|SP|S|F)$', term.strip(), re.IGNORECASE)
    if not m:
        return (9999, 9999)
    return (int(m.group(1)), SEASON_ORDER.get(m.group(2).upper(), 9999))


def _term_before(t1: str, t2: str) -> bool:
    return _term_key(t1) < _term_key(t2)


# ---------------------------------------------------------------------------
# Phase 1 — Catalog parsing
# ---------------------------------------------------------------------------
COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,8}[\s\-]?\d{3,5}[A-Z]?)\b")


def _extract_codes(text: str) -> list[str]:
    return [_norm(m) for m in COURSE_CODE_RE.findall(text.upper())]


def _parse_prereqs(raw: str) -> list[str]:
    if not raw or raw.strip().lower() in ("none", "n/a", "-", ""):
        return []
    return _extract_codes(raw)


def _parse_cross(raw: str) -> list[str]:
    if not raw or raw.strip().lower() in ("none", "n/a", "-", ""):
        return []
    return _extract_codes(raw)


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


def _parse_catalog_html(html_bytes: bytes) -> dict:
    soup = BeautifulSoup(html_bytes, "html.parser")
    courses = {}
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

            def ct(idx, cells=cells):
                return cells[idx].get_text(separator=" ", strip=True) if idx < len(cells) else ""

            raw_code = ct(col_map.get("code", 0))
            nc = _norm(raw_code)
            if not nc or not re.match(r'^[A-Z]{2,8}\d{3,5}[A-Z]?$', nc):
                continue
            credits_raw = ct(col_map.get("credits", 2))
            try:
                credits = int(re.search(r"\d+", credits_raw).group())
            except (AttributeError, ValueError):
                credits = 0
            courses[nc] = {
                "course_code":   raw_code.strip(),
                "title":         ct(col_map.get("title", 1)),
                "credits":       credits,
                "prerequisites": _parse_prereqs(ct(col_map.get("prerequisites", 3))),
                "cross_listed":  _parse_cross(ct(col_map.get("cross_listed", 4))),
            }
    return courses


# ---------------------------------------------------------------------------
# Phase 2 — Transcript parsing
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

            status = cell(status_idx)
            course = cell(course_idx)
            term   = cell(term_idx)
            grade  = cell(grade_idx)
            cr     = cell(credits_idx)
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
# Phase 3 — Audit engine
# ---------------------------------------------------------------------------
def _build_completed_map(history: list[dict]) -> dict:
    completed = {}
    for entry in history:
        if entry.get("status", "").lower() == "completed":
            nc = _norm(entry["course_code"])
            completed.setdefault(nc, []).append(entry)
    return completed


def _total_earned(history: list[dict]) -> int:
    best = {}
    for entry in history:
        if entry.get("status", "").lower() == "completed":
            nc = _norm(entry["course_code"])
            c = entry.get("credits_earned", 0)
            if nc not in best or c > best[nc]:
                best[nc] = c
    return sum(best.values())


# ---------------------------------------------------------------------------
# Phase 4 — Recommendations (Kahn's topological sort)
# ---------------------------------------------------------------------------
def _compute_recommendations(student_id: str) -> list[dict]:
    s = students[student_id]
    history = s["history"]
    completed_map = _build_completed_map(history)
    completed_set = set(completed_map.keys())

    # Only consider courses in catalog not yet completed
    remaining = {nc: info for nc, info in catalog.items()
                 if nc not in completed_set}

    # Build DAG among remaining courses only
    # prereqs that are already completed count as satisfied
    in_degree = {nc: 0 for nc in remaining}
    dependents = {nc: [] for nc in remaining}

    for nc, info in remaining.items():
        for prereq in info.get("prerequisites", []):
            if prereq in remaining:
                # prereq is also a remaining course — must come before nc
                in_degree[nc] += 1
                dependents[prereq].append(nc)
            # if prereq is already completed, it's satisfied — no edge needed

    # Kahn's BFS level-by-level
    queue = collections.deque(
        [nc for nc in remaining if in_degree[nc] == 0]
    )
    pathway = []
    visited = set()

    while queue:
        # All nodes at this level form one term
        level_size = len(queue)
        term_courses = []
        for _ in range(level_size):
            nc = queue.popleft()
            visited.add(nc)
            display = catalog[nc]["course_code"]
            term_courses.append(display)
            for dep in dependents[nc]:
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    queue.append(dep)
        pathway.append(term_courses)

    # Assign term labels starting from next logical term
    # Use simple sequential labels: 26F, 27W, 27SP, 27S, 27F, ...
    term_labels = _generate_term_labels(len(pathway))

    recommended_pathway = []
    for i, courses in enumerate(pathway):
        if courses:
            recommended_pathway.append({
                "term": term_labels[i],
                "courses": sorted(courses),
            })

    return recommended_pathway


def _generate_term_labels(n: int) -> list[str]:
    """Generate n sequential term labels starting from 26F."""
    seasons = ["W", "SP", "S", "F"]
    labels = []
    year = 26
    season_idx = 3  # start at F
    for _ in range(n):
        labels.append(f"{year:02d}{seasons[season_idx]}")
        season_idx += 1
        if season_idx >= len(seasons):
            season_idx = 0
            year += 1
    return labels


# ===========================================================================
# ROUTES
# ===========================================================================

# ---------------------------------------------------------------------------
# Phase 4 — Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/auth/register", status_code=201)
async def register(body: dict):
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required.")
    if username in users:
        raise HTTPException(status_code=409, detail="Username already exists.")
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    users[username] = {"hashed_pw": hashed, "role": "student"}
    return JSONResponse(status_code=201, content={"status": "registered"})


@app.post("/api/v1/auth/login")
async def login(body: dict):
    username = body.get("username", "").strip()
    password = body.get("password", "")
    user = users.get(username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    if not bcrypt.checkpw(password.encode(), user["hashed_pw"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    token = _create_token(username, user["role"])
    return {"access_token": token, "token_type": "bearer"}


# ---------------------------------------------------------------------------
# Phase 1 — Catalog
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
# Phase 2 — History (with BOLA on import)
# ---------------------------------------------------------------------------

@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(
    student_id: str,
    file: UploadFile = File(...),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    # BOLA: require valid token whose sub == student_id
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization required.")
    user = _decode_token(credentials.credentials)
    _require_owner(student_id, user)

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
        raise HTTPException(status_code=404, detail="Student not found.")
    students[student_id]["history"] = body.get("history", [])
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
async def delete_history(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found.")
    students[student_id]["history"] = []
    return {"status": "success", "message": "Academic history cleared."}


# ---------------------------------------------------------------------------
# Phase 2 — Plan
# ---------------------------------------------------------------------------

@app.post("/api/v1/students/{student_id}/plan")
async def create_plan(student_id: str, body: dict):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found.")
    planned = body.get("planned_courses", [])
    students[student_id]["plan"] = planned
    return {"status": "success", "planned_courses_saved": len(planned)}


@app.put("/api/v1/students/{student_id}/plan")
async def update_plan(student_id: str, body: dict):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found.")
    planned = body.get("planned_courses", [])
    students[student_id]["plan"] = planned
    return {"status": "success", "planned_courses_saved": len(planned),
            "message": "Plan updated successfully."}


@app.delete("/api/v1/students/{student_id}/plan")
async def delete_plan(student_id: str):
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found.")
    students[student_id]["plan"] = []
    return {"status": "success", "message": "Plan cleared."}


# ---------------------------------------------------------------------------
# Phase 2 — Profile (RBAC: owner or admin)
# ---------------------------------------------------------------------------

@app.get("/api/v1/students/{student_id}/profile")
async def get_profile(
    student_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization required.")
    user = _decode_token(credentials.credentials)
    _require_owner_or_admin(student_id, user)
    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found.")
    s = students[student_id]
    return {"student_id": student_id, "history": s["history"], "plan": s["plan"]}


# ---------------------------------------------------------------------------
# Phase 3 — Audit report (rate limited)
# ---------------------------------------------------------------------------

@app.get("/api/v1/students/{student_id}/audit-report")
async def audit_report(
    request: Request,
    student_id: str,
    strict: bool = False,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    # Rate limit by JWT sub if authenticated, else by IP
    if credentials:
        try:
            user = _decode_token(credentials.credentials)
            rate_key = f"audit:{user.get('sub', 'anon')}"
        except Exception:
            rate_key = f"audit:ip:{request.client.host}"
    else:
        rate_key = f"audit:ip:{request.client.host}"

    _check_rate_limit(rate_key)

    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found.")

    s = students[student_id]
    history = s["history"]
    plan    = s["plan"]
    completed_map = _build_completed_map(history)

    by_term: dict[str, list] = {}
    for pc in plan:
        by_term.setdefault(pc.get("term", ""), []).append(pc)

    timeline_validation = []
    for term in sorted(by_term.keys(), key=_term_key):
        errors = []
        for pc in by_term[term]:
            pc_norm = _norm(pc["course_code"])
            cat = catalog.get(pc_norm)
            if not cat:
                continue
            for prereq in cat.get("prerequisites", []):
                completions = completed_map.get(prereq, [])
                satisfied = any(_term_before(c["term"], term) for c in completions)
                if not satisfied:
                    prereq_display = catalog.get(prereq, {}).get("course_code", prereq)
                    errors.append({
                        "course_code": pc["course_code"],
                        "type": "MISSING_PREREQUISITE",
                        "message": f"Missing prerequisite: {prereq_display}",
                    })
        if errors:
            timeline_validation.append({"term": term, "errors": errors})

    cross_list_violations = []
    for pc in plan:
        pc_norm = _norm(pc["course_code"])
        cat = catalog.get(pc_norm)
        if not cat:
            continue
        for cross in cat.get("cross_listed", []):
            if cross in completed_map:
                cross_display = catalog.get(cross, {}).get("course_code", cross)
                cross_list_violations.append({
                    "course_code": pc["course_code"],
                    "type": "CROSS_LIST_CONFLICT",
                    "message": f"Cross-listed with completed course {cross_display}",
                })

    total_earned = _total_earned(history)
    total_planned = sum(
        catalog[_norm(p["course_code"])].get("credits", 0)
        for p in plan if _norm(p["course_code"]) in catalog
    )
    total_remaining = max(0, 120 - total_earned - total_planned)

    has_issues = bool(timeline_validation or cross_list_violations)
    if not has_issues:
        status = "ok"
    elif strict:
        status = "failed"
    else:
        status = "warning"

    return {
        "student_id":            student_id,
        "status":                status,
        "timeline_validation":   timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary": {
            "total_earned":                   total_earned,
            "total_planned":                  total_planned,
            "total_remaining_for_graduation": total_remaining,
        },
    }


# ---------------------------------------------------------------------------
# Phase 4 — Recommendations (RBAC: owner or admin)
# ---------------------------------------------------------------------------

@app.get("/api/v1/students/{student_id}/recommendations")
async def get_recommendations(
    student_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization required.")
    user = _decode_token(credentials.credentials)
    _require_owner_or_admin(student_id, user)

    if student_id not in students:
        raise HTTPException(status_code=404, detail="Student not found.")

    pathway = _compute_recommendations(student_id)
    return {"student_id": student_id, "recommended_pathway": pathway}


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"message": "Course Registration API Phase 4 running", "docs": "/docs"}
