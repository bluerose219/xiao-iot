// XIAO ESP32S3 - 센서값을 서버로 POST 하고, 응답의 LED 상태를 반영한다.
// 서버가 인터넷(Render)에 있으므로 기기와 대시보드가 같은 WiFi일 필요가 없다.
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "secrets.h"  // WIFI_SSID, WIFI_PASSWORD, API_BASE, DEVICE_KEY

const int LED_PIN = LED_BUILTIN;   // XIAO ESP32S3 사용자 LED (GPIO21, LOW = 켜짐)
const int ANALOG_PIN = A0;
const unsigned long INTERVAL_MS = 3000;

unsigned long lastSend = 0;

void setLed(bool on) {
  digitalWrite(LED_PIN, on ? LOW : HIGH);
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("WiFi 연결 중: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi 연결됨, IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("WiFi 연결 실패, 다음 주기에 재시도");
  }
}

void sendReading() {
  WiFiClientSecure tls;
  tls.setInsecure();  // 테스트용: 인증서 검증 생략

  HTTPClient http;
  http.setTimeout(60000);  // Render 무료 플랜은 잠들었다 깨어날 때 오래 걸릴 수 있음
  if (!http.begin(tls, String(API_BASE) + "/device/data")) {
    Serial.println("http.begin 실패");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);

  float temp = temperatureRead();
  int analog = analogRead(ANALOG_PIN);
  int rssi = WiFi.RSSI();
  String body = "{\"temperature\":" + String(temp, 2) +
                ",\"analog\":" + String(analog) +
                ",\"rssi\":" + String(rssi) + "}";

  int code = http.POST(body);
  if (code == 200) {
    String resp = http.getString();  // {"led":true}
    bool on = resp.indexOf("\"led\":true") >= 0;
    setLed(on);
    Serial.printf("전송 OK temp=%.2f analog=%d rssi=%d -> LED %s\n", temp, analog, rssi, on ? "ON" : "OFF");
  } else {
    Serial.printf("전송 실패: %d %s\n", code, code > 0 ? http.getString().c_str() : http.errorToString(code).c_str());
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  setLed(false);
  delay(1000);
  connectWiFi();
}

void loop() {
  if (millis() - lastSend >= INTERVAL_MS) {
    lastSend = millis();
    connectWiFi();
    if (WiFi.status() == WL_CONNECTED) sendReading();
  }
}
