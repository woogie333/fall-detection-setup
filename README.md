# RDK X3 낙상 감지 시스템 — 셋업 저장소

열화상 + mmWave + 진동센서를 융합한 낙상 감지 시스템의 **환경 구축·운영 도구 모음**입니다.

감지 모델 본체는 별도 저장소에 있습니다 →
[ehgus06-alt/thermal-fall-detection](https://github.com/ehgus06-alt/thermal-fall-detection)

---

## 빠른 시작

새 SD 카드로 보드를 세팅하는 경우:

```bash
git clone https://github.com/<계정>/fall-detection-setup.git
cd fall-detection-setup
chmod +x scripts/setup_rdk.sh
./scripts/setup_rdk.sh
```

기본 패키지, VNC, edgebridge, AI 모델 환경까지 한 번에 설치합니다.
자세한 순서는 [docs/01-setup.md](docs/01-setup.md)를 보세요.

부분 설치와 상태 점검:

```bash
./scripts/setup_rdk.sh base      # 기본 패키지
./scripts/setup_rdk.sh vnc       # VNC + XFCE
./scripts/setup_rdk.sh bridge    # SmartThings edgebridge
./scripts/setup_rdk.sh model     # torch + 모델 저장소
./scripts/setup_rdk.sh check     # 상태 점검 (언제든)
```

---

## 시스템 구성

```
 [ 메인 유닛 — RDK X3 ]                    [ 진동센서 노드 ]

  Lepton 3.0 (USB/UVC)  ─┐                 ADXL345
  LD2450 (UART)         ─┼─ 융합 판정         │ I2C
  ESP32 수신기 (USB)     ─┘      │           XIAO ESP32C3
                                 │                │
                          edgebridge         배터리 구동
                                 │                │
                          SmartThings 허브   └─ ESP-NOW ─┘
                                 │
                             휴대폰 알림
```

---

## 스크립트

| 파일 | 용도 |
|---|---|
| [`scripts/setup_rdk.sh`](scripts/setup_rdk.sh) | 전체 자동 설치 및 상태 점검 |
| [`scripts/camera_test.py`](scripts/camera_test.py) | 카메라 인식 확인, 스냅샷, 브라우저 스트리밍 |
| [`scripts/web_view.py`](scripts/web_view.py) | AI 판정 화면(HUD)을 브라우저로 스트리밍 |
| [`scripts/smartthings_notifier.py`](scripts/smartthings_notifier.py) | 알림 전송 모듈 (융합 로직에서 import) |
| [`scripts/test_notify.py`](scripts/test_notify.py) | SmartThings 연동 테스트 |

### camera_test.py

```bash
python3 scripts/camera_test.py --info              # 장치·포맷 조회
python3 scripts/camera_test.py -d 9 --raw --stream # Lepton Y16
# → 브라우저에서 http://<보드IP>:8090
```

### web_view.py

`lepton_live.py`와 **같은 폴더**에서 실행하세요. 원본 코드는 수정하지 않고,
HUD 출력만 가로채 브라우저로 넘깁니다.

```bash
cd ~/fall-detection
python3 web_view.py --camera 9 --y16 --thr 0.4
```

`lepton_live.py`의 모든 옵션을 그대로 쓸 수 있습니다.

---

## 문서

| 문서 | 내용 |
|---|---|
| [01-setup.md](docs/01-setup.md) | **SD 카드부터 시작하는 전체 세팅 순서** |
| [02-smartthings.md](docs/02-smartthings.md) | SmartThings 연동 상세 |
| [03-model.md](docs/03-model.md) | AI 모델 코드 분석, 성능 벤치마크, 실행 |
| [04-architecture.md](docs/04-architecture.md) | 3센서 융합 설계, 판정 로직 |
| [05-remote-and-hardware.md](docs/05-remote-and-hardware.md) | 원격접속, 무선 노드, 3D 모델링 제원 |
| [06-purchase-list.md](docs/06-purchase-list.md) | 디바이스마트 구매 목록 (가격 포함) |
| [07-bom-review.md](docs/07-bom-review.md) | 부품 재점검 — 누락·정정 사항 |
| [08-bom-no-solder.md](docs/08-bom-no-solder.md) | 납땜 없는 구성 대안 |
| [enclosure_sketch.html](docs/enclosure_sketch.html) | 케이스 형태 스케치 (브라우저로 열기) |

---

## 자주 막히는 곳

01-setup.md 마지막에 전체 목록이 있습니다. 특히 자주 겪은 것들:

**edgebridge에 `127.0.0.1`로 요청하면 거부됩니다.** 출발지 IP가 앱에 등록된 주소와
일치해야 하므로 **보드의 LAN IP**로 요청하세요.

**edgebridge를 재시작하면 등록이 날아갑니다.** `No existing registrations`가 뜨면
SmartThings 앱에서 기기를 삭제하고 다시 만드세요.

**기기 이름은 대소문자까지 정확해야** 합니다. 다르면 조용히 400을 반환합니다.

**루틴 조건은 "눌림(Pushed)"입니다.** 이 드라이버의 기기는 스위치가 아니라 버튼입니다.

**Lepton 프레임이 `(1, 19200)`으로 옵니다.** OpenCV가 Y16 버퍼를 펴주지 않아서,
`camera_test.py`와 `web_view.py`에 교정 로직(`fix_frame`)이 들어 있습니다.
없으면 메모리 초과로 프로세스가 죽습니다.

---

## systemd 유닛

```bash
sudo cp systemd/edgebridge.service /etc/systemd/system/
sudo nano /etc/systemd/system/edgebridge.service   # User/경로 수정
sudo systemctl daemon-reload && sudo systemctl enable --now edgebridge
```

VNC와 감지 서비스 유닛은 [01-setup.md](docs/01-setup.md)에 붙여넣기용 블록으로 있습니다.

---

## 하드웨어

| 역할 | 부품 | 인터페이스 |
|---|---|---|
| 연산 | RDK X3 | — |
| 열화상 | FLIR Lepton 3.0 + PureThermal | USB (UVC), Y16 160×120 @ 9fps |
| mmWave | HLK-LD2450 | UART 256000 8N1 |
| 충격 | ADXL345 | I2C (0x53) |
| 무선 | XIAO ESP32C3 ×2 | ESP-NOW + USB |

구매 목록과 배선은 [06-purchase-list.md](docs/06-purchase-list.md)를 보세요.

---

## 안전 고지

이 시스템은 **보조 수단**입니다. 어떤 센서 조합도 낙상을 100% 감지하지 못하며,
감시 구역 밖·가구에 가려진 위치·화장실 등은 사각지대입니다.

실제 보호 목적으로 사용하실 경우 응급 호출 버튼 같은 수동 수단을 반드시 병행하세요.
