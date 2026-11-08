# RelayLoop — independent conversation relay

RelayLoop is a one-pair, loopback-only ChatGPT Planner ↔ OpenCode Worker relay. It does not call Relay Beacon or its authority service, database, pair registry, or runtime.

## Start

```sh
cd /Users/lazydeepak/dev/RelayLoop
npm run dev
```

The dashboard binds to `127.0.0.1:5566` by default and prints its URL. Override the port with `--port`; if the selected port is busy, startup reports the bind error rather than silently choosing another port. It never starts automation automatically. Only Planner URL and Worker session ID are saved as configuration under `~/.local/state/relayloop/standalone` (or `--runtime PATH`); adapter dependencies are runtime support, while logs and relay state are memory-only. Existing Relay Beacon data and release archives are left untouched.

## Use

The dashboard exposes a Planner conversation URL, Worker session ID, Save, Verify, Refresh, Start and Stop. Verify checks the current form values without starting automation. Refresh is available only while stopped; after explicit confirmation it reloads the Planner conversation and clears all pending/unresolved in-memory session state. Clearing a possibly-sent delivery can permit a duplicate if automation is started again. Start independently verifies both configured destinations before beginning. Stop stops scheduling new turns and preserves an in-flight operation if its provider call has not returned.

On Start, the runner records fresh message baselines. If exactly one side is already active, it waits for that side to complete and relays its message; if both sides are idle, Planner is the default next speaker. If both are active, Start refuses to guess turn order. Worker execution has no arbitrary timeout. Before delivering a Worker report, the runner waits up to ten minutes for Planner generation to finish and the exact conversation composer to be ready; it does not paste or click while Planner is busy. After a confirmed Worker → Planner delivery, Planner has a separate ten-minute response window.

Each Start creates a fresh in-memory session, clears previous activity/errors/holds/turn state, and baselines the current Planner and Worker messages. Existing messages are never replayed. Planner-to-Worker delivery extracts only the single structured ChatGPT `writing_block` from the latest assistant response; surrounding commentary is not forwarded. If no unique writing block is present, RelayLoop asks Planner once to generate the next Worker prompt, then rechecks the next completed response. A second missing/ambiguous prompt pauses the relay with `Worker prompt not available`. During that run, source IDs and destination baselines prevent duplicate delivery; Planner submission remains a separate one-shot step followed by read-only receipt verification. Stop ends the run. A timeout or uncertain delivery can be held only in that active run; restarting discards it and establishes fresh baselines. No delivery journal, persistent hold, historical reconciliation, or cross-run recovery is used. Only one runner may own the transient process lock at a time.

## Safety and development checks

- The runner and dashboard are independent of Relay Beacon, either Bot checkout, Tailscale, Electron, and mobile hosts.
- `send_chatgpt.js verify` is read-only; `send_chatgpt.js verify-refresh` refreshes the same conversation and reads it again; `send_chatgpt.js submit` clicks once and does not attempt receipt reconciliation. Planner receipt checks poll every five seconds for up to ten minutes, refresh the page, then poll for a second ten-minute window. A single identifiable post-baseline Planner user message confirms receipt; text equality is recorded as diagnostic evidence, not a gate. Multiple post-baseline messages remain ambiguous. Submission is never repeated.
- Worker delivery and completion are identified from the selected OpenCode session export; `opencode run` has no fixed timeout.
- No real provider messaging is performed by automated tests.

The main dashboard includes a **Project Dashboard** button next to Graceful Stop for live progress, milestones, file changes, tests, and corrections.

```sh
python3 -m unittest discover -v
python3 -m py_compile dashboard.py relayloop_dashboard.py relayloop_runner.py test_dashboard.py test_relayloop_runner.py
node --check send_chatgpt.js
```
