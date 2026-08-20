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

typedef struct __attribute__((packed)) {
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

  // 파싱하기 쉬운 한 줄 형식. 파이썬 쪽 impact_test.py 와 짝을 이룬다.
  Serial.printf("IMPACT device=%s seq=%lu peak=%.2f dur=%.0f rssi=%d batt=%u\n",
                m.device_id, (unsigned long)m.seq, m.peak_g, m.duration_ms,
                info->rx_ctrl ? info->rx_ctrl->rssi : 0, m.battery_mv);
}

void setup() {
  Serial.begin(115200);
  delay(1500);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  Serial.println();
  Serial.println("# ESP-NOW 수신기");
  Serial.print("# 내 MAC: ");
  Serial.println(WiFi.macAddress());
  Serial.println("# 이 주소를 sensor_node.ino 의 RECEIVER_MAC 에 넣으세요.");
  Serial.print("# 채널: ");
  Serial.println(WiFi.channel());

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
    Serial.printf("# alive rx=%lu uptime=%lus\n",
                  (unsigned long)rx_count, millis() / 1000);
  }
  delay(50);
}
