# RelayLoop Project Progress Dashboard

The project progress dashboard tracks development work, milestones, file edits, validation, and corrections for RelayLoop. It is available from the main dashboard via the **Project Dashboard** button next to **Graceful Stop**, or directly at `/project-dashboard`.

## Purpose

- Provide a live, timestamped view of project state.
- Record intended corrections/upgrades and their outcomes.
- Track edited files automatically.
- Track test/validation results.
- Record corrections applied to earlier milestones or jobs.

## Data model

Progress data is stored in runtime state as `project_progress.json` and exposed through:

- `GET /api/project`
- `POST /api/project/record`

Supported record kinds:

- `milestone`: create or update a milestone.
- `job`: create or update a job/upgrade/correction.
- `test`: append a validation result.
- `correction`: append a correction and mark its target as corrected.
- `file_change`: append a detected file change.
- generic event: append to the activity timeline.

## Automatic file tracking

A background watcher scans the project root every five seconds and records modified, added, or deleted files with timestamps. Runtime, dependency, cache, and Git internals are ignored.

## Milestones

Initial milestones include:

1. Initial RelayLoop import — done
2. Resolve dashboard port conflict — done
3. Multi-pair upgrade preparation — done
4. Project progress dashboard — in progress
5. Multi-pair concurrent support — pending
6. Validation and test hardening — pending

## Visualization

The dashboard includes summary cards, a milestone status pie chart, test and job bar charts, most-edited file bars, and tables for milestones, jobs, tests, file changes, corrections, and an activity timeline. Colors distinguish done, in-progress, pending, failed, and corrected states.

## Notes

- Progress state is runtime-local and disposable; it is not required for relay operation.
- The dashboard is loopback-only and protected by the same CSRF/origin checks as the main dashboard.
- Milestones and jobs can be corrected later; corrections are timestamped and reflected in status counts.