#!/usr/bin/env bash
set -euo pipefail

sudo systemctl restart bardbox-monitor
sudo systemctl status bardbox-monitor --no-pager

