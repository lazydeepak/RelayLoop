#!/usr/bin/env python3
"""Loopback-only dashboard for the independent single-pair relay."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from relayloop_runner import LiveAdapters, RelayRunner, _empty_state

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "controller/releases/47bb3b7/safeguarded-cli.tar.gz"
CHECKSUM = ROOT / "controller/releases/47bb3b7/safeguarded-cli.tar.gz.sha256"
DEFAULT_RUNTIME = Path.home() / ".local/state/relayloop/standalone"
DEFAULT_PORT = 5566
DEFAULTS = {
    "planner_url": "https://chatgpt.com/g/g-p-6ab749f725548191934d1e5c4da00f2a-bot/c/6ae4d33a-bee0-83ec-9308-7d48b781c915",
    "worker_session": "ses_e68a35de3ffep9JYScN5236MsG",
}


def validate_settings(data):
    data = data or {}
    url = str(data.get("planner_url") or "").strip()
    session = str(data.get("worker_session") or "").strip()
    parsed = urlparse(url)
    errors = {}
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or "/c/" not in parsed.path or not parsed.path.split("/c/", 1)[1].strip("/"):
        errors["planner_url"] = "Planner URL must be an http(s) conversation URL"
    if not session or any(c.isspace() for c in session):
        errors["worker_session"] = "Worker session ID is required and may not contain spaces"
    if errors: raise PairValidationError(errors)
    return {"planner_url": url, "worker_session": session}


def normalize_planner_url(value):
    parsed = urlparse(str(value).strip())
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/").lower(), "", "", ""))


def _patch_extractor_source(text):
    text = text.replace("conversation_url: page.url(),", "conversation_url: page.url(),\n        conversation_id: convId,\n        latest_message_is_assistant: snap?.latest_message_is_assistant === true,")
    prompt_anchor = "        extracted_text: snap.text,"
    prompt_field = '''        extracted_text: snap.text,
        writing_blocks: await page.evaluate((messageId) => {
          const roots = [...document.querySelectorAll('[data-content-search-unit-key*="assistant"], [data-message-author-role="assistant"], [data-dil-message-id], [data-message-id]')];
          const root = roots.find((node) => {
            const ids = (node.getAttribute('data-chatgpt-search-message-ids') || '').split(/\\s+/);
            return ids.includes(messageId) || node.getAttribute('data-dil-message-id') === messageId
              || node.getAttribute('data-message-id') === messageId;
          });
          if (!root) return [];
          return [...root.querySelectorAll('[data-client-defined-widget="writing_block"]')].map((widget) => {
            const surface = widget.querySelector('[data-markdown-copy-text]');
            return surface?.getAttribute('data-markdown-copy-text') || '';
          }).filter((value) => value.trim());
        }, snap.message_identifier),'''
    if prompt_anchor not in text:
        raise RuntimeError("pinned ChatGPT extractor has an unsupported success-output format")
    return text.replace(prompt_anchor, prompt_field, 1)


def _prepare_runtime(runtime):
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    adapter_dir = runtime / "adapters"
    adapter_dir.mkdir(exist_ok=True, mode=0o700)
    expected = CHECKSUM.read_text().split()[0]
    if hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != expected:
        raise RuntimeError("pinned extraction adapter archive checksum mismatch")
    wanted = {"relay-beacon-cli/extract_chatgpt.js", "relay-beacon-cli/chatgpt_dom.js", "relay-beacon-cli/chatgpt_tab_state.js"}
    with tarfile.open(ARCHIVE, "r:gz") as archive:
        for member in archive.getmembers():
            if member.name not in wanted: continue
            if not member.isfile(): raise RuntimeError("unsafe adapter archive member")
            target = adapter_dir / Path(member.name).name
            source = archive.extractfile(member)
            text = source.read().decode("utf8")
            if target.name == "extract_chatgpt.js":
                text = _patch_extractor_source(text)
            target.write_text(text, encoding="utf8")
    shutil.copy2(ROOT / "send_chatgpt.js", adapter_dir / "send_chatgpt.js")
    shutil.copy2(ROOT / "chatgpt_composer.mjs", adapter_dir / "chatgpt_composer.mjs")
    package = runtime / "package.json"
    if not package.exists():
        package.write_text(json.dumps({"private": True, "type": "module", "dependencies": {"playwright": "1.64.0"}}, indent=2) + "\n")
        os.chmod(package, 0o600)
    if not (runtime / "node_modules/playwright").exists():
        result = subprocess.run(["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
                                cwd=runtime, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError("RelayLoop adapter dependency installation failed: " + (result.stderr or result.stdout)[-1200:])
    return adapter_dir


class App:
    def __init__(self, runtime=None, runner=None, adapters=None):
        self.runtime = Path(runtime or DEFAULT_RUNTIME).expanduser().resolve()
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings_path = self.runtime / "settings.json"
        self.lock = threading.RLock()
        self.operation_busy = False
        self.csrf = secrets.token_urlsafe(32)
        self.last_error = None
        self._proc_runner = runner or subprocess.run
        self.adapter_dir = _prepare_runtime(self.runtime) if adapters is None else self.runtime / "adapters"
        self.adapters = adapters or LiveAdapters(self.adapter_dir, runner=self._proc_runner)
        self.pairs = []
        self.selected_pair_id = None
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf8"))
            if isinstance(saved, dict):
                legacy = validate_settings(saved)
                self.pairs = list(saved.get("pairs") or [])
                selected = saved.get("selected_pair_id")
                if not self.pairs:
                    self.pairs = [{"id": secrets.token_urlsafe(18), "name": "Core plus plugin architecture", **legacy}]
                    selected = self.pairs[0]["id"]
                    self._write_profiles(self.pairs, selected)
                self.selected_pair_id = selected if any(p.get("id") == selected for p in self.pairs) else self.pairs[0]["id"]
                selected_pair = next(p for p in self.pairs if p["id"] == self.selected_pair_id)
                self.settings = validate_settings(selected_pair)
        except FileNotFoundError: pass
        except (ValueError, TypeError): self.last_error = "Saved settings are invalid; review both connection fields"
        if not hasattr(self, "settings"):
            self.settings = dict(DEFAULTS)
            self.pairs = [{"id": secrets.token_urlsafe(18), "name": "Core plus plugin architecture", **self.settings}]
            self.selected_pair_id = self.pairs[0]["id"]
            self._write_profiles(self.pairs, self.selected_pair_id)
        self.relay = self._make_relay()

    def _write_profiles(self, pairs, selected_id):
        selected = next(p for p in pairs if p["id"] == selected_id)
        payload = {"schema": 2, "pairs": pairs, "selected_pair_id": selected_id,
                   "planner_url": selected["planner_url"], "worker_session": selected["worker_session"]}
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.runtime)
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(payload, stream, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, 0o600); os.replace(temporary, self.settings_path)
        finally:
            try: os.unlink(temporary)
            except FileNotFoundError: pass

    def _make_relay(self):
        return RelayRunner(self.runtime, self.settings["planner_url"], self.settings["worker_session"],
                           self.adapters, on_event=self._record_event)

    def _record_event(self, row):
        if row.get("level") == "error": self.last_error = row.get("message")

    def timeline(self):
        return self.relay.status()["activity"]

    def status(self):
        relay = self.relay.status()
        worker_observation = {"state": "unknown", "busy": None, "latest_completed_id": None}
        try:
            observed_worker = self.adapters.worker_state(self.settings["worker_session"])
            busy = bool(observed_worker.get("busy"))
            latest = observed_worker.get("latest_completed") or observed_worker.get("latest")
            worker_observation = {
                "state": "busy" if busy else "idle",
                "busy": busy,
                "latest_completed_id": latest.get("id") if isinstance(latest, dict) else None,
            }
        except Exception:
            # Status rendering must remain available when OpenCode cannot be
            # queried; the UI can distinguish unavailable from observed idle.
            pass
        pending = relay["pending"]
        unresolved = relay["unresolved"]
        phase = "unresolved" if unresolved else (relay.get("phase") or "stopped")
        if phase == "unresolved":
            extraction = "Delivery unresolved — held"
        elif pending and pending.get("direction") == "worker_to_planner" and pending.get("stage") in (
                "waiting_for_planner_ready", "planner_ready_timeout", "planner_readiness_error"):
            extraction = "Worker report queued; Planner delivery was not attempted"
        elif pending and pending.get("direction") == "planner_to_worker":
            extraction = "Planner message extracted"
        elif pending and pending.get("direction") == "worker_to_planner":
            extraction = "Worker response extracted"
        elif pending and pending.get("direction") == "relayloop_to_planner":
            extraction = "Requesting a Worker prompt from Planner"
        elif phase == "worker_prompt_unavailable":
            extraction = "Worker prompt not available"
        elif phase == "waiting_for_worker":
            extraction = "Waiting for Worker completion"
        elif phase == "waiting_for_planner_completion":
            extraction = "Planner response is still generating"
        elif phase == "waiting_for_planner":
            extraction = "Waiting for a new completed Planner message"
        elif phase == "planner_timeout":
            extraction = "Planner response window expired"
        else:
            extraction = "Idle"
        deadline = relay.get("planner_wait_deadline")
        wait_remaining = max(0, int(deadline - time.time())) if deadline else None
        turn = {
            "phase": phase,
            "expected_speaker": relay["expected_speaker"],
            "pending_direction": pending.get("direction") if pending else None,
            "pending_source_id": pending.get("source_id") if pending else None,
            "pending_stage": pending.get("stage") if pending else None,
            "last_planner_id": relay["last_planner_id"],
            "last_worker_id": relay["last_worker_id"],
            "last_confirmed_delivery": relay["last_confirmed_delivery"],
            "last_confirmed_turn": relay["last_confirmed_turn"],
            "planner_wait_seconds_remaining": wait_remaining,
        }
        return {"status": relay["status"], "expected_turn": relay["expected_speaker"],
                "worker_observation": worker_observation,
                "pending": pending, "phase": phase, "turn": turn,
                "unresolved": unresolved,
                "last_confirmed_delivery": relay["last_confirmed_delivery"],
                "last_confirmed_turn": relay["last_confirmed_turn"],
                "last_planner_id": relay["last_planner_id"], "last_worker_id": relay["last_worker_id"],
                "extraction": extraction,
                "delivery": (unresolved.get("stage") or "Delivery unresolved — held") if unresolved else
                    (pending["stage"] if pending else ("Worker prompt not available" if phase == "worker_prompt_unavailable" else
                     "No pending delivery" if phase != "planner_timeout" else "Planner response timed out")),
                # Errors and holds belong only to this process/session.
                "error": self.last_error,
                "timeline": relay["activity"], "pairs": self.pairs, "selected_pair_id": self.selected_pair_id,
                "settings": self.settings, "now": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())}

    def save(self, data):
        with self.lock:
            self._ensure_unlocked()
            settings = validate_settings(data)
            current = next(p for p in self.pairs if p["id"] == self.selected_pair_id)
            candidate = {**current, **settings}
            self._check_duplicate(candidate, current)
            pairs = [candidate if p is current else p for p in self.pairs]
            self._write_profiles(pairs, self.selected_pair_id)
            self.pairs = pairs; self.settings = settings; self.relay = self._make_relay()
            self.last_error = None
            return dict(settings)

    def _ensure_unlocked(self, check_busy=True):
        state = self.relay.status()
        if (check_busy and self.operation_busy) or state["status"] != "stopped" or state.get("pending") or state.get("unresolved"):
            raise ValueError("Stop automation and resolve pending delivery before changing pair profiles")

    def save_pair(self, data):
        with self.lock:
            return self._save_pair(data)

    def _check_duplicate(self, profile, exclude=None):
        errors = {}
        for p in self.pairs:
            if p is exclude: continue
            if p["name"].strip().casefold() == profile["name"].strip().casefold(): errors["name"] = "Name is already used"
            if normalize_planner_url(p["planner_url"]) == normalize_planner_url(profile["planner_url"]): errors["planner_url"] = "Planner URL is already used"
            if p["worker_session"].strip() == profile["worker_session"].strip(): errors["worker_session"] = "Worker session ID is already used"
        if errors: raise PairValidationError(errors)

    def _save_pair(self, data):
        self._ensure_unlocked()
        name = str(data.get("name", "")).strip()
        settings = validate_settings(data)
        pair_id = data.get("id")
        existing = next((p for p in self.pairs if p["id"] == pair_id), None) if pair_id else None
        if pair_id and not existing: raise ValueError("Unknown pair profile")
        if existing and existing["id"] != self.selected_pair_id:
            raise ValueError("Only the selected pair profile can be updated")
        profile = {"id": existing["id"] if existing else secrets.token_urlsafe(18), "name": name, **settings}
        if not name: raise PairValidationError({"name": "Name is required"})
        self._check_duplicate(profile, existing)
        pairs = [profile if p is existing else p for p in self.pairs]
        if not existing: pairs.append(profile)
        selected = profile["id"] if not existing or profile["id"] == self.selected_pair_id else self.selected_pair_id
        self._write_profiles(pairs, selected)
        self.pairs, self.selected_pair_id = pairs, selected
        self.settings = validate_settings(next(p for p in pairs if p["id"] == selected))
        self.relay = self._make_relay()
        return {"ok": True, "pair": profile}

    def select_pair(self, pair_id):
        with self.lock:
            self._ensure_unlocked()
            profile = next((p for p in self.pairs if p["id"] == pair_id), None)
            if not profile: raise ValueError("Unknown pair profile")
            self._write_profiles(self.pairs, pair_id)
            self.selected_pair_id = pair_id; self.settings = validate_settings(profile); self.relay = self._make_relay()
            return {"ok": True, "settings": self.settings}


    def verify(self, data=None):
        try:
            self._ensure_unlocked(check_busy=False)
            settings = validate_settings(data or self.settings)
            result = self.adapters.verify(settings["planner_url"], settings["worker_session"])
            if result.get("ok") is not True: raise RuntimeError(result.get("error") or "Connection verification failed")
            self.last_error = None
            self.relay._event("verify", "Planner conversation and Worker session identities verified")
            return {"ok": True, **result}
        except Exception as exc:
            self.last_error = str(exc)
            self.relay._event("verify", str(exc), "error")
            return {"ok": False, "error": str(exc)}

    def mutate(self, action, data=None):
        with self.lock:
            if self.operation_busy: raise RuntimeError("Another dashboard operation is already in progress")
            self.operation_busy = True
        try:
            if action == "verify":
                result = self.verify(data)
                if not result.get("ok"):
                    raise ValueError(result.get("error") or "Connection verification failed")
                return result
            if action == "refresh":
                relay_status = self.relay.status()
                if relay_status["status"] != "stopped":
                    raise RuntimeError("Stop automation before refreshing Planner or clearing session state")
                cleared = bool(relay_status.get("pending") or relay_status.get("unresolved"))
                # This explicit operator action abandons the disposable run,
                # including ambiguous holds; Start will establish fresh
                # baselines and verify both destinations before execution.
                self.relay.state = _empty_state(self.settings["planner_url"], self.settings["worker_session"])
                self.last_error = None
                try:
                    self.adapters.refresh_planner(self.settings["planner_url"])
                except Exception as exc:
                    self.last_error = str(exc)
                    self.relay._event("refresh", f"Session state cleared; Planner refresh failed: {exc}", "error")
                    raise
                self.relay._event("refresh", "Planner page refreshed; pending and unresolved session state cleared")
                return {"ok": True, "status": "stopped", "planner_refreshed": True,
                        "session_state_cleared": True, "previous_hold_cleared": cleared}
            if action == "start":
                # A new run discards prior diagnostics and turn state before
                # verification, including a verification failure.
                self.relay.state = _empty_state(self.settings["planner_url"], self.settings["worker_session"])
                self.last_error = None
                self.relay.start()
                self.last_error = None
                return {"ok": True, "status": "running", "warning": "Live ChatGPT/OpenCode messaging is enabled"}
            if action == "stop":
                result = self.relay.stop()
                if not result["ok"]: self.last_error = result.get("warning")
                return result
            raise ValueError("Unsupported operation")
        except Exception as exc:
            self.last_error = str(exc)
            raise
        finally:
            with self.lock: self.operation_busy = False


class PairValidationError(ValueError):
    def __init__(self, errors): self.errors = errors; super().__init__("Pair validation failed")


class Handler(BaseHTTPRequestHandler):
    server_version = "RelayLoop/2.0"
    def app(self): return self.server.app
    def json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status": return self.json(200, self.app().status())
        if path == "/api/csrf": return self.json(200, {"csrf": self.app().csrf})
        if path == "/": path = "/index.html"
        if path == "/index.html":
            body = (ROOT / "index.html").read_bytes(); self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def do_POST(self):
        expected = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if self.headers.get("Origin") not in expected: return self.json(403, {"error": "same-origin request required"})
        if self.headers.get("X-CSRF-Token") != self.app().csrf: return self.json(403, {"error": "CSRF token required"})
        try:
            size = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(size) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/settings": result = {"ok": True, "settings": self.app().save(data)}
            elif path == "/api/pairs/save": result = self.app().save_pair(data)
            elif path == "/api/pairs/select": result = self.app().select_pair(data.get("id"))
            elif path in ("/api/start", "/api/stop", "/api/verify", "/api/refresh"): result = self.app().mutate(path.rsplit("/", 1)[1], data)
            else: return self.json(404, {"error": "not found"})
            return self.json(200, result)
        except Exception as exc: return self.json(409, {"error": str(exc), **({"field_errors": exc.errors} if isinstance(exc, PairValidationError) else {})})
    def log_message(self, *args): pass


def make_server(app, port=DEFAULT_PORT):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler); server.app = app; return server


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME)); parser.add_argument("--port", type=int, default=DEFAULT_PORT); parser.add_argument("--open", action="store_true")
    args = parser.parse_args(); app = App(args.runtime); server = make_server(app, args.port)
    url = f"http://127.0.0.1:{server.server_port}/"; print(url, flush=True)
    if args.open: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        app.relay.stop(); server.server_close()


if __name__ == "__main__": main()
