#pragma once

// Copy this file to src/secrets.h before building.
// Never commit src/secrets.h to Git.

// -----------------------------------------------------------------------------
// Wi-Fi configuration
// -----------------------------------------------------------------------------
#define WIFI_SSID_VALUE "YOUR_WIFI_SSID"
#define WIFI_PASS_VALUE "YOUR_WIFI_PASSWORD"

// -----------------------------------------------------------------------------
// BardBox node identity
// -----------------------------------------------------------------------------
// Change XXX to the assigned node number, for example bb-solar-pnl-001.
#define DEVICE_UID_VALUE "bb-solar-pnl-XXX"

// -----------------------------------------------------------------------------
// Network services
// -----------------------------------------------------------------------------
// The Raspberry Pi connects to this TCP port and sends BardBox commands.
#define TCP_SERVER_PORT_VALUE 1234

// Browser-readable status endpoints: / and /json
#define HTTP_SERVER_PORT_VALUE 80
