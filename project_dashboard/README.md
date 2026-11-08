# Portable Project Dashboard

Copy this folder into any project, then run:

```sh
python3 project_dashboard/server.py --root /path/to/project --port 5577
```

Open `http://127.0.0.1:5577/`.

Files:
- `server.py` - standalone dashboard server and file watcher
- `dashboard.html` - self-contained UI
- `AGENTS.md` - required worker workflow

Progress data is stored in runtime state and is intentionally not part of the target project.