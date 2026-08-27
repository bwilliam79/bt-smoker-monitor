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
static String devicePass = "";
static String sessionSid = "";
static int lastRssi = 0;
static const char *FW_VERSION = "v1.4.2";
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
static uint32_t lastPollMs = 0;
static const uint32_t POLL_EVERY_MS = 20000;
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

static bool passwordOk(const String &pw) {
  if (pw.length() < 8 || pw.length() > 64) {
    return false;
  }
  for (size_t i = 0; i < pw.length(); i++) {
    char c = pw[i];
    if (c < 32 || c > 126 || c == '"' || c == '\\' || c == '<' || c == '>') {
      return false;
    }
  }
  return true;
}

static bool saveDevicePass(const String &pw) {
  if (!passwordOk(pw)) {
    return false;
  }
  devicePass = pw;
  prefs.begin("relay", false);
  prefs.putString("pw", devicePass);
  prefs.remove("tok");
  prefs.end();
  return true;
}

static String serialLine = "";

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      serialLine.trim();
      if (serialLine.startsWith("pass ")) {
        String pw = serialLine.substring(5);
        pw.trim();
        if (saveDevicePass(pw)) {
          Serial.println("password saved");
        } else {
          Serial.println("password rejected");
        }
      }
      serialLine = "";
    } else if (serialLine.length() < 80) {
      serialLine += c;
    }
  }
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

static bool pollOnce() {
  // BlueZ-style: connect, READ bb01 + cc01, disconnect. No CCCD, no persist.
  // NXE treats a subscribed staying-central as the controller; dropping it
  // puts the grill into shutdown. Never write setpoints.
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
  bool ok = false;
  if (tempChar && tempChar->canRead()) {
    std::string raw = tempChar->readValue();
    std::string cu = tempChar->getUUID().toString();
    publish(reinterpret_cast<const uint8_t *>(raw.data()), raw.size(), cu.c_str());
    ok = haveReading;
  }
  if (!haveReading) {
    setLastErr("poll read missed packet");
  }
  bleClient->disconnect();
  delay(50);
  Serial.printf("poll done ok=%d\n", ok ? 1 : 0);
  return ok;
}

static void onScanDone(NimBLEScanResults) { scanning = false; }

// Soft-pause only. Never tear down the NimBLE stack from inside a WebServer
// upload callback — that can wedge the sole HTTP task (TCP accept, 0-byte
// replies / RST) while ICMP still answers. Full BLE teardown waits for reboot
// after a successful OTA.
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
  bleClient = nullptr;
  if (bleInited) {
    NimBLEDevice::getScan()->stop();
  }
}

static void otaFailCleanup() {
  Update.abort();
  otaBusy = false;
  otaAuthed = false;
  otaGot = 0;
}

static bool headerTokenOk() {
  if (!devicePass.length()) {
    return false;
  }
  String t = server.header("X-Relay-Password");
  t.trim();
  return t.length() > 0 && t == devicePass;
}

static bool formTokenOk() {
  if (!devicePass.length()) {
    return false;
  }
  String t = server.arg("password");
  t.trim();
  return t.length() > 0 && t == devicePass;
}

static bool tokenOk() {
  return headerTokenOk() || formTokenOk();
}

static bool sessionOk() {
  if (!sessionSid.length()) {
    return false;
  }
  String c = server.header("Cookie");
  int i = c.indexOf("sid=");
  if (i < 0) {
    return false;
  }
  String got = c.substring(i + 4);
  int sc = got.indexOf(';');
  if (sc >= 0) {
    got = got.substring(0, sc);
  }
  got.trim();
  return got == sessionSid;
}

static void mintSession() {
  char buf[17];
  snprintf(buf, sizeof(buf), "%08x%08x", (unsigned)esp_random(), (unsigned)esp_random());
  sessionSid = buf;
}

static void addSessionCookie() {
  mintSession();
  server.sendHeader("Set-Cookie", "sid=" + sessionSid + "; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400");
}

static void clearSessionCookie() {
  sessionSid = "";
  server.sendHeader("Set-Cookie", "sid=; Path=/; HttpOnly; Max-Age=0");
}

static bool otaAuthOk() {
  return headerTokenOk() || sessionOk();
}

static bool requireAuth() {
  if (tokenOk() || sessionOk()) {
    return true;
  }
  server.send(401, "application/json", "{\"ok\":false,\"error\":\"auth\"}");
  return false;
}

static void handleReading() {
  server.send(haveReading ? 200 : 503, "application/json", lastJson);
}

static void handleGatt() {
  if (bleClient && bleClient->isConnected()) {
    dumpGattJson(bleClient);
  }
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


static void redirectHome() {
  server.sendHeader("Location", "/", true);
  server.send(303, "text/plain", "");
}

static void sendPage(const char *flash, bool forceIn = false) {
  bool hasPass = devicePass.length() > 0;
  // forceIn: Set-Cookie is on this response; request Cookie is not present yet.
  bool in = forceIn || (hasPass && sessionOk());
  int wifiRssi = (WiFi.status() == WL_CONNECTED) ? WiFi.RSSI() : 0;
  bool bleOn = bleClient && bleClient->isConnected();
  String html;
  html.reserve(4500);
  html += "<!doctype html><html><head><meta charset=utf-8>";
  html += "<meta name=viewport content='width=device-width,initial-scale=1'>";
  html += "<title>";
  html += relayName;
  html += "</title><style>";
  html += "body{margin:0;background:#1a1612;color:#e8dcc8;font:16px system-ui,sans-serif}";
  html += ".w{max-width:28rem;margin:0 auto;padding:20px}";
  html += "h1{font-size:1.15rem;color:#c4a574;font-weight:600;margin:0 0 12px}";
  html += ".tel{display:flex;flex-wrap:wrap;gap:10px 16px;background:#241e18;border:1px solid #3d3428;border-radius:8px;padding:12px 14px;margin:0 0 16px;color:#e8dcc8}";
  html += ".dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#6b5e4e;margin-right:6px;vertical-align:middle}";
  html += ".dot.on{background:#5a8f5a}";
  html += "h2{font-size:1rem;color:#e8dcc8;font-weight:600;margin:8px 0 4px}";
  html += "label{display:block;margin:14px 0 6px;color:#c4a574;font-size:.85rem}";
  html += "input,button{font:16px system-ui,sans-serif;width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #3d3428;background:#120f0c;color:#e8dcc8}";
  html += "button{background:#3d2e18;color:#e4c48a;border-color:#6b5428;margin-top:14px;font-weight:600}";
  html += ".row{display:flex;gap:10px}.row button{flex:1}";
  html += ".msg{color:#c4a574;margin:0 0 12px}";
  html += ".fw{margin-top:20px;color:#6b5e4e;font-size:.75rem}";
  html += "</style></head><body><div class=w><h1>";
  html += relayName;
  html += "</h1><div class=tel>";
  // Connected = cached NXE packet path, not merely a BLE ACL link.
  if (haveReading) {
    html += "<span><span class='dot on'></span>Connected</span>";
  } else if (bleOn) {
    html += "<span><span class=dot></span>Waiting for packet</span>";
  } else {
    html += "<span><span class=dot></span>Not connected</span>";
  }
  html += "<span>Wi-Fi ";
  html += (WiFi.status() == WL_CONNECTED) ? (String(wifiRssi) + " dBm") : String("—");
  html += "</span><span>BT to smoker ";
  html += lastRssi ? (String(lastRssi) + " dBm") : String("—");
  html += "</span></div>";
  if (flash && flash[0]) {
    html += "<p class=msg>";
    html += flash;
    html += "</p>";
  }
  if (!hasPass) {
    html += "<h2>Set password</h2><form method=POST action=/setpass>";
    html += "<label>New password</label><input name=newpass type=password maxlength=64 autocomplete=new-password>";
    html += "<label>Re-enter new password</label><input name=again type=password maxlength=64 autocomplete=new-password>";
    html += "<button type=submit>Set password</button></form>";
  } else if (!in) {
    html += "<h2>Unlock config</h2><form method=POST action=/unlock>";
    html += "<label>Password</label><input name=password type=password maxlength=64 autocomplete=current-password>";
    html += "<button type=submit>Unlock</button></form>";
  } else {
    html += "<form method=POST action=/save>";
    html += "<label>Name</label><input name=name maxlength=32 value='";
    html += relayName;
    html += "'>";
    html += "<label>Wi-Fi SSID</label><input name=ssid maxlength=32 value='";
    html += staSsid;
    html += "' autocomplete=off>";
    html += "<label>Wi-Fi password</label><input name=pass type=password maxlength=64 autocomplete=new-password>";
    html += "<label>New device password</label><input name=newpass type=password maxlength=64 autocomplete=new-password>";
    html += "<label>Re-enter new password</label><input name=again type=password maxlength=64 autocomplete=new-password>";
    html += "<div class=row><button type=submit>Save</button></div></form>";
    html += "<form method=POST action=/lock><button type=submit>Lock</button></form>";
    html += "<form id=otaForm><label>OTA firmware</label><input name=firmware type=file accept=.bin>";
    html += "<button type=submit>Upload firmware</button></form>";
    html += "<script>document.getElementById('otaForm').addEventListener('submit',async function(e){e.preventDefault();var f=e.target.firmware.files[0];if(!f){return;}var fd=new FormData();fd.append('firmware',f);var r=await fetch('/ota',{method:'POST',credentials:'same-origin',body:fd});alert(await r.text());if(r.ok){setTimeout(function(){location.reload();},1500);}});</script>";
  }
  html += "<p class=fw>fw ";
  html += FW_VERSION;
  html += "</p></div></body></html>";
  server.send(200, "text/html", html);
}

static void handleRoot() {
  sendPage("");
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

static void handleSetPass() {
  if (devicePass.length()) {
    sendPage("Password already set. Unlock.");
    return;
  }
  String a = server.arg("newpass");
  String b = server.arg("again");
  a.trim();
  b.trim();
  if (a != b || !saveDevicePass(a)) {
    sendPage("Passwords must match, 8+ printable.");
    return;
  }
  addSessionCookie();
  redirectHome();
}

static void handleUnlock() {
  if (!devicePass.length()) {
    server.sendHeader("Location", "/", true);
    server.send(302, "text/plain", "");
    return;
  }
  if (!formTokenOk()) {
    sendPage("Unlock failed.");
    return;
  }
  addSessionCookie();
  redirectHome();
}

static void handleLock() {
  clearSessionCookie();
  redirectHome();
}

static void handleSave() {
  if (!sessionOk()) {
    sendPage("Unlock first.");
    return;
  }
  String name = sanitizeRelayName(server.arg("name"));
  String ssid = server.arg("ssid");
  String wpass = server.arg("pass");
  String a = server.arg("newpass");
  String b = server.arg("again");
  ssid.trim();
  a.trim();
  b.trim();
  prefs.begin("relay", false);
  prefs.putString("name", name);
  relayName = name;
  prefs.end();
  if (a.length() || b.length()) {
    if (a != b || !saveDevicePass(a)) {
      sendPage("New passwords must match, 8+ printable.");
      return;
    }
  }
  if (ssid.length()) {
    prefs.begin("relay", false);
    prefs.putString("ssid", ssid);
    if (wpass.length()) {
      prefs.putString("pass", wpass);
    } else {
      wpass = prefs.getString("pass", "");
    }
    prefs.end();
    staSsid = ssid;
    trySta(ssid, wpass);
    sendPage(WiFi.status() == WL_CONNECTED ? "Saved." : "Saved, but Wi-Fi join failed.");
    return;
  }
  sendPage("Saved.");
}

static void handleWifiPost() {
  server.send(404, "application/json", "{\"ok\":false,\"error\":\"wifi form is SoftAP only\"}");
}

static void handleNamePost() {
  server.send(404, "application/json", "{\"ok\":false,\"error\":\"use /save\"}");
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
    otaAuthed = otaAuthOk();
    otaGot = 0;
    if (!otaAuthed) {
      return;
    }
    pauseBle();
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
      Update.printError(Serial);
      otaFailCleanup();
      return;
    }
    Serial.println("ota start");
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (!otaAuthed) {
      return;
    }
    otaGot += up.currentSize;
    if (otaGot > OTA_MAX) {
      otaFailCleanup();
      return;
    }
    if (Update.write(up.buf, up.currentSize) != up.currentSize) {
      Update.printError(Serial);
      otaFailCleanup();
      return;
    }
    yield();
  } else if (up.status == UPLOAD_FILE_END) {
    if (!otaAuthed) {
      return;
    }
    if (Update.end(true)) {
      Serial.printf("ota ok %u\n", (unsigned)up.totalSize);
    } else {
      Update.printError(Serial);
      otaFailCleanup();
    }
  } else if (up.status == UPLOAD_FILE_ABORTED) {
    Serial.println("ota aborted");
    otaFailCleanup();
  }
}

static void handleOtaPost() {
  if (!otaAuthOk()) {
    // Never began flash (auth fails before pauseBle). Clear flags only.
    server.send(401, "application/json", "{\"ok\":false,\"error\":\"auth\"}");
    otaBusy = false;
    otaAuthed = false;
    return;
  }
  if (!otaAuthed || !Update.isFinished() || Update.hasError()) {
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"ota failed\"}");
    otaFailCleanup();
    return;
  }
  server.send(200, "application/json", "{\"ok\":true}");
  prefs.begin("relay", false);
  prefs.end();
  delay(200);
  ESP.restart();
}

static void startWifi() {
  prefs.begin("relay", false);
  relayName = sanitizeRelayName(prefs.getString("name", "smoker-relay"));
  staSsid = prefs.getString("ssid", "");
  String pass = prefs.getString("pass", "");
  devicePass = prefs.getString("pw", "");
  prefs.remove("holdBle");
  prefs.remove("fwSeen");
  prefs.remove("tok");
  if (!devicePass.length()) {
    Serial.println("no password set. LAN form or serial: pass <password>");
  } else {
    Serial.println("password set");
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
  Serial.print("fw ");
  Serial.println(FW_VERSION);

  startWifi();

  const char *hdrs[] = {"X-Relay-Password", "Content-Type", "Cookie"};
  server.collectHeaders(hdrs, 3);
  server.on("/", HTTP_GET, handleRoot);
  server.on("/setpass", HTTP_POST, handleSetPass);
  server.on("/unlock", HTTP_POST, handleUnlock);
  server.on("/lock", HTTP_POST, handleLock);
  server.on("/save", HTTP_POST, handleSave);
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
  pollSerial();
  if (otaBusy) {
    return;
  }
  if (!bleInited || millis() < bleReadyMs) {
    return;
  }


  // Never remain the NXE controller. Disconnect leftovers, then poll.
  if (bleClient && bleClient->isConnected()) {
    bleClient->disconnect();
    delay(50);
  }

  if (!haveTarget) {
    if (!scanning) {
      scanning = true;
      NimBLEDevice::getScan()->start(8, onScanDone, false);
    }
    return;
  }
  if (scanning) {
    NimBLEDevice::getScan()->stop();
    scanning = false;
    return;
  }
  if (lastPollMs != 0 && millis() - lastPollMs < POLL_EVERY_MS) {
    return;
  }
  lastPollMs = millis();
  if (!pollOnce()) {
    haveTarget = false;
  }
}
