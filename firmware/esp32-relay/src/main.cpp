#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <NimBLEDevice.h>

#ifndef RELAY_AP_PASS
#include "secrets.h"
#endif
#ifndef RELAY_AP_PASS
#error "Copy firmware/esp32-relay/secrets.example.h to secrets.h and set RELAY_AP_PASS. Do not commit secrets.h or house Wi-Fi."
#endif
#ifndef RELAY_AP_SSID
#define RELAY_AP_SSID "smoker-relay"
#endif

// Nexgrill NXE packet (same offsets as server.py decode_packet).
static const char *TARGET_PREFIX = "NXE";
static NimBLEUUID CHAR_TEMP("0000cc01-0000-1000-8000-00805f9b34fb");
static NimBLEUUID CHAR_IP("0000bb01-0000-1000-8000-00805f9b34fb");

#ifndef RELAY_HTTP_PORT
#define RELAY_HTTP_PORT 80
#endif
static WebServer server(RELAY_HTTP_PORT);
static Preferences prefs;
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
static String staSsid = "";
static String staStatus = "not joined";
static String relayName = "smoker-relay";
static uint32_t bleReadyMs = 0;

static uint16_t u16le(const uint8_t *p) {
  return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static String sanitizeRelayName(String s) {
  s.trim();
  String out;
  for (size_t i = 0; i < s.length() && out.length() < 32; i++) {
    char c = s[i];
    if (c >= 32 && c < 127 && c != '"' && c != '\\' && c != '\'' && c != '<' && c != '>' && c != '&') {
      out += c;
    }
  }
  out.trim();
  if (!out.length()) {
    return String("smoker-relay");
  }
  return out;
}

static String jsonEscape(const String &s) {
  String out;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '"' || c == '\\') {
      out += '\\';
    }
    if (c >= 32 && c < 127) {
      out += c;
    }
  }
  return out;
}

static void publish(const uint8_t *data, size_t len) {
  Serial.printf("pkt len=%u hex=", (unsigned)len);
  for (size_t i = 0; i < len && i < 24; i++) {
    Serial.printf("%02x", data[i]);
  }
  Serial.println();
  if (len < 8) {
    Serial.printf("packet too short len=%u\n", (unsigned)len);
    return;
  }
  bool printable = len > 0;
  for (size_t i = 0; i < len; i++) {
    uint8_t c = data[i];
    if (c == 0) {
      break;
    }
    if (c < 32 || c > 126) {
      printable = false;
      break;
    }
  }
  if (printable) {
    Serial.println("skip ascii characteristic (not NXE temps)");
    return;
  }
  uint16_t setPoint = u16le(data + 4);
  uint16_t grill = u16le(data + 6);
  uint16_t pt0 = (len >= 10) ? u16le(data + 8) : 0;
  uint16_t pt1 = (len >= 12) ? u16le(data + 10) : 0;
  uint16_t p0 = (len >= 18) ? u16le(data + 16) : ((len >= 14) ? u16le(data + 12) : 0);
  uint16_t p1 = (len >= 20) ? u16le(data + 18) : ((len >= 16) ? u16le(data + 14) : 0);
  char buf[320];
  snprintf(
      buf, sizeof(buf),
      "{\"ok\":true,\"setPoint\":%u,\"grill\":%u,\"probeTargets\":[%u,%u],"
      "\"probes\":[%u,%u],\"rssi\":%d,\"name\":\"%s\",\"smokerIp\":\"%s\",\"len\":%u}",
      setPoint, grill, pt0, pt1, p0, p1, lastRssi, lastName.c_str(), lastIp.c_str(),
      (unsigned)len);
  lastJson = buf;
  haveReading = true;
  Serial.printf("reading ok grill=%u set=%u len=%u\n", grill, setPoint, (unsigned)len);
}

class ScanCallbacks : public NimBLEAdvertisedDeviceCallbacks {
 public:
  void onResult(NimBLEAdvertisedDevice *adv) override {
    // Do not stop the scan from this callback — NimBLE 1.4 LoadProhibited on ESP32.
    std::string name = adv->getName();
    if (name.rfind(TARGET_PREFIX, 0) != 0) {
      return;
    }
    lastName = name.c_str();
    lastRssi = adv->getRSSI();
    targetAddr = adv->getAddress();
    haveTarget = true;
  }
};

static ScanCallbacks scanCbs;

static NimBLERemoteCharacteristic *findChar(NimBLEClient *client, const NimBLEUUID &uuid) {
  if (!client || !client->isConnected()) {
    return nullptr;
  }
  std::vector<NimBLERemoteService *> *services = client->getServices(true);
  if (!services) {
    return nullptr;
  }
  for (auto *svc : *services) {
    if (!svc) {
      continue;
    }
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
  String nameEsc = jsonEscape(relayName);
  char buf[320];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"name\":\"%s\",\"ble\":%s,\"haveReading\":%s,\"ap\":\"%s\",\"sta\":\"%s\"}",
           nameEsc.c_str(),
           bleClient && bleClient->isConnected() ? "true" : "false",
           haveReading ? "true" : "false",
           WiFi.softAPIP().toString().c_str(),
           WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "");
  server.send(200, "application/json", buf);
}

static void sendForm(const char *flash) {
  String html;
  html += "<!doctype html><html><head><meta charset=utf-8>";
  html += "<meta name=viewport content='width=device-width,initial-scale=1'>";
  html += "<title>";
  html += relayName;
  html += "</title></head><body>";
  html += "<h1>";
  html += relayName;
  html += "</h1>";
  html += "<p>SoftAP stays up. House Wi-Fi and relay name are stored on this board only.</p>";
  if (flash && flash[0]) {
    html += "<p>";
    html += flash;
    html += "</p>";
  }
  html += "<p>STA status: ";
  html += staStatus;
  html += "</p>";
  html += "<p>LAN IP: ";
  html += (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : "(not joined)";
  html += "</p>";
  html += "<form method=POST action=/wifi>";
  html += "<p>Relay name<br><input name=name maxlength=32 value='";
  html += relayName;
  html += "'></p>";
  html += "<p>House SSID<br><input name=ssid value='";
  html += staSsid;
  html += "'></p>";
  html += "<p>Password<br><input name=pass type=password></p>";
  html += "<p><button type=submit>Save / join house Wi-Fi</button></p>";
  html += "</form></body></html>";
  server.send(200, "text/html", html);
}

static void handleRoot() { sendForm(""); }

static void handleCaptive() {
  server.sendHeader("Location", "http://192.168.4.1/", true);
  server.send(302, "text/plain", "");
}

static void trySta(const String &ssid, const String &pass) {
  if (!ssid.length()) {
    staStatus = "not joined";
    return;
  }
  Serial.println("sta join start");
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    staStatus = "joined";
    Serial.print("sta ip ");
    Serial.println(WiFi.localIP());
  } else {
    staStatus = "join failed";
    Serial.println("sta join failed");
    WiFi.disconnect(false, false);
    // Fall back to SoftAP so house Wi-Fi can be re-entered without a serial flash.
    WiFi.mode(WIFI_AP);
    WiFi.softAP(RELAY_AP_SSID, RELAY_AP_PASS);
    Serial.print("ap ip ");
    Serial.println(WiFi.softAPIP());
  }
}

static void handleWifiPost() {
  String ssid = server.arg("ssid");
  String pass = server.arg("pass");
  String name = sanitizeRelayName(server.arg("name"));
  ssid.trim();
  prefs.begin("relay", false);
  prefs.putString("name", name);
  relayName = name;
  if (!ssid.length()) {
    prefs.end();
    sendForm("Relay name saved. SSID required to join house Wi-Fi.");
    return;
  }
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
  staSsid = ssid;
  trySta(ssid, pass);
  sendForm(WiFi.status() == WL_CONNECTED ? "Saved. Joined house Wi-Fi." : "Saved, but join failed. Recheck the password.");
}

static void startWifi() {
  prefs.begin("relay", true);
  relayName = sanitizeRelayName(prefs.getString("name", "smoker-relay"));
  staSsid = prefs.getString("ssid", "");
  String pass = prefs.getString("pass", "");
  prefs.end();

  // Never run SoftAP+STA with NimBLE on this ESP32 — modem-sleep abort.
  if (staSsid.length()) {
    trySta(staSsid, pass);
  } else {
    WiFi.mode(WIFI_AP);
    WiFi.softAP(RELAY_AP_SSID, RELAY_AP_PASS);
    Serial.print("ap ip ");
    Serial.println(WiFi.softAPIP());
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("smoker-ble-relay boot");

  startWifi();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/wifi", HTTP_POST, handleWifiPost);
  server.on("/api/reading", HTTP_GET, handleReading);
  server.on("/health", HTTP_GET, handleHealth);
  server.on("/generate_204", HTTP_GET, handleCaptive);
  server.on("/hotspot-detect.html", HTTP_GET, handleCaptive);
  server.on("/fwlink", HTTP_GET, handleCaptive);
  server.begin();

  // BLE after Wi-Fi is STA-only (or SoftAP-only). Modem sleep stays enabled.
  delay(1000);
  NimBLEDevice::init("smoker-relay");
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEScan *scan = NimBLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(&scanCbs, false);
  scan->setActiveScan(false);
  scan->setInterval(160);
  scan->setWindow(40);
  Serial.println("ble ready");
}

void loop() {
  server.handleClient();

  if (bleReadyMs == 0) {
    bleReadyMs = millis() + 1500;
  }
  if (millis() < bleReadyMs) {
    return;
  }

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

  if (haveTarget && scanning) {
    NimBLEDevice::getScan()->stop();
    scanning = false;
    return;
  }

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
