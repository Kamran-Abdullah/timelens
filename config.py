"""Central configuration constants for TimeLens.

Settings the user can change at runtime (idle threshold, startup mode, ...) will be
stored in the SQLite `settings` table (Phase 2+). The values here are only defaults.
"""
import os
from pathlib import Path

APP_NAME = "TimeLens"

# How often the foreground window is sampled.
POLL_INTERVAL_SECONDS = 2.0

# Default inactivity threshold before ACTIVE becomes IDLE (configurable in Settings later).
DEFAULT_IDLE_THRESHOLD_SECONDS = 5 * 60

# If two consecutive samples are further apart than this, the computer was asleep, the
# process was frozen, or TimeLens was not running. That time is NOT counted as activity.
MAX_SAMPLE_GAP_SECONDS = POLL_INTERVAL_SECONDS * 5

# Defaults for the `settings` table. Values are stored as text; see Database.get_setting.
DEFAULT_SETTINGS = {
    "idle_threshold_seconds": str(DEFAULT_IDLE_THRESHOLD_SECONDS),
    "start_with_windows": "1",
    "open_dashboard_on_startup": "0",
    "background_monitoring": "1",
    "system_tray": "1",
    "minimize_to_tray": "1",
    "theme": "light",
    # Extra folders for the Assistant to search, separated by ";" (empty = Desktop, Documents, Downloads).
    "assistant_folders": "",
    # Website tracking is OFF until the user opts in (Settings > Websites). See services/web_history.py.
    "track_websites": "0",
    "web_browsers": "chrome,edge",
    "web_excluded_domains": "",
    # Which browser profiles to read ("chrome:Profile 1,edge:Default"). Empty = only each browser's Default profile.
    "web_profiles": "",
    "web_since": "",
    # Productivity score weights (percent credit each category kind earns). Editable in the
    # Productivity page; see analytics/productivity.py for the exact formula.
    "weight_productive": "100",
    "weight_neutral": "50",
    "weight_distraction": "0",
    "weight_personal": "50",
    "weight_custom": "50",
}

# How often the crash-recovery heartbeat is written (seconds). Bounds data lost in a crash.
HEARTBEAT_INTERVAL_SECONDS = 30

# Per-user data folder (no administrator rights needed).
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "timelens.db"
BACKUP_DIR = DATA_DIR / "backups"
