#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <NimBLEDevice.h>

// Nexgrill NXE packet (same offsets as server.py decode_packet).
static const char *TARGET_PREFIX = "NXE";
static NimBLEUUID CHAR_TEMP("0000cc01-0000-1000-8000-00805f9b34fb");
static NimBLEUUID CHAR_IP("0000bb01-0000-1000-8000-00805f9b34fb");

#ifndef RELAY_HTTP_PORT
#define RELAY_HTTP_PORT 80
#endif
static WebServer server(RELAY_HTTP_PORT);
static String lastJson = "{\"ok\":false}";
static String lastName = "";
static String lastIp = "";
static int lastRssi = 0;
static bool haveReading = false;
static bool scanning = false;
static bool haveTarget = false;
static NimBLEAddress targetAddr;

static NimBLEClient *bleClient = nullptr;
static NimBLERemoteCharacteristic *tempChar = nullptr;
static uint32_t lastReadMs = 0;
static const uint32_t READ_EVERY_MS = 5000;

static uint16_t u16le(const uint8_t *p) {
  return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static void publish(const uint8_t *data, size_t len) {
  if (len < 20) {
    return;
  }
  // LAN JSON only — no Bluetooth address field.
  char buf[320];
  snprintf(
      buf, sizeof(buf),
      "{\"ok\":true,\"setPoint\":%u,\"grill\":%u,\"probeTargets\":[%u,%u],"
      "\"probes\":[%u,%u],\"rssi\":%d,\"name\":\"%s\",\"smokerIp\":\"%s\"}",
      u16le(data + 4), u16le(data + 6), u16le(data + 8), u16le(data + 10),
      u16le(data + 16), u16le(data + 18), lastRssi, lastName.c_str(),
      lastIp.c_str());
  lastJson = buf;
  haveReading = true;
  Serial.printf("reading ok grill=%u set=%u\n", u16le(data + 6), u16le(data + 4));
}

class ScanCallbacks : public NimBLEAdvertisedDeviceCallbacks {
 public:
  void onResult(NimBLEAdvertisedDevice *adv) override {
    std::string name = adv->getName();
    if (name.rfind(TARGET_PREFIX, 0) != 0) {
      return;
    }
    lastName = name.c_str();
    lastRssi = adv->getRSSI();
    targetAddr = adv->getAddress();
    haveTarget = true;
    NimBLEDevice::getScan()->stop();
  }
};

static ScanCallbacks scanCbs;

static NimBLERemoteCharacteristic *findChar(NimBLEClient *client, const NimBLEUUID &uuid) {
  std::vector<NimBLERemoteService *> *services = client->getServices(true);
  if (!services) {
    return nullptr;
  }
  for (auto *svc : *services) {
    NimBLERemoteCharacteristic *ch = svc->getCharacteristic(uuid);
    if (ch) {
      return ch;
    }
  }
  return nullptr;
}

static bool connectAndRead() {
  if (!haveTarget) {
    return false;
  }
  if (!bleClient) {
    bleClient = NimBLEDevice::createClient();
    bleClient->setConnectTimeout(15);
  }
  if (bleClient->isConnected()) {
    bleClient->disconnect();
    delay(200);
  }
  tempChar = nullptr;
  if (!bleClient->connect(targetAddr, false)) {
    Serial.println("connect failed");
    return false;
  }
  lastRssi = bleClient->getRssi();
  tempChar = findChar(bleClient, CHAR_TEMP);
  if (!tempChar || !tempChar->canRead()) {
    Serial.println("no temp characteristic");
    bleClient->disconnect();
    tempChar = nullptr;
    return false;
  }
  NimBLERemoteCharacteristic *ipCh = findChar(bleClient, CHAR_IP);
  if (ipCh && ipCh->canRead()) {
    std::string ip = ipCh->readValue();
    String cleaned;
    for (char c : ip) {
      if (c >= 32 && c < 127) {
        cleaned += c;
      }
    }
    lastIp = cleaned;
  }
  std::string raw = tempChar->readValue();
  publish(reinterpret_cast<const uint8_t *>(raw.data()), raw.size());
  return haveReading;
}

static void onScanDone(NimBLEScanResults) {
  scanning = false;
}

static void handleReading() {
  server.send(haveReading ? 200 : 503, "application/json", lastJson);
}

static String radioIp() {
  if (WiFi.status() == WL_CONNECTED) {
    return WiFi.localIP().toString();
  }
  return WiFi.softAPIP().toString();
}

static void handleHealth() {
  char buf[192];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"ble\":%s,\"haveReading\":%s,\"ip\":\"%s\"}",
           bleClient && bleClient->isConnected() ? "true" : "false",
           haveReading ? "true" : "false", radioIp().c_str());
  server.send(200, "application/json", buf);
}

static void handleRoot() {
  server.send(200, "application/json", "{\"ok\":true,\"service\":\"smoker-ble-relay\"}");
}

static void startWifi() {
  if (RELAY_STA_SSID[0] != '\0') {
    WiFi.mode(WIFI_STA);
    WiFi.begin(RELAY_STA_SSID, RELAY_STA_PASS);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
      delay(250);
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.print("sta ip ");
      Serial.println(WiFi.localIP());
      return;
    }
    Serial.println("sta join failed; falling back to AP");
  }
  WiFi.mode(WIFI_AP);
  WiFi.softAP(RELAY_AP_SSID, RELAY_AP_PASS);
  Serial.print("ap ip ");
  Serial.println(WiFi.softAPIP());
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("smoker-ble-relay boot");

  startWifi();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/reading", HTTP_GET, handleReading);
  server.on("/health", HTTP_GET, handleHealth);
  server.begin();

  NimBLEDevice::init("smoker-relay");
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEScan *scan = NimBLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(&scanCbs, false);
  scan->setActiveScan(true);
  scan->setInterval(134);
  scan->setWindow(89);
}

void loop() {
  server.handleClient();

  if (bleClient && bleClient->isConnected()) {
    if (millis() - lastReadMs >= READ_EVERY_MS) {
      lastReadMs = millis();
      if (tempChar && tempChar->canRead()) {
        lastRssi = bleClient->getRssi();
        std::string raw = tempChar->readValue();
        publish(reinterpret_cast<const uint8_t *>(raw.data()), raw.size());
      } else {
        bleClient->disconnect();
        tempChar = nullptr;
      }
    }
    return;
  }

  tempChar = nullptr;

  if (!scanning && !haveTarget) {
    haveReading = false;
    lastJson = "{\"ok\":false}";
    scanning = true;
    NimBLEDevice::getScan()->start(8, onScanDone, false);
    return;
  }

  if (haveTarget && !(bleClient && bleClient->isConnected())) {
    if (connectAndRead()) {
      lastReadMs = millis();
    } else {
      haveTarget = false;
    }
  }
}
