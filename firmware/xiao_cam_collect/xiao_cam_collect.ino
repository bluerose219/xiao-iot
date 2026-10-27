// XIAO ESP32S3 Sense - Edge Impulse 학습용 이미지 수집
// 카메라 프레임(JPEG)을 서버로 계속 올린다. 대시보드에서 보고 있을 때만 빠르게, 아니면 5초 간격.
// 촬영/라벨링/저장은 서버와 대시보드가 담당한다.
//
// 빌드: arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi --upload -p COM9 firmware/xiao_cam_collect
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "esp_camera.h"
#include "secrets.h"  // WIFI_SSID, WIFI_PASSWORD, API_BASE, DEVICE_KEY

// 수집 해상도. Edge Impulse 이미지 분류는 정사각형 입력(96x96 등)이 일반적이라 240x240으로 수집
const framesize_t FRAME_SIZE = FRAMESIZE_240X240;
const int JPEG_QUALITY = 12;  // 0-63, 낮을수록 고화질

// XIAO ESP32S3 Sense 카메라 핀
#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM  10
#define SIOD_GPIO_NUM  40
#define SIOC_GPIO_NUM  39
#define Y9_GPIO_NUM    48
#define Y8_GPIO_NUM    11
#define Y7_GPIO_NUM    12
#define Y6_GPIO_NUM    14
#define Y5_GPIO_NUM    16
#define Y4_GPIO_NUM    18
#define Y3_GPIO_NUM    17
#define Y2_GPIO_NUM    15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM  47
#define PCLK_GPIO_NUM  13

const int LED_PIN = LED_BUILTIN;  // 전송 중 깜빡임 (LOW = 켜짐)

WiFiClientSecure tls;
HTTPClient http;
unsigned long nextSendAt = 0;
int failCount = 0;

bool initCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAME_SIZE;
  config.jpeg_quality = JPEG_QUALITY;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;  // 전송이 느려도 항상 가장 최신 프레임

  if (!psramFound()) {
    Serial.println("PSRAM 없음: 빌드 옵션 PSRAM=opi 를 확인하세요");
    return false;
  }
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("카메라 초기화 실패 0x%x (카메라 보드가 연결됐는지 확인)\n", err);
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  Serial.printf("카메라 센서 PID=0x%x\n", s->id.PID);
  return true;
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("WiFi 연결 중: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // 전송 지연 줄이기
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi 연결됨, IP: %s\n", WiFi.localIP().toString().c_str());
  }
}

// 프레임 하나를 올리고 서버가 알려준 다음 전송 간격(ms)을 돌려준다. 실패 시 -1
long sendFrame() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("프레임 캡처 실패");
    return -1;
  }

  digitalWrite(LED_PIN, LOW);
  http.begin(tls, String(API_BASE) + "/device/frame");  // setReuse로 TLS 연결 재사용
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  unsigned long t0 = millis();
  int code = http.POST(fb->buf, fb->len);
  size_t len = fb->len;
  esp_camera_fb_return(fb);

  long interval = -1;
  if (code == 200) {
    String resp = http.getString();  // {"interval_ms":0}
    int i = resp.indexOf("interval_ms");
    interval = i >= 0 ? resp.substring(resp.indexOf(':', i) + 1).toInt() : 1000;
    Serial.printf("frame %u B, %lu ms -> next %ld ms\n", len, millis() - t0, interval);
  } else {
    Serial.printf("전송 실패: %d %s\n", code, code > 0 ? http.getString().c_str() : http.errorToString(code).c_str());
  }
  http.end();
  digitalWrite(LED_PIN, HIGH);
  return interval;
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(1000);

  if (!initCamera()) {
    // 카메라가 없으면 진행 불가: LED를 빠르게 깜빡여 표시
    while (true) {
      digitalWrite(LED_PIN, !digitalRead(LED_PIN));
      delay(150);
    }
  }

  tls.setInsecure();  // 테스트용: 인증서 검증 생략
  http.setReuse(true);
  http.setTimeout(60000);  // Render 무료 플랜이 잠들었다 깨어날 때 오래 걸릴 수 있음
  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    delay(1000);
    return;
  }
  if ((long)(millis() - nextSendAt) < 0) {
    delay(10);
    return;
  }

  long interval = sendFrame();
  if (interval < 0) {
    failCount++;
    interval = min(1000L * failCount, 10000L);  // 실패가 이어지면 점점 늦게 재시도
  } else {
    failCount = 0;
  }
  nextSendAt = millis() + interval;
}
