"""
Course Registration API - Phase 1
Endpoints:
  POST /api/v1/admin/catalog/import  - Upload & parse HTML catalog
  GET  /api/v1/catalog/courses/{course_code} - Retrieve a course by code
"""

import re
from typing import Any

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI(title="Course Catalog API")

# In-memory store: { "COSC3506": { ...course dict... }, ... }
catalog: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,8}\s*\d{3,5}[A-Z]?)\b")


def _normalize_code(raw: str) -> str:
    """Remove internal whitespace and uppercase: 'COSC 3506' -> 'COSC3506'."""
    return re.sub(r"\s+", "", raw.strip().upper())


def _extract_codes(text: str) -> list[str]:
    """Return all course codes found in a text string (normalised)."""
    return [_normalize_code(m) for m in COURSE_CODE_RE.findall(text)]


def _parse_prerequisites(raw: str) -> list[str]:
    """
    Turn a messy prerequisites string into a list of course codes.
    Works regardless of surrounding prose like
    'Requires COSC 1046 and either COSC 1047 or ITEC 1047'.
    """
    if not raw or raw.strip().lower() in ("none", "n/a", "-", ""):
        return []
    return _extract_codes(raw)


def _parse_cross_listed(raw: str) -> list[str]:
    """Extract cross-listed course codes from a cell string."""
    if not raw or raw.strip().lower() in ("none", "n/a", "-", ""):
        return []
    return _extract_codes(raw)


def _parse_catalog_html(html_bytes: bytes) -> dict[str, dict[str, Any]]:
    """
    Parse the course-catalog HTML.

    Expected table structure (generalised - no hardcoded course names):
        Column 0: Course Code
        Column 1: Title
        Column 2: Credits
        Column 3: Prerequisites  (may be absent)
        Column 4: Cross-listed   (may be absent)

    The function is tolerant of different column counts and orders by
    inspecting the <th> header row first.
    """
    soup = BeautifulSoup(html_bytes, "html.parser")
    courses: dict[str, dict[str, Any]] = {}

    for table in soup.find_all("table"):
        # ---- detect header row ------------------------------------------------
        header_cells = table.find("tr")
        if not header_cells:
            continue

        headers = [th.get_text(strip=True).lower()
                   for th in header_cells.find_all(["th", "td"])]

        if not headers:
            continue

        # Build a flexible column-index map from whatever headers exist
        col_map = _build_col_map(headers)

        # We need at least a course-code column and a title column to proceed
        if "code" not in col_map or "title" not in col_map:
            # Try heuristic: if first column looks like course codes in data rows
            if not _table_has_course_codes(table):
                continue
            # Assign positional defaults
            col_map = {"code": 0, "title": 1, "credits": 2,
                       "prerequisites": 3, "cross_listed": 4}

        # ---- iterate data rows ------------------------------------------------
        rows = table.find_all("tr")[1:]  # skip header
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            def cell_text(idx: int) -> str:
                if idx < len(cells):
                    return cells[idx].get_text(separator=" ", strip=True)
                return ""

            raw_code = cell_text(col_map.get("code", 0))
            code = _normalize_code(raw_code)

            if not code or not COURSE_CODE_RE.match(raw_code.strip()):
                # Skip rows that don't start with a valid course code
                continue

            title = cell_text(col_map.get("title", 1))

            credits_raw = cell_text(col_map.get("credits", 2))
            try:
                credits = int(re.search(r"\d+", credits_raw).group())
            except (AttributeError, ValueError):
                credits = 0

            prereq_raw = cell_text(col_map.get("prerequisites", 3))
            cross_raw = cell_text(col_map.get("cross_listed", 4))

            courses[code] = {
                "course_code": code,
                "title": title,
                "credits": credits,
                "prerequisites": _parse_prerequisites(prereq_raw),
                "cross_listed": _parse_cross_listed(cross_raw),
            }

    return courses


def _build_col_map(headers: list[str]) -> dict[str, int]:
    """
    Map semantic field names to column indices using keyword matching.
    Handles variations like 'course code', 'code', 'course #', etc.
    """
    col_map: dict[str, int] = {}
    keyword_map = {
        "code":          ["code", "course code", "course #", "course no", "coursecode"],
        "title":         ["title", "course title", "name", "course name"],
        "credits":       ["credits", "credit", "units", "hrs", "hours"],
        "prerequisites": ["prerequisites", "prerequisite", "prereq", "pre-req",
                          "pre req", "required", "requirement"],
        "cross_listed":  ["cross", "cross-listed", "crosslisted", "also listed"],
    }
    for i, h in enumerate(headers):
        for field, keywords in keyword_map.items():
            if field not in col_map and any(kw in h for kw in keywords):
                col_map[field] = i
    return col_map


def _table_has_course_codes(table) -> bool:
    """Heuristic: check whether the first data column contains course codes."""
    rows = table.find_all("tr")[1:4]  # sample first 3 data rows
    for row in rows:
        cells = row.find_all(["td", "th"])
        if cells and COURSE_CODE_RE.search(cells[0].get_text(strip=True)):
            return True
    return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    """
    Accept a multipart/form-data HTML file upload, parse the course catalog,
    and store it in memory.
    """
    if not file.filename.lower().endswith((".html", ".htm")):
        raise HTTPException(status_code=400,
                            detail="Only .html / .htm files are accepted.")

    html_bytes = await file.read()
    if not html_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    parsed = _parse_catalog_html(html_bytes)

    if not parsed:
        raise HTTPException(status_code=422,
                            detail="No courses could be parsed from the file. "
                                   "Ensure the HTML contains a valid course table.")

    catalog.clear()
    catalog.update(parsed)

    return JSONResponse(
        status_code=200,
        content={
            "message": "Catalog imported successfully.",
            "courses_imported": len(catalog),
            "course_codes": sorted(catalog.keys()),
        },
    )


@app.get("/api/v1/catalog/courses/{course_code}")
async def get_course(course_code: str):
    """
    Return a single course record by code (case-insensitive, space-tolerant).
    Example: GET /api/v1/catalog/courses/COSC3506
             GET /api/v1/catalog/courses/COSC%203506  (both work)
    """
    normalized = _normalize_code(course_code)
    course = catalog.get(normalized)
    if not course:
        raise HTTPException(
            status_code=404,
            detail=f"Course '{normalized}' not found. "
                   f"Import a catalog first or check the course code.",
        )
    return course


@app.get("/api/v1/catalog/courses")
async def list_courses():
    """Return all courses currently in memory (convenience endpoint)."""
    return {"total": len(catalog), "courses": list(catalog.values())}


@app.get("/")
async def root():
    return {
        "message": "Course Catalog API is running.",
        "docs": "/docs",
        "endpoints": {
            "import_catalog": "POST /api/v1/admin/catalog/import",
            "get_course":     "GET  /api/v1/catalog/courses/{course_code}",
            "list_courses":   "GET  /api/v1/catalog/courses",
        },
    }
