# Deployment

Typical deployment flow:

1. Copy this template for the new installation.
2. Edit `raspi/config/app_config.example.json`.
3. Replace `raspi/drivers/example_driver.py`.
4. Install requirements on the Raspberry Pi.
5. Run the app with `uvicorn` or a systemd service.

Recommended next step after first boot: add a real config file outside version control and point the app at it with `BARDBOX_APP_CONFIG`.

