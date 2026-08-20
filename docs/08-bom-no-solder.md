# 납땜 없는 버전 — 디바이스마트 기준

조사일: 2026-08-17

> 가격은 전부 미확인입니다. 디바이스마트가 가격을 JS로 렌더링해서 웹으로는 못 읽습니다.
> 링크에서 확인하세요.

---

## 결론부터

**XIAO를 포기하면 됩니다.** XIAO는 배터리를 뒷면 패드에 납땜해야 해서 구조적으로
무납땜이 불가능합니다.

대신 **FireBeetle ESP32-E (헤더 기납땜 버전)**을 쓰면 헤더도 붙어 있고
배터리도 커넥터로 꽂습니다.

| | XIAO ESP32C3 | **FireBeetle ESP32-E [DFR0654-F]** |
|---|---|---|
| 핀헤더 | 미납땜 | **기납땜** |
| 배터리 연결 | BAT 패드 납땜 | **JST PH 2.0 커넥터** |
| 충전 회로 | 내장 | **내장** |
| 딥슬립 소비전류 | 약 43 µA | **약 13 µA** |
| 크기 | 20×17.5mm | 더 큼 (Feather 폼팩터) |

딥슬립 전류가 오히려 3배 좋습니다. 배터리 수명이 늘어나요.
**크기는 커지지만** 바닥에 두는 노드라 큰 문제는 아닙니다.

---

## 부품 목록 (무납땜)

| # | 부품 | 링크 | 비고 |
|---|---|---|---|
| 1 | **FireBeetle ESP32-E with Header [DFR0654-F]** | [상품](https://www.devicemart.co.kr/goods/view?no=13903818) | **"with Header" 버전 필수.** 헤더 없는 DFR0654와 혼동 주의 |
| 2 | Seeed XIAO ESP32C3 (수신기용) | [상품](https://www.devicemart.co.kr/goods/view?no=15195259) | **납땜 불필요** — 아래 설명 |
| 3 | ADXL345 모듈 (헤더 기납땜) | [상품](https://www.devicemart.co.kr/goods/view?no=38318) | 구매 전 판매자에게 헤더 상태 문의 권장 |
| 4 | 점퍼 와이어 암-암 (F/F) | [카테고리](https://www.devicemart.co.kr/goods/catalog?code=001100130003) | 5가닥 사용 |
| 5 | 리튬폴리머 배터리 | — | **아래 주의사항 참고** |
| 6 | USB-A to USB-C 케이블 | — | 수신기 ↔ RDK X3 |
| 7 | 3M VHB 양면테이프 | [상품](https://www.devicemart.co.kr/goods/view?no=12749838) | 바닥 고정 |

### 수신기는 원래 납땜이 필요 없습니다

이건 앞서 제가 헷갈리게 설명했는데, **수신기 XIAO는 USB만 꽂으면 됩니다.**
GPIO를 하나도 안 쓰거든요. ESP-NOW로 받아서 USB 시리얼로 보드에 넘기는 게 전부입니다.

그러니 수신기는 XIAO를 그냥 사서 케이블만 꽂으면 끝입니다. 헤더는 봉지째 서랍에 두세요.

---

## ⚠️ 유일하게 걸리는 곳 — 배터리 커넥터

**이것만 해결하면 완전 무납땜입니다.**

FireBeetle의 배터리 커넥터는 **JST PH 2.0**인데, 디바이스마트가 파는 국산 셀 다수가
**Molex 1.25mm (A1251)**입니다. 크기가 달라 안 꽂힙니다.

확인된 것: 1000mAh TW102050은 Molex 1.25 → **직결 불가**

해결 방법 세 가지입니다.

**1. 판매자에게 문의** (가장 확실)

디바이스마트 문의로 "JST PH 2.0 커넥터가 달린 3.7V 리튬폴리머가 있는지" 물어보세요.
DTP 계열([DTP103040 1200mAh](https://www.devicemart.co.kr/goods/view?no=12710293) 등)은
커넥터 규격이 페이지에 안 나와 있어서, 이게 PH 2.0이면 바로 해결됩니다.

**2. DFRobot 정품 배터리 찾기**

FireBeetle과 같은 DFRobot 배터리는 당연히 PH 2.0입니다. 디바이스마트에 있는지
"DFRobot 리튬 배터리"로 검색해보세요.

**3. 커넥터 교체** — 압착 공구가 필요합니다

[JST 커넥터 키트 FIT0299](https://www.devicemart.co.kr/goods/view?no=1358437)로
바꿀 수 있지만 압착 공구가 있어야 합니다. 납땜은 아니지만 공정이 하나 늘어요.

> **주의**: 커넥터가 안 맞는다고 선을 꼬아서 테이프로 감는 건 하지 마세요.
> 리튬 배터리는 접촉 불량이 발열·발화로 이어집니다.

---

## STEMMA QT 방식은 왜 안 되나

Adafruit의 STEMMA QT(Qwiic)를 쓰면 케이블만 꽂아서 I2C가 연결됩니다.
[ADXL343 STEMMA QT](https://www.devicemart.co.kr/goods/view?no=10912933)가
ADXL345와 레지스터 호환이라 코드도 그대로 돌아가고요.

**그런데 이 프로젝트에는 못 씁니다.**

STEMMA QT 커넥터에는 SDA/SCL/VCC/GND 4선만 있고 **INT 핀이 없습니다.**
우리는 ADXL345의 INT1으로 ESP32를 딥슬립에서 깨워야 하는데, 그 선을 뽑을 방법이
케이블에 없어요. 헤더 쪽에 납땜해야 합니다.

**단, 타이머 웨이크로 타협하면 STEMMA QT가 완전 무납땜입니다.** 예를 들어
1초마다 깨어나 가속도를 확인하는 방식이죠. 다만 이러면 소비전류가 크게 늘어
배터리 수명이 몇 달에서 몇 주로 줄고, 1초 사이의 충격을 놓칠 수 있습니다.

낙상 충격은 수십 밀리초 단위라 **타이머 폴링으로는 놓칠 가능성이 높습니다.**
그래서 INT1 인터럽트 방식을 권합니다.

---

## 배선 (F/F 점퍼 5가닥)

| ADXL345 | FireBeetle ESP32-E |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SDA | IO21 (D21) |
| SCL | IO22 (D22) |
| INT1 | IO25 등 RTC GPIO |

FireBeetle은 ESP32(오리지널) 기반이라 I2C 기본 핀이 21/22입니다.
XIAO(ESP32-C3)와 핀 번호가 다르니 주의하세요.

**INT1은 RTC GPIO에 연결해야** 딥슬립에서 깨울 수 있습니다. ESP32는
GPIO 0, 2, 4, 12~15, 25~27, 32~39가 RTC 도메인입니다. 실제 보드에서
어느 핀이 노출돼 있는지 확인하고 고르세요.

ADXL345 모듈의 CS와 SDO도 처리해야 하는데, 점퍼로 각각 3V3과 GND에 꽂으면 됩니다.
핀이 모자라면 브레드보드를 쓰시고요.

---

## 두 가지 안 비교

### A안 — 완전 무납땜 (배터리 커넥터 해결 시)

FireBeetle ESP32-E with Header + 헤더 기납땜 ADXL345 + F/F 점퍼 + PH 2.0 배터리

장점: 납땜 0회. 딥슬립 13µA로 배터리 수명 최상.
단점: 보드가 큼. 배터리 커넥터 확인 필요. 점퍼 배선이 케이스 안에서 지저분함.

### B안 — 납땜 최소화 (XIAO 유지)

XIAO 2개 + ADXL345 + 배터리

납땜 위치: XIAO 헤더 14핀, ADXL345 헤더, 배터리 리드 2군데.
장점: 훨씬 작음(20×17.5mm). 케이스가 얇아짐.
단점: 납땜 필요.

---

## 제 의견

**납땜이 가능하시다면 XIAO(B안)를 권합니다.**

바닥에 두는 물건이라 크기가 실제로 중요하고, FireBeetle은 Feather 폼팩터라
케이스가 눈에 띄게 커집니다. 납땜도 헤더 몇 개와 배터리 선 2가닥이라
한 시간이면 끝나는 양이에요.

**A안이 의미 있는 경우**는 팀원분들이 여러 노드를 만들어야 하거나,
납땜 환경에 매번 가기 어려운 상황입니다. 그때는 크기를 감수할 만합니다.

어느 쪽이든 **배터리 커넥터 규격은 주문 전에 확인**하세요.
A안은 PH 2.0이 필요하고, B안은 어떤 커넥터든 잘라내고 납땜하므로 상관없습니다.

---

## 미확인 항목

- 모든 가격
- DFR0654-F 충전 전류 수치
- ADXL345 모듈들의 헤더 기납땜 여부 (상세페이지에 명시 없음 → 문의 필요)
- DTP 계열 배터리의 커넥터 규격
- 디바이스마트에 PH 2.0 커넥터 장착 셀이 있는지

Sources:
- [FireBeetle ESP32-E with Header (DFR0654-F)](https://www.devicemart.co.kr/goods/view?no=13903818)
- [Adafruit HUZZAH32 Feather pre-soldered (ada-3591)](https://www.devicemart.co.kr/goods/view?no=14040482)
- [ADXL343 STEMMA QT (ada-4097)](https://www.devicemart.co.kr/goods/view?no=10912933)
- [Seeed XIAO ESP32-C3](https://www.devicemart.co.kr/goods/view?no=15195259)
- [1000mAh TW102050](https://www.devicemart.co.kr/goods/view?no=1376863)
- [JST 커넥터 키트 FIT0299](https://www.devicemart.co.kr/goods/view?no=1358437)
