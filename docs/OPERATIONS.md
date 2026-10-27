# Local operations

## First deployment

Use a dedicated data directory and retain a copy of the monitor configuration. Run `doctor`, then `monitor --config config/monitor.json`, then `serve`. No `init` or hand-entered inclusion links are required for normal journal monitoring. The local model is optional; absent or rejected model output remains visible and cannot invent a contribution link.

Defaults limit resources for an 8GB M1 Mac. One model request runs at a time; Ollama requests use a 4096-token context, 512-token output limit and a short keep-alive. Resource use still depends on the document and local machine. Do not run multiple large models at once. The supported pilot has not passed a long-running memory or reliability qualification.

## Monitoring and failure states

`daily` uses the configuration saved by a previous monitor run. Incomplete operations return exit code 2. Inspect `reports/status.json`, source health, `runs` and access/extraction details. A failed run is not a clean research result. `--force` retries inside normal intervals. `--refresh` before the command bypasses cached HTTP responses.

Public HTTP requests are capped. Large queues use oldest/never-checked ordering. Crossref checks for newly mapped reports occur on the next pass; the current Retraction Watch snapshot is matched immediately. Increase `crossref_reports_per_run` and total request budget to cover the deployment's contributing reports daily, or the dashboard will report deferred checks. The schedule is an attempt, not a guarantee of upstream notice availability.

Mac scheduling: `reviewguard --data-dir /absolute/data schedule --hour 9`. It installs `~/Library/LaunchAgents/org.reviewguard.daily.plist` for the logged-in user and starts a run. Time is the Mac's local time zone. For Tehran scheduling, ensure macOS itself uses Tehran. Sleep, logout, power loss and network outages can delay jobs. Remove with the same data-dir prefix and `schedule --remove`.

On Linux, use your existing system scheduler to run `/absolute/venv/bin/reviewguard --data-dir /absolute/data daily` once a day, with logs redirected to that data directory. This project does not create system services on Linux automatically.

## Backup and restore

Stop the monitor and dashboard before copying the entire data directory, including SQLite WAL/SHM files if present, documents, extractions and saved evidence. Alternatively make a consistent SQLite backup with the SQLite backup API and copy its source evidence alongside it. Keep a protected copy of configuration. Restoring only SQLite without evidence files loses the ability to inspect document versions.

Test restoration into a separate directory with `status` and `serve --port 8766`. Do not run experiments in the live journal database. Version 0.3 upgrades schema 1 by adding tables and recording schema 2; old data is preserved. Back up before upgrading; downgrading to 0.2 after migration is unsupported.

## Publication

Runtime paths, credentials, article PDFs, full extracted text, model weights, databases and logs must not enter Git. `scripts/package_release.py` packages tracked source files only and checks this boundary. `scripts/publish_github.sh` checks the signed-in GitHub account, refuses unexpected remotes, creates the intended public repository if necessary, then pushes the source branch and release tag. It requires GitHub CLI authentication; never paste access tokens into chat.

The source repository is useful for development and research pilots. Keep its alpha status visible. Do not advertise validated inclusion accuracy, complete notice coverage, a completed journal backfile, or an unattended reliability trial until those results actually exist.
