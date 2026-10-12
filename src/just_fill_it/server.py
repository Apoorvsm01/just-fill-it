"""Local backend for the job-hunter Chrome extension (`extension/`): one click on a job page saves
the posting as a JD file and runs Claude Code headless with the resume-generator or outreach-writer
skill, then hands the finished PDF back to the extension to drop into the application form.

Each run is one tool-free Claude call on a compact context pack ; Python
lays out the PDF (just_fill_it.compose). The extension picks Fast (Sonnet) or Best (Opus) per run.

Loopback only, and every /api request except /api/health must carry the token from
`state/token` in this repo (its path is printed at startup; paste it into the extension's settings once), so
other websites open in the browser cannot start runs. Job text is saved as a file and reaches
Claude only inside the piped prompt; the call has no tools except optional WebSearch.

Usage:
    uv run just-fill-it                                   # job-hunter next to this repo
    uv run just-fill-it --job-hunter /path/to/job-hunter  # or point at it
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import sys
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from job_hunter.rootutil import chdir_to_project_root
from job_hunter.runlock import RunLockHeld, run_lock
from pydantic import ValidationError

from .helper import Composer, PageJob, TaskQueue, load_or_create_token, save_page_jd

MAX_BODY_BYTES = 1024 * 1024
# The helper's own state (token, draft HTML) stays in this repo, never in job-hunter.
REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / "state"
OUTPUT_ROOT = Path("data/output")
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8767, help="loopback port (default 8767)")
    parser.add_argument("--claude-bin", default="claude", help="path to the claude CLI (default: found on PATH)")
    parser.add_argument("--timeout-minutes", type=float, default=10, help="per-run limit (default 10)")
    parser.add_argument("--job-hunter", dest="project", default=None,
                        help="job-hunter project folder (default: $JOB_HUNTER_ROOT, else ../job-hunter next to this repo)")
    return parser


def default_job_hunter() -> str | None:
    """`../job-hunter` beside this repo, when it exists (the usual layout)."""
    sibling = Path(__file__).resolve().parents[3] / "job-hunter"
    return str(sibling) if (sibling / "pyproject.toml").is_file() else None


def origin_allowed(origin: str | None) -> bool:
    """Requests come from the extension's service worker (chrome-extension:// or Safari's
    safari-web-extension:// origin) or from tools with no Origin at all (curl). A web page's origin
    is always refused."""
    return origin is None or origin.startswith(("chrome-extension://", "safari-web-extension://"))


def token_matches(header: str | None, token: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].strip().encode(), token.encode())


class HelperServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, token: str, queue: TaskQueue, project_root: Path):
        super().__init__(("127.0.0.1", port), HelperHandler)
        self.token = token
        self.queue = queue
        self.project_root = project_root
        self.allowed_hosts = {f"127.0.0.1:{self.server_address[1]}", f"localhost:{self.server_address[1]}"}


class HelperHandler(BaseHTTPRequestHandler):
    server: HelperServer
    server_version = "just-fill-it"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write(f"[just-fill-it] {self.address_string()} {format % args}\n")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _guard(self, *, auth: bool = True) -> bool:
        if (self.headers.get("Host") or "").lower() not in self.server.allowed_hosts:
            self._json(403, {"error": "bad Host header"})
            return False
        if not origin_allowed(self.headers.get("Origin")):
            self._json(403, {"error": "requests from web pages are not allowed"})
            return False
        if auth and not token_matches(self.headers.get("Authorization"), self.server.token):
            self._json(401, {"error": "missing or wrong token: paste state/token (in the Just Fill it! folder) into the extension settings"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        try:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            if parts == ["api", "health"]:
                if self._guard(auth=False):
                    self._json(200, {"ok": True})
                return
            if not self._guard():
                return
            if parts == ["api", "ping"]:
                self._json(200, {"ok": True, "authorized": True})
            elif len(parts) == 3 and parts[:2] == ["api", "tasks"]:
                task = self.server.queue.get(parts[2])
                if task is None:
                    self._json(404, {"error": "unknown task"})
                else:
                    self._json(200, task.public(self.server.project_root))
            elif len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "files":
                self._file(parts[2], parts[4])
            elif parts == ["api", "file"]:
                self._file_by_path(parse_qs(urlsplit(self.path).query).get("path", [""])[0])
            else:
                self._json(404, {"error": "not found"})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": "internal error (see the server terminal)"})

    def _file(self, task_id: str, index: str) -> None:
        task = self.server.queue.get(task_id)
        if task is None or not index.isdigit() or int(index) >= len(task.files):
            self._json(404, {"error": "unknown file"})
            return
        path = task.files[int(index)]
        output_root = (self.server.project_root / OUTPUT_ROOT).resolve()
        if not path.resolve().is_relative_to(output_root) or not path.is_file():
            self._json(404, {"error": "file is gone"})
            return
        self._send_file(path)

    def _file_by_path(self, relative: str) -> None:
        """A past run's file, by its project-relative path (the extension's history survives a
        helper restart, which forgets in-memory tasks). Only result files under data/output/."""
        output_root = (self.server.project_root / OUTPUT_ROOT).resolve()
        path = (self.server.project_root / relative).resolve() if relative else output_root
        if (not path.is_relative_to(output_root) or path.suffix.lower() not in CONTENT_TYPES
                or not path.is_file()):
            self._json(404, {"error": "file not found (moved or deleted?)"})
            return
        self._send_file(path)

    def _send_file(self, path: Path) -> None:
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, path.read_bytes(), content_type,
                   {"X-File-Name": path.name.encode("ascii", "replace").decode()})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if urlsplit(self.path).path != "/api/generate":
                self._json(404, {"error": "not found"})
                return
            if not self._guard():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(413, {"error": "body missing or too large"})
                return
            try:
                page = PageJob.model_validate_json(self.rfile.read(length))
            except ValidationError as exc:
                self._json(422, {"error": "invalid request", "details": json.loads(exc.json())})
                return
            jd = save_page_jd(page, project_root=self.server.project_root,
                              output_root=self.server.project_root / OUTPUT_ROOT, today=date.today())
            task = self.server.queue.submit(page, jd)
            self._json(202, task.public(self.server.project_root))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": "internal error (see the server terminal)"})


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = chdir_to_project_root(args.project or os.environ.get("JOB_HUNTER_ROOT") or default_job_hunter()).resolve()
    if not (root / "config").is_dir():
        print(f"just-fill-it: {root} doesn't look like a job-hunter project; pass --job-hunter", file=sys.stderr)
        return 2
    if shutil.which(args.claude_bin) is None:
        print(f"just-fill-it: '{args.claude_bin}' not found on PATH; pass --claude-bin", file=sys.stderr)
        return 2
    load_or_create_token(STATE_DIR / "token")
    token = (STATE_DIR / "token").read_text(encoding="utf-8").strip()
    try:
        with run_lock("just-fill-it"):
            queue = TaskQueue(root, Composer(root, work_root=STATE_DIR / "work", claude_bin=args.claude_bin,
                                            timeout=args.timeout_minutes * 60))
            try:
                server = HelperServer(args.port, token, queue, root)
            except OSError as exc:
                print(f"just-fill-it: cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
                return 2
            print(f"Just Fill it! helper listening on http://127.0.0.1:{server.server_address[1]} (Ctrl+C to stop)")
            print(f"Extension token: {STATE_DIR / 'token'} (paste it into the extension settings once)", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nStopping.")
            finally:
                queue.stop()
                server.server_close()
    except RunLockHeld as exc:
        print(f"just-fill-it: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
