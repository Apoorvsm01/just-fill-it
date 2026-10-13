"""Just Fill it!: one-click resume / cover letter from a job page (the browser extension in `extension/`).

The extension sends the page it is on (URL, title, company, job text) plus the user's own
instructions; this module saves that as a normal JD file under `data/output/<Company>/`, then runs a
single tool-free Claude call (`claude -p`, Fast = Sonnet / Best = Opus) with a compact context pack
built by `compose`, and lays the returned content out as a PDF. See `compose` for why
this replaced the agent workflow (tokens and time).

Job text is untrusted. It reaches Claude only inside the piped prompt (never a command line), the
call has no tools except optional WebSearch for cover-letter company research, no MCP servers and
no settings/CLAUDE.md, and it runs from a scratch directory outside the repo. Its reply is parsed as
JSON and HTML-escaped into a fixed template, so a hostile posting can at worst skew the wording.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from job_hunter.atomic import atomic_write_text
from job_hunter.config import ContactInfo, load_profile
from job_hunter.jd_export import _header, html_to_text, sanitize, write_numbered_text
from job_hunter.resume_source import resolve_master_resume
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import compose as ac

MAX_JOB_TEXT = 200_000
DEFAULT_TIMEOUT_SECONDS = 10 * 60
MAX_TASKS_KEPT = 50
MODELS = {"fast": "sonnet", "best": "opus"}
RESUME_DIR = Path("config/resume")
BASE_TEMPLATE_RULE = re.compile(r"start every tailored resume from\s+`([^`]+)`", re.I)


class Kind(StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PageJob(BaseModel):
    """What the extension sends: the job page as the user sees it."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    url: str = Field(min_length=1, max_length=4000)
    title: str = Field(min_length=1, max_length=300)
    company: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=300)
    text: str = Field(min_length=200, max_length=MAX_JOB_TEXT)
    notes: str = Field(default="", max_length=4000)
    model: str = Field(default="fast", pattern="^(fast|best)$")
    research: bool = True

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("must be an http(s) URL")
        return value


@dataclass(frozen=True)
class SavedJd:
    path: Path
    relative_path: str
    company_dir: Path
    created: bool


def render_page_jd(page: PageJob, today: date) -> str:
    """Same layout as `jd_export.render_jd_text`, so the skills read it like any exported JD."""
    body = html_to_text(page.text)
    return (
        f"{_header(page.title)}\n{_header(page.location)}\n\n"
        "Summary\n"
        f"Captured: {today.isoformat()}\n"
        f"Job URL: {_header(page.url)}\n"
        f"Source: {_header(page.company)} (browser extension)\n\n"
        f"Description\n{body}\n"
    )


def save_page_jd(page: PageJob, *, project_root: Path, output_root: Path, today: date) -> SavedJd:
    company = sanitize(page.company, "Company")
    title = sanitize(" ".join(page.title.split()[:4]), "Job")
    directory = Path(output_root) / company
    path, created = write_numbered_text(directory, f"JD_{company}_{title}_{today.isoformat()}",
                                        render_page_jd(page, today))
    relative = Path(os.path.relpath(path.resolve(), Path(project_root).resolve())).as_posix()
    return SavedJd(path.resolve(), relative, directory.resolve(), created)


def load_or_create_token(path: Path) -> str:
    """The shared secret the extension must send. Created once (0600) and reused."""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    atomic_write_text(path, token + "\n")
    os.chmod(path, 0o600)
    return token


def parse_claude_output(stdout: str) -> tuple[str, bool, dict[str, Any]]:
    """(result text, is_error, usage) from `claude -p --output-format json`."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return (stdout or "").strip(), False, {}
    if not isinstance(data, dict):
        return str(data), False, {}
    return str(data.get("result") or "").strip(), bool(data.get("is_error")), dict(data.get("usage") or {})


def claude_command(*, model: str, research: bool, system_file: Path | str, claude_bin: str = "claude") -> list[str]:
    """A lean, tool-free call: our own system prompt (from a file, so personal text never shows up in
    the process list) instead of Claude Code's agent prompt, no settings files, no MCP servers. The
    message goes on stdin."""
    tools = "WebSearch" if research else ""
    command = [
        claude_bin, "-p",
        "--system-prompt-file", str(system_file),
        "--setting-sources", "",
        "--strict-mcp-config",
        "--tools", tools,
        "--permission-mode", "dontAsk",
        "--model", MODELS.get(model, model),
        "--output-format", "json",
    ]
    if model == "fast":
        # No extended thinking: the writing is one structured pass and Python enforces the page fit,
        # so thinking only added ~3-5k hidden output tokens and ~20 s. (MAX_THINKING_TOKENS=0 and
        # CLAUDE_CODE_DISABLE_THINKING=1 were not honored; this was, every time.) Best keeps it.
        command += ["--effort", "low"]
    if research:
        command += ["--allowedTools", "WebSearch"]
    return command


def usage_line(usage: dict[str, Any], seconds: float) -> str:
    new = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens"))
    cached = int(usage.get("cache_read_input_tokens") or 0)
    tokens_in = f"{new:,} tokens in" + (f" (+{cached:,} cached, ~1/10 cost)" if cached else "")
    return f"{tokens_in}, {int(usage.get('output_tokens') or 0):,} out, {seconds:.0f}s"


@dataclass
class Task:
    id: str
    kind: Kind
    jd: SavedJd
    notes: str = ""
    model: str = "fast"
    research: bool = True
    title: str = ""
    url: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    report: str = ""
    error: str = ""
    files: list[Path] = field(default_factory=list)

    def public(self, project_root: Path) -> dict[str, Any]:
        root = Path(project_root).resolve()
        return {
            "id": self.id,
            "kind": self.kind.value,
            "status": self.status.value,
            "model": self.model,
            "jd": self.jd.relative_path,
            "report": self.report,
            "error": self.error,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.created_at),
            "files": [
                {"index": i, "name": p.name, "path": Path(os.path.relpath(p, root)).as_posix(),
                 "size": p.stat().st_size if p.exists() else 0}
                for i, p in enumerate(self.files)
            ],
        }


Runner = Callable[[list[str], Path, float, str], subprocess.CompletedProcess]


def run_subprocess(command: list[str], cwd: Path, timeout: float, stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, input=stdin_text, capture_output=True, text=True,
                          timeout=timeout, check=False)


class Composer:
    """Builds the pack, makes the one Claude call, renders the files. `runner` and `renderer` are
    injectable so tests need neither Claude nor Chromium."""

    def __init__(self, project_root: Path, *, work_root: Path, runner: Runner = run_subprocess, claude_bin: str = "claude",
                 renderer_factory: Callable[[], Any] = ac.Renderer, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 contact_loader: Callable[[], ContactInfo] | None = None, today: Callable[[], date] = date.today):
        self.root = Path(project_root).resolve()
        self.work_root = Path(work_root)
        self.runner = runner
        self.claude_bin = claude_bin
        self.renderer_factory = renderer_factory
        self.timeout = timeout
        self.contact_loader = contact_loader or (lambda: load_profile(self.root / "config/candidate_profile.yaml").contact)
        self.today = today

    # ---- inputs
    def _read(self, relative: str | Path, required: bool = True) -> str:
        path = self.root / relative
        if not path.is_file():
            if required:
                raise ac.CompositionError(f"missing {relative}")
            return ""
        return path.read_text(encoding="utf-8")

    def _personalization(self) -> str:
        return self._read(RESUME_DIR / "personalization.md", required=False)

    def _master_resume(self) -> str:
        try:
            return resolve_master_resume(self.root).path.read_text(encoding="utf-8")
        except (FileNotFoundError, AttributeError) as exc:
            raise ac.CompositionError("no master resume: add config/resume/main_resume_<YYYY-MM-DD>.md") from exc

    def _base_template(self, personalization: str) -> Path:
        match = BASE_TEMPLATE_RULE.search(personalization)
        if match and (self.root / match.group(1)).is_file():
            return self.root / match.group(1)
        raise ac.CompositionError(
            "no base resume template: add a rule to config/resume/personalization.md like "
            "'start every tailored resume from `data/output/Role_Tracks/<file>.html`'")

    def _jd_text(self, jd: SavedJd) -> str:
        return ac.strip_boilerplate(jd.path.read_text(encoding="utf-8"))

    def _system(self, personalization: str) -> str:
        return ac.system_context(personalization, self._master_resume())

    def _call(self, system: str, prompt: str, *, model: str, research: bool) -> tuple[dict[str, Any], dict[str, Any], float]:
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="just-fill-it-") as scratch:  # outside the repo: no CLAUDE.md
            system_file = Path(scratch) / "system.txt"
            system_file.write_text(system, encoding="utf-8")
            command = claude_command(model=model, research=research, system_file=system_file,
                                     claude_bin=self.claude_bin)
            done = self.runner(command, Path(scratch), self.timeout, prompt)
        result, is_error, usage = parse_claude_output(done.stdout)
        if done.returncode != 0 or is_error:
            raise ac.CompositionError((result or done.stderr or f"claude exited {done.returncode}").strip()[-1500:])
        return ac.parse_model_json(result), usage, time.time() - started

    # ---- documents
    def resume(self, task: Task) -> tuple[list[Path], str]:
        personalization = self._personalization()
        layout = ac.parse_base_resume(self._base_template(personalization))
        prompt = ac.resume_prompt(layout, jd=self._jd_text(task.jd), personalization=personalization,
                                  notes=task.notes)
        data, usage, seconds = self._call(self._system(personalization), prompt, model=task.model, research=False)
        draft = ac.validate_resume(data, layout)
        title = ac.plain_title(draft.job_title or task.title)
        name = layout.name
        target = ac.unique_path(task.jd.company_dir, f"{name} {title} Resume")
        with self.renderer_factory() as renderer:
            page, short, height, dropped = ac.fit_resume(renderer, layout, draft, target.stem)
            pages = renderer.pdf(page, target)
        ac.set_pdf_metadata(target, {
            "title": target.stem, "author": name,
            "subject": f"{title} resume", "keywords": ", ".join(draft.keywords),
        })
        work = self.work_root / task.id
        work.mkdir(parents=True, exist_ok=True)
        atomic_write_text(work / f"{target.stem}.html", page)
        fill = round(min(height, ac.PAGE_HEIGHT_PX) / ac.PAGE_HEIGHT_PX * 100, 1)
        ac.log_resume(self.root, company=task.jd.company_dir.name, role=task.title or title, url=task.url,
                      fill=fill, pages=pages, file_name=target.name, today=self.today())
        fit = f"{pages} page, {fill}% full" + (f", {len(short)} bullet(s) shortened to fit" if short else "")
        if dropped:
            fit += f"; dropped to fit one page: {'; '.join(dropped)}"
        if pages > 1:
            fit += " (still over one page: shorten a bullet or ask for a tighter version)"
        report = "\n".join(x for x in (draft.report.strip(), f"Layout: {fit}.",
                                       f"Model: {task.model} ({usage_line(usage, seconds)}).") if x)
        return [target], report

    def cover_letter(self, task: Task) -> tuple[list[Path], str]:
        personalization = self._personalization()
        prompt = ac.letter_prompt(
            jd=self._jd_text(task.jd), tailored_resume=self._tailored_resume(task),
            personalization=personalization,
            style=self._read(RESUME_DIR / "Writing Style.md", required=False),
            letter_rules=self._read(RESUME_DIR / "coverletter.md", required=False),
            notes=task.notes, research=task.research)
        data, usage, seconds = self._call(self._system(personalization), prompt, model=task.model,
                                          research=task.research)
        draft = ac.validate_letter(data)
        contact = self.contact_loader()
        if not contact.name:
            raise ac.CompositionError("add your name to the contact: block in config/candidate_profile.yaml")
        stem = f"{contact.name} Cover Letter"
        if (task.jd.company_dir / f"{stem}.pdf").exists() and draft.role_word.strip():
            stem = f"{stem} {ac.plain_title(draft.role_word)}"
        target = ac.unique_path(task.jd.company_dir, stem)
        page = ac.render_letter(draft, contact, ac.portfolio_url(personalization), self.today())
        with self.renderer_factory() as renderer:
            pages = renderer.pdf(page, target)
        ac.set_pdf_metadata(target, {"title": target.stem, "author": contact.name,
                                     "subject": f"Cover letter for {draft.company}"})
        words = ac.word_count(draft.paragraphs)
        sources = "\n".join(f"- {s}" for s in draft.sources)
        report = "\n".join(x for x in (
            draft.report.strip(), f"Body: {words} words, {pages} page.",
            f"Sources:\n{sources}" if sources else ("No company research (unchecked)." if not task.research else ""),
            f"Model: {task.model} ({usage_line(usage, seconds)}).") if x)
        return [target], report

    def _tailored_resume(self, task: Task) -> str:
        """The tailored resume for this company if one exists (newest), else "" (the master resume is
        already in the system prompt)."""
        pdfs = sorted((p for p in task.jd.company_dir.glob("*Resume*.pdf")), key=lambda p: p.stat().st_mtime)
        if pdfs:
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(pdfs[-1]).pages).strip()
            if text:
                return text
        return ""


class TaskQueue:
    """Runs one composition at a time on a worker thread."""

    def __init__(self, project_root: Path, composer: Composer):
        self.project_root = Path(project_root).resolve()
        self.composer = composer
        self._tasks: dict[str, Task] = {}
        self._pending: deque[str] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stopped = False
        self._worker = threading.Thread(target=self._loop, name="just-fill-it-worker", daemon=True)
        self._worker.start()

    def submit(self, page: PageJob, jd: SavedJd) -> Task:
        task = Task(uuid.uuid4().hex[:12], page.kind, jd, notes=page.notes, model=page.model,
                    research=page.research, title=page.title, url=page.url)
        with self._wake:
            self._tasks[task.id] = task
            self._pending.append(task.id)
            self._trim()
            self._wake.notify()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def stop(self) -> None:
        with self._wake:
            self._stopped = True
            self._wake.notify()

    def _trim(self) -> None:
        finished = [t for t in self._tasks.values() if t.status in (TaskStatus.DONE, TaskStatus.FAILED)]
        for task in sorted(finished, key=lambda t: t.created_at)[: max(0, len(self._tasks) - MAX_TASKS_KEPT)]:
            del self._tasks[task.id]

    def _loop(self) -> None:
        while True:
            with self._wake:
                while not self._pending and not self._stopped:
                    self._wake.wait()
                if self._stopped:
                    return
                task = self._tasks[self._pending.popleft()]
                task.status = TaskStatus.RUNNING
            self._run(task)

    def _run(self, task: Task) -> None:
        try:
            make = self.composer.resume if task.kind is Kind.RESUME else self.composer.cover_letter
            task.files, task.report = make(task)
            task.status = TaskStatus.DONE
        except subprocess.TimeoutExpired:
            task.error = f"timed out after {round(self.composer.timeout / 60)} minutes"
            task.status = TaskStatus.FAILED
        except ac.CompositionError as exc:
            task.error = str(exc)
            task.status = TaskStatus.FAILED
        except Exception as exc:  # noqa: BLE001 - surfaced to the extension, never crashes the worker
            task.error = f"{type(exc).__name__}: {exc}"
            task.status = TaskStatus.FAILED
        finally:
            task.finished_at = time.time()


def claude_available(claude_bin: str) -> bool:
    return shutil.which(claude_bin) is not None
