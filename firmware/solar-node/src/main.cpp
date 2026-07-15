// BardBox Solar Panel Wi-Fi Node Template
// Board: Adafruit Feather ESP32-S3 No PSRAM
// Current sensor source: simulated solar-panel voltage, 0.000 to 10.000 V
// Future sensor source: external ADC
// Transport: Wi-Fi TCP server + HTTP status page
// Protocol: bardbox-node-v1

#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <math.h>
#include "secrets.h"

const char* WIFI_SSID = WIFI_SSID_VALUE;
const char* WIFI_PASS = WIFI_PASS_VALUE;

#define DEVICE_UID DEVICE_UID_VALUE
#define FW_VERSION "0.1.0"
#define PROTOCOL_VERSION "bardbox-node-v1"
#define NODE_TYPE "solar_panel"
#define NODE_MODEL "dummy_voltage_wifi"

// Five seconds is convenient while developing. This can be changed later.
const unsigned long SAMPLE_INTERVAL_MS = 5000;
const unsigned long WIFI_RETRY_MS = 10000;

WiFiServer tcpServer(TCP_SERVER_PORT_VALUE);
WebServer webServer(HTTP_SERVER_PORT_VALUE);
WiFiClient tcpClient;

bool tcpStreaming = false;
bool serialStreaming = false;
unsigned long lastTcpSampleMs = 0;
unsigned long lastSerialSampleMs = 0;
unsigned long lastWifiAttemptMs = 0;

// -----------------------------------------------------------------------------
// Sensor abstraction
// -----------------------------------------------------------------------------
// Mohamed should eventually replace only this function with the ADC reading and
// scaling code. The rest of the BardBox transport/debug interface can remain.
float readPanelVoltage(bool& ok) {
  // Simulate a smooth day/night-like waveform between 0 and 10 V.
  // One complete cycle takes 60 seconds, which makes testing easy to observe.
  const float cycleMs = 60000.0f;
  const float phase = fmodf((float)millis(), cycleMs) / cycleMs;
  const float normalized = 0.5f - 0.5f * cosf(phase * TWO_PI);

  ok = true;
  return normalized * 10.0f;
}

// -----------------------------------------------------------------------------
// Shared BardBox output helpers
// -----------------------------------------------------------------------------
void sendTcpHeader(WiFiClient& client) {
  client.println("HDR,v1,panel_voltage_v,voltage_ok,rssi_dbm");
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
  client.print(" sensors=DUMMY_VOLTAGE");
  client.print(" ip=");
  client.print(WiFi.localIP());
  client.print(" mac=");
  client.print(WiFi.macAddress());
  client.print(" rssi_dbm=");
  client.println(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
}

void sendTcpStatus(WiFiClient& client) {
  client.print("OK STATUS ");
  client.print(tcpStreaming ? "RUNNING" : "STOPPED");
  client.print(" wifi=");
  client.print(WiFi.status() == WL_CONNECTED ? "CONNECTED" : "DISCONNECTED");
  client.print(" ip=");
  client.print(WiFi.localIP());
  client.print(" rssi_dbm=");
  client.println(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
}

bool sendTcpSample(WiFiClient& client) {
  bool voltageOk = false;
  const float panelVoltage = readPanelVoltage(voltageOk);

  client.print("DAT,");
  if (voltageOk) client.print(panelVoltage, 3);
  else client.print("nan");
  client.print(",");
  client.print(voltageOk ? 1 : 0);
  client.print(",");
  client.println(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);

  return voltageOk;
}

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
  Serial.print(",sample_interval_ms=");
  Serial.print(SAMPLE_INTERVAL_MS);
  Serial.print(",wifi=");
  Serial.print(WiFi.status() == WL_CONNECTED ? 1 : 0);
  Serial.print(",ip=");
  Serial.print(WiFi.localIP());
  Serial.print(",mac=");
  Serial.print(WiFi.macAddress());
  Serial.print(",rssi_dbm=");
  Serial.println(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
}

void printSerialHeader() {
  Serial.println(
    "millis,uid,node_type,node_model,fw_version,protocol_version,transport,"
    "panel_voltage_v,voltage_ok,wifi,rssi_dbm"
  );
}

void printSerialRead() {
  bool voltageOk = false;
  const float panelVoltage = readPanelVoltage(voltageOk);

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
  Serial.print(",wifi_tcp,");

  if (voltageOk) Serial.print(panelVoltage, 3);
  else Serial.print("nan");

  Serial.print(",");
  Serial.print(voltageOk ? 1 : 0);
  Serial.print(",");
  Serial.print(WiFi.status() == WL_CONNECTED ? 1 : 0);
  Serial.print(",");
  Serial.println(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
}

// -----------------------------------------------------------------------------
// Serial command interface
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
    Serial.print(serialStreaming ? "RUNNING" : "STOPPED");
    Serial.print(",wifi=");
    Serial.println(WiFi.status() == WL_CONNECTED ? "CONNECTED" : "DISCONNECTED");
  } else {
    Serial.println("ERR,UNKNOWN_COMMAND");
  }
}

void handleSerialCommands() {
  static char commandBuffer[64];
  static size_t commandIndex = 0;

  while (Serial.available() > 0) {
    const char character = (char)Serial.read();

    if (character == '\n' || character == '\r') {
      if (commandIndex > 0) {
        commandBuffer[commandIndex] = '\0';
        processSerialCommand(commandBuffer);
        commandIndex = 0;
      }
    } else if (commandIndex < sizeof(commandBuffer) - 1) {
      commandBuffer[commandIndex++] = character;
    } else {
      commandIndex = 0;
      Serial.println("ERR,COMMAND_TOO_LONG");
    }
  }
}

void handleSerialStreaming() {
  if (!serialStreaming) return;

  if (lastSerialSampleMs == 0 ||
      millis() - lastSerialSampleMs >= SAMPLE_INTERVAL_MS) {
    lastSerialSampleMs = millis();
    printSerialRead();
  }
}

// -----------------------------------------------------------------------------
// TCP command interface
// -----------------------------------------------------------------------------
String readTcpCommand(WiFiClient& client) {
  String command;

  while (client.available()) {
    const char character = (char)client.read();
    if (character == '\r') continue;
    if (character == '\n') break;
    command += character;
  }

  command.trim();
  return command;
}

void handleTcpCommand(const String& command, WiFiClient& client) {
  if (command.length() == 0) return;

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
// HTTP diagnostics
// -----------------------------------------------------------------------------
void handleHttpRoot() {
  bool voltageOk = false;
  const float panelVoltage = readPanelVoltage(voltageOk);

  String body;
  body += "uid=" + String(DEVICE_UID) + "\n";
  body += "node_type=" + String(NODE_TYPE) + "\n";
  body += "node_model=" + String(NODE_MODEL) + "\n";
  body += "fw_version=" + String(FW_VERSION) + "\n";
  body += "protocol_version=" + String(PROTOCOL_VERSION) + "\n";
  body += "panel_voltage_v=";
  body += voltageOk ? String(panelVoltage, 3) : "nan";
  body += "\nvoltage_ok=" + String(voltageOk ? 1 : 0) + "\n";
  body += "wifi=" + String(WiFi.status() == WL_CONNECTED ? 1 : 0) + "\n";
  body += "ip=" + WiFi.localIP().toString() + "\n";
  body += "mac=" + WiFi.macAddress() + "\n";
  body += "rssi_dbm=" + String(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0) + "\n";
  body += "millis=" + String(millis()) + "\n";

  webServer.send(200, "text/plain", body);
}

void handleHttpJson() {
  bool voltageOk = false;
  const float panelVoltage = readPanelVoltage(voltageOk);

  String body = "{";
  body += "\"uid\":\"" + String(DEVICE_UID) + "\",";
  body += "\"node_type\":\"" + String(NODE_TYPE) + "\",";
  body += "\"node_model\":\"" + String(NODE_MODEL) + "\",";
  body += "\"fw_version\":\"" + String(FW_VERSION) + "\",";
  body += "\"protocol_version\":\"" + String(PROTOCOL_VERSION) + "\",";
  body += "\"panel_voltage_v\":";
  body += voltageOk ? String(panelVoltage, 3) : "null";
  body += ",\"voltage_ok\":" + String(voltageOk ? 1 : 0);
  body += ",\"wifi\":" + String(WiFi.status() == WL_CONNECTED ? 1 : 0);
  body += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
  body += ",\"mac\":\"" + WiFi.macAddress() + "\"";
  body += ",\"rssi_dbm\":" + String(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
  body += ",\"millis\":" + String(millis());
  body += "}";

  webServer.send(200, "application/json", body);
}

// -----------------------------------------------------------------------------
// Wi-Fi lifecycle
// -----------------------------------------------------------------------------
void startNetworkServices() {
  tcpServer.begin();
  tcpServer.setNoDelay(true);

  webServer.on("/", handleHttpRoot);
  webServer.on("/json", handleHttpJson);
  webServer.begin();

  Serial.print("TCP server: ");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(TCP_SERVER_PORT_VALUE);

  Serial.print("HTTP status: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/");
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.print("Connecting to Wi-Fi SSID: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  const unsigned long connectionStartMs = millis();
  while (WiFi.status() != WL_CONNECTED &&
         millis() - connectionStartMs < 15000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("=== WIFI CONNECTED ===");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("MAC: ");
    Serial.println(WiFi.macAddress());
    Serial.print("RSSI: ");
    Serial.println(WiFi.RSSI());
    startNetworkServices();
  } else {
    Serial.println("Wi-Fi connection failed; will retry.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("=== BardBox Solar Wi-Fi Node Start ===");
  Serial.println("Type INFO, PING, STATUS, HEADER, READ, START, or STOP.");

  connectWifi();
}

void loop() {
  handleSerialCommands();
  handleSerialStreaming();

  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastWifiAttemptMs >= WIFI_RETRY_MS) {
      lastWifiAttemptMs = millis();
      Serial.println("Wi-Fi disconnected. Reconnecting...");
      connectWifi();
    }
    delay(5);
    return;
  }

  webServer.handleClient();

  WiFiClient newClient = tcpServer.available();
  if (newClient) {
    if (tcpClient && tcpClient.connected()) tcpClient.stop();
    tcpClient = newClient;
    tcpClient.setTimeout(50);
    Serial.println("TCP client connected");
  }

  if (tcpClient && !tcpClient.connected()) {
    tcpClient.stop();
    tcpStreaming = false;
    Serial.println("TCP client disconnected");
  }

  if (tcpClient && tcpClient.connected() && tcpClient.available()) {
    const String command = readTcpCommand(tcpClient);
    handleTcpCommand(command, tcpClient);
  }

  if (tcpStreaming && tcpClient && tcpClient.connected() &&
      (lastTcpSampleMs == 0 ||
       millis() - lastTcpSampleMs >= SAMPLE_INTERVAL_MS)) {
    lastTcpSampleMs = millis();
    sendTcpSample(tcpClient);
  }

  delay(2);
}
