// XIAO ESP32S3 Sense - Edge Impulse 이미지 분류 추론
// 카메라 프레임을 계속 분류하고, 결과(라벨/점수/처리시간)를 프레임과 함께 서버로 올린다.
// 대시보드(infer.html)에서 보고 있을 때는 매 추론마다, 아니면 5초에 한 번 전송한다.
//
// 필요: Edge Impulse Arduino 라이브러리 (firmware/ei_model_arduino.zip 을 Arduino libraries 폴더에 설치)
// 빌드: arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi --upload -p COM9 firmware/xiao_cam_infer
#include <wendy119-project-1_inferencing.h>
#include "edge-impulse-sdk/dsp/image/image.hpp"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "esp_camera.h"
#include "secrets.h"  // WIFI_SSID, WIFI_PASSWORD, API_BASE, DEVICE_KEY

// 수집 때와 같은 240x240으로 찍어 모델 입력(96x96)으로 줄인다
const framesize_t FRAME_SIZE = FRAMESIZE_240X240;
const int RAW_W = 240;
const int RAW_H = 240;
const int JPEG_QUALITY = 12;
const float CONFIDENCE_THRESHOLD = 0.6;  // 최고 점수가 이보다 낮으면 "uncertain"

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

const int LED_PIN = LED_BUILTIN;  // 전송 중 켜짐 (LOW = 켜짐)

WiFiClientSecure tls;
HTTPClient http;
uint8_t *rgbBuf = nullptr;  // RAW_W*RAW_H*3, PSRAM. 리사이즈 후 앞부분이 96x96 RGB
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
  config.grab_mode = CAMERA_GRAB_LATEST;

  if (!psramFound()) {
    Serial.println("PSRAM 없음: 빌드 옵션 PSRAM=opi 를 확인하세요");
    return false;
  }
  // 소프트 리셋 직후엔 카메라 센서가 바로 응답하지 않을 때가 있어 몇 번 재시도
  for (int attempt = 1; attempt <= 5; attempt++) {
    esp_err_t err = esp_camera_init(&config);
    if (err == ESP_OK) return true;
    Serial.printf("카메라 초기화 실패 0x%x (%d/5)\n", err, attempt);
    esp_camera_deinit();
    delay(500 * attempt);
  }
  return false;
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("WiFi 연결 중: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
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

// Edge Impulse가 신호를 읽어갈 때 호출: 96x96 RGB 버퍼를 픽셀당 0xRRGGBB float로 변환
static int getSignalData(size_t offset, size_t length, float *out) {
  size_t px = offset * 3;
  for (size_t i = 0; i < length; i++, px += 3) {
    // esp32-camera의 fmt2rgb888은 BGR 순서로 디코딩한다 (espressif/esp32-camera#379)
    out[i] = (rgbBuf[px + 2] << 16) + (rgbBuf[px + 1] << 8) + rgbBuf[px];
  }
  return 0;
}

// JPEG 프레임을 분류. 성공 시 result 채우고 true
bool classify(camera_fb_t *fb, ei_impulse_result_t &result) {
  if (!fmt2rgb888(fb->buf, fb->len, PIXFORMAT_JPEG, rgbBuf)) {
    Serial.println("JPEG 디코딩 실패");
    return false;
  }
  ei::image::processing::crop_and_interpolate_rgb888(
      rgbBuf, RAW_W, RAW_H, rgbBuf, EI_CLASSIFIER_INPUT_WIDTH, EI_CLASSIFIER_INPUT_HEIGHT);

  signal_t signal;
  signal.total_length = EI_CLASSIFIER_INPUT_WIDTH * EI_CLASSIFIER_INPUT_HEIGHT;
  signal.get_data = &getSignalData;
  EI_IMPULSE_ERROR err = run_classifier(&signal, &result, false);
  if (err != EI_IMPULSE_OK) {
    Serial.printf("run_classifier 실패: %d\n", err);
    return false;
  }
  return true;
}

// {"label":"a","scores":{"a":0.98,"b":0.02},"dsp":3,"nn":120,"loop":180}
String resultJson(const ei_impulse_result_t &result, unsigned long loopMs, const char *&label, float &best) {
  best = -1;
  label = "uncertain";
  String scores;
  for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    const auto &c = result.classification[i];
    if (c.value > best) {
      best = c.value;
      label = c.label;
    }
    if (i) scores += ",";
    scores += "\"" + String(c.label) + "\":" + String(c.value, 4);
  }
  if (best < CONFIDENCE_THRESHOLD) label = "uncertain";
  return "{\"label\":\"" + String(label) + "\",\"scores\":{" + scores + "},\"dsp\":" + String(result.timing.dsp) +
         ",\"nn\":" + String(result.timing.classification) + ",\"loop\":" + String(loopMs) + "}";
}

// 프레임 + 결과를 올리고 서버가 알려준 다음 전송 간격(ms)을 돌려준다. 실패 시 -1
long upload(camera_fb_t *fb, const String &json) {
  digitalWrite(LED_PIN, LOW);
  http.begin(tls, String(API_BASE) + "/device/infer");
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.addHeader("X-Result", json);
  int code = http.POST(fb->buf, fb->len);
  long interval = -1;
  if (code == 200) {
    String resp = http.getString();  // {"interval_ms":0}
    int i = resp.indexOf("interval_ms");
    interval = i >= 0 ? resp.substring(resp.indexOf(':', i) + 1).toInt() : 1000;
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

  Serial.printf("모델: %s, 입력 %dx%d, 클래스 %d개\n", EI_CLASSIFIER_PROJECT_NAME,
                EI_CLASSIFIER_INPUT_WIDTH, EI_CLASSIFIER_INPUT_HEIGHT, EI_CLASSIFIER_LABEL_COUNT);

  rgbBuf = (uint8_t *)ps_malloc(RAW_W * RAW_H * 3);
  if (!rgbBuf || !initCamera()) {
    Serial.println("초기화 실패");
    while (true) {
      digitalWrite(LED_PIN, !digitalRead(LED_PIN));
      delay(150);
    }
  }

  tls.setInsecure();  // 테스트용: 인증서 검증 생략
  http.setReuse(true);
  http.setTimeout(60000);
  connectWiFi();
}

void loop() {
  unsigned long t0 = millis();
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("프레임 캡처 실패");
    delay(100);
    return;
  }

  ei_impulse_result_t result = {};
  if (!classify(fb, result)) {
    esp_camera_fb_return(fb);
    return;
  }
  const char *label;
  float best;
  String json = resultJson(result, millis() - t0, label, best);
  Serial.printf("%-9s %.2f  (dsp %d ms, nn %d ms)\n", label, best, result.timing.dsp, result.timing.classification);

  // 추론은 매 프레임 하고, 서버 전송은 서버가 정한 간격에 맞춘다
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  } else if ((long)(millis() - nextSendAt) >= 0) {
    long interval = upload(fb, json);
    if (interval < 0) {
      failCount++;
      interval = min(1000L * failCount, 10000L);
    } else {
      failCount = 0;
    }
    nextSendAt = millis() + interval;
  }
  esp_camera_fb_return(fb);
}
