// BardBox Solar Panel Wi-Fi Node
// Board: Adafruit Feather ESP32-S3 No PSRAM
// ADC: Adafruit ADS1015 over I2C/STEMMA QT, address 0x48
// Analog inputs: ADS1015 A0, A1, A2, A3
// Transport: Wi-Fi TCP server + HTTP status page
// Protocol: bardbox-node-v1

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Adafruit_ADS1X15.h>
#include <math.h>
#include "secrets.h"

// -----------------------------------------------------------------------------
// Wi-Fi configuration
// -----------------------------------------------------------------------------

const char* WIFI_SSID = WIFI_SSID_VALUE;
const char* WIFI_PASS = WIFI_PASS_VALUE;

// -----------------------------------------------------------------------------
// Device information
// -----------------------------------------------------------------------------

#define DEVICE_UID DEVICE_UID_VALUE
#define FW_VERSION "0.3.0"
#define PROTOCOL_VERSION "bardbox-node-v1"
#define NODE_TYPE "solar_panel"
#define NODE_MODEL "ads1015_4channel_wifi"

// -----------------------------------------------------------------------------
// Timing
// -----------------------------------------------------------------------------

const unsigned long SAMPLE_INTERVAL_MS = 5000;
const unsigned long WIFI_RETRY_MS = 10000;

// -----------------------------------------------------------------------------
// ADS1015 configuration
// -----------------------------------------------------------------------------

constexpr uint8_t ADS_I2C_ADDRESS = 0x48;
constexpr uint8_t ADS_CHANNEL_COUNT = 4;

Adafruit_ADS1015 ads;
bool adsConnected = false;

// -----------------------------------------------------------------------------
// Network objects
// -----------------------------------------------------------------------------

WiFiServer tcpServer(TCP_SERVER_PORT_VALUE);
WebServer webServer(HTTP_SERVER_PORT_VALUE);
WiFiClient tcpClient;

// -----------------------------------------------------------------------------
// Runtime state
// -----------------------------------------------------------------------------

bool tcpStreaming = false;
bool serialStreaming = false;

unsigned long lastTcpSampleMs = 0;
unsigned long lastSerialSampleMs = 0;
unsigned long lastWifiAttemptMs = 0;

// -----------------------------------------------------------------------------
// Channel reading structure
// -----------------------------------------------------------------------------

struct AdcReadings {
  float voltage[ADS_CHANNEL_COUNT];
  bool ok[ADS_CHANNEL_COUNT];
};

// -----------------------------------------------------------------------------
// Read one ADS1015 channel
// -----------------------------------------------------------------------------

float readChannelVoltage(uint8_t channel, bool& ok) {
  if (!adsConnected || channel >= ADS_CHANNEL_COUNT) {
    ok = false;
    return NAN;
  }

  int16_t raw = ads.readADC_SingleEnded(channel);
  float voltage = ads.computeVolts(raw);

  ok = isfinite(voltage);

  return voltage;
}

// -----------------------------------------------------------------------------
// Read all four ADS1015 channels
// -----------------------------------------------------------------------------

AdcReadings readAllChannels() {
  AdcReadings readings;

  for (uint8_t channel = 0; channel < ADS_CHANNEL_COUNT; channel++) {
    readings.voltage[channel] =
      readChannelVoltage(channel, readings.ok[channel]);
  }

  return readings;
}

// -----------------------------------------------------------------------------
// Print one voltage or nan
// -----------------------------------------------------------------------------

void printSerialVoltage(float voltage, bool ok) {
  if (ok) {
    Serial.print(voltage, 3);
  } else {
    Serial.print("nan");
  }
}

void printClientVoltage(
  WiFiClient& client,
  float voltage,
  bool ok
) {
  if (ok) {
    client.print(voltage, 3);
  } else {
    client.print("nan");
  }
}

// -----------------------------------------------------------------------------
// TCP output
// -----------------------------------------------------------------------------

void sendTcpHeader(WiFiClient& client) {
  client.println(
    "HDR,v1,"
    "a0_voltage_v,a0_ok,"
    "a1_voltage_v,a1_ok,"
    "a2_voltage_v,a2_ok,"
    "a3_voltage_v,a3_ok,"
    "rssi_dbm"
  );
}

void sendTcpInfo(WiFiClient& client) {
  client.print("OK INFO uid=");
  client.print(DEVICE_UID);

  client.print(" node_type=");
  client.print(NODE_TYPE);

  client.print(" node_model=");
  client.print(NODE_MODEL);

  client.print(" fw=");
  client.print(FW_VERSION);

  client.print(" protocol=");
  client.print(PROTOCOL_VERSION);

  client.print(" sensors=ADS1015_A0_A1_A2_A3");

  client.print(" adc=");
  client.print(
    adsConnected
      ? "CONNECTED"
      : "DISCONNECTED"
  );

  client.print(" adc_addr=0x");
  client.print(ADS_I2C_ADDRESS, HEX);

  client.print(" ip=");
  client.print(WiFi.localIP());

  client.print(" mac=");
  client.print(WiFi.macAddress());

  client.print(" rssi_dbm=");
  client.println(
    WiFi.status() == WL_CONNECTED
      ? WiFi.RSSI()
      : 0
  );
}

void sendTcpStatus(WiFiClient& client) {
  client.print("OK STATUS ");

  client.print(
    tcpStreaming
      ? "RUNNING"
      : "STOPPED"
  );

  client.print(" wifi=");
  client.print(
    WiFi.status() == WL_CONNECTED
      ? "CONNECTED"
      : "DISCONNECTED"
  );

  client.print(" adc=");
  client.print(
    adsConnected
      ? "CONNECTED"
      : "DISCONNECTED"
  );

  client.print(" ip=");
  client.print(WiFi.localIP());

  client.print(" rssi_dbm=");
  client.println(
    WiFi.status() == WL_CONNECTED
      ? WiFi.RSSI()
      : 0
  );
}

bool sendTcpSample(WiFiClient& client) {
  AdcReadings readings = readAllChannels();

  client.print("DAT,");

  for (uint8_t channel = 0;
       channel < ADS_CHANNEL_COUNT;
       channel++) {

    printClientVoltage(
      client,
      readings.voltage[channel],
      readings.ok[channel]
    );

    client.print(",");
    client.print(readings.ok[channel] ? 1 : 0);
    client.print(",");
  }

  client.println(
    WiFi.status() == WL_CONNECTED
      ? WiFi.RSSI()
      : 0
  );

  bool allOk = true;

  for (uint8_t channel = 0;
       channel < ADS_CHANNEL_COUNT;
       channel++) {

    if (!readings.ok[channel]) {
      allOk = false;
    }
  }

  return allOk;
}

// -----------------------------------------------------------------------------
// Serial output
// -----------------------------------------------------------------------------

void printSerialInfo() {
  Serial.print("BARD_BOX_NODE");

  Serial.print(",uid=");
  Serial.print(DEVICE_UID);

  Serial.print(",node_type=");
  Serial.print(NODE_TYPE);

  Serial.print(",node_model=");
  Serial.print(NODE_MODEL);

  Serial.print(",fw_version=");
  Serial.print(FW_VERSION);

  Serial.print(",protocol_version=");
  Serial.print(PROTOCOL_VERSION);

  Serial.print(",transport=wifi_tcp");

  Serial.print(",sensor=ADS1015_A0_A1_A2_A3");

  Serial.print(",adc=");
  Serial.print(adsConnected ? 1 : 0);

  Serial.print(",adc_addr=0x");
  Serial.print(ADS_I2C_ADDRESS, HEX);

  Serial.print(",sample_interval_ms=");
  Serial.print(SAMPLE_INTERVAL_MS);

  Serial.print(",wifi=");
  Serial.print(
    WiFi.status() == WL_CONNECTED
      ? 1
      : 0
  );

  Serial.print(",ip=");
  Serial.print(WiFi.localIP());

  Serial.print(",mac=");
  Serial.print(WiFi.macAddress());

  Serial.print(",rssi_dbm=");
  Serial.println(
    WiFi.status() == WL_CONNECTED
      ? WiFi.RSSI()
      : 0
  );
}

void printSerialHeader() {
  Serial.println(
    "millis,"
    "uid,"
    "node_type,"
    "node_model,"
    "fw_version,"
    "protocol_version,"
    "transport,"
    "a0_voltage_v,"
    "a0_ok,"
    "a1_voltage_v,"
    "a1_ok,"
    "a2_voltage_v,"
    "a2_ok,"
    "a3_voltage_v,"
    "a3_ok,"
    "wifi,"
    "rssi_dbm"
  );
}

void printSerialRead() {
  AdcReadings readings = readAllChannels();

  Serial.print(millis());
  Serial.print(",");

  Serial.print(DEVICE_UID);
  Serial.print(",");

  Serial.print(NODE_TYPE);
  Serial.print(",");

  Serial.print(NODE_MODEL);
  Serial.print(",");

  Serial.print(FW_VERSION);
  Serial.print(",");

  Serial.print(PROTOCOL_VERSION);
  Serial.print(",");

  Serial.print("wifi_tcp");
  Serial.print(",");

  for (uint8_t channel = 0;
       channel < ADS_CHANNEL_COUNT;
       channel++) {

    printSerialVoltage(
      readings.voltage[channel],
      readings.ok[channel]
    );

    Serial.print(",");
    Serial.print(readings.ok[channel] ? 1 : 0);
    Serial.print(",");
  }

  Serial.print(
    WiFi.status() == WL_CONNECTED
      ? 1
      : 0
  );

  Serial.print(",");

  Serial.println(
    WiFi.status() == WL_CONNECTED
      ? WiFi.RSSI()
      : 0
  );
}

// -----------------------------------------------------------------------------
// Serial commands
// -----------------------------------------------------------------------------

void processSerialCommand(const char* command) {
  if (strcasecmp(command, "INFO") == 0) {
    printSerialInfo();

  } else if (strcasecmp(command, "PING") == 0) {
    Serial.println("PONG");

  } else if (strcasecmp(command, "HEADER") == 0) {
    printSerialHeader();

  } else if (strcasecmp(command, "READ") == 0) {
    printSerialRead();

  } else if (strcasecmp(command, "START") == 0) {
    serialStreaming = true;
    lastSerialSampleMs = 0;

    printSerialHeader();

  } else if (strcasecmp(command, "STOP") == 0) {
    serialStreaming = false;
    Serial.println("STOP_OK");

  } else if (strcasecmp(command, "STATUS") == 0) {
    Serial.print("STATUS,");

    Serial.print(
      serialStreaming
        ? "RUNNING"
        : "STOPPED"
    );

    Serial.print(",wifi=");

    Serial.print(
      WiFi.status() == WL_CONNECTED
        ? "CONNECTED"
        : "DISCONNECTED"
    );

    Serial.print(",adc=");

    Serial.println(
      adsConnected
        ? "CONNECTED"
        : "DISCONNECTED"
    );

  } else {
    Serial.println("ERR,UNKNOWN_COMMAND");
  }
}

void handleSerialCommands() {
  static char commandBuffer[64];
  static size_t commandIndex = 0;

  while (Serial.available() > 0) {
    char character = (char)Serial.read();

    if (
      character == '\n' ||
      character == '\r'
    ) {
      if (commandIndex > 0) {
        commandBuffer[commandIndex] = '\0';

        processSerialCommand(commandBuffer);

        commandIndex = 0;
      }

    } else if (
      commandIndex <
      sizeof(commandBuffer) - 1
    ) {
      commandBuffer[commandIndex++] = character;

    } else {
      commandIndex = 0;

      Serial.println(
        "ERR,COMMAND_TOO_LONG"
      );
    }
  }
}

void handleSerialStreaming() {
  if (!serialStreaming) {
    return;
  }

  if (
    lastSerialSampleMs == 0 ||
    millis() - lastSerialSampleMs >=
      SAMPLE_INTERVAL_MS
  ) {
    lastSerialSampleMs = millis();

    printSerialRead();
  }
}

// -----------------------------------------------------------------------------
// TCP commands
// -----------------------------------------------------------------------------

String readTcpCommand(WiFiClient& client) {
  String command;

  while (client.available()) {
    char character = (char)client.read();

    if (character == '\r') {
      continue;
    }

    if (character == '\n') {
      break;
    }

    command += character;
  }

  command.trim();

  return command;
}

void handleTcpCommand(
  const String& command,
  WiFiClient& client
) {
  if (command.length() == 0) {
    return;
  }

  Serial.print("TCP command: ");
  Serial.println(command);

  if (command == "INFO") {
    sendTcpInfo(client);

  } else if (command == "PING") {
    client.println("PONG");

  } else if (command == "STATUS") {
    sendTcpStatus(client);

  } else if (command == "HEADER") {
    sendTcpHeader(client);

  } else if (command == "READ") {
    sendTcpSample(client);

  } else if (command == "START") {
    tcpStreaming = true;
    lastTcpSampleMs = 0;

    client.println("OK START");
    sendTcpHeader(client);

  } else if (command == "STOP") {
    tcpStreaming = false;

    client.println("OK STOP");

  } else {
    client.println("ERR UNKNOWN_CMD");
  }
}

// -----------------------------------------------------------------------------
// HTTP plain-text page
// -----------------------------------------------------------------------------

void handleHttpRoot() {
  AdcReadings readings = readAllChannels();

  String body;

  body += "uid=";
  body += DEVICE_UID;
  body += "\n";

  body += "node_type=";
  body += NODE_TYPE;
  body += "\n";

  body += "node_model=";
  body += NODE_MODEL;
  body += "\n";

  body += "fw_version=";
  body += FW_VERSION;
  body += "\n";

  body += "protocol_version=";
  body += PROTOCOL_VERSION;
  body += "\n";

  body += "sensor=ADS1015_A0_A1_A2_A3\n";

  body += "adc_connected=";
  body += String(adsConnected ? 1 : 0);
  body += "\n";

  body += "adc_address=0x";
  body += String(ADS_I2C_ADDRESS, HEX);
  body += "\n";

  for (uint8_t channel = 0;
       channel < ADS_CHANNEL_COUNT;
       channel++) {

    body += "a";
    body += String(channel);
    body += "_voltage_v=";

    if (readings.ok[channel]) {
      body += String(
        readings.voltage[channel],
        3
      );
    } else {
      body += "nan";
    }

    body += "\n";

    body += "a";
    body += String(channel);
    body += "_ok=";
    body += String(
      readings.ok[channel]
        ? 1
        : 0
    );

    body += "\n";
  }

  body += "wifi=";
  body += String(
    WiFi.status() == WL_CONNECTED
      ? 1
      : 0
  );
  body += "\n";

  body += "ip=";
  body += WiFi.localIP().toString();
  body += "\n";

  body += "mac=";
  body += WiFi.macAddress();
  body += "\n";

  body += "rssi_dbm=";
  body += String(
    WiFi.status() == WL_CONNECTED
      ? WiFi.RSSI()
      : 0
  );
  body += "\n";

  body += "millis=";
  body += String(millis());
  body += "\n";

  webServer.send(
    200,
    "text/plain",
    body
  );
}

// -----------------------------------------------------------------------------
// HTTP JSON page
// -----------------------------------------------------------------------------

void handleHttpJson() {
  AdcReadings readings = readAllChannels();

  String body = "{";

  body += "\"uid\":\"";
  body += DEVICE_UID;
  body += "\",";

  body += "\"node_type\":\"";
  body += NODE_TYPE;
  body += "\",";

  body += "\"node_model\":\"";
  body += NODE_MODEL;
  body += "\",";

  body += "\"fw_version\":\"";
  body += FW_VERSION;
  body += "\",";

  body += "\"protocol_version\":\"";
  body += PROTOCOL_VERSION;
  body += "\",";

  body += "\"sensor\":\"ADS1015_A0_A1_A2_A3\",";

  body += "\"adc_connected\":";
  body += String(adsConnected ? 1 : 0);
  body += ",";

  body += "\"adc_address\":\"0x";
  body += String(ADS_I2C_ADDRESS, HEX);
  body += "\",";

  for (uint8_t channel = 0;
       channel < ADS_CHANNEL_COUNT;
       channel++) {

    body += "\"a";
    body += String(channel);
    body += "_voltage_v\":";

    if (readings.ok[channel]) {
      body += String(
        readings.voltage[channel],
        3
      );
    } else {
      body += "null";
    }

    body += ",";

    body += "\"a";
    body += String(channel);
    body += "_ok\":";

    body += String(
      readings.ok[channel]
        ? 1
        : 0
    );

    body += ",";
  }

  body += "\"wifi\":";
  body += String(
    WiFi.status() == WL_CONNECTED
      ? 1
      : 0
  );
  body += ",";

  body += "\"ip\":\"";
  body += WiFi.localIP().toString();
  body += "\",";

  body += "\"mac\":\"";
  body += WiFi.macAddress();
  body += "\",";

  body += "\"rssi_dbm\":";
  body += String(
    WiFi.status() == WL_CONNECTED
      ? WiFi.RSSI()
      : 0
  );
  body += ",";

  body += "\"millis\":";
  body += String(millis());

  body += "}";

  webServer.send(
    200,
    "application/json",
    body
  );
}

// -----------------------------------------------------------------------------
// Start TCP and HTTP servers
// -----------------------------------------------------------------------------

void startNetworkServices() {
  tcpServer.begin();
  tcpServer.setNoDelay(true);

  webServer.on(
    "/",
    handleHttpRoot
  );

  webServer.on(
    "/json",
    handleHttpJson
  );

  webServer.begin();

  Serial.print("TCP server: ");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(TCP_SERVER_PORT_VALUE);

  Serial.print("HTTP status: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/");

  Serial.print("HTTP JSON: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/json");
}

// -----------------------------------------------------------------------------
// Connect to Wi-Fi
// -----------------------------------------------------------------------------

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.print("Connecting to Wi-Fi SSID: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long connectionStartMs = millis();

  while (
    WiFi.status() != WL_CONNECTED &&
    millis() - connectionStartMs < 15000
  ) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(
      "=== WIFI CONNECTED ==="
    );

    Serial.print("IP: ");
    Serial.println(WiFi.localIP());

    Serial.print("MAC: ");
    Serial.println(WiFi.macAddress());

    Serial.print("RSSI: ");
    Serial.println(WiFi.RSSI());

    startNetworkServices();

  } else {
    Serial.println(
      "Wi-Fi connection failed; will retry."
    );
  }
}

// -----------------------------------------------------------------------------
// Setup
// -----------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println(
    "=== BardBox Solar Wi-Fi Node Start ==="
  );

  Serial.println(
    "Type INFO, PING, STATUS, HEADER, READ, START, or STOP."
  );

  Wire.begin();

  Serial.print(
    "Searching for ADS1015 at I2C address 0x"
  );

  Serial.println(
    ADS_I2C_ADDRESS,
    HEX
  );

  adsConnected = ads.begin(
    ADS_I2C_ADDRESS
  );

  if (adsConnected) {
    // ±4.096 V ADC full-scale setting.
    // The physical inputs must remain within
    // the ADS1015 board supply rails.
    ads.setGain(GAIN_ONE);

    Serial.println(
      "=== ADS1015 CONNECTED ==="
    );

    Serial.println(
      "Reading single-ended voltage from A0, A1, A2, and A3."
    );

  } else {
    Serial.println(
      "ERROR: ADS1015 not detected."
    );

    Serial.println(
      "Check STEMMA QT cable and I2C address."
    );
  }

  connectWifi();
}

// -----------------------------------------------------------------------------
// Main loop
// -----------------------------------------------------------------------------

void loop() {
  handleSerialCommands();
  handleSerialStreaming();

  if (WiFi.status() != WL_CONNECTED) {
    if (
      millis() - lastWifiAttemptMs >=
      WIFI_RETRY_MS
    ) {
      lastWifiAttemptMs = millis();

      Serial.println(
        "Wi-Fi disconnected. Reconnecting..."
      );

      connectWifi();
    }

    delay(5);
    return;
  }

  webServer.handleClient();

  WiFiClient newClient =
    tcpServer.available();

  if (newClient) {
    if (
      tcpClient &&
      tcpClient.connected()
    ) {
      tcpClient.stop();
    }

    tcpClient = newClient;
    tcpClient.setTimeout(50);

    Serial.println(
      "TCP client connected"
    );
  }

  if (
    tcpClient &&
    !tcpClient.connected()
  ) {
    tcpClient.stop();
    tcpStreaming = false;

    Serial.println(
      "TCP client disconnected"
    );
  }

  if (
    tcpClient &&
    tcpClient.connected() &&
    tcpClient.available()
  ) {
    String command =
      readTcpCommand(tcpClient);

    handleTcpCommand(
      command,
      tcpClient
    );
  }

  if (
    tcpStreaming &&
    tcpClient &&
    tcpClient.connected() &&
    (
      lastTcpSampleMs == 0 ||
      millis() - lastTcpSampleMs >=
        SAMPLE_INTERVAL_MS
    )
  ) {
    lastTcpSampleMs = millis();

    sendTcpSample(tcpClient);
  }

  delay(2);
}