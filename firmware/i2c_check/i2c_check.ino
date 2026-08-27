/*
 * I2C 배선 점검 — 납땜 전에 선만 눌러 붙여서 확인하는 용도
 *
 * XIAO ESP32C3 에 이 스케치를 올리고, ADXL345 의 선을 손으로 눌러 접촉시킨 채
 * 시리얼 모니터(115200)를 보세요. 접촉이 되는 순간 바로 반응합니다.
 *
 * 최소 배선 — 이 4가닥만 있으면 됩니다:
 *     VCC → 3V3
 *     GND → GND
 *     SDA → D4 (GPIO6)
 *     SCL → D5 (GPIO7)
 *
 * (CS, SDO, INT1 은 이 테스트에 없어도 됩니다. GY-291 은 대개 CS 가 보드에서
 *  풀업되어 있어 I2C 모드로 동작합니다. 만약 아무것도 안 잡히면 CS 를 3V3 에
 *  대보세요 — 그게 원인인 경우가 있습니다.)
 *
 * 출력 예:
 *     [  12] 0x53 ADXL345  DEVID=0xE5  x=+0.01 y=-0.02 z=+1.00 |1.00g|   안정 12회
 *     [  13] --- 장치 없음 ---                                            끊김 1회
 *
 * 숫자가 계속 올라가면서 값이 나오면 배선이 맞는 겁니다.
 * "끊김" 이 자주 뜨면 접촉 불량이니 선을 다시 눌러보세요.
 */

#include <Wire.h>

#define ADXL_ADDR   0x53
#define ALT_ADDR    0x1D    // SDO 를 3V3 에 연결했을 때의 주소
#define REG_DEVID   0x00
#define REG_POWER   0x2D
#define REG_FORMAT  0x31
#define REG_DATAX0  0x32

uint32_t tick = 0;
uint32_t ok_streak = 0, fail_streak = 0;
bool initialized = false;
uint8_t found_addr = 0;

void wr(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

int16_t rd16(uint8_t addr, uint8_t reg) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -32768;
  Wire.requestFrom((int)addr, 2);
  if (Wire.available() < 2) return -32768;
  uint8_t lo = Wire.read(), hi = Wire.read();
  return (int16_t)(lo | (hi << 8));
}

uint8_t rd8(uint8_t addr, uint8_t reg, bool *ok) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) { *ok = false; return 0; }
  Wire.requestFrom((int)addr, 1);
  if (!Wire.available()) { *ok = false; return 0; }
  *ok = true;
  return Wire.read();
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Wire.begin();
  Wire.setClock(400000);   // 충격을 놓치지 않으려면 빨라야 한다.
                           // 접촉 불량으로 읽기 실패가 잦으면 100000 으로 낮추세요.

  Serial.println();
  Serial.println("=================================================");
  Serial.println(" I2C 배선 점검");
  Serial.println();
  Serial.println("  VCC -> 3V3     SDA -> D4");
  Serial.println("  GND -> GND     SCL -> D5");
  Serial.println();
  Serial.println(" 선을 눌러 접촉시키면 바로 반응합니다.");
  Serial.println("=================================================");
  Serial.println();
}

void loop() {
  tick++;

  // ── I2C 버스 전체 스캔 ──
  uint8_t found[8];
  int n = 0;
  for (uint8_t a = 1; a < 127 && n < 8; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) found[n++] = a;
  }

  if (n == 0) {
    ok_streak = 0;
    fail_streak++;
    initialized = false;
    found_addr = 0;
    Serial.printf("[%4lu] --- 장치 없음 ---", tick);
    if (fail_streak == 1) Serial.print("   (접촉 끊김)");
    else if (fail_streak == 5) Serial.print("   <-- 배선을 확인하세요");
    Serial.println();
    delay(700);
    return;
  }

  fail_streak = 0;
  ok_streak++;

  // ADXL345 가 있는지 확인
  uint8_t adxl = 0;
  for (int i = 0; i < n; i++) {
    if (found[i] == ADXL_ADDR || found[i] == ALT_ADDR) adxl = found[i];
  }

  if (!adxl) {
    Serial.printf("[%4lu] 장치 %d개 발견:", tick, n);
    for (int i = 0; i < n; i++) Serial.printf(" 0x%02X", found[i]);
    Serial.println("   <-- ADXL345(0x53) 아님. SDO 를 GND 에 연결했나요?");
    delay(700);
    return;
  }

  // 처음 잡혔으면 초기화
  if (!initialized || found_addr != adxl) {
    found_addr = adxl;
    wr(adxl, REG_POWER, 0x00);    // 대기 (설정 변경 전)
    wr(adxl, REG_FORMAT, 0x0B);   // full-res, ±16g
    wr(adxl, 0x2C, 0x0D);         // BW_RATE: 800Hz — 짧은 충격을 잡으려면 필요
    wr(adxl, REG_POWER, 0x08);    // 측정 시작
    initialized = true;
    delay(20);
  }

  bool ok = false;
  uint8_t devid = rd8(adxl, REG_DEVID, &ok);

  if (!ok) {
    Serial.printf("[%4lu] 0x%02X 응답하나 레지스터 읽기 실패   (접촉 불안정)\n", tick, adxl);
    delay(700);
    return;
  }

  // ── 빠른 샘플링 + 피크 유지 ──
  // 충격은 20~50ms 라 느리게 읽으면 통째로 놓친다. 300ms 동안 최대한 빨리
  // 읽어서 그 구간의 최대값을 잡아 보여준다.
  float x = 0, y = 0, z = 0;
  float peak = 0, low = 99;
  uint32_t samples = 0;
  uint32_t t_end = millis() + 300;

  while (millis() < t_end) {
    int16_t rx = rd16(adxl, REG_DATAX0);
    int16_t ry = rd16(adxl, REG_DATAX0 + 2);
    int16_t rz = rd16(adxl, REG_DATAX0 + 4);
    if (rx == -32768 || ry == -32768 || rz == -32768) break;   // 읽기 실패
    x = rx * 0.0039f; y = ry * 0.0039f; z = rz * 0.0039f;
    float m = sqrtf(x * x + y * y + z * z);
    if (m > peak) peak = m;
    if (m < low) low = m;
    samples++;
  }

  if (samples == 0) {
    Serial.printf("[%4lu] 0x%02X 읽기 실패 (접촉 불안정)\n", tick, adxl);
    delay(300);
    return;
  }

  // 피크를 막대로 표시 — 두드릴 때 길이가 확 늘어나야 정상
  char bar[41];
  int blen = (int)(peak * 8);
  if (blen > 40) blen = 40;
  for (int i = 0; i < blen; i++) bar[i] = '#';
  bar[blen] = '\0';

  Serial.printf("[%4lu] 0x%02X %s  x=%+5.2f y=%+5.2f z=%+5.2f  "
                "peak=%5.2fg low=%5.2fg (%lu샘플) %s",
                tick, adxl, devid == 0xE5 ? "ADXL345" : "??????",
                x, y, z, peak, low, samples, bar);

  if (devid != 0xE5) {
    Serial.print("   <-- DEVID 가 0xE5 가 아닙니다");
  } else if (peak > 1.6f) {
    Serial.print("  <<< 충격");
  }
  Serial.println();

  delay(100);
}
