# RelayLoop Agent Guidelines (Primary)

These guidelines are primary for any AI agent or worker modifying RelayLoop. They take precedence over convenience shortcuts.

## Required project dashboard workflow

1. **Before work**
   - Open the Project Dashboard.
   - Create or update a milestone and/or job with:
     - title
     - intended scope
     - priority
     - status `in_progress`
   - Do not begin file edits until the milestone/job is recorded.

2. **During work**
   - Let the file watcher record edited files automatically.
   - Record important decisions, blockers, or scope changes as events or corrections.
   - Keep the milestone/job status current.

3. **After completion**
   - Run validation/tests.
   - Update the milestone/job status to `done`.
   - Add test results to the dashboard.
   - Record a correction instead of silently changing prior history.

4. **Corrections**
   - If a previous milestone or job was wrong, add a correction entry and mark the target `corrected`.
   - Do not overwrite historical intent without a correction record.

## General rules

- Prefer small, focused changes.
- Do not bypass verification.
- Keep project progress data and documentation synchronized.
- Use the Project Dashboard as the source of truth for work state.