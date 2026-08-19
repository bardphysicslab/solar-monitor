# Deployment

Typical deployment flow:

1. Copy this template for the new installation.
2. Edit `raspi/config/app_config.example.json`.
3. Replace `raspi/drivers/example_driver.py`.
4. Install requirements on the Raspberry Pi.
5. Run the app with `uvicorn` or a systemd service.

Recommended next step after first boot: add a real config file outside version control and point the app at it with `BARDBOX_APP_CONFIG`.

## BardBox Operations

After `git pull`, preview new example fields with:

```bash
python3 scripts/sync_app_config.py
```

After reviewing the output, apply them with:

```bash
python3 scripts/sync_app_config.py --write
```

The merge preserves deployment-specific values, secrets, and unknown local
fields, validates JSON, and replaces the live file atomically.

### Read-only Data API

Configure `data_api.token` only in the ignored production
`raspi/config/app_config.json`. Empty or missing tokens disable the API. Use:

```bash
curl -H "Authorization: Bearer ${DATA_API_TOKEN}" \
  https://solar-monitor.example/api/data/files

curl -H "Authorization: Bearer ${DATA_API_TOKEN}" \
  --output readings.csv \
  https://solar-monitor.example/api/data/files/bb-solar-pnl-001/2026-08-19.csv
```

`GET /api/data/files` recursively lists eligible CSV/CSV.GZ files beneath
`data/sensor_data/`; `GET /api/data/files/{path}` downloads one. The interface
is intended for generic read-only consumers such as `bardbox-mcp`. It provides
no writes, administration, arbitrary filesystem access, or analysis, and
successful responses include `Cache-Control: no-store`.
