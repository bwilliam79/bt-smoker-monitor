#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <Update.h>
#include <NimBLEDevice.h>
#include <esp_system.h>
#include <cstring>

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
static const size_t OTA_MAX = 1572864;

static WebServer server(RELAY_HTTP_PORT);
static Preferences prefs;
static String lastJson = "{\"ok\":false}";
static String lastName = "";
static String lastIp = "";
static String lastErr = "";
static String lastPacketChar = "";
static String gattJson = "{\"ok\":false}";
static String otaToken = "";
static int lastRssi = 0;
static bool haveReading = false;
static bool scanning = false;
static bool haveTarget = false;
static bool otaBusy = false;
static bool otaAuthed = false;
static size_t otaGot = 0;
static bool bleInited = false;
static NimBLEAddress targetAddr;

static NimBLEClient *bleClient = nullptr;
static NimBLERemoteCharacteristic *tempChar = nullptr;
static uint32_t lastReadMs = 0;
static const uint32_t READ_EVERY_MS = 15000;
static String staSsid = "";
static String staStatus = "not joined";
static String relayName = "smoker-relay";
static uint32_t bleReadyMs = 0;

static uint8_t notifyBuf[32];
static size_t notifyLen = 0;
static NimBLERemoteCharacteristic *notifySrc = nullptr;
static volatile bool notifyPending = false;

static uint16_t u16le(const uint8_t *p) {
  return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static bool isSoftAp() {
  wifi_mode_t m = WiFi.getMode();
  return m == WIFI_AP || m == WIFI_AP_STA;
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

static String makeToken() {
  char buf[17];
  snprintf(buf, sizeof(buf), "%08x%08x", (unsigned)esp_random(), (unsigned)esp_random());
  return String(buf);
}

static void setLastErr(const char *s) { lastErr = s; }

static bool looksAscii(const uint8_t *data, size_t len) {
  if (!len) {
    return false;
  }
  for (size_t i = 0; i < len; i++) {
    uint8_t c = data[i];
    if (c == 0) {
      break;
    }
    if (c < 32 || c > 126) {
      return false;
    }
  }
  return true;
}

static void hexAppend(String &out, const uint8_t *data, size_t len, size_t cap) {
  char hex[3];
  size_t n = len < cap ? len : cap;
  for (size_t i = 0; i < n; i++) {
    snprintf(hex, sizeof(hex), "%02x", data[i]);
    out += hex;
  }
}

static void publish(const uint8_t *data, size_t len, const char *uuid) {
  Serial.printf("pkt len=%u hex=", (unsigned)len);
  for (size_t i = 0; i < len && i < 24; i++) {
    Serial.printf("%02x", data[i]);
  }
  Serial.println();
  if (len < 20) {
    setLastErr("packet too short");
    Serial.printf("packet too short len=%u\n", (unsigned)len);
    return;
  }
  if (looksAscii(data, len)) {
    setLastErr("ascii skip (not NXE temps)");
    Serial.println("skip ascii characteristic (not NXE temps)");
    return;
  }
  uint16_t setPoint = u16le(data + 4);
  uint16_t grill = u16le(data + 6);
  uint16_t pt0 = u16le(data + 8);
  uint16_t pt1 = u16le(data + 10);
  uint16_t p0 = u16le(data + 16);
  uint16_t p1 = u16le(data + 18);
  if (uuid && uuid[0]) {
    lastPacketChar = uuid;
  }
  String charEsc = jsonEscape(lastPacketChar);
  char buf[420];
  snprintf(
      buf, sizeof(buf),
      "{\"ok\":true,\"setPoint\":%u,\"grill\":%u,\"probeTargets\":[%u,%u],"
      "\"probes\":[%u,%u],\"rssi\":%d,\"wifiRssi\":%d,\"name\":\"%s\",\"smokerIp\":\"%s\","
      "\"len\":%u,\"char\":\"%s\"}",
      setPoint, grill, pt0, pt1, p0, p1, lastRssi,
      (WiFi.status() == WL_CONNECTED) ? WiFi.RSSI() : 0,
      lastName.c_str(), lastIp.c_str(), (unsigned)len, charEsc.c_str());
  lastJson = buf;
  haveReading = true;
  setLastErr("");
  Serial.printf("reading ok grill=%u set=%u len=%u chr=%s\n", grill, setPoint, (unsigned)len,
                lastPacketChar.c_str());
}

static void onNotify(NimBLERemoteCharacteristic *ch, uint8_t *data, size_t len, bool isNotify) {
  (void)isNotify;
  if (notifyPending || !data || len == 0) {
    return;
  }
  size_t n = len < sizeof(notifyBuf) ? len : sizeof(notifyBuf);
  memcpy(notifyBuf, data, n);
  notifyLen = n;
  notifySrc = ch;
  notifyPending = true;
}

static void dumpGattJson(NimBLEClient *client) {
  gattJson = "{\"ok\":false}";
  if (!client || !client->isConnected()) {
    return;
  }
  std::vector<NimBLERemoteService *> *services = client->getServices(true);
  if (!services) {
    Serial.println("gatt: no services");
    return;
  }
  String out = "{\"ok\":true,\"services\":[";
  bool firstSvc = true;
  Serial.printf("gatt services=%u\n", (unsigned)services->size());
  for (auto *svc : *services) {
    if (!svc) {
      continue;
    }
    std::string su = svc->getUUID().toString();
    Serial.printf("svc %s\n", su.c_str());
    if (!firstSvc) {
      out += ",";
    }
    firstSvc = false;
    out += "{\"uuid\":\"";
    out += jsonEscape(String(su.c_str()));
    out += "\",\"chars\":[";
    std::vector<NimBLERemoteCharacteristic *> *chars = svc->getCharacteristics(true);
    bool firstCh = true;
    if (chars) {
      for (auto *ch : *chars) {
        if (!ch) {
          continue;
        }
        std::string cu = ch->getUUID().toString();
        Serial.printf(
            "  chr %s r=%d w=%d n=%d i=%d\n",
            cu.c_str(),
            ch->canRead() ? 1 : 0,
            ch->canWrite() ? 1 : 0,
            ch->canNotify() ? 1 : 0,
            ch->canIndicate() ? 1 : 0);
        if (!firstCh) {
          out += ",";
        }
        firstCh = false;
        out += "{\"uuid\":\"";
        out += jsonEscape(String(cu.c_str()));
        out += "\",\"r\":";
        out += ch->canRead() ? "1" : "0";
        out += ",\"w\":";
        out += ch->canWrite() ? "1" : "0";
        out += ",\"n\":";
        out += ch->canNotify() ? "1" : "0";
        out += ",\"i\":";
        out += ch->canIndicate() ? "1" : "0";
        if (ch->canRead()) {
          std::string raw = ch->readValue();
          out += ",\"len\":";
          out += String((unsigned)raw.size());
          out += ",\"hex\":\"";
          hexAppend(out, reinterpret_cast<const uint8_t *>(raw.data()), raw.size(), 24);
          out += "\"";
          if (looksAscii(reinterpret_cast<const uint8_t *>(raw.data()), raw.size())) {
            out += ",\"ascii\":1";
          }
        }
        out += "}";
      }
    }
    out += "]}";
  }
  out += "]}";
  gattJson = out;
}

static bool subscribeAll(NimBLEClient *client) {
  if (!client || !client->isConnected()) {
    return false;
  }
  std::vector<NimBLERemoteService *> *services = client->getServices(false);
  if (!services) {
    return false;
  }
  unsigned n = 0;
  for (auto *svc : *services) {
    if (!svc) {
      continue;
    }
    std::vector<NimBLERemoteCharacteristic *> *chars = svc->getCharacteristics(false);
    if (!chars) {
      continue;
    }
    for (auto *ch : *chars) {
      if (!ch) {
        continue;
      }
      bool wantNotify = ch->canNotify();
      bool wantIndicate = !wantNotify && ch->canIndicate();
      if (!wantNotify && !wantIndicate) {
        continue;
      }
      if (ch->subscribe(onNotify, wantNotify, false)) {
        n++;
        Serial.printf("subscribed %s notify=%d\n", ch->getUUID().toString().c_str(), wantNotify ? 1 : 0);
      } else {
        Serial.printf("subscribe failed %s\n", ch->getUUID().toString().c_str());
      }
    }
  }
  Serial.printf("subscribed count=%u\n", n);
  return n > 0;
}

static void tryReadBinary(NimBLEClient *client) {
  if (!client || !client->isConnected()) {
    return;
  }
  std::vector<NimBLERemoteService *> *services = client->getServices(false);
  if (!services) {
    return;
  }
  for (auto *svc : *services) {
    if (!svc) {
      continue;
    }
    std::vector<NimBLERemoteCharacteristic *> *chars = svc->getCharacteristics(false);
    if (!chars) {
      continue;
    }
    for (auto *ch : *chars) {
      if (!ch || !ch->canRead()) {
        continue;
      }
      if (ch->getUUID().equals(CHAR_IP)) {
        continue;
      }
      std::string raw = ch->readValue();
      if (raw.size() < 20) {
        continue;
      }
      if (looksAscii(reinterpret_cast<const uint8_t *>(raw.data()), raw.size())) {
        continue;
      }
      std::string cu = ch->getUUID().toString();
      publish(reinterpret_cast<const uint8_t *>(raw.data()), raw.size(), cu.c_str());
      if (haveReading) {
        return;
      }
    }
  }
}

class ScanCallbacks : public NimBLEAdvertisedDeviceCallbacks {
 public:
  void onResult(NimBLEAdvertisedDevice *adv) override {
    if (!adv || otaBusy) {
      return;
    }
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

static bool connectAndSubscribe() {
  if (!haveTarget || otaBusy) {
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
  notifyPending = false;
  notifySrc = nullptr;
  notifyLen = 0;
  if (!bleClient->connect(targetAddr, false)) {
    setLastErr("connect failed");
    Serial.println("connect failed");
    return false;
  }
  lastRssi = bleClient->getRssi();
  dumpGattJson(bleClient);
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
  tempChar = findChar(bleClient, CHAR_TEMP);
  if (tempChar && tempChar->canRead()) {
    std::string raw = tempChar->readValue();
    std::string cu = tempChar->getUUID().toString();
    publish(reinterpret_cast<const uint8_t *>(raw.data()), raw.size(), cu.c_str());
  }
  if (!haveReading) {
    tryReadBinary(bleClient);
  }
  if (subscribeAll(bleClient)) {
    if (!haveReading) {
      setLastErr("subscribed, waiting packet");
    }
  } else if (!haveReading) {
    setLastErr("no notify characteristic");
    Serial.println("no notify characteristic");
  }
  return true;
}

static void onScanDone(NimBLEScanResults) { scanning = false; }

static void pauseBle() {
  otaBusy = true;
  scanning = false;
  haveTarget = false;
  tempChar = nullptr;
  notifyPending = false;
  notifySrc = nullptr;
  if (bleClient && bleClient->isConnected()) {
    bleClient->disconnect();
  }
  if (bleInited) {
    NimBLEDevice::getScan()->stop();
    NimBLEDevice::deinit(true);
    bleClient = nullptr;
    bleInited = false;
  }
}

static bool headerTokenOk() {
  if (!otaToken.length()) {
    return false;
  }
  String t = server.header("X-Relay-Token");
  t.trim();
  return t.length() > 0 && t == otaToken;
}

static bool formTokenOk() {
  if (!otaToken.length()) {
    return false;
  }
  String t = server.arg("token");
  t.trim();
  return t.length() > 0 && t == otaToken;
}

static bool tokenOk() {
  return headerTokenOk() || formTokenOk();
}

static bool requireAuth() {
  if (tokenOk()) {
    return true;
  }
  server.send(401, "application/json", "{\"ok\":false,\"error\":\"auth\"}");
  return false;
}

static void handleReading() {
  server.send(haveReading ? 200 : 503, "application/json", lastJson);
}

static void handleGatt() {
  server.send(200, "application/json", gattJson);
}

static void handleHealth() {
  String nameEsc = jsonEscape(relayName);
  String errEsc = jsonEscape(lastErr);
  String charEsc = jsonEscape(lastPacketChar);
  String ap = isSoftAp() ? WiFi.softAPIP().toString() : "";
  String sta = (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : "";
  char buf[520];
  int wifiRssi = (WiFi.status() == WL_CONNECTED) ? WiFi.RSSI() : 0;
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"name\":\"%s\",\"ble\":%s,\"haveReading\":%s,\"ap\":\"%s\",\"sta\":\"%s\","
           "\"wifiRssi\":%d,\"bleRssi\":%d,\"lastErr\":\"%s\",\"packetChar\":\"%s\"}",
           nameEsc.c_str(),
           bleClient && bleClient->isConnected() ? "true" : "false",
           haveReading ? "true" : "false",
           ap.c_str(),
           sta.c_str(),
           wifiRssi, lastRssi, errEsc.c_str(), charEsc.c_str());
  server.send(200, "application/json", buf);
}

static void sendSoftApForm(const char *flash) {
  String html;
  html += "<!doctype html><html><head><meta charset=utf-8>";
  html += "<meta name=viewport content='width=device-width,initial-scale=1'>";
  html += "<title>";
  html += relayName;
  html += "</title></head><body>";
  html += "<h1>";
  html += relayName;
  html += "</h1>";
  html += "<p>SoftAP. House Wi-Fi, relay name, and OTA token stay on this board (not git).</p>";
  if (flash && flash[0]) {
    html += "<p>";
    html += flash;
    html += "</p>";
  }
  html += "<p>STA status: ";
  html += staStatus;
  html += "</p>";
  html += "<form method=POST action=/wifi>";
  html += "<p>Relay name<br><input name=name maxlength=32 value='";
  html += relayName;
  html += "'></p>";
  html += "<p>House SSID<br><input name=ssid value='";
  html += staSsid;
  html += "'></p>";
  html += "<p>Password<br><input name=pass type=password></p>";
  html += "<p>OTA token (blank keeps current)<br><input name=token type=password maxlength=32></p>";
  html += "<p><button type=submit>Save / join house Wi-Fi</button></p>";
  html += "</form></body></html>";
  server.send(200, "text/html", html);
}

static void sendStaForm(const char *flash) {
  String html;
  html += "<!doctype html><html><head><meta charset=utf-8>";
  html += "<meta name=viewport content='width=device-width,initial-scale=1'>";
  html += "<title>";
  html += relayName;
  html += "</title></head><body>";
  html += "<h1>";
  html += relayName;
  html += "</h1>";
  html += "<p>LAN rename only. House Wi-Fi is not accepted on this page.</p>";
  if (flash && flash[0]) {
    html += "<p>";
    html += flash;
    html += "</p>";
  }
  html += "<p>LAN IP: ";
  html += WiFi.localIP().toString();
  html += "</p>";
  html += "<form method=POST action=/name>";
  html += "<p>Relay name<br><input name=name maxlength=32 value='";
  html += relayName;
  html += "'></p>";
  html += "<p>OTA token<br><input name=token type=password maxlength=32></p>";
  html += "<p><button type=submit>Save name</button></p>";
  html += "</form></body></html>";
  server.send(200, "text/html", html);
}

static void handleRoot() {
  if (isSoftAp()) {
    sendSoftApForm("");
  } else {
    sendStaForm("");
  }
}

static void handleCaptive() {
  if (!isSoftAp()) {
    server.send(404, "text/plain", "Not found");
    return;
  }
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
    WiFi.mode(WIFI_AP);
    WiFi.softAP(RELAY_AP_SSID, RELAY_AP_PASS);
    Serial.print("ap ip ");
    Serial.println(WiFi.softAPIP());
  }
}

static void handleWifiPost() {
  if (!isSoftAp()) {
    server.send(404, "application/json", "{\"ok\":false,\"error\":\"wifi form is SoftAP only\"}");
    return;
  }
  String ssid = server.arg("ssid");
  String pass = server.arg("pass");
  String name = sanitizeRelayName(server.arg("name"));
  String tok = server.arg("token");
  tok.trim();
  ssid.trim();
  prefs.begin("relay", false);
  prefs.putString("name", name);
  relayName = name;
  if (tok.length()) {
    otaToken = tok;
    prefs.putString("tok", otaToken);
  }
  if (!ssid.length()) {
    prefs.end();
    sendSoftApForm("Relay name saved. SSID required to join house Wi-Fi.");
    return;
  }
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
  staSsid = ssid;
  trySta(ssid, pass);
  sendSoftApForm(WiFi.status() == WL_CONNECTED ? "Saved. Joined house Wi-Fi." : "Saved, but join failed. Recheck the password.");
}

static void handleNamePost() {
  if (!requireAuth()) {
    return;
  }
  String name = sanitizeRelayName(server.arg("name"));
  prefs.begin("relay", false);
  prefs.putString("name", name);
  prefs.end();
  relayName = name;
  sendStaForm("Name saved.");
}

static void handleOtaUpload() {
  // WebServer calls this ufn as raw() for non-multipart POST. server.upload()
  // is null then and would RST the TCP client. Only touch HTTPUpload on multipart.
  String ct = server.header("Content-Type");
  if (!ct.startsWith("multipart/")) {
    return;
  }
  HTTPUpload &up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    otaAuthed = headerTokenOk();
    otaGot = 0;
    if (!otaAuthed) {
      return;
    }
    pauseBle();
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
      Update.printError(Serial);
    }
    Serial.println("ota start");
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (!otaAuthed) {
      return;
    }
    otaGot += up.currentSize;
    if (otaGot > OTA_MAX) {
      Update.abort();
      return;
    }
    if (Update.write(up.buf, up.currentSize) != up.currentSize) {
      Update.printError(Serial);
    }
  } else if (up.status == UPLOAD_FILE_END) {
    if (!otaAuthed) {
      return;
    }
    if (Update.end(true)) {
      Serial.printf("ota ok %u\n", (unsigned)up.totalSize);
    } else {
      Update.printError(Serial);
    }
  }
}

static void handleOtaPost() {
  // Header only so a prior POST /name token arg cannot authorize OTA.
  if (!headerTokenOk()) {
    server.send(401, "application/json", "{\"ok\":false,\"error\":\"auth\"}");
    otaBusy = false;
    otaAuthed = false;
    return;
  }
  if (!otaAuthed || !Update.isFinished() || Update.hasError()) {
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"ota failed\"}");
    otaBusy = false;
    otaAuthed = false;
    return;
  }
  server.send(200, "application/json", "{\"ok\":true}");
  delay(200);
  ESP.restart();
}

static void startWifi() {
  prefs.begin("relay", false);
  relayName = sanitizeRelayName(prefs.getString("name", "smoker-relay"));
  staSsid = prefs.getString("ssid", "");
  String pass = prefs.getString("pass", "");
  otaToken = prefs.getString("tok", "");
  if (!otaToken.length()) {
    otaToken = makeToken();
    prefs.putString("tok", otaToken);
    Serial.print("nvs token minted ");
    Serial.println(otaToken);
  } else {
    Serial.println("nvs token present");
  }
  prefs.end();

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
  Serial.println("fw notify-subscribe");

  startWifi();

  const char *hdrs[] = {"X-Relay-Token", "Content-Type"};
  server.collectHeaders(hdrs, 2);
  server.on("/", HTTP_GET, handleRoot);
  server.on("/wifi", HTTP_POST, handleWifiPost);
  server.on("/name", HTTP_POST, handleNamePost);
  server.on("/ota", HTTP_POST, handleOtaPost, handleOtaUpload);
  server.on("/api/reading", HTTP_GET, handleReading);
  server.on("/api/gatt", HTTP_GET, handleGatt);
  server.on("/health", HTTP_GET, handleHealth);
  server.on("/generate_204", HTTP_GET, handleCaptive);
  server.on("/hotspot-detect.html", HTTP_GET, handleCaptive);
  server.on("/fwlink", HTTP_GET, handleCaptive);
  server.begin();

  delay(1500);
  NimBLEDevice::init("smoker-relay");
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEScan *scan = NimBLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(&scanCbs, false);
  scan->setActiveScan(false);
  scan->setInterval(160);
  scan->setWindow(40);
  bleInited = true;
  bleReadyMs = millis() + 2000;
  Serial.println("ble ready");
}

void loop() {
  server.handleClient();
  if (otaBusy) {
    return;
  }
  if (!bleInited || millis() < bleReadyMs) {
    return;
  }

  if (notifyPending) {
    notifyPending = false;
    const char *uuid = "";
    std::string cu;
    if (notifySrc) {
      cu = notifySrc->getUUID().toString();
      uuid = cu.c_str();
    }
    publish(notifyBuf, notifyLen, uuid);
  }

  if (bleClient && bleClient->isConnected()) {
    if (!haveReading && millis() - lastReadMs >= READ_EVERY_MS) {
      lastReadMs = millis();
      lastRssi = bleClient->getRssi();
      tryReadBinary(bleClient);
    }
    return;
  }

  tempChar = nullptr;
  notifySrc = nullptr;

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
    if (connectAndSubscribe()) {
      lastReadMs = millis();
    } else {
      haveTarget = false;
    }
  }
}
