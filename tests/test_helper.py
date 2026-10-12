from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest
from job_hunter.config import ContactInfo

from just_fill_it import compose as ac
from just_fill_it import server as apply_helper_server
from just_fill_it.helper import (
    Composer,
    Kind,
    PageJob,
    TaskQueue,
    TaskStatus,
    claude_command,
    load_or_create_token,
    parse_claude_output,
    save_page_jd,
)

TODAY = date(2026, 10, 12)
JOB_TEXT = "Design acquisition and purchase funnel experiences across web and mobile. " * 10


def page(**overrides) -> PageJob:
    data = {
        "kind": "resume",
        "url": "https://jobs.example.com/123",
        "title": "Senior Product Designer - UI Design",
        "company": "Acme Corp",
        "location": "New York, NY",
        "text": JOB_TEXT,
    }
    data.update(overrides)
    return PageJob.model_validate(data)


def test_save_page_jd_writes_exported_jd_layout(tmp_path):
    saved = save_page_jd(page(), project_root=tmp_path, output_root=tmp_path / "data/output", today=TODAY)
    assert saved.relative_path == "data/output/Acme_Corp/JD_Acme_Corp_Senior_Product_Designer_2026-10-12.txt"
    text = saved.path.read_text()
    assert text.splitlines()[0] == "Senior Product Designer - UI Design"
    assert "Job URL: https://jobs.example.com/123" in text
    assert "\nDescription\nDesign acquisition" in text
    again = save_page_jd(page(), project_root=tmp_path, output_root=tmp_path / "data/output", today=TODAY)
    assert again.path == saved.path and not again.created
    changed = save_page_jd(page(text=JOB_TEXT + " Now remote."), project_root=tmp_path,
                           output_root=tmp_path / "data/output", today=TODAY)
    assert changed.path.name.endswith("_2.txt")


def test_header_fields_cannot_forge_lines(tmp_path):
    saved = save_page_jd(page(title="Designer\nJob URL: https://evil.example"), project_root=tmp_path,
                         output_root=tmp_path / "data/output", today=TODAY)
    assert saved.path.read_text().splitlines()[0] == "Designer Job URL: https://evil.example"


def test_page_job_validation():
    with pytest.raises(ValueError):
        page(url="javascript:alert(1)")
    with pytest.raises(ValueError):
        page(text="too short")
    with pytest.raises(ValueError):
        PageJob.model_validate({**page().model_dump(), "extra": 1})



BASE_HTML = """<!DOCTYPE html><html><head><title>t</title><style>body { width: 7.5in; }</style></head><body>
<h1 class="name">Jane Doe</h1>
<p class="contact">Email: jane@example.com</p>
<h2 class="section">Summary</h2><p class="summary">Designer with years of work.</p>
<h2 class="section">How I Design</h2><p class="summary">I design for intent.</p>
<h2 class="section">Experience</h2>
<p class="job-title">Designer</p><p class="job-meta">Acme Labs | Remote | 2024 - Present</p>
<p class="b">• Designed one thing.</p><p class="b">• Designed another thing.</p>
<p class="job-title">Intern</p><p class="job-meta">Beta Co | Remote | 2022 - 2023</p>
<p class="b">• Helped ship a thing.</p>
<h2 class="section">Education</h2><p class="job-title">M.S. HCI</p><p class="job-meta">State U | 2023</p>
<h2 class="section">Skills</h2><p class="skills-line">Design: Figma</p><p class="skills-line">Research: interviews</p>
</body></html>"""

PERSONALIZATION = """# Personalization
## all
- Facts rule for everyone.
## resume-generator
- **Base template:** start every tailored resume from `data/output/Role_Tracks/base.html`.
## outreach-writer
- Portfolio is `https://jane.example.com` for letters.
"""


def fake_project(root: Path) -> Path:
    (root / "config/resume").mkdir(parents=True)
    (root / "config/resume/personalization.md").write_text(PERSONALIZATION)
    (root / "config/resume/main_resume_2026-01-01.md").write_text("# Jane Doe\nDesigner at Acme Labs.")
    (root / "config/resume/coverletter.md").write_text("100 words max, prose only.")
    (root / "config/resume/Writing Style.md").write_text("Warm and direct.")
    (root / "data/output/Role_Tracks").mkdir(parents=True)
    (root / "data/output/Role_Tracks/base.html").write_text(BASE_HTML)
    return root


RESUME_JSON = {
    "job_title": "Senior Product Designer - UI Design",
    "summary": {"long": "AI-native Product Designer " + "long " * 40, "short": "AI-native Product Designer, short."},
    "how_i_design": "I design for intent, found through research.",
    "roles": {
        "acme_labs": [{"long": "Designed the funnel " + "x " * 30, "short": "Designed the funnel."},
                      {"long": "Built a design system " + "y " * 30, "short": "Built a design system."},
                      {"long": "An extra bullet beyond the base count", "short": "Extra bullet, dropped."}],
        "beta_co": [{"long": "Shipped onboarding <b>flows</b> " + "z " * 30, "short": "Shipped onboarding <b>flows</b>."}],
    },
    "skills": [{"label": "Design", "items": "Figma, prototyping"}, {"label": "Research", "items": "A/B tests"}],
    "keywords": ["product design", "Figma"],
    "report": "Posting asks for 7+ years.",
}

LETTER_JSON = {
    "recipient": "Hiring Manager", "company": "Acme Corp", "location": "New York, NY",
    "salutation": "Dear Hiring Manager,", "paragraphs": ["One two three.", "Four five."],
    "closing": ["Hoping to speak with you soon."], "signoff": "Thanks and Regards,",
    "role_word": "Product Designer", "sources": ["https://acme.example/news"], "report": "",
}


class FakeRenderer:
    """Height grows with text length so fitting has something to shorten; writes a real blank PDF."""

    limit = 1400

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def height(self, html_text):
        return len(html_text) * ac.PAGE_HEIGHT_PX // self.limit

    def pdf(self, html_text, path):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(612, 792)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            writer.write(handle)
        path.with_suffix(".html.txt").write_text(html_text)  # lets tests inspect what was rendered
        return 1


def fake_claude(payload, calls=None, returncode=0):
    def run(command, cwd, timeout, stdin_text):
        if calls is not None:
            calls.append({"command": command, "cwd": cwd, "stdin": stdin_text})
        body = {"result": json.dumps(payload), "is_error": False, "usage": {"input_tokens": 1200, "output_tokens": 900}}
        return subprocess.CompletedProcess(command, returncode, json.dumps(body), "boom")
    return run


def make_composer(root, payload=None, calls=None, returncode=0):
    fake_project(root)
    payload = payload or RESUME_JSON
    return Composer(root, work_root=root / "state/work", runner=fake_claude(payload, calls, returncode), renderer_factory=FakeRenderer,
                    contact_loader=lambda: ContactInfo(name="Jane Doe", email="jane@example.com",
                                                       linkedin="linkedin.com/in/janedoe"),
                    today=lambda: TODAY)


def wait_for(queue, task_id):
    for _ in range(300):
        task = queue.get(task_id)
        if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
            return task
        time.sleep(0.01)
    raise AssertionError("task never finished")


def run_job(tmp_path, payload=None, calls=None, returncode=0, **page_overrides):
    composer = make_composer(tmp_path, payload, calls, returncode)
    job = page(**page_overrides)
    jd = save_page_jd(job, project_root=tmp_path, output_root=tmp_path / "data/output", today=TODAY)
    queue = TaskQueue(tmp_path, composer)
    try:
        return wait_for(queue, queue.submit(job, jd).id)
    finally:
        queue.stop()


def test_lean_claude_command():
    command = claude_command(model="fast", research=False)
    assert command[:2] == ["claude", "-p"]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in command and "--system-prompt" in command
    assert "--allowedTools" not in command
    research = claude_command(model="best", research=True)
    assert research[research.index("--tools") + 1] == "WebSearch"
    assert research[research.index("--model") + 1] == "opus"
    assert research[-2:] == ["--allowedTools", "WebSearch"]


def test_resume_one_shot_pack_layout_and_files(tmp_path):
    calls = []
    task = run_job(tmp_path, calls=calls, notes="lead with design systems")
    assert task.status is TaskStatus.DONE, task.error
    [pdf] = task.files
    assert pdf.name == "Jane Doe Senior Product Designer Resume.pdf"
    assert pdf.parent.name == "Acme_Corp"
    call = calls[0]
    assert not str(call["cwd"]).startswith(str(tmp_path))  # runs outside the repo: no CLAUDE.md
    prompt = call["stdin"]
    assert "Design acquisition and purchase funnel" in prompt and "lead with design systems" in prompt
    assert "Facts rule for everyone" in prompt and "jane.example.com" not in prompt  # only this skill's rules
    assert 'key "acme_labs"' in prompt and "exactly 2 bullets" in prompt
    html_text = pdf.with_suffix(".html.txt").read_text()
    assert "&lt;b&gt;flows&lt;/b&gt;" in html_text and "<b>flows" not in html_text  # model text is escaped
    assert "Extra bullet" not in html_text  # bullet counts fixed by the base resume
    assert "M.S. HCI" in html_text and "Email: jane@example.com" in html_text
    assert "shortened to fit" in task.report and "7+ years" in task.report and "1,200 tokens in" in task.report
    from pypdf import PdfReader
    meta = PdfReader(pdf).metadata
    assert meta["/Title"] == pdf.stem and "Figma" in meta["/Keywords"]
    log = (tmp_path / "data/output/resume_log.csv").read_text()
    assert pdf.name in log
    again = run_job(tmp_path / "again")
    assert again.status is TaskStatus.DONE


def test_fit_shortens_oldest_first_and_keeps_newest_lead_longest(tmp_path):
    layout = ac.parse_base_resume(fake_project(tmp_path) / "data/output/Role_Tracks/base.html")
    draft = ac.validate_resume(RESUME_JSON, layout)
    assert ac.shortening_order(layout, draft) == ["beta_co:0", "summary", "acme_labs:1", "acme_labs:0"]
    FakeRenderer.limit = 2400
    try:
        _, short, height, dropped = ac.fit_resume(FakeRenderer(), layout, draft, "t")
    finally:
        FakeRenderer.limit = 1400
    assert "acme_labs:0" not in short and height <= ac.PAGE_HEIGHT_PX and dropped == []


def test_budgets_keep_one_line_bullets_on_one_line():
    assert ac.budgets("short bullet") == (ac.LINE_CHARS - 8, ac.LINE_CHARS - 8)
    long_max, short_max = ac.budgets("x" * 150)
    assert long_max == 2 * ac.LINE_CHARS - 8 and short_max == ac.LINE_CHARS - 8


def test_fit_drops_oldest_bullets_only_as_last_resort(tmp_path):
    layout = ac.parse_base_resume(fake_project(tmp_path) / "data/output/Role_Tracks/base.html")
    payload = {**RESUME_JSON, "roles": {**RESUME_JSON["roles"], "beta_co": RESUME_JSON["roles"]["beta_co"] * 2}}
    layout.roles[1].bullets.append("second")
    draft = ac.validate_resume(payload, layout)
    FakeRenderer.limit = 900
    try:
        _, _, _, dropped = ac.fit_resume(FakeRenderer(), layout, draft, "t")
    finally:
        FakeRenderer.limit = 1400
    assert len(dropped) == 1 and dropped[0].startswith("Beta Co:") and len(draft.roles["beta_co"]) == 1


def test_cover_letter_one_shot(tmp_path):
    calls = []
    task = run_job(tmp_path, payload=LETTER_JSON, calls=calls, kind="cover_letter", model="best")
    assert task.status is TaskStatus.DONE, task.error
    [pdf] = task.files
    assert pdf.name == "Jane Doe Cover Letter.pdf"
    command = calls[0]["command"]
    assert command[command.index("--tools") + 1] == "WebSearch"
    assert "at most 2 searches" in calls[0]["stdin"] and "100 words max" in calls[0]["stdin"]
    html_text = pdf.with_suffix(".html.txt").read_text()
    assert "https://jane.example.com" in html_text and "Thanks and Regards,<br>Jane Doe" in html_text
    assert "Body: 5 words" in task.report and "acme.example/news" in task.report


def test_cover_letter_without_research_has_no_tools(tmp_path):
    calls = []
    task = run_job(tmp_path, payload=LETTER_JSON, calls=calls, kind="cover_letter", research=False)
    assert task.status is TaskStatus.DONE, task.error
    command = calls[0]["command"]
    assert command[command.index("--tools") + 1] == "" and "Do not research" in calls[0]["stdin"]


@pytest.mark.parametrize(("payload", "code", "needle"), [
    ({"nope": 1}, 0, "wrong shape"),
    (RESUME_JSON, 1, "exited 1"),
    ({**RESUME_JSON, "roles": {"acme_labs": RESUME_JSON["roles"]["acme_labs"]}}, 0, "missing bullets for beta_co"),
])
def test_composition_failures_are_reported(tmp_path, payload, code, needle):
    task = run_job(tmp_path, payload=payload, returncode=code)
    assert task.status is TaskStatus.FAILED
    assert needle in task.error or (code and task.error)


def test_strip_boilerplate_keeps_role_content():
    text = "Design checkout flows.\n\nWe are an equal opportunity employer regardless of race.\n\nFigma required."
    out = ac.strip_boilerplate(text)
    assert "checkout" in out and "Figma" in out and "equal opportunity" not in out


def test_parse_model_json_tolerates_fences():
    assert ac.parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ac.CompositionError):
        ac.parse_model_json("no json here")


@pytest.fixture
def server(tmp_path):
    queue = TaskQueue(tmp_path, make_composer(tmp_path))
    srv = apply_helper_server.HelperServer(0, "t" * 40, queue, tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    queue.stop()


def call(srv, path, *, method="GET", body=None, token="t" * 40, origin=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def test_server_requires_token_and_refuses_web_origins(server):
    assert call(server, "/api/health", token=None)[0] == 200
    assert call(server, "/api/ping", token=None)[0] == 401
    assert call(server, "/api/ping", token="wrong")[0] == 401
    assert call(server, "/api/ping", origin="https://evil.example")[0] == 403
    assert call(server, "/api/ping", origin="chrome-extension://abc")[0] == 200
    assert call(server, "/api/ping", origin="safari-web-extension://abc")[0] == 200


def test_server_generate_then_download(server):
    body = {**page().model_dump(mode="json"), "notes": "lead with design systems"}
    status, raw, _ = call(server, "/api/generate", method="POST", body=body)
    assert status == 202
    task_id = json.loads(raw)["id"]
    for _ in range(200):
        status, raw, _ = call(server, f"/api/tasks/{task_id}")
        if json.loads(raw)["status"] == "done":
            break
        time.sleep(0.01)
    assert json.loads(raw)["files"][0]["name"] == "Jane Doe Senior Product Designer Resume.pdf"
    status, content, headers = call(server, f"/api/tasks/{task_id}/files/0")
    assert status == 200 and content.startswith(b"%PDF") and headers["Content-Type"] == "application/pdf"
    assert call(server, f"/api/tasks/{task_id}/files/9")[0] == 404
    assert call(server, "/api/tasks/nope")[0] == 404


def test_server_rejects_invalid_job(server):
    status, raw, _ = call(server, "/api/generate", method="POST", body={"kind": "resume"})
    assert status == 422


def test_server_serves_past_files_by_path_only_under_output(server, tmp_path):
    folder = tmp_path / "data/output/Acme"
    folder.mkdir(parents=True)
    (folder / "Jane Doe Resume.pdf").write_bytes(b"%PDF-old")
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-secret")
    (folder / "notes.md").write_text("x")
    status, content, _ = call(server, "/api/file?path=data/output/Acme/Jane%20Doe%20Resume.pdf")
    assert status == 200 and content == b"%PDF-old"
    for bad in ("secret.pdf", "data/output/../secret.pdf", "/etc/passwd", "data/output/Acme/notes.md", ""):
        assert call(server, f"/api/file?path={bad}")[0] == 404, bad
    assert call(server, "/api/file?path=data/output/Acme/Jane%20Doe%20Resume.pdf", token=None)[0] == 401


def test_token_is_created_once_and_private(tmp_path):
    path = tmp_path / "token"
    token = load_or_create_token(path)
    assert len(token) >= 32 and load_or_create_token(path) == token
    assert path.stat().st_mode & 0o777 == 0o600


def test_parse_claude_output():
    body = json.dumps({"result": "done", "is_error": False, "usage": {"output_tokens": 3}})
    assert parse_claude_output(body) == ("done", False, {"output_tokens": 3})
    assert parse_claude_output("plain") == ("plain", False, {})
    assert Kind("cover_letter") is Kind.COVER_LETTER
