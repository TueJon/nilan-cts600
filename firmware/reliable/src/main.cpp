// Transport only. The server remains the sole Nilan protocol/control owner.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <USB.h>
#include <USBCDC.h>
#include <WebServer.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <Update.h>
#include <driver/uart.h>
#include <esp_system.h>
#include <esp_task_wdt.h>
#include <atomic>
#include <sys/socket.h>
#include "policy.h"
#include "secrets.h"

constexpr char VERSION[] = "2.0.1";
constexpr char HOSTNAME[] = "nilan-bridge";
constexpr uint16_t TCP_PORT = 6638;
constexpr uint32_t WIFI_ATTEMPT_MS = 20000;
constexpr uint32_t WIFI_RESET_MS = 600000;
constexpr uint32_t USB_IDLE_MS = 120000;
constexpr uint32_t TCP_IDLE_MS = 120000;
constexpr uart_port_t UART = UART_NUM_1;
static_assert(sizeof(RECOVERY_AP_PASSWORD) >= 13, "Use a unique recovery password of at least 12 characters");
static_assert(sizeof(RECOVERY_AP_PASSWORD) <= 64, "Recovery WPA password must be at most 63 characters");
static_assert(sizeof(ADMIN_PASSWORD) >= 13, "Use a unique admin password of at least 12 characters");
USBCDC busUsb;
Preferences prefs;
WiFiServer tunnel(TCP_PORT);
WiFiClient tcp;
WebServer web(80);
policy::Owner owner = policy::Owner::none;
IPAddress controller;
String ssid, password, previousSsid, previousPassword;
String candidateSsid, candidatePassword;
bool testingCandidate = false, wifiAttempt = false, wasConnected = false;
bool safeMode = false, storageOk = false, uartOk = false;
bool uploading = false, uploadAuthorized = false, uploadOk = false;
bool uploadSession = false;
bool wifiConfigQueued = false, recycleWifi = false;
uint32_t candidateStart = 0, attemptStart = 0, nextAttemptAt = 0;
uint32_t candidateStableSince = 0;
uint32_t uploadLastActivity = 0;
uint32_t lastWifiReset = 0, lastUsbRx = 0, lastTcpRx = 0, rebootAt = 0;
uint32_t failures = 0, reconnects = 0, wifiResets = 0, rejectedClients = 0;
uint64_t uartTx = 0, uartRx = 0, droppedRx = 0, rejectedUsbBytes = 0;
uint32_t transportFaults = 0;
std::atomic<uint32_t> disconnectReason{0};
RTC_DATA_ATTR uint32_t rtcMagic;
RTC_DATA_ATTR uint32_t quickBoots;

struct Event { uint32_t at; const char *name; uint32_t detail; };
Event events[16]{};
unsigned eventCursor = 0;
void event(const char *name, uint32_t detail = 0) {
  events[eventCursor++ % 16] = {millis(), name, detail};
}
bool auth() {
  if (web.authenticate("admin", ADMIN_PASSWORD)) return true;
  web.requestAuthentication();
  return false;
}
bool writeAuth() {
  if (!auth()) return false;
  const String origin=web.header("Origin");
  if (origin.length() && origin != "http://"+web.hostHeader()) {
    web.send(403,"text/plain","Cross-origin operation rejected");return false;
  }
  return true;
}
bool storageWrite(const String &name, const String &pass) {
  // One blob makes credential replacement atomic across power loss.
  StaticJsonDocument<384> doc;
  doc["ssid"] = name; doc["password"] = pass;
  String encoded; serializeJson(doc, encoded);
  return storageOk && prefs.putString("wifi", encoded) == encoded.length() &&
    prefs.getString("wifi", "") == encoded;
}
void loadCredentials() {
  storageOk = prefs.begin("nilan-bridge", false);
  StaticJsonDocument<384> doc;
  if (storageOk && !deserializeJson(doc, prefs.getString("wifi", "")) &&
      doc["ssid"].is<String>() && doc["password"].is<String>()) {
    ssid = doc["ssid"].as<String>(); password = doc["password"].as<String>();
  } else {
    ssid = SEED_WIFI_SSID; password = SEED_WIFI_PASSWORD;
    if (!storageWrite(ssid, password)) event("credential_storage_failed");
  }
}
void startRadio() {
  WiFi.persistent(false); // Own NVS record; reconnects must not wear flash.
  WiFi.setHostname(HOSTNAME);
  WiFi.mode(WIFI_AP_STA);
  WiFi.setAutoReconnect(false); // A single bounded retry state machine below.
  WiFi.setSleep(false);
  WiFi.softAPConfig(IPAddress(192,168,4,1), IPAddress(192,168,4,1), IPAddress(255,255,255,0));
  if (!WiFi.softAP("Nilan-Bridge Recovery", RECOVERY_AP_PASSWORD, 1, false, 2))
    event("recovery_ap_failed");
  tunnel.begin();
  tunnel.setNoDelay(true);
  MDNS.begin(HOSTNAME);
  MDNS.addService("http", "tcp", 80);
  MDNS.addService("nilan", "tcp", TCP_PORT);
  wifiAttempt = false;
  nextAttemptAt = millis();
  lastWifiReset = millis();
}
void closeTransport(const char *reason) {
  tcp.stop(); owner = policy::Owner::none;
  while(busUsb.available()) {busUsb.read();++rejectedUsbBytes;}
  uart_flush_input(UART); // Discard orphan replies; never replay old commands.
  event(reason);
}
void wifiTick(uint32_t now) {
  if (wifiConfigQueued) {
    wifiConfigQueued = false;
    previousSsid = ssid; previousPassword = password;
    ssid = candidateSsid; password = candidatePassword;
    testingCandidate = true; candidateStart = now; candidateStableSince = 0;
    // Let the controller observe a clean close before the radio goes away.
    // Otherwise TCP keepalive can delay the USB fallback after a planned change.
    if(owner==policy::Owner::tcp){closeTransport("wifi_candidate_switch");delay(50);}
    WiFi.disconnect(false, false); wifiAttempt = false; nextAttemptAt = now;
    event("wifi_candidate_started");
  }
  if (testingCandidate && policy::elapsed(now, candidateStart, 60000)) {
    ssid = previousSsid; password = previousPassword; previousPassword = "";
    testingCandidate = false; candidateStableSince = 0;
    WiFi.disconnect(false, false); wifiAttempt = false; nextAttemptAt = now;
    event("wifi_candidate_reverted");
  }
  const bool connected = WiFi.status() == WL_CONNECTED && uint32_t(WiFi.localIP()) != 0;
  if (connected) {
    if (!wasConnected) { ++reconnects; event("wifi_connected"); }
    wasConnected = true; wifiAttempt = false; failures = 0; lastWifiReset = now;
    if (testingCandidate && !candidateStableSince) candidateStableSince=now;
    if (testingCandidate && policy::elapsed(now,candidateStableSince,15000)) {
      if (storageWrite(ssid, password)) event("wifi_candidate_saved");
      else {
        ssid=previousSsid;password=previousPassword;
        WiFi.disconnect(false,false);nextAttemptAt=now;
        event("wifi_candidate_storage_failed_reverted");
      }
      testingCandidate = false; previousPassword = "";
    }
    return;
  }
  candidateStableSince=0;
  if (wasConnected) {
    wasConnected = false; event("wifi_lost", disconnectReason.load());
    if (owner == policy::Owner::tcp) closeTransport("tcp_wifi_lost");
    wifiAttempt = false; nextAttemptAt = now;
  }
  // Keep a recovery operator's channel stable. Only an explicit candidate
  // credential test may roam while someone is connected to the recovery AP.
  if (WiFi.softAPgetStationNum()>0 && !testingCandidate) {
    if(wifiAttempt) {WiFi.disconnect(false,false);wifiAttempt=false;nextAttemptAt=now+5000;}
    return;
  }
  if (wifiAttempt && policy::elapsed(now, attemptStart, WIFI_ATTEMPT_MS)) {
    WiFi.disconnect(false, false); wifiAttempt = false;
    nextAttemptAt = now + policy::backoff(failures++);
    event("wifi_retry", disconnectReason.load());
  }
  // Do not take the recovery AP away from an operator or interrupt a TCP owner.
  // USB continues independently through radio restarts. Never reboot on Wi-Fi loss.
  if ((recycleWifi || policy::elapsed(now, lastWifiReset, WIFI_RESET_MS)) &&
      WiFi.softAPgetStationNum() == 0 && owner != policy::Owner::tcp && !uploading) {
    recycleWifi = false; tunnel.end(); MDNS.end();
    WiFi.mode(WIFI_OFF); startRadio(); ++wifiResets;
    event("radio_reinitialized");
  }
  if (!safeMode && !wifiAttempt && int32_t(now - nextAttemptAt) >= 0 && ssid.length()) {
    // No channel/BSSID pin: allow discovery and recovery after AP channel changes.
    WiFi.begin(ssid.c_str(), password.c_str()); wifiAttempt = true; attemptStart = now;
  }
}
void busTick(uint32_t now) {
  if (safeMode || uploading || !uartOk) return;
  const bool usbOpen = bool(busUsb);
  if (owner == policy::Owner::usb && (!usbOpen || policy::elapsed(now,lastUsbRx,USB_IDLE_MS)))
    closeTransport("usb_released");
  if (owner == policy::Owner::tcp && (!tcp.connected() || policy::elapsed(now,lastTcpRx,TCP_IDLE_MS)))
    closeTransport("tcp_released");
  if (tunnel.hasClient()) {
    WiFiClient incoming = tunnel.available();
    if (incoming.remoteIP() == controller && policy::canClaim(owner,policy::Owner::tcp)) {
      if (owner == policy::Owner::none) {
        uart_flush_input(UART); tcp = incoming; tcp.setNoDelay(true);
        tcp.setTimeout(50); owner = policy::Owner::tcp; lastTcpRx = now; event("tcp_claimed");
      } else { incoming.stop(); ++rejectedClients; }
    } else { incoming.stop(); ++rejectedClients; }
  }
  uint8_t bytes[256]; size_t n = 0;
  while (busUsb.available() && n < sizeof(bytes)) bytes[n++] = busUsb.read();
  if (n) {
    if (usbOpen && policy::canClaim(owner,policy::Owner::usb)) {
      if (owner == policy::Owner::none) { uart_flush_input(UART); owner=policy::Owner::usb; event("usb_claimed"); }
      lastUsbRx = now;
      int sent = uart_write_bytes(UART,bytes,n);
      if (sent > 0) uartTx += sent;
      if (sent != int(n)) { ++transportFaults; closeTransport("uart_write_failed"); }
    } else rejectedUsbBytes += n;
  }
  if (owner == policy::Owner::tcp && tcp.available()) {
    int count = tcp.read(bytes,sizeof(bytes));
    if (count > 0) {
      lastTcpRx = now; int sent = uart_write_bytes(UART,bytes,count);
      if (sent > 0) uartTx += sent;
      if (sent != count) { ++transportFaults; closeTransport("uart_write_failed"); }
    }
  }
  int count = uart_read_bytes(UART,bytes,sizeof(bytes),0);
  if (count > 0) {
    uartRx += count;
    size_t sent = 0;
    if (owner == policy::Owner::usb && usbOpen) sent = busUsb.write(bytes,count);
    else if (owner == policy::Owner::tcp) {
      // WiFiClient::write can wait ~10 seconds for a stalled peer. A short/failed
      // nonblocking send closes this session instead of blocking the watchdog.
      int result=send(tcp.fd(),bytes,count,MSG_DONTWAIT);
      if(result>0)sent=result;
    }
    if (sent != size_t(count)) {
      droppedRx += count - sent;
      if (owner != policy::Owner::none) { ++transportFaults; closeTransport("output_backpressure"); }
    }
  }
}
void sendStatus() {
  StaticJsonDocument<3072> doc;
  doc["version"] = VERSION; doc["uptime_s"] = millis()/1000;
  doc["reset_reason"] = int(esp_reset_reason()); doc["quick_boots"] = quickBoots;
  doc["safe_mode"] = safeMode; doc["storage_ok"] = storageOk; doc["uart_ok"] = uartOk;
  doc["firmware_upload_active"] = uploading;
  doc["free_heap"] = ESP.getFreeHeap();
  doc["wifi_connected"] = WiFi.status()==WL_CONNECTED;
  doc["wifi_configured"] = ssid.length()>0; doc["wifi_candidate_pending"] = testingCandidate;
  doc["wifi_rssi"] = WiFi.RSSI(); doc["wifi_channel"] = WiFi.channel();
  doc["wifi_bssid"] = WiFi.BSSIDstr(); doc["ip"] = WiFi.localIP().toString();
  doc["ap_ip"] = WiFi.softAPIP().toString(); doc["ap_clients"] = WiFi.softAPgetStationNum();
  doc["wifi_retry_paused_for_recovery_client"] = WiFi.softAPgetStationNum()>0 && !testingCandidate;
  doc["disconnect_reason"] = disconnectReason.load(); doc["wifi_connections"] = reconnects;
  doc["wifi_resets"] = wifiResets; doc["retry_failures"] = failures;
  doc["owner"] = owner==policy::Owner::usb?"usb":owner==policy::Owner::tcp?"tcp":"none";
  doc["uart_tx"] = uartTx; doc["uart_rx"] = uartRx; doc["dropped_rx"] = droppedRx;
  doc["rejected_clients"] = rejectedClients; doc["rejected_usb_bytes"] = rejectedUsbBytes;
  doc["transport_faults"] = transportFaults;
  auto list=doc.createNestedArray("events");
  unsigned start=eventCursor>16?eventCursor-16:0;
  for (unsigned i=start;i<eventCursor;i++) {
    const auto &e=events[i%16]; auto entry=list.createNestedObject();
    entry["at_ms"]=e.at;entry["event"]=e.name;entry["detail"]=e.detail;
  }
  String out;serializeJson(doc,out);web.sendHeader("Cache-Control","no-store");web.send(200,"application/json",out);
}
void setupWeb() {
  const char *headers[]={"Origin"};web.collectHeaders(headers,1);
  web.on("/status",HTTP_GET,[](){if(auth())sendStatus();});
  web.on("/",HTTP_GET,[](){
    if(!auth())return;
    web.send(200,"text/html",F("<!doctype html><meta name=viewport content='width=device-width'><title>Nilan Bridge</title><h1>Nilan Bridge</h1><p><a href=/status>Connection and recovery status</a></p><h2>Wi-Fi</h2><p>New credentials are tested for 60 seconds. If the connection fails, the previous credentials are restored.</p><form action=/wifi method=post>Network <input name=ssid required maxlength=32><br>Password <input type=password name=password required maxlength=63><br><button>Test and save</button></form><h2>Firmware update</h2><p>Updating briefly interrupts the connection to the ventilation unit.</p><form action=/update method=post enctype=multipart/form-data><input name=firmware type=file required><button>Install firmware</button></form>"));
  });
  web.on("/wifi",HTTP_POST,[](){
    if(!writeAuth())return;
    if (testingCandidate || wifiConfigQueued || uploading) {web.send(409,"text/plain","Another operation is active");return;}
    String name=web.arg("ssid"),pass=web.arg("password");
    if(name.length()<1||name.length()>32||pass.length()<8||pass.length()>63) {web.send(400,"text/plain","SSID 1-32 bytes; WPA password 8-63 bytes");return;}
    candidateSsid=name;candidatePassword=pass;wifiConfigQueued=true;
    web.send(202,"text/plain","Testing network; previous credentials retained until success");
  });
  web.on("/wifi/retry",HTTP_POST,[](){if(writeAuth()){recycleWifi=true;web.send(202,"text/plain","Radio recovery queued; active sessions are protected");}});
  web.on("/reboot",HTTP_POST,[](){if(writeAuth()){web.send(202,"text/plain","Reboot scheduled");closeTransport("admin_reboot_queued");uploading=true;rebootAt=millis()+1000;}});
  web.on("/update",HTTP_POST,[](){
    if(!writeAuth())return;
    if(uploadOk){web.send(200,"text/plain","Firmware verified and installed; rebooting");rebootAt=millis()+1000;}
    else{web.send(400,"text/plain","Update rejected; current firmware retained");}
  },[](){
    esp_task_wdt_reset();
    auto &u=web.upload();
    if(uploadSession) uploadLastActivity=millis();
    if(u.status==UPLOAD_FILE_START){
      const String origin=web.header("Origin");
      uploadAuthorized=!uploading && web.authenticate("admin",ADMIN_PASSWORD) &&
        (!origin.length() || origin=="http://"+web.hostHeader());uploadOk=false;
      if(!uploadAuthorized)return;
      uploadSession=true;uploadLastActivity=millis();uploading=true;closeTransport("firmware_update_started");
      if(!Update.begin(UPDATE_SIZE_UNKNOWN)) {uploadAuthorized=false;event("update_begin_failed");}
    }else if(u.status==UPLOAD_FILE_WRITE && uploadSession){
      while(busUsb.available()){busUsb.read();++rejectedUsbBytes;}
      if(uploadAuthorized && Update.write(u.buf,u.currentSize)!=u.currentSize){
        Update.abort();uploadAuthorized=false;event("update_write_failed");
      }
    }else if(u.status==UPLOAD_FILE_END && uploadSession){
      uploadOk=uploadAuthorized && Update.end(true);uploading=uploadOk;
      uploadAuthorized=false;uploadSession=false;
      closeTransport(uploadOk?"update_complete":"update_failed");
    }else if(u.status==UPLOAD_FILE_ABORTED && uploadSession){
      Update.abort();uploading=false;uploadAuthorized=false;uploadSession=false;closeTransport("update_aborted");
    }
  });
  web.onNotFound([](){if(auth())web.send(404,"text/plain","Not found");});
  web.begin();
}
void setup() {
  if(rtcMagic!=0x4e494c32){rtcMagic=0x4e494c32;quickBoots=0;}
  auto reason=esp_reset_reason();
  bool crashed=reason==ESP_RST_PANIC||reason==ESP_RST_TASK_WDT||
    reason==ESP_RST_INT_WDT||reason==ESP_RST_WDT||reason==ESP_RST_BROWNOUT;
  quickBoots=policy::bootFailures(quickBoots,crashed);safeMode=quickBoots>=4;
  busUsb.begin(115200);busUsb.setTxTimeoutMs(50);busUsb.enableReboot(true);USB.begin();
  esp_task_wdt_init(10,true);esp_task_wdt_add(nullptr);
  event(safeMode?"boot_recovery_mode":"boot",int(esp_reset_reason()));
  loadCredentials();
  controller.fromString(CONTROLLER_IP);
  uart_config_t cfg={};cfg.baud_rate=19200;cfg.data_bits=UART_DATA_8_BITS;
  cfg.parity=UART_PARITY_DISABLE;cfg.stop_bits=UART_STOP_BITS_1;
  cfg.flow_ctrl=UART_HW_FLOWCTRL_DISABLE;cfg.source_clk=UART_SCLK_APB;
  // 8N1 is intentional: measured on the live unit; old repo 8N2 comments are stale.
  uartOk=uart_param_config(UART,&cfg)==ESP_OK &&
    uart_set_pin(UART,17,18,21,UART_PIN_NO_CHANGE)==ESP_OK &&
    uart_driver_install(UART,2048,0,0,nullptr,0)==ESP_OK &&
    uart_set_mode(UART,UART_MODE_RS485_HALF_DUPLEX)==ESP_OK;
  uart_set_rx_timeout(UART,2);
  WiFi.onEvent([](WiFiEvent_t e,WiFiEventInfo_t info){
    if(e==ARDUINO_EVENT_WIFI_STA_DISCONNECTED)disconnectReason.store(info.wifi_sta_disconnected.reason);
  });
  startRadio();setupWeb();
}
void loop() {
  esp_task_wdt_reset();uint32_t now=millis();
  if(quickBoots && now>120000)quickBoots=0;
  busTick(now);wifiTick(now);web.handleClient();
  if(uploadSession && policy::elapsed(millis(),uploadLastActivity,30000)) {
    Update.abort();uploadSession=false;uploadAuthorized=false;uploading=false;uploadOk=false;
    closeTransport("update_inactivity_timeout");
  }
  if(rebootAt && int32_t(now-rebootAt)>=0){closeTransport("admin_reboot");ESP.restart();}
  delay(1);
}
