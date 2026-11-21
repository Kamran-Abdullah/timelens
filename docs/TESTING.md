# TimeLens testing record

Run everything: `.venv\Scripts\python -m pytest -q`. The full run is slow on some PCs
(a few minutes) because of Python/Qt start-up cost.

## Latest full run

407 automated tests, all passed (141 s). The built EXE passed its own 10-step self-test
(`TimeLens.exe --selftest`): database, tracking, power events, startup shortcut, all 10 pages with
charts, Excel + PDF reports, backup, the file assistant, optional website tracking (off by default),
and operation with the network blocked.

## Website tracking (optional)

`tests/test_web_history.py` (35) and `tests/test_ui_websites.py` (10), using fake Chrome-style History files:
nothing is read while tracking is off; only site + search words + title + times are stored (a
test proves tokens, paths and query strings never reach the database); incremental imports never
duplicate; durations are capped; "from now" vs "past 30 days"; excluded sites (parents included);
only the Default profile is read unless more are ticked; unreadable files are reported not raised;
temp copies are removed; the browser's file is never modified; date-range and delete-all also remove
website visits; and upgrading an older v1 database keeps all history. Checked against this PC's real
Chrome/Edge files for structure and row counts only (no browsing content was read): 9 profiles found,
all copyable while the browsers run, schema as expected.

## Assistant (file chat)

`tests/test_assistant.py` (67 tests) and `tests/test_ui_assistant.py` (12 tests): request
parsing; the open-only-documents rule (programs, scripts, double extensions and trailing-dot
tricks are refused, and the opener is never called); index limits and skipped folders; ranking,
plural and spelling tolerance; filters by type, folder and date; ambiguous requests list choices
instead of guessing; deleted-file handling; the activity summary; and proof that the assistant
never writes file paths or chat text to the database. Tested for real on this PC's folders
(1,803 documents, searches in a few milliseconds) with the opener replaced by a recorder, so no
real file was opened during testing.

## The 18 required scenarios

"Simulated" means the time logic ran in automated tests with a controlled clock and (where
relevant) the *real Windows messages* sent to TimeLens' hidden window. "Manual" means it needs a
person at a real PC and is listed in the checklist below.

| # | Scenario | Result | Where |
|---|---|---|---|
| 1 | Excel open for 30 minutes | automated, exact seconds | `test_session_manager.py::test_excel_30_minutes` |
| 2 | Switch between two Excel workbooks | automated | `test_switch_between_two_workbooks` |
| 3 | Switch between Excel and Chrome | automated | `test_switch_excel_to_chrome` |
| 4 | Idle for five minutes (split exactly at threshold) | automated | `test_idle_splits_exactly_at_threshold` |
| 5 | Return after 20 minutes idle | automated | `test_return_after_20_minutes_idle_and_reconciliation` |
| 6 | Lock Windows | simulated + real Windows message | `test_phase8_core.py::test_lock_*` |
| 7 | Unlock Windows | simulated + real Windows message | same |
| 8 | Sleep | simulated + real Windows message | `test_sleep_is_recorded_exactly...` |
| 9 | Wake | simulated + real Windows message | same |
| 10 | Restart Windows | simulated (shutdown event, then next start) | `test_shutdown_then_restart_counts_powered_off` |
| 11 | Close the dashboard (hides to tray, keeps monitoring) | automated | `test_ui_phase8.py::test_close_hides_to_tray...` |
| 12 | Exit TimeLens (monitoring fully stops) | automated | `test_tray_exit_fully_stops_monitoring` |
| 13 | Disable automatic startup | automated, real shortcut | `test_startup_modes_create_and_remove_the_shortcut` |
| 14 | Run without internet | automated (all network calls forbidden, no networking imports) | `test_offline_and_scale.py` |
| 15 | Export daily and monthly reports | automated (Excel + PDF) | `test_exports.py`, `test_ui_reports.py` |
| 16 | Multiple windows of one application | automated | `test_switch_between_two_workbooks`, document analytics tests |
| 17 | Unknown document titles | automated | `test_document_detector.py` |
| 18 | Application crash and recovery | automated | `test_database.py::test_crash_*`, `test_integration.py` |

## Scale and performance

58,400 sessions (about two years of heavy use): dashboard for one day 0.13 s, all-time
application report 1.45 s, monthly report 1.4 s. One hour of steady work writes one database
row, not 1,800 (no per-sample writes).

## Manual checklist (needs a real PC; not automated)

- [ ] Press Win+L, wait 30 s, unlock. Timeline shows a Locked entry of about 30 s.
- [ ] Sleep the PC for a minute, wake it. Timeline shows a Sleep / Off entry.
- [ ] Restart Windows with Start with Windows ON. TimeLens appears in the tray after sign-in
      and the time between shutdown and start shows as Sleep / Off.
- [ ] Open Excel, Word and a PDF; confirm their document names appear in Document Analytics.
- [ ] Open an exported .xlsx in Excel and check the charts.

## Known limits

- Document detection depends on what an application puts in its window title.
- Web browsers: titles are not stored (privacy).
- Time between a Windows shutdown and the next TimeLens start is counted as Sleep / Off,
  including the first seconds of Windows start-up.
- The built EXE is not code-signed, so Windows SmartScreen / antivirus may warn on first run.
