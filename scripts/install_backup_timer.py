#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from raspi.backup import backup_config_from_app_config, render_systemd_timer  # noqa: E402


SERVICE_CONTENT = """[Unit]
Description=Solar Monitor CSV backup
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={repo_root}
Environment=BARDBOX_APP_CONFIG={config_path}
ExecStart={script_path}
"""


BACKUP_SCRIPT_CONTENT = """#!/usr/bin/env bash
set -euo pipefail

cd "{repo_root}"
source .venv/bin/activate
python - <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path

from raspi.backup import DataBackupManager, backup_config_from_app_config
from raspi.main import APP_CONFIG, DEFAULT_RECORDING_DATA_ROOT, RECORDER, iso_utc

manager = DataBackupManager(
    backup_config_from_app_config(APP_CONFIG),
    data_root=DEFAULT_RECORDING_DATA_ROOT,
    snapshot_root=Path("data/backup_snapshots"),
    recorder=RECORDER,
)
result = manager.backup_once(iso_utc(datetime.now(timezone.utc)))
if result["status"] != "ok":
    raise SystemExit(result.get("last_error") or "Backup failed")
PY
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Solar Monitor backup systemd units from app config.")
    parser.add_argument("--config", default="raspi/config/app_config.json", help="Path to app_config.json")
    parser.add_argument("--output-dir", default="/etc/systemd/system", help="Directory for generated unit files")
    parser.add_argument("--bin-dir", default="/usr/local/bin", help="Directory for generated backup script")
    parser.add_argument("--dry-run", action="store_true", help="Print generated files without writing them")
    args = parser.parse_args()

    config_path = (REPO_ROOT / args.config).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        app_config = json.load(handle)

    backup_config = backup_config_from_app_config(app_config)
    script_path = (Path(args.bin_dir) / "solar-monitor-backup.sh").resolve()
    service = SERVICE_CONTENT.format(repo_root=REPO_ROOT, config_path=config_path, script_path=script_path)
    timer = render_systemd_timer(backup_config.interval_minutes)
    script = BACKUP_SCRIPT_CONTENT.format(repo_root=REPO_ROOT)

    if args.dry_run:
        print("# solar-monitor-backup.service")
        print(service)
        print("# solar-monitor-backup.timer")
        print(timer)
        print("# solar-monitor-backup.sh")
        print(script)
        return 0

    output_dir = Path(args.output_dir)
    bin_dir = Path(args.bin_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "solar-monitor-backup.service").write_text(service, encoding="utf-8")
    (output_dir / "solar-monitor-backup.timer").write_text(timer, encoding="utf-8")
    written_script_path = bin_dir / "solar-monitor-backup.sh"
    written_script_path.write_text(script, encoding="utf-8")
    written_script_path.chmod(0o755)

    print(f"Wrote {output_dir / 'solar-monitor-backup.service'}")
    print(f"Wrote {output_dir / 'solar-monitor-backup.timer'}")
    print(f"Wrote {written_script_path}")
    print("Run: sudo systemctl daemon-reload && sudo systemctl enable --now solar-monitor-backup.timer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
