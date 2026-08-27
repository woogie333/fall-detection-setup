/*
 * 무선 진동센서 노드 — XIAO ESP32C3 + ADXL345
 *
 * 바닥에 붙어 충격을 감지하고 ESP-NOW 로 수신기에 알린다.
 * 평소에는 딥슬립, ADXL345 의 Activity 인터럽트가 깨운다.
 *
 * 배선 (ADXL345 GY-291 → XIAO ESP32C3):
 *     VCC → 3V3      ⚠ 5V 아님. 배터리 구동 시 5V 핀은 죽어 있다.
 *     GND → GND
 *     SDA → D4 (GPIO6)
 *     SCL → D5 (GPIO7)
 *     INT1→ D1 (GPIO3)   ⚠ 딥슬립 웨이크업 가능한 핀이어야 한다 (C3: GPIO0~5)
 *     CS  → 3V3          (I2C 모드 고정)
 *     SDO → GND          (주소 0x53)
 *
 * 배터리: 뒷면 BAT+ / BAT- 패드. ⚠ 극성 반대면 보드가 즉시 죽는다.
 *
 * ── 개발 순서 (중요) ────────────────────────────────────────────────
 * DEBUG_MODE = 1 로 먼저 로직을 완성하세요. 딥슬립 상태에서는 시리얼
 * 디버깅이 거의 불가능합니다. 유선·상시전원으로 임계값을 정한 뒤
 * DEBUG_MODE = 0 으로 바꿔 절전을 얹는 순서가 훨씬 빠릅니다.
 * ───────────────────────────────────────────────────────────────────
 */

#include <Wire.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_sleep.h>
#include <esp_wifi.h>
#include <esp_idf_version.h>

// ─── 설정 ──────────────────────────────────────────────────────────

#define DEBUG_MODE 1        // 1 = 안 자고 계속 측정값 출력 (개발용)
                            // 0 = 딥슬립 + 인터럽트 (실사용)

// ── 무선 디버깅용 ──────────────────────────────────────────────────
#define USE_BROADCAST 1     // 1 = 특정 MAC 대신 브로드캐스트로 전송.
                            //     수신기에 뜨면 MAC 문제, 안 뜨면 채널/무선 문제.
                            //     원인 확인이 끝나면 0 으로 되돌리세요.

#define MONITOR_HZ 5        // 대시보드 실시간 그래프용. 이 주기로 현재 진동 수준을
                            //     보낸다 (0 = 끔). DEBUG_MODE(=상시 전원) 에서만
                            //     동작한다. 딥슬립 배터리 모드에서는 물리적으로
                            //     불가능하다 — 자고 있는 동안은 보낼 수 없다.

#define HEARTBEAT_SEC 0     // DEBUG_MODE 에서 이 주기로 자동 전송 (0 = 끔).
                            //     두드리지 않아도 무선 경로를 확인할 수 있다.
// ───────────────────────────────────────────────────────────────────

// 수신기 XIAO 의 MAC 주소. receiver 스케치의 시리얼 출력에서 확인한 값.
uint8_t RECEIVER_MAC[6] =
#if USE_BROADCAST
    { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };   // 브로드캐스트 (디버깅용)
#else
    { 0x1C, 0xDB, 0xD4, 0xF0, 0xDE, 0x44 };   // 수신기 실제 MAC
#endif

const uint8_t PIN_INT1   = 3;      // D1
const uint8_t ADXL_ADDR  = 0x53;   // SDO=GND

// 충격 임계값. THRESH_ACT 는 range 와 무관하게 62.5 mg/LSB 다.
// AC 커플링(아래 ACT_INACT=0xF0)이라 이 값은 **중력을 뺀 변화량** 기준이다.
//   8 = 0.5g,  12 = 0.75g,  16 = 1.0g,  32 = 2.0g
// 사람이 넘어질 때 바닥에 놓인 노드가 받는 값은 보통 0.3~1.5g 다.
// 2.0g 는 망치로 쳐야 나오는 값이라 실제 낙상을 통째로 놓친다.
// 너무 낮으면 발소리에도 깨어나 배터리를 갉아먹으므로, DEBUG_MODE 로
// 실제 낙상 값을 재본 뒤 그 절반쯤으로 잡으세요.
const uint8_t ACT_THRESHOLD = 8;

const uint16_t CAPTURE_MS = 400;   // 깨어난 뒤 파형을 볼 시간
const char*    DEVICE_ID  = "vib-01";

// ESP-NOW 는 송수신이 같은 채널에 있어야 한다. 양쪽 스케치의 값을 일치시킬 것.
// 수신기 시리얼에 찍히는 "# 채널:" 값과 같아야 한다.
const uint8_t  ESPNOW_CHANNEL = 1;

// ─── ADXL345 레지스터 ──────────────────────────────────────────────

#define REG_THRESH_ACT   0x24
#define REG_ACT_INACT    0x27
#define REG_BW_RATE      0x2C
#define REG_POWER_CTL    0x2D
#define REG_INT_ENABLE   0x2E
#define REG_INT_MAP      0x2F
#define REG_INT_SOURCE   0x30
#define REG_DATA_FORMAT  0x31
#define REG_DATAX0       0x32
#define REG_DEVID        0x00

// ─── 전송 페이로드 ─────────────────────────────────────────────────

// kind: 0 = 충격 이벤트, 1 = 실시간 수준 보고(모니터링)
typedef struct __attribute__((packed)) {
  uint8_t  kind;
  char     device_id[8];
  uint32_t seq;
  float    peak_g;        // 충격 최대 크기
  float    duration_ms;   // 임계 초과가 지속된 시간
  uint16_t battery_mv;    // 0 = 미측정
} ImpactMsg;

RTC_DATA_ATTR uint32_t boot_count = 0;   // 딥슬립을 넘어 유지된다

// ─── I2C 헬퍼 ─────────────────────────────────────────────────────

void adxlWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(ADXL_ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

uint8_t adxlRead(uint8_t reg) {
  Wire.beginTransmission(ADXL_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL_ADDR, 1);
  return Wire.available() ? Wire.read() : 0;
}

void adxlReadXYZ(int16_t &x, int16_t &y, int16_t &z) {
  Wire.beginTransmission(ADXL_ADDR);
  Wire.write(REG_DATAX0);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL_ADDR, 6);
  x = Wire.read() | (Wire.read() << 8);
  y = Wire.read() | (Wire.read() << 8);
  z = Wire.read() | (Wire.read() << 8);
}

// ±16g, full resolution 에서 3.9 mg/LSB
float toG(int16_t raw) { return raw * 0.0039f; }

// ─── ADXL345 초기화 ────────────────────────────────────────────────

bool adxlBegin() {
  uint8_t id = adxlRead(REG_DEVID);
  if (id != 0xE5) {
    Serial.printf("ADXL345 를 찾지 못했습니다 (DEVID=0x%02X, 기대 0xE5)\n", id);
    Serial.println("  배선과 SDO/CS 연결을 확인하세요.");
    return false;
  }

  adxlWrite(REG_POWER_CTL, 0x00);      // 대기
  adxlWrite(REG_DATA_FORMAT, 0x0B);    // full-res, ±16g
  // ⚠ 800Hz. 100Hz(0x0A)로 두면 20~50ms 짜리 충격을 통째로 놓친다.
  adxlWrite(REG_BW_RATE, 0x0D);        // 800 Hz

  // Activity 인터럽트: 임계 초과 시 INT1 을 HIGH 로
  adxlWrite(REG_THRESH_ACT, ACT_THRESHOLD);
  // 0xF0 = AC 커플링 + x,y,z 참여.
  // AC 커플링이면 칩이 직전 값을 기준선으로 삼아 **중력 1g 를 자동으로 뺀다.**
  // DC(0x70) 로 두면 가만히 있어도 1g 라 임계값을 1g 만큼 낭비하게 된다.
  adxlWrite(REG_ACT_INACT, 0xF0);
  adxlWrite(REG_INT_MAP, 0x00);        // 모든 인터럽트를 INT1 으로
  adxlWrite(REG_INT_ENABLE, 0x10);     // Activity 만 활성
  adxlRead(REG_INT_SOURCE);            // 래치 초기화

  adxlWrite(REG_POWER_CTL, 0x08);      // 측정 시작
  return true;
}

// ─── ESP-NOW ───────────────────────────────────────────────────────

volatile bool     last_delivered = false;
volatile bool     got_result = false;

// 실제 도달 여부는 이 콜백으로만 알 수 있다.
// esp_now_send() 의 반환값은 "큐에 넣었다" 는 뜻일 뿐이다.
//
// ESP-IDF 5.4 부터 첫 인자가 const uint8_t* mac 에서
// const wifi_tx_info_t* 로 바뀌었다. 두 버전 모두에서 빌드되게 분기한다.
#if defined(ESP_IDF_VERSION) && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)
void onSent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
#else
void onSent(const uint8_t *mac, esp_now_send_status_t status) {
#endif
  last_delivered = (status == ESP_NOW_SEND_SUCCESS);
  got_result = true;
}

bool espnowBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  // 채널을 명시적으로 맞춘다. 이게 어긋나면 패킷이 조용히 사라진다.
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW 초기화 실패");
    return false;
  }
  esp_now_register_send_cb(onSent);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, RECEIVER_MAC, 6);
  peer.channel = 0;              // 0 = 현재 채널 사용. 명시값보다 안전하다.
  peer.ifidx   = WIFI_IF_STA;    // ⚠ 이걸 빼면 일부 코어에서 조용히 실패한다
  peer.encrypt = false;
  esp_now_del_peer(RECEIVER_MAC);          // 재등록 시 충돌 방지
  esp_err_t pe = esp_now_add_peer(&peer);
  if (pe != ESP_OK) {
    Serial.printf("ESP-NOW peer 등록 실패 (err=%d)\n", pe);
    return false;
  }

  // 설정이 실제로 먹었는지 확인 — WiFi.disconnect() 가 채널을 되돌리는 경우가 있다
  uint8_t ch; wifi_second_chan_t sc;
  esp_wifi_get_channel(&ch, &sc);
  Serial.printf("ESP-NOW 준비 — 실제 채널 %u (설정값 %u), 내 MAC %s\n",
                ch, ESPNOW_CHANNEL, WiFi.macAddress().c_str());
  Serial.printf("  대상 %02X:%02X:%02X:%02X:%02X:%02X\n",
                RECEIVER_MAC[0], RECEIVER_MAC[1], RECEIVER_MAC[2],
                RECEIVER_MAC[3], RECEIVER_MAC[4], RECEIVER_MAC[5]);
  if (ch != ESPNOW_CHANNEL) {
    Serial.println("  ⚠ 채널이 설정과 다릅니다 — 이게 도달 실패의 원인일 수 있습니다");
  }
  return true;
}

// 실시간 수준 보고. 응답을 기다리지 않는다 — 초당 여러 번 보내므로
// sendImpact 처럼 300ms 씩 블로킹하면 측정 루프가 멈춘다.
void sendLevel(float peak_g) {
  ImpactMsg m = {};
  m.kind = 1;
  strncpy(m.device_id, DEVICE_ID, sizeof(m.device_id) - 1);
  m.seq = boot_count;
  m.peak_g = peak_g;
  esp_now_send(RECEIVER_MAC, (uint8_t *)&m, sizeof(m));
}

void sendImpact(float peak_g, float dur_ms) {
  ImpactMsg m = {};
  m.kind = 0;
  strncpy(m.device_id, DEVICE_ID, sizeof(m.device_id) - 1);
  m.seq = boot_count;
  m.peak_g = peak_g;
  m.duration_ms = dur_ms;
  m.battery_mv = 0;

  got_result = false;
  esp_err_t r = esp_now_send(RECEIVER_MAC, (uint8_t *)&m, sizeof(m));

  if (r != ESP_OK) {
    Serial.printf("전송 호출 실패 (err=%d)  peak=%.2fg\n", r, peak_g);
    return;
  }

  // 콜백을 기다린다. 이게 실제 도달 여부다.
  uint32_t t0 = millis();
  while (!got_result && millis() - t0 < 300) delay(5);

  if (!got_result) {
    Serial.printf("전송 결과 없음 (타임아웃)  peak=%.2fg\n", peak_g);
  } else if (last_delivered) {
#if USE_BROADCAST
    Serial.printf("→ 브로드캐스트 송출  peak=%.2fg  (수신기 쪽에서 확인하세요)\n", peak_g);
#else
    Serial.printf("★ 도달 성공  peak=%.2fg dur=%.0fms\n", peak_g, dur_ms);
#endif
  } else {
    Serial.printf("✗ 도달 실패 — 수신기가 꺼져 있거나 채널/MAC 불일치  peak=%.2fg\n", peak_g);
  }
}

// ─── 충격 파형 측정 ────────────────────────────────────────────────

void captureImpact(float &peak_g, float &dur_ms) {
  const float TRIG_G = ACT_THRESHOLD * 0.0625f;
  uint32_t t0 = millis();
  uint32_t over_start = 0, over_total = 0;
  peak_g = 0;

  while (millis() - t0 < CAPTURE_MS) {
    int16_t x, y, z;
    adxlReadXYZ(x, y, z);
    // 중력을 뺀 **변화량**을 본다. 가만히 있으면 0 근처, 충격이 오면 튄다.
    // 예전처럼 절대 크기를 쓰면 정지 상태에서도 1.0g 라 임계값이 왜곡된다.
    float mag = fabsf(sqrtf(toG(x) * toG(x) + toG(y) * toG(y)
                            + toG(z) * toG(z)) - 1.0f);
    if (mag > peak_g) peak_g = mag;

    if (mag >= TRIG_G) {
      if (over_start == 0) over_start = millis();
    } else if (over_start) {
      over_total += millis() - over_start;
      over_start = 0;
    }
    delayMicroseconds(500);   // 800Hz 센서에 맞춰 촘촘히 읽는다
  }
  if (over_start) over_total += millis() - over_start;
  dur_ms = over_total;
}

// ─── 메인 ─────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(DEBUG_MODE ? 1500 : 200);

  boot_count++;
  pinMode(PIN_INT1, INPUT);
  Wire.begin();
  Wire.setClock(400000);   // 접촉 불량으로 읽기 실패가 잦으면 100000 으로 낮추세요

  if (!adxlBegin()) {
    Serial.println("센서 초기화 실패 — 10초 후 재시도");
    delay(10000);
    ESP.restart();
  }

  Serial.printf("\n부팅 #%lu  (원인: %d)\n", boot_count, esp_sleep_get_wakeup_cause());
  Serial.print("내 MAC: "); Serial.println(WiFi.macAddress());

  if (DEBUG_MODE) {
    Serial.println("\n[DEBUG] 딥슬립 없이 가속도를 계속 출력합니다.");
    Serial.println("바닥을 두드리거나 물건을 떨어뜨려 보세요.");
    Serial.println("peak 은 **중력을 뺀** 값입니다 (가만히 두면 0.0 근처).");
    Serial.println("실제로 넘어져 보고 그때 peak 을 적어두세요.");
    Serial.println("ACT_THRESHOLD = 그 값의 절반 / 0.0625  로 잡으면 됩니다.\n");
    espnowBegin();
    return;
  }

  // ── 실사용 경로 ──
  espnowBegin();

  bool woke_by_int = (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_GPIO);
  if (woke_by_int || boot_count == 1) {
    float peak, dur;
    captureImpact(peak, dur);
    if (peak >= ACT_THRESHOLD * 0.0625f) {
      sendImpact(peak, dur);
    } else {
      Serial.printf("임계 미달 (peak=%.2fg) — 전송 안 함\n", peak);
    }
  }

  adxlRead(REG_INT_SOURCE);   // 인터럽트 래치 해제

  Serial.println("딥슬립 진입\n");
  Serial.flush();
  esp_deep_sleep_enable_gpio_wakeup(BIT(PIN_INT1), ESP_GPIO_WAKEUP_GPIO_HIGH);
  esp_deep_sleep_start();
}

void loop() {
  if (!DEBUG_MODE) return;   // 실사용 경로는 setup 에서 잠든다

  static uint32_t last_send = 0;

  // 300ms 창에서 최대한 빨리 읽어 피크를 잡는다.
  // 한 번씩 띄엄띄엄 읽으면 짧은 충격을 놓친다.
  float gx = 0, gy = 0, gz = 0, peak = 0;
  uint32_t samples = 0;
  uint32_t t_end = millis() + 300;
  while (millis() < t_end) {
    int16_t x, y, z;
    adxlReadXYZ(x, y, z);
    gx = toG(x); gy = toG(y); gz = toG(z);
    float m = fabsf(sqrtf(gx * gx + gy * gy + gz * gz) - 1.0f);   // 중력 제외
    if (m > peak) peak = m;
    samples++;
  }

  const float TRIG_G = ACT_THRESHOLD * 0.0625f;
  bool over = peak >= TRIG_G;

  char bar[41];
  int blen = (int)(peak * 20);   // 0.5g 가 10칸
  if (blen > 40) blen = 40;
  for (int i = 0; i < blen; i++) bar[i] = '#';
  bar[blen] = '\0';

  Serial.printf("x=%+5.2f y=%+5.2f z=%+5.2f  peak=%5.2fg (임계 %.2fg, %lu샘플) INT1=%d %s%s\n",
                gx, gy, gz, peak, TRIG_G, samples,
                digitalRead(PIN_INT1), bar, over ? "  <<< 충격" : "");

  if (over && millis() - last_send > 2000) {
    last_send = millis();
    sendImpact(peak, 0);
  }

#if MONITOR_HZ > 0
  // 대시보드 실시간 그래프. 충격이 아니어도 현재 수준을 계속 보낸다.
  static uint32_t last_lv = 0;
  if (millis() - last_lv > (1000UL / MONITOR_HZ)) {
    last_lv = millis();
    sendLevel(peak);
  }
#endif

#if HEARTBEAT_SEC > 0
  // 두드리지 않아도 무선 경로를 확인할 수 있게 주기적으로 보낸다.
  static uint32_t last_hb = 0;
  if (millis() - last_hb > HEARTBEAT_SEC * 1000UL) {
    last_hb = millis();
    Serial.print("[하트비트] ");
    sendImpact(peak, 0);
  }
#endif
}
