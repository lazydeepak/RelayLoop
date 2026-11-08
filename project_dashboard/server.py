#!/usr/bin/env python3
"""Standalone, portable Project Dashboard.

Copy this folder into any project and run:
    python3 server.py --root /path/to/project --port 5577
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME = Path.home() / ".local/state/projectdashboard/standalone"


def project_files(root):
    ignored = {".git", "node_modules", "__pycache__", ".pytest_cache"}
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [n for n in dirnames if n not in ignored]
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                result[str(path.relative_to(root))] = path.stat().st_mtime
            except OSError:
                continue
    return result


class ProgressStore:
    def __init__(self, runtime):
        self.runtime = Path(runtime).expanduser().resolve()
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.runtime / "project_progress.json"
        self.lock = threading.RLock()
        self.data = self.load()

    def now(self):
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())

    def normalize(self, data):
        for item in data.get("milestones", []):
            item.setdefault("priority", 5)
            try:
                item["priority"] = int(item.get("priority", 5))
            except (TypeError, ValueError):
                item["priority"] = 5
        for item in data.get("jobs", []):
            item.setdefault("priority", 5)
            try:
                item["priority"] = int(item.get("priority", 5))
            except (TypeError, ValueError):
                item["priority"] = 5
        return data

    def default(self):
        now = self.now()
        return self.normalize({
            "project": "Project Dashboard",
            "created_at": now,
            "updated_at": now,
            "milestones": [],
            "jobs": [],
            "file_changes": [],
            "tests": [],
            "corrections": [],
            "events": [{"id": "e1", "kind": "milestone", "title": "Progress tracking initialized", "timestamp": now, "details": "Empty project dashboard created."}],
        })

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
            if isinstance(data, dict):
                for key in ("milestones", "jobs", "file_changes", "tests", "corrections", "events"):
                    data.setdefault(key, [])
                return self.normalize(data)
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, OSError):
            pass
        data = self.default()
        self.save(data)
        return data

    def save(self, data=None):
        data = data or self.data
        data["updated_at"] = self.now()
        fd, temporary = tempfile.mkstemp(prefix=".project-progress-", dir=self.runtime)
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def status(self):
        with self.lock:
            data = json.loads(json.dumps(self.data))
            milestones = data.get("milestones", [])
            jobs = data.get("jobs", [])
            tests = data.get("tests", [])
            changes = data.get("file_changes", [])
            data["summary"] = {
                "milestones": {
                    "total": len(milestones),
                    "done": sum(1 for m in milestones if m.get("status") == "done"),
                    "in_progress": sum(1 for m in milestones if m.get("status") == "in_progress"),
                    "pending": sum(1 for m in milestones if m.get("status") == "pending"),
                    "corrected": sum(1 for m in milestones if m.get("status") == "corrected"),
                },
                "jobs": {
                    "total": len(jobs),
                    "done": sum(1 for j in jobs if j.get("status") == "done"),
                    "in_progress": sum(1 for j in jobs if j.get("status") == "in_progress"),
                    "pending": sum(1 for j in jobs if j.get("status") == "pending"),
                    "failed": sum(1 for j in jobs if j.get("status") == "failed"),
                },
                "tests": {
                    "total": len(tests),
                    "pass": sum(1 for t in tests if t.get("status") == "pass"),
                    "fail": sum(1 for t in tests if t.get("status") == "fail"),
                },
                "file_changes": len(changes),
                "corrections": len(data.get("corrections", [])),
            }
            data["recent_events"] = list(reversed(data.get("events", [])[-50:]))
            data["recent_file_changes"] = list(reversed(changes[-50:]))
            return data

    def record(self, payload):
        payload = payload or {}
        kind = str(payload.get("kind") or payload.get("type") or "event").strip().lower()
        now = self.now()
        with self.lock:
            if kind == "milestone":
                existing = next((m for m in self.data["milestones"] if m["id"] == payload.get("id")), None) if payload.get("id") else None
                if "priority" in payload:
                    try:
                        priority = int(payload.get("priority", 5))
                    except (TypeError, ValueError):
                        priority = 5
                elif existing:
                    priority = existing.get("priority", 5)
                else:
                    priority = 5
                milestone = {
                    "id": payload.get("id") or f"m{len(self.data['milestones']) + 1}",
                    "title": str(payload.get("title") or "Untitled milestone").strip(),
                    "status": str(payload.get("status") or "pending").strip(),
                    "priority": priority,
                    "description": str(payload.get("description") or "").strip(),
                    "created_at": now,
                    "updated_at": now,
                }
                existing = next((m for m in self.data["milestones"] if m["id"] == milestone["id"]), None)
                if existing:
                    existing.update({k: v for k, v in milestone.items() if k != "created_at"})
                    existing["updated_at"] = now
                    milestone = existing
                else:
                    self.data["milestones"].append(milestone)
                self.data["events"].append({"id": f"e{len(self.data['events']) + 1}", "kind": "milestone", "title": milestone["title"], "timestamp": now, "details": milestone["status"]})
            elif kind == "job":
                existing_job = next((j for j in self.data["jobs"] if j["id"] == payload.get("id")), None) if payload.get("id") else None
                if "priority" in payload:
                    try:
                        job_priority = int(payload.get("priority", 5))
                    except (TypeError, ValueError):
                        job_priority = 5
                elif existing_job:
                    job_priority = existing_job.get("priority", 5)
                else:
                    job_priority = 5
                job = {
                    "id": payload.get("id") or f"j{len(self.data['jobs']) + 1}",
                    "title": str(payload.get("title") or "Untitled job").strip(),
                    "kind": str(payload.get("job_kind") or payload.get("job_type") or "edit").strip(),
                    "priority": job_priority,
                    "files": list(payload.get("files") or []),
                    "intended": str(payload.get("intended") or "").strip(),
                    "status": str(payload.get("status") or "done").strip(),
                    "timestamp": now,
                    "result": str(payload.get("result") or "").strip(),
                    "correction_of": payload.get("correction_of"),
                }
                existing = next((j for j in self.data["jobs"] if j["id"] == job["id"]), None)
                if existing:
                    existing.update(job)
                    existing["timestamp"] = now
                    job = existing
                else:
                    self.data["jobs"].append(job)
                self.data["events"].append({"id": f"e{len(self.data['events']) + 1}", "kind": "job", "title": job["title"], "timestamp": now, "details": f"{job['status']} · {', '.join(job['files'])}"})
            elif kind == "test":
                test = {"name": str(payload.get("name") or "Unnamed test").strip(), "status": str(payload.get("status") or "pass").strip(), "timestamp": now, "details": str(payload.get("details") or "").strip()}
                self.data["tests"].append(test)
                self.data["events"].append({"id": f"e{len(self.data['events']) + 1}", "kind": "test", "title": test["name"], "timestamp": now, "details": f"{test['status']} · {test['details']}"})
            elif kind == "correction":
                correction = {"id": f"c{len(self.data['corrections']) + 1}", "target_type": str(payload.get("target_type") or "job").strip(), "target_id": str(payload.get("target_id") or "").strip(), "reason": str(payload.get("reason") or "").strip(), "timestamp": now}
                self.data["corrections"].append(correction)
                self.data["events"].append({"id": f"e{len(self.data['events']) + 1}", "kind": "correction", "title": f"Correction: {correction['target_type']} {correction['target_id']}", "timestamp": now, "details": correction["reason"]})
                collection = self.data["milestones"] if correction["target_type"] == "milestone" else self.data["jobs"]
                for item in collection:
                    if item.get("id") == correction["target_id"]:
                        item["status"] = "corrected"
                        item["updated_at" if "updated_at" in item else "timestamp"] = now
            elif kind == "file_change":
                change = {"path": str(payload.get("path") or "").strip(), "mtime": payload.get("mtime"), "status": str(payload.get("status") or "changed").strip(), "detected_at": now}
                self.data["file_changes"].append(change)
                self.data["file_changes"] = self.data["file_changes"][-500:]
                self.data["events"].append({"id": f"e{len(self.data['events']) + 1}", "kind": "file_change", "title": change["path"], "timestamp": now, "details": change["status"]})
                self.data["events"] = self.data["events"][-500:]
            else:
                self.data["events"].append({"id": f"e{len(self.data['events']) + 1}", "kind": kind, "title": str(payload.get("title") or kind).strip(), "timestamp": now, "details": str(payload.get("details") or "").strip()})
                self.data["events"] = self.data["events"][-500:]
            self.save()
            return {"ok": True, "kind": kind, "updated_at": self.data["updated_at"]}


class Handler(BaseHTTPRequestHandler):
    def app(self):
        return self.server.app

    def json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/project":
            return self.json(200, self.app().store.status())
        if path == "/api/csrf":
            return self.json(200, {"csrf": self.app().csrf})
        if path in ("/", "/index.html", "/project-dashboard", "/project_dashboard.html"):
            page = self.app().html_path
            if page.exists():
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_error(404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(size) or b"{}")
        path = self.path.split("?")[0]
        if path == "/api/project/record":
            return self.json(200, self.app().store.record(data))
        self.send_error(404)

    def log_message(self, *args):
        pass


class ProjectDashboardApp:
    def __init__(self, root, runtime):
        self.root = Path(root).resolve()
        self.store = ProgressStore(runtime)
        self.csrf = secrets.token_urlsafe(32)
        self.html_path = Path(__file__).resolve().parent / "dashboard.html"
        self.stop_event = threading.Event()
        self.watcher = threading.Thread(target=self.watch, daemon=True)
        self.watcher.start()

    def watch(self):
        known = project_files(self.root)
        while not self.stop_event.is_set():
            time.sleep(5)
            current = project_files(self.root)
            for path, mtime in current.items():
                if path not in known or known.get(path) != mtime:
                    self.store.record({"kind": "file_change", "path": path, "mtime": mtime, "status": "modified" if path in known else "added"})
            for path in known:
                if path not in current:
                    self.store.record({"kind": "file_change", "path": path, "status": "deleted"})
            known = current


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    parser.add_argument("--port", type=int, default=5577)
    args = parser.parse_args()
    app = ProjectDashboardApp(args.root, args.runtime)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.app = app
    url = f"http://127.0.0.1:{server.server_port}/"
    print(url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
PY