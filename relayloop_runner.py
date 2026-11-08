"""Small, single-pair Planner <-> OpenCode relay with disposable session state."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class PlannerNotReady(RuntimeError):
    """A pre-submit check found Planner busy; no send was attempted."""


class PlannerSubmissionError(RuntimeError):
    def __init__(self, message, submission_attempted):
        self.submission_attempted = bool(submission_attempted)
        super().__init__(message)


class PlannerReadinessTimeout(RuntimeError):
    pass


def _empty_state(url, session):
    return {
        "planner_url": url, "worker_session": session,
        "last_planner_id": None, "last_worker_id": None,
        "expected_speaker": "planner", "pending": None,
        "last_confirmed_turn": None, "unresolved": None,
        "prompt_recovery_attempted": False,
        "phase": "stopped", "planner_wait_deadline": None,
        "updated_at": time.time(), "activity": [],
    }


class LiveAdapters:
    """Real destinations. No call is made until the operator starts the relay."""
    WORKER_EXPORT_CACHE_SECONDS = 3.0

    def __init__(self, adapter_dir, opencode_bin="opencode", runner=subprocess.run):
        self.adapter_dir = Path(adapter_dir)
        self.opencode_bin = opencode_bin
        self.runner = runner
        self._worker_cache = {}

    @staticmethod
    def _json(result):
        for line in reversed((result.stdout or "").splitlines()):
            try: return json.loads(line)
            except ValueError: continue
        return {}

    def _extract(self, url):
        result = self.runner(["node", str(self.adapter_dir / "extract_chatgpt.js"), url],
                             capture_output=True, text=True, timeout=75,
                             env={**os.environ, "BOT_CHATGPT_URL": url})
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "Planner extraction failed")[-1500:])
        data = self._json(result)
        from urllib.parse import urlparse
        def conv_id(value):
            parts = urlparse(value or "").path.rstrip("/").split("/")
            return parts[-1] if len(parts) >= 2 and parts[-2] == "c" else None
        expected_id = conv_id(url)
        if not expected_id or conv_id(data.get("conversation_url")) != expected_id:
            raise RuntimeError("Planner extractor observed a different conversation")
        turn = next((m for m in reversed(data.get("all_turns", [])) if m.get("id")), None)
        return {
            "id": data.get("message_identifier"), "text": data.get("extracted_text", ""),
            "writing_blocks": data.get("writing_blocks") if isinstance(data.get("writing_blocks"), list) else [],
            "role": "assistant" if data.get("latest_message_is_assistant") else "other",
            "completed": data.get("completion_status") == "completed",
            "conversation_id": data.get("conversation_id") or expected_id,
            "diagnostics": data.get("diagnostics") or {}, "all_turns": data.get("all_turns") or [],
            "turn": turn,
        }

    def verify(self, url, session):
        planner = self._extract(url)
        diagnostics = planner.get("diagnostics", {})
        if not diagnostics.get("rendered") or diagnostics.get("tab_state", {}).get("authBlocked") is not False:
            raise RuntimeError("Planner conversation is not authenticated and rendered")
        planner_id = planner["conversation_id"]
        result = self.runner([self.opencode_bin, "api", "session.get", "--param", "sessionID=" + session],
                             capture_output=True, text=True, timeout=15, env=os.environ.copy())
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "Worker session lookup failed")[-1000:])
        payload = self._json(result).get("data", {})
        if payload.get("id") != session:
            raise RuntimeError("OpenCode returned a different Worker session")
        return {"ok": True, "planner_conversation_id": planner_id,
                "worker_session": payload["id"]}

    def planner_latest(self, url):
        return self._extract(url)

    def _worker_session_info(self, session):
        result = self.runner([self.opencode_bin, "api", "session.get", "--param", "sessionID=" + session],
                             capture_output=True, text=True, timeout=15, env=os.environ.copy())
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "Worker session lookup failed")[-1000:])
        payload = self._json(result).get("data", {})
        if payload.get("id") != session:
            raise RuntimeError("OpenCode returned a different Worker session")
        return payload

    def _worker_messages(self, session, updated=None):
        cached = self._worker_cache.get(session)
        if (updated is not None and cached and cached[0] == updated
                and time.monotonic() - cached[2] < self.WORKER_EXPORT_CACHE_SECONDS):
            return cached[1]
        fd, path = tempfile.mkstemp(prefix="relayloop-export-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf8") as output:
                result = self.runner([self.opencode_bin, "session", "export", session],
                                     stdout=output, stderr=subprocess.PIPE, text=True,
                                     timeout=30, env=os.environ.copy())
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or "Worker export failed")[-1000:])
            data = json.loads(Path(path).read_text(encoding="utf8"))
            messages = data.get("messages", [])
            if updated is not None:
                self._worker_cache[session] = (updated, messages, time.monotonic())
            return messages
        finally:
            try: os.unlink(path)
            except OSError: pass

    def worker_destination_messages(self, session):
        info = self._worker_session_info(session)
        messages = self._worker_messages(session, (info.get("time") or {}).get("updated"))
        return [{"id": (message.get("info") or {}).get("id", message.get("id")),
                 "text": self._worker_text(message),
                 "role": (message.get("info") or {}).get("role", message.get("type"))}
                for message in messages]

    @staticmethod
    def _worker_text(msg):
        info = msg.get("info", {}) if isinstance(msg, dict) else {}
        parts = msg.get("content", msg.get("parts", info.get("parts", [])))
        text = msg.get("text")
        if not text:
            text = "\n".join(p.get("text", "") for p in parts
                              if isinstance(p, dict) and p.get("type") == "text")
        return text or ""

    def worker_state(self, session):
        payload = self._worker_session_info(session)
        messages = self._worker_messages(session, (payload.get("time") or {}).get("updated"))
        timing = payload.get("time") or {}
        busy = bool(timing.get("busy") and timing.get("busy", 0) > timing.get("idle", 0))
        latest = self._completed_worker_message(messages)
        return {"busy": busy, "latest": None if busy else latest, "latest_completed": latest}

    def _completed_worker_message(self, messages):
        if not messages: return None
        for msg in reversed(messages):
            info = msg.get("info", {}) if isinstance(msg, dict) else {}
            role = info.get("role", msg.get("type"))
            if role != "assistant": continue
            text = self._worker_text(msg)
            done = bool((msg.get("time") or info.get("time") or {}).get("completed")) or messages[-1].get("type") == "idle"
            if text.strip() and done:
                return {"id": info.get("id", msg.get("id")), "text": text, "role": "assistant", "completed": True}
        return None

    def deliver_worker(self, session, text):
        result = self.runner([self.opencode_bin, "run", "--session", session, "--format", "json", text],
                             capture_output=True, text=True, env=os.environ.copy())
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "Worker delivery outcome is uncertain")[-1500:])
        # A successful OpenCode run is the delivery boundary. Do not impose an
        # extra exact-text/unique-message acceptance rule on its session export;
        # extract the completed assistant response there and relay it unchanged.
        return {"confirmed": True, "provenance": "opencode.run.completed"}

    def verify_delivery(self, url, require_complete=True):
        """Read-only Planner snapshot. This method never touches the composer."""
        return self._planner_snapshot(url, "verify", require_complete)

    def refresh_planner(self, url):
        """Refresh the Planner page, then take a read-only receipt snapshot."""
        return self._planner_snapshot(url, "verify-refresh", False)

    def planner_readiness(self, url, expected_text=None):
        """Read Planner generation/composer readiness without changing the page."""
        command = ["node", str(self.adapter_dir / "send_chatgpt.js"), "readiness", url]
        if expected_text is not None:
            command.append(expected_text)
        result = self.runner(command,
                             capture_output=True, text=True, timeout=30, env=os.environ.copy())
        payload = self._json(result)
        if result.returncode or payload.get("ok") is not True:
            raise RuntimeError(payload.get("reason") or "Planner readiness check failed")
        from urllib.parse import urlparse
        expected_id = urlparse(url).path.rstrip("/").split("/")[-1]
        if payload.get("conversation_id") != expected_id:
            raise RuntimeError("Planner readiness check belongs to a different conversation")
        return payload

    def _planner_snapshot(self, url, operation, require_complete):
        result = self.runner(["node", str(self.adapter_dir / "send_chatgpt.js"), operation, url],
                             capture_output=True, text=True, timeout=75, env=os.environ.copy())
        payload = self._json(result)
        if result.returncode or payload.get("ok") is not True:
            raise RuntimeError(payload.get("reason") or "Planner receipt verification failed")
        from urllib.parse import urlparse
        expected_id = urlparse(url).path.rstrip("/").split("/")[-1]
        if payload.get("conversation_id") != expected_id:
            raise RuntimeError("Planner receipt snapshot belongs to a different conversation")
        messages = payload.get("messages")
        if not isinstance(messages, list) or (require_complete and any(not row.get("id") for row in messages)):
            raise RuntimeError("Planner baseline has incomplete message identities")
        return messages

    @staticmethod
    def match_planner_receipt(messages, expected_text, conversation_id, baseline_ids):
        if not isinstance(baseline_ids, list):
            raise RuntimeError("A Planner receipt baseline is required")
        baseline = set(baseline_ids)
        rendered_link = re.compile(r"\[([^\]]+)\]\\\(\[([^\]]+)\]\(([^)]+)\)\)")
        def normalize(value):
            value = value.replace("\r\n", "\n").replace("\r", "\n").replace("`", "").strip()
            return rendered_link.sub(
                lambda match: "[%s](%s)" % (match.group(1), match.group(2))
                if match.group(2) == match.group(3) else match.group(0),
                value,
            )
        candidates = [row for row in messages if row.get("id") and row["id"] not in baseline]
        if len(candidates) > 1:
            raise RuntimeError("Planner contains multiple post-baseline messages; delivery is ambiguous")
        if not candidates:
            return None
        expected = normalize(expected_text)
        observed = normalize(candidates[0].get("text", ""))
        return {"confirmed": True, "conversation_id": conversation_id,
                "destination_message_id": candidates[0]["id"],
                "provenance": "chatgpt_dom.user_message_bubble",
                "match_method": "unique_post_baseline_user_message",
                "content_match": expected == observed,
                "expected_payload_bytes": len(expected_text.encode("utf-8")),
                "observed_text_bytes": len(candidates[0].get("text", "").encode("utf-8"))}

    def submit_message(self, url, text):
        """Attempt one Planner submission after a final busy/composer guard."""
        try:
            result = self.runner(["node", str(self.adapter_dir / "send_chatgpt.js"), "submit", url, text],
                                 capture_output=True, text=True, timeout=120, env=os.environ.copy())
        except subprocess.TimeoutExpired as exc:
            raise PlannerSubmissionError("Planner submit process timed out; send outcome is uncertain", True) from exc
        payload = self._json(result)
        if payload.get("not_ready"):
            raise PlannerNotReady(payload.get("reason") or "Planner became busy before send; no send was attempted")
        if result.returncode or payload.get("submitted") is not True:
            raise PlannerSubmissionError(payload.get("reason") or "Planner submission outcome is uncertain",
                                         payload.get("submission_attempted", payload.get("submitted") is True))
        return payload


class RelayRunner:
    def __init__(self, runtime, planner_url, worker_session, adapters, interval=5.0,
                 on_event=None, planner_wait_timeout=600.0, receipt_timeout=600.0,
                 receipt_poll_interval=5.0, planner_ready_timeout=600.0,
                 planner_ready_poll_interval=5.0):
        self.runtime = Path(runtime)
        self.planner_url, self.worker_session = planner_url, worker_session
        self.adapters, self.interval, self.on_event = adapters, interval, on_event
        self.planner_wait_timeout = planner_wait_timeout
        self.receipt_timeout = receipt_timeout
        self.receipt_poll_interval = receipt_poll_interval
        self.planner_ready_timeout = planner_ready_timeout
        self.planner_ready_poll_interval = planner_ready_poll_interval
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
        self._lock_fd = None
        self.state = _empty_state(self.planner_url, self.worker_session)

    def _save(self):
        """Touch session-local memory only; no relay state is written to disk."""
        self.state["updated_at"] = time.time()

    def _event(self, kind, message, level="info"):
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
               "kind": kind, "message": message, "level": level}
        self.state.setdefault("activity", []).append(row)
        self.state["activity"] = self.state["activity"][-50:]
        self._save()
        if self.on_event: self.on_event(row)

    def verify(self):
        return self.adapters.verify(self.planner_url, self.worker_session)

    def start(self):
        with self.lock:
            if self.running: raise RuntimeError("Automation is already running")
            # Clear every piece of prior session presentation/state before any
            # new observations. Nothing from the preceding run is resumable.
            self.state = _empty_state(self.planner_url, self.worker_session)
            # Per-pair lock so multiple pairs can run concurrently.
            lock_key = hashlib.sha256(f"{self.planner_url}\0{self.worker_session}".encode()).hexdigest()[:16]
            lock_path = self.runtime / f"automation.{lock_key}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self._lock_fd); self._lock_fd = None
                raise RuntimeError("Another RelayLoop automation loop already owns this runtime")
            try:
                # Start is always a new, disposable communication session. Observe
                # both destinations before admission and never consume preexisting messages.
                self.adapters.verify(self.planner_url, self.worker_session)
                planner = self.adapters.planner_latest(self.planner_url)
                worker_state = self.adapters.worker_state(self.worker_session)
                worker_busy = bool(worker_state.get("busy"))
                planner_active = bool(planner and planner.get("role") == "assistant"
                                      and not planner.get("completed"))
                if worker_busy and planner_active:
                    raise RuntimeError("Planner and Worker are both active; refusing to guess turn order")
                self.state = _empty_state(self.planner_url, self.worker_session)
                latest_worker = worker_state.get("latest_completed")
                if worker_busy:
                    # Continue an already-running Worker turn after its completion.
                    self.state["expected_speaker"] = "worker"
                    self.state["phase"] = "waiting_for_worker"
                    self.state["last_planner_id"] = planner.get("id") if planner else None
                    self.state["last_worker_id"] = latest_worker.get("id") if latest_worker else None
                    self.state["worker_response_baseline_id"] = latest_worker.get("id") if latest_worker else None
                elif planner_active:
                    # The latest Planner assistant turn is still generating. Use
                    # the preceding turn as watermark so this one is consumed once complete.
                    latest_id = planner.get("id")
                    turns = planner.get("all_turns") or []
                    previous = next((turn.get("id") for turn in reversed(turns)
                                     if turn.get("id") and turn.get("id") != latest_id), None)
                    self.state["last_planner_id"] = previous
                    self.state["last_worker_id"] = latest_worker.get("id") if latest_worker else None
                    self.state["phase"] = "waiting_for_planner_completion"
                    self.state["planner_wait_deadline"] = time.time() + self.planner_wait_timeout
                else:
                    # Idle/ambiguous non-active snapshots use Planner as default.
                    self.state["last_planner_id"] = planner.get("id") if planner else None
                    self.state["last_worker_id"] = latest_worker.get("id") if latest_worker else None
                    self.state["phase"] = "waiting_for_planner"
                    self.state["planner_wait_deadline"] = time.time() + self.planner_wait_timeout
                self.stop_event.clear()
                self.running = True
                self._event("start", "New session started; Planner and Worker baselines established")
                self.thread = threading.Thread(target=self._loop, name="relayloop-relay", daemon=True)
                self.thread.start()
            except Exception:
                self._release_runtime_lock()
                raise

    def _release_runtime_lock(self):
        if self._lock_fd is not None:
            try: fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def stop(self, timeout=10):
        with self.lock:
            if not self.running: return {"ok": True, "status": "stopped"}
            self.stop_event.set()
            thread = self.thread
        thread.join(timeout)
        with self.lock:
            if thread.is_alive():
                return {"ok": False, "status": "stopping", "warning": "Current adapter call has not returned; state is retained"}
            self.running = False
            self._event("stop", "Automation stopped gracefully")
            return {"ok": True, "status": "stopped"}

    def release_hold(self):
        """Release an ambiguous hold and resume the same turn.

        Preserves last planner/worker IDs, expected speaker, activity, and
        confirmed delivery. The delivery that created the hold is abandoned —
        restarting the turn may duplicate it.
        """
        with self.lock:
            held = self.state.get("unresolved")
            if not held:
                return {"ok": True, "released": False, "status": "running" if self.running else "stopped"}
            operation_id = held.get("operation_id")
            self.state["unresolved"] = None
            self.state["pending"] = None
            self.state["prompt_recovery_attempted"] = False
            expected = self.state.get("expected_speaker") or "planner"
            if expected == "planner":
                self.state["phase"] = "waiting_for_planner"
                self.state["planner_wait_deadline"] = time.time() + self.planner_wait_timeout
            else:
                self.state["phase"] = "waiting_for_worker"
                self.state["planner_wait_deadline"] = None
            self._event("release", f"Released hold {operation_id}; resuming {expected} turn")
            thread_alive = self.running and self.thread is not None and self.thread.is_alive()
            if thread_alive:
                # Loop is still alive (e.g. held while waiting); wake it.
                return {"ok": True, "released": True, "status": "running", "operation_id": operation_id}
            # Thread is dead (error break released the lock): start a new loop
            # from the current turn without baselining or clearing history.
            lock_key = hashlib.sha256(f"{self.planner_url}\0{self.worker_session}".encode()).hexdigest()[:16]
            lock_path = self.runtime / f"automation.{lock_key}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self._lock_fd)
                self._lock_fd = None
                raise RuntimeError("Another RelayLoop automation loop already owns this pair")
            self.stop_event.clear()
            self.running = True
            self.thread = threading.Thread(target=self._loop, name="relayloop-relay", daemon=True)
            self.thread.start()
            return {"ok": True, "released": True, "status": "running", "operation_id": operation_id}

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                if self.state.get("unresolved"):
                    break
                try:
                    if self.state["expected_speaker"] == "planner": self._planner_turn()
                    else: self._worker_turn()
                except Exception as exc:
                    with self.lock:
                        operation = self.state.get("pending")
                        if operation and operation.get("submission_attempted"):
                            self.state["unresolved"] = {**operation, "reason": str(exc)}
                            self.state["pending"] = None
                            self.state["phase"] = "unresolved"
                            self._event("error", str(exc), "error")
                        elif operation and operation.get("direction") == "worker_to_planner":
                            operation["stage"] = "planner_ready_timeout" if isinstance(exc, PlannerReadinessTimeout) else "planner_readiness_error"
                            self.state["phase"] = operation["stage"]
                            self._event("timeout" if isinstance(exc, PlannerReadinessTimeout) else "error",
                                        f"Worker → Planner report remains queued; delivery was not attempted: {exc}",
                                        "warning" if isinstance(exc, PlannerReadinessTimeout) else "error")
                        else:
                            if operation:
                                self.state["unresolved"] = {**operation, "reason": str(exc)}
                                self.state["pending"] = None
                                self.state["phase"] = "unresolved"
                            self._event("error", str(exc), "error")
                    break
                if self.state.get("phase") == "planner_timeout":
                    break
                self.stop_event.wait(self.interval)
        finally:
            with self.lock:
                if (self.stop_event.is_set() and not self.state.get("pending")
                        and not self.state.get("unresolved") and self.state.get("phase") not in
                        ("planner_timeout", "worker_prompt_unavailable")):
                    self.state["phase"] = "stopped"
                    self._save()
                self.running = False
                self._release_runtime_lock()

    def _planner_turn(self):
        msg = self.adapters.planner_latest(self.planner_url)
        if msg and msg.get("role") == "assistant" and not msg.get("completed"):
            self._wait_for_planner("waiting_for_planner_completion")
            return
        if not msg or msg.get("role") != "assistant" or not msg.get("id") or not msg.get("text"):
            self._wait_for_planner("waiting_for_planner")
            return
        if msg["id"] == self.state.get("last_planner_id"):
            self._wait_for_planner("waiting_for_planner")
            return
        blocks = msg.get("writing_blocks")
        prompts = [block.strip() for block in blocks if isinstance(block, str) and block.strip()] \
            if isinstance(blocks, list) else []
        if len(prompts) != 1:
            if self.state.get("prompt_recovery_attempted"):
                with self.lock:
                    self.state["phase"] = "worker_prompt_unavailable"
                    self._event("error", "Worker prompt not available", "error")
                    self._save()
                self.stop_event.set()
                return
            self.state["prompt_recovery_attempted"] = True
            request = "Worker prompt not found. Please generate the next Worker prompt."
            operation = self._new_operation("relayloop_to_planner", msg["id"], request)
            self._begin_operation(operation)
            operation["stage"] = "requesting_worker_prompt"
            self._event("extract", "No writing block found; requesting a Worker prompt from Planner")
            self._submit_and_confirm_planner(operation)
            return
        self.state["prompt_recovery_attempted"] = False
        operation = self._new_operation("planner_to_worker", msg["id"], prompts[0])
        self._begin_operation(operation)
        worker_before = self.adapters.worker_state(self.worker_session).get("latest_completed")
        operation["worker_response_baseline_id"] = worker_before.get("id") if worker_before else self.state.get("last_worker_id")
        operation["destination_baseline_ids"] = self._worker_destination_ids()
        operation["stage"] = "prepared"
        self._set_operation(operation, "delivering_to_worker")
        self._event("extract", "New completed Planner assistant message extracted")
        self._submit_and_confirm_worker(operation)

    def _wait_for_planner(self, phase):
        now = time.time()
        deadline = self.state.get("planner_wait_deadline")
        changed = self.state.get("phase") != phase
        if deadline is None:
            deadline = now + self.planner_wait_timeout
            self.state["planner_wait_deadline"] = deadline
            changed = True
        if now >= deadline:
            self.state["phase"] = "planner_timeout"
            self._event("timeout", "No new completed Planner message within the configured ten-minute response window", "warning")
            return
        self.state["phase"] = phase
        if changed:
            self._save()

    def _worker_turn(self):
        worker_state = self.adapters.worker_state(self.worker_session)
        msg = worker_state.get("latest")
        if worker_state.get("busy") or not msg:
            with self.lock:
                if self.state.get("phase") != "waiting_for_worker":
                    self.state["phase"] = "waiting_for_worker"
                    self._save()
            return
        if not msg.get("completed") or msg.get("role") != "assistant" or not msg.get("id") or not msg.get("text"):
            return
        if msg["id"] in (self.state.get("last_worker_id"), self.state.get("worker_response_baseline_id")): return
        operation = self._new_operation("worker_to_planner", msg["id"], msg["text"])
        self._begin_operation(operation)
        operation["stage"] = "waiting_for_planner_ready"
        self._set_operation(operation, "waiting_for_planner_ready")
        self._event("extract", "Completed Worker response extracted; queued until Planner is ready")
        self._submit_and_confirm_planner(operation)

    def _planner_readiness(self, expected_text=None):
        check = getattr(self.adapters, "planner_readiness", None)
        if check:
            return check(self.planner_url, expected_text)
        # Compatibility path for injected/test adapters. Production adapters
        # inspect generation and the exact conversation composer in the DOM.
        msg = self.adapters.planner_latest(self.planner_url)
        if not msg:
            return {"ready": True, "busy": False}
        if msg.get("role") == "assistant":
            busy = not bool(msg.get("completed"))
            return {"ready": not busy, "busy": busy}
        return {"ready": False, "busy": True}

    @staticmethod
    def _payload_fingerprint(text):
        payload = text.encode("utf8")
        return hashlib.sha256(payload).hexdigest(), len(payload)

    def _new_operation(self, direction, source_id, text):
        digest, length = self._payload_fingerprint(text)
        identity = hashlib.sha256(f"{direction}\0{source_id}\0{digest}\0{length}".encode()).hexdigest()
        return {"operation_id": identity, "direction": direction, "source_id": source_id,
                "payload_sha256": digest, "payload_bytes": length, "text": text,
                "stage": "preparing", "destination_baseline_ids": None,
                "submission_attempted": False}

    @staticmethod
    def _stable_ids(messages):
        return sorted({row["id"] for row in messages if isinstance(row, dict) and row.get("id")})

    def _worker_destination_ids(self):
        messages = self.adapters.worker_destination_messages(self.worker_session)
        return self._stable_ids(messages)

    def _begin_operation(self, operation):
        with self.lock:
            self.state["pending"] = operation
            self.state["unresolved"] = None
            self.state["phase"] = "extracting"
            self.state["planner_wait_deadline"] = None
            self._save()

    def _set_operation(self, operation, phase):
        with self.lock:
            self.state["pending"] = operation
            self.state["phase"] = phase
            self._save()

    @staticmethod
    def _same_payload(expected, observed):
        normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n").replace("`", "").strip()
        expected = normalize(expected)
        observed = normalize(observed)
        if observed == expected:
            return True
        # `opencode run ... <prompt>` serializes its submitted prompt as a JSON
        # string in the session export. Decode that transport representation so
        # embedded quotes (for example a quoted command path) do not cause a
        # false-negative receipt match.
        if len(observed) >= 2 and observed[0] == observed[-1] == '"':
            try:
                decoded = json.loads(observed)
            except (TypeError, ValueError):
                # Older OpenCode exports used raw control characters inside a
                # JSON-style quoted envelope while still escaping embedded
                # quotes and backslashes. Escape only those raw controls, then
                # decode the transport string normally.
                relaxed = "".join(
                    "\\u%04x" % ord(char) if ord(char) < 0x20 else char
                    for char in observed
                )
                try:
                    decoded = json.loads(relaxed)
                except (TypeError, ValueError):
                    return normalize(observed[1:-1]) == expected
            return isinstance(decoded, str) and normalize(decoded) == expected
        return False

    def _planner_receipt(self, operation):
        messages = self.adapters.verify_delivery(self.planner_url, require_complete=False)
        baseline_ids = operation.get("destination_baseline_ids")
        if not isinstance(baseline_ids, list):
            raise RuntimeError("Planner receipt baseline is missing for this session")
        return self.adapters.match_planner_receipt(messages, operation["text"],
            self._planner_conversation_id(), baseline_ids)

    def _planner_conversation_id(self):
        from urllib.parse import urlparse
        return urlparse(self.planner_url).path.rstrip("/").split("/")[-1]

    def _worker_receipt(self, operation):
        messages = self.adapters.worker_destination_messages(self.worker_session)
        baseline_ids = operation.get("destination_baseline_ids")
        if not isinstance(baseline_ids, list):
            raise RuntimeError("Worker receipt baseline is missing for this session")
        baseline = set(baseline_ids)
        matches = [row for row in messages if row.get("role") == "user" and row.get("id")
                   and row["id"] not in baseline and self._same_payload(operation["text"], row.get("text", ""))]
        if len(matches) > 1:
            raise RuntimeError("Multiple post-baseline Worker receipts match; delivery is unresolved")
        if not matches:
            return None
        return {"confirmed": True, "session_id": self.worker_session,
                "destination_message_id": matches[0]["id"],
                "provenance": "opencode.session.export.user_message",
                "payload_sha256": operation["payload_sha256"],
                "payload_bytes": operation["payload_bytes"]}

    def _submit_and_confirm_planner(self, operation):
        deadline = time.monotonic() + self.planner_ready_timeout
        last_wait_phase = None
        while not self.stop_event.is_set():
            readiness = self._planner_readiness(operation["text"])
            if readiness.get("ready") is True:
                # Capture the baseline only after Planner is ready, so manual
                # Planner activity during the wait cannot look like our receipt.
                baseline = self.adapters.verify_delivery(self.planner_url, require_complete=True)
                operation["destination_baseline_ids"] = self._stable_ids(baseline)
                operation["stage"] = "prepared"
                self._set_operation(operation, "verifying")
                receipt = self._planner_receipt(operation)
                if receipt is not None:
                    self.confirm_delivery(operation, receipt)
                    return
                operation["stage"] = "submitting"
                # Once control enters the adapter submit boundary, an
                # unclassified adapter failure must be held as an attempted
                # delivery. The adapter may explicitly prove that no click
                # occurred with PlannerNotReady below.
                operation["submission_attempted"] = True
                self._set_operation(operation, "submitting")
                try:
                    self.adapters.submit_message(self.planner_url, operation["text"])
                except PlannerNotReady:
                    # The final DOM guard caught a race after the read-only
                    # readiness poll. No click occurred, so wait and retry only
                    # the readiness check; never repeat an attempted send.
                    operation["submission_attempted"] = False
                    operation["stage"] = "waiting_for_planner_ready"
                    self._set_operation(operation, "waiting_for_planner_ready")
                    last_wait_phase = None
                    if self.stop_event.wait(self.planner_ready_poll_interval):
                        return
                    continue
                except PlannerSubmissionError as exc:
                    operation["submission_attempted"] = exc.submission_attempted
                    raise
                operation["stage"] = "verifying"
                self._set_operation(operation, "verifying")
                receipt = self._wait_for_planner_receipt(operation)
                self.confirm_delivery(operation, receipt)
                return

            phase = "waiting_for_planner_completion" if readiness.get("busy") else "waiting_for_planner_ready"
            operation["submission_attempted"] = False
            operation["stage"] = "waiting_for_planner_ready"
            with self.lock:
                changed = self.state.get("phase") != phase
                self.state["pending"] = operation
                self.state["phase"] = phase
                self._save()
                if changed or phase != last_wait_phase:
                    if operation["direction"] == "relayloop_to_planner":
                        message = ("Planner is generating after the prompt request"
                                   if phase == "waiting_for_planner_completion"
                                   else "Waiting for Planner readiness; prompt request not sent")
                    else:
                        message = ("Planner is generating; Worker report queued and not sent"
                                   if phase == "waiting_for_planner_completion"
                                   else "Waiting for Planner composer readiness; Worker report not sent")
                    reason = readiness.get("reason")
                    if reason:
                        message += f" ({reason})"
                    self._event("wait", message, "info")
            last_wait_phase = phase
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlannerReadinessTimeout("Planner did not become ready within the ten-minute readiness window")
            if self.stop_event.wait(min(self.planner_ready_poll_interval, remaining)):
                return

    def _wait_for_planner_receipt(self, operation):
        deadline = time.monotonic() + self.receipt_timeout
        refreshed = False
        while True:
            receipt = self._planner_receipt(operation)
            if receipt: return receipt
            if time.monotonic() >= deadline:
                if refreshed:
                    raise RuntimeError("Planner submission outcome is uncertain; unique receipt not observed after two verification windows and a page refresh")
                refresh = getattr(self.adapters, "refresh_planner", None)
                if refresh:
                    refresh(self.planner_url)
                refreshed = True
                deadline = time.monotonic() + self.receipt_timeout
                continue
            if self.stop_event.wait(min(self.receipt_poll_interval, max(0, deadline - time.monotonic()))):
                raise RuntimeError("Stopped while Planner receipt was still unconfirmed")

    def _submit_and_confirm_worker(self, operation):
        if self.stop_event.is_set():
            return
        operation["submission_attempted"] = True
        operation["stage"] = "submitting"
        self._set_operation(operation, "submitting")
        self.adapters.deliver_worker(self.worker_session, operation["text"])
        operation["stage"] = "verifying"
        self._set_operation(operation, "verifying")
        deadline = time.monotonic() + self.receipt_timeout
        while True:
            receipt = self._worker_receipt(operation)
            if receipt:
                self.confirm_delivery(operation, receipt)
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Worker completed without a unique destination receipt; delivery is unresolved")
            if self.stop_event.wait(min(self.interval, max(0, deadline - time.monotonic()))):
                raise RuntimeError("Stopped while Worker delivery receipt was unconfirmed")

    def _submit_prepared_operation(self, operation):
        if operation["direction"] in ("worker_to_planner", "relayloop_to_planner"):
            return self._submit_and_confirm_planner(operation)
        return self._submit_and_confirm_worker(operation)

    def confirm_delivery(self, operation, receipt):
        """Commit one uniquely verified receipt and advance this session's turn."""
        if not receipt or receipt.get("confirmed") is not True or not receipt.get("destination_message_id"):
            raise RuntimeError("A stable destination receipt is required to commit delivery")
        with self.lock:
            if self.state.get("last_confirmed_operation_id") == operation["operation_id"]:
                return
            active = self.state.get("pending") or self.state.get("unresolved")
            if not active or active.get("operation_id") != operation["operation_id"]:
                raise RuntimeError("Operation identity changed before receipt commit")
            record = {"operation_id": operation["operation_id"], "direction": operation["direction"],
                      "source_id": operation["source_id"], "payload_sha256": operation["payload_sha256"],
                      "payload_bytes": operation["payload_bytes"], "receipt": receipt,
                      "confirmed_at": time.time()}
            self.state["last_confirmed_operation_id"] = operation["operation_id"]
            self.state["last_confirmed_delivery"] = record
            if operation["direction"] == "planner_to_worker":
                self.state["last_planner_id"] = operation["source_id"]
                self.state["worker_response_baseline_id"] = operation.get("worker_response_baseline_id")
                self.state["expected_speaker"] = "worker"
                self.state["phase"] = "waiting_for_worker"
                self.state["planner_wait_deadline"] = None
            elif operation["direction"] == "relayloop_to_planner":
                self.state["last_planner_id"] = operation["source_id"]
                self.state["expected_speaker"] = "planner"
                self.state["phase"] = "waiting_for_planner_completion"
                self.state["planner_wait_deadline"] = time.time() + self.planner_wait_timeout
            else:
                self.state["last_worker_id"] = operation["source_id"]
                self.state["last_confirmed_turn"] = {"planner_id": self.state.get("last_planner_id"),
                    "worker_id": operation["source_id"], "planner_receipt": receipt,
                    "operation_id": operation["operation_id"]}
                self.state["expected_speaker"] = "planner"
                self.state["phase"] = "waiting_for_planner"
                self.state["planner_wait_deadline"] = time.time() + self.planner_wait_timeout
            self.state["pending"] = None
            self.state["unresolved"] = None
            message = ("Planner → Worker delivery confirmed" if operation["direction"] == "planner_to_worker"
                       else "RelayLoop → Planner prompt request confirmed" if operation["direction"] == "relayloop_to_planner"
                       else "Worker → Planner delivery confirmed; turn advanced")
            self._event("delivery", message)

    def status(self):
        with self.lock:
            return {"status": "running" if self.running else "stopped",
                    "expected_speaker": self.state.get("expected_speaker"),
                    "phase": self.state.get("phase"),
                    "planner_wait_deadline": self.state.get("planner_wait_deadline"),
                    "pending": self.state.get("pending"), "unresolved": self.state.get("unresolved"),
                    "last_confirmed_delivery": self.state.get("last_confirmed_delivery"),
                    "last_confirmed_turn": self.state.get("last_confirmed_turn"),
                    "last_planner_id": self.state.get("last_planner_id"),
                    "last_worker_id": self.state.get("last_worker_id"),
                    "activity": list(reversed(self.state.get("activity", [])))}
