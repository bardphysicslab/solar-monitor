# Raspberry Pi App

This is the deployment-facing Pi app layer.

Rules for customization:

- keep hardware-specific protocol parsing inside drivers
- keep deployment identity in config
- keep `main.py` as the orchestrator
- return normalized readings only from drivers

The included app runs immediately with the example driver and example config.

