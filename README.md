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

