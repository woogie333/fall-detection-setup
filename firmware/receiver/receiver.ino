/*
 * ESP-NOW 수신기 — XIAO ESP32C3
 *
 * RDK X3 의 USB 포트에 꽂아두기만 하면 된다. 배선도 납땜도 필요 없다.
 * 센서 노드가 보낸 충격 이벤트를 받아 USB 시리얼로 한 줄씩 내보낸다.
 *
 * 보드에서는 /dev/ttyACM0 으로 잡힌다:
 *     python3 scripts/impact_test.py
 *
 * ── 먼저 할 일 ─────────────────────────────────────────────────────
 * 이 스케치를 올리고 시리얼 모니터에 찍히는 MAC 주소를 복사해,
 * sensor_node.ino 의 RECEIVER_MAC 에 넣으세요.
 *
 * 출력 형식 (한 줄 = 이벤트 하나):
 *     IMPACT device=vib-01 seq=42 peak=3.21 dur=85 rssi=-48 batt=0
 * ───────────────────────────────────────────────────────────────────
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

// 송신 측 sensor_node.ino 의 ESPNOW_CHANNEL 과 반드시 같아야 한다.
const uint8_t ESPNOW_CHANNEL = 1;

// kind: 0 = 충격 이벤트, 1 = 실시간 수준 보고, 2 = 배터리 잔량
//       (sensor_node.ino 와 반드시 일치)
typedef struct __attribute__((packed)) {
  uint8_t  kind;
  char     device_id[8];
  uint32_t seq;
  float    peak_g;
  float    duration_ms;
  uint16_t battery_mv;
} ImpactMsg;

volatile uint32_t rx_count = 0;

void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len != sizeof(ImpactMsg)) {
    Serial.printf("# 크기가 맞지 않는 패킷 무시 (%d bytes, 기대 %d)\n",
                  len, (int)sizeof(ImpactMsg));
    return;
  }
  ImpactMsg m;
  memcpy(&m, data, sizeof(m));
  m.device_id[sizeof(m.device_id) - 1] = '\0';
  rx_count++;

  int rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;

  // 실시간 수준 보고는 별도 태그로 내보낸다. 충격 이벤트와 섞이면
  // 대시보드가 초당 5번 "충격 발생" 을 띄우게 된다.
  if (m.kind == 1) {
    Serial.printf("LEVEL device=%s peak=%.3f rssi=%d\r\n",
                  m.device_id, m.peak_g, rssi);
    return;
  }

  if (m.kind == 2) {
    Serial.printf("BATT device=%s mv=%u rssi=%d\r\n",
                  m.device_id, m.battery_mv, rssi);
    return;
  }

  // 파싱하기 쉬운 한 줄 형식. 파이썬 쪽 impact_test.py 와 짝을 이룬다.
  // \r\n 으로 끝낸다. macOS 터미널(screen)에서 LF 만 보내면 줄이 계단처럼 밀린다.
  Serial.printf("IMPACT device=%s seq=%lu peak=%.2f dur=%.0f rssi=%d batt=%u\r\n",
                m.device_id, (unsigned long)m.seq, m.peak_g, m.duration_ms,
                rssi, m.battery_mv);
}

void setup() {
  Serial.begin(115200);
  delay(1500);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  // 채널 고정. 송신 측과 같아야 패킷이 도달한다.
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  Serial.println();
  Serial.println("# ESP-NOW 수신기");
  Serial.print("# 내 MAC: ");
  Serial.println(WiFi.macAddress());
  Serial.println("# 이 주소를 sensor_node.ino 의 RECEIVER_MAC 에 넣으세요.");
  uint8_t ch; wifi_second_chan_t sc;
  esp_wifi_get_channel(&ch, &sc);
  Serial.printf("# 채널: %u  (sensor_node.ino 의 ESPNOW_CHANNEL 과 같아야 함)\n", ch);

  if (esp_now_init() != ESP_OK) {
    Serial.println("# ESP-NOW 초기화 실패 — 3초 후 재시작");
    delay(3000);
    ESP.restart();
  }
  esp_now_register_recv_cb(onRecv);
  Serial.println("# 수신 대기 중");
}

void loop() {
  // 살아 있음을 주기적으로 알린다. 파이썬 쪽에서 연결 확인용으로 쓴다.
  static uint32_t last = 0;
  if (millis() - last > 10000) {
    last = millis();
    Serial.printf("# alive rx=%lu uptime=%lus\r\n",
                  (unsigned long)rx_count, millis() / 1000);
  }
  delay(50);
}
