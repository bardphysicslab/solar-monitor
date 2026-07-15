# bardbox-project-template

`bardbox-project-template` is the standard starter repo for new Bard Box deployments.

It is a deployment template: a small, runnable Pi app with an example driver, a minimal dashboard, starter scripts, and example config. It is meant to be copied and customized for a specific installation.

## What This Repo Is

- A starter template for new Bard Box deployments
- A Pi app skeleton that already runs with example data
- A clean place to swap in real drivers, config, and deployment identity

## What This Repo Is Not

- Not the canonical Bard Box standards/spec repo
- Not the place to define protocol or architecture standards
- Not a hardware-specific project

Use the separate `bardbox` repo as the standards and reference source for protocol, reading format, driver boundaries, runtime structure, and UI conventions.

## Expected Workflow

1. Copy or clone this template for a new deployment.
2. Change the deployment title and `app_id`.
3. Replace the example driver with one or more real drivers.
4. Add real deployment config.
5. Run locally, then deploy to the Raspberry Pi.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn raspi.main:app --reload --app-dir .
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Mohamed's BardBox Wi-Fi Solar Node

The Raspberry Pi app can use Mohamed's BardBox Wi-Fi dummy solar node through
the `wifi_node` driver. The node exposes the BardBox TCP commands on port
`1234` and reports simulated panel voltage for UID `bb-solar-pnl-001`.

Find the ESP32 IP address from your router's DHCP client list, the ESP32 serial
monitor, or a local network scan from the Pi, for example:

```bash
hostname -I
arp -a
```

Enter the ESP32 IP in `raspi/config/app_config.json`. The tracked
`raspi/config/app_config.example.json` shows the expected entry:

```json
{
  "driver": "wifi_node",
  "uid": "bb-solar-pnl-001",
  "config": {
    "host": "REPLACE_WITH_ESP32_IP",
    "port": 1234,
    "timeout_s": 3
  }
}
```

Keep the actual node IP in `app_config.json`; do not hardcode it in Python.
To make Mohamed's node appear on the dashboard in the current single-primary
driver app, put the `wifi_node` entry first in the `drivers` list. Keep the
`spn1` entry in the list when SPN1 support is still needed.

Manual tests from the Pi:

```bash
printf 'PING\n' | nc NODE_IP 1234
printf 'READ\n' | nc NODE_IP 1234
```

After changing config, restart the Solar Monitor service. If your deployment
uses `solar-monitor` as the systemd unit name:

```bash
sudo systemctl restart solar-monitor
sudo systemctl status solar-monitor --no-pager
```

This repo's helper script currently targets the existing `bardbox-monitor`
service name used by the template deployment:

```bash
./scripts/restart_service.sh
```

## Repo Layout

```text
bardbox-project-template/
  docs/        deployment-facing notes
  firmware/    starter firmware area for Bard Box nodes
  raspi/       Pi app, drivers, config, templates, static assets
  scripts/     helper scripts for setup, restart, health checks
  tests/       starter test notes
  data/        runtime data directory
```

## First Things To Customize

1. `raspi/config/app_config.example.json`
   Change `app_id`, `title`, mode, and driver list.
2. `raspi/drivers/example_driver.py`
   Replace the example driver with a real one.
3. `raspi/main.py`
   Wire in your real drivers and deployment-specific routes only where needed.
4. `raspi/templates/index.html`
   Adjust the dashboard content for your deployment.
5. `raspi/static/Bard-Web-Logos/bard-logo-red.png`
   Confirm the approved Bard branding asset placement for your deployment.

## Standards Reference

This template should be kept aligned with the Bard Box standards repo, especially for:

- Pi runtime structure
- driver separation
- normalized reading format
- config-driven deployment
- monitor header/layout conventions

## Local Notes

- The example app serves fake but realistic normalized readings.
- No real hardware is required to boot the template.
- The header already includes a Bard logo, a human-readable deployment title, and a live clock.
