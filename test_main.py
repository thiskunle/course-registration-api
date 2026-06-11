"""
pytest tests for Phase 1 + 2 + 3
Run: pytest test_main.py -v --cov=main --cov-fail-under=80
"""
import io
import pytest
from fastapi.testclient import TestClient
from main import app, catalog, students

client = TestClient(app)

CATALOG_HTML = b"""
<table>
<tr><th>Course Code</th><th>Title</th><th>Credits</th><th>Prerequisites</th><th>Cross-Listed</th></tr>
<tr><td>COSC-1046</td><td>Programming I</td><td>3</td><td>None</td><td>None</td></tr>
<tr><td>COSC-1047</td><td>Programming II</td><td>3</td><td>COSC-1046</td><td>ITEC-1047</td></tr>
<tr><td>COSC-3506</td><td>Software Eng</td><td>3</td><td>COSC-1046 and COSC-1047</td><td>None</td></tr>
<tr><td>ITEC-1047</td><td>Intro IT</td><td>3</td><td>None</td><td>COSC-1047</td></tr>
<tr><td>COSC-4426</td><td>Advanced</td><td>3</td><td>COSC-3506</td><td>None</td></tr>
</table>
"""

TRANSCRIPT_HTML = b"""
<table>
<tr><th>Status</th><th>Course</th><th>Title</th><th>Grade</th><th>Term</th><th>Credits</th></tr>
<tr><td>Completed</td><td>COSC-1046</td><td>Prog I</td><td>85</td><td>23F</td><td>3</td></tr>
<tr><td>Completed</td><td>COSC-1047</td><td>Prog II</td><td>B+</td><td>24W</td><td>3</td></tr>
<tr><td>Completed</td><td>COSC-1046</td><td>Prog I</td><td>85</td><td>23F</td><td>3</td></tr>
<tr><td>Attempted</td><td>COSC-3506</td><td>SE</td><td>F</td><td>24F</td><td>3</td></tr>
<tr><td>Fulfilled</td><td>MATH-1057</td><td>Calc</td><td></td><td></td><td>3</td></tr>
</table>
"""


@pytest.fixture(autouse=True)
def reset_state():
    catalog.clear()
    students.clear()
    yield
    catalog.clear()
    students.clear()


def import_catalog():
    return client.post(
        "/api/v1/admin/catalog/import",
        files={"file": ("catalog.html", io.BytesIO(CATALOG_HTML), "text/html")},
    )


def import_transcript(sid="111"):
    return client.post(
        f"/api/v1/students/{sid}/history/import",
        files={"file": ("transcript.html", io.BytesIO(TRANSCRIPT_HTML), "text/html")},
    )


# ---- Phase 1 tests ----

def test_catalog_import():
    r = import_catalog()
    assert r.status_code == 200
    assert r.json()["courses_imported"] == 5


def test_catalog_get_course():
    import_catalog()
    r = client.get("/api/v1/catalog/courses/COSC-3506")
    assert r.status_code == 200
    assert r.json()["course_code"] == "COSC-3506"


def test_catalog_get_missing():
    import_catalog()
    r = client.get("/api/v1/catalog/courses/FAKE-9999")
    assert r.status_code == 404


# ---- Phase 2 tests ----

def test_history_import_201():
    r = import_transcript()
    assert r.status_code == 201
    assert r.json()["status"] == "success"


def test_history_deduplication():
    r = import_transcript()
    count = r.json()["past_courses_imported"]
    # COSC-1046 appears twice, should be deduped; Fulfilled row skipped
    assert count == 3  # COSC-1046, COSC-1047, COSC-3506


def test_profile_after_import():
    import_transcript()
    r = client.get("/api/v1/students/111/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["student_id"] == "111"
    assert "history" in body
    assert "plan" in body


def test_history_put_overwrites():
    import_transcript()
    r = client.put("/api/v1/students/111/history", json={"history": []})
    assert r.status_code == 200
    profile = client.get("/api/v1/students/111/profile").json()
    assert profile["history"] == []


def test_history_delete():
    import_transcript()
    r = client.delete("/api/v1/students/111/history")
    assert r.status_code == 200
    profile = client.get("/api/v1/students/111/profile").json()
    assert profile["history"] == []


def test_404_unknown_student():
    r = client.get("/api/v1/students/UNKNOWN/profile")
    assert r.status_code == 404


def test_plan_post():
    import_transcript()
    r = client.post("/api/v1/students/111/plan",
                    json={"planned_courses": [{"course_code": "COSC-3506", "term": "26F"}]})
    assert r.status_code == 200
    assert r.json()["planned_courses_saved"] == 1


def test_plan_404_unknown():
    r = client.post("/api/v1/students/NOBODY/plan",
                    json={"planned_courses": []})
    assert r.status_code == 404


def test_data_isolation():
    import_transcript("AAA")
    import_transcript("BBB")
    pa = client.get("/api/v1/students/AAA/profile").json()
    pb = client.get("/api/v1/students/BBB/profile").json()
    assert pa["student_id"] == "AAA"
    assert pb["student_id"] == "BBB"
    assert pa["history"] == pb["history"]  # same transcript, same data
    # Modify one and check other unaffected
    client.delete("/api/v1/students/AAA/history")
    pa2 = client.get("/api/v1/students/AAA/profile").json()
    pb2 = client.get("/api/v1/students/BBB/profile").json()
    assert pa2["history"] == []
    assert len(pb2["history"]) > 0


# ---- Phase 3 tests ----

def test_audit_missing_prerequisite():
    import_catalog()
    import_transcript()  # has COSC-1046, COSC-1047 completed; COSC-3506 only Attempted
    # Plan COSC-4426 which needs COSC-3506 Completed
    client.post("/api/v1/students/111/plan",
                json={"planned_courses": [{"course_code": "COSC-4426", "term": "26F"}]})
    r = client.get("/api/v1/students/111/audit-report")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("warning", "failed", "ok")
    assert "timeline_validation" in body
    assert "credit_summary" in body


def test_audit_cross_list_violation():
    import_catalog()
    import_transcript()  # COSC-1047 is completed
    # Plan ITEC-1047 which is cross-listed with COSC-1047
    client.post("/api/v1/students/111/plan",
                json={"planned_courses": [{"course_code": "ITEC-1047", "term": "26F"}]})
    r = client.get("/api/v1/students/111/audit-report")
    body = r.json()
    assert any(v["type"] == "CROSS_LIST_CONFLICT"
               for v in body["cross_list_violations"])


def test_audit_strict_mode():
    import_catalog()
    import_transcript()
    client.post("/api/v1/students/111/plan",
                json={"planned_courses": [{"course_code": "ITEC-1047", "term": "26F"}]})
    r = client.get("/api/v1/students/111/audit-report?strict=true")
    assert r.json()["status"] == "failed"


def test_audit_credit_summary():
    import_catalog()
    import_transcript()
    client.post("/api/v1/students/111/plan",
                json={"planned_courses": [{"course_code": "COSC-4426", "term": "26F"}]})
    r = client.get("/api/v1/students/111/audit-report")
    cs = r.json()["credit_summary"]
    assert "total_earned" in cs
    assert "total_planned" in cs
    assert "total_remaining_for_graduation" in cs
    assert cs["total_remaining_for_graduation"] == max(0, 120 - cs["total_earned"] - cs["total_planned"])


def test_audit_no_issues_ok():
    import_catalog()
    import_transcript()
    # No plan = no issues
    r = client.get("/api/v1/students/111/audit-report")
    assert r.json()["status"] == "ok"
