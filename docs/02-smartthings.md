# SmartThings 연동 셋업 가이드

목표: **센서 없이**, RDK X3에서 HTTP 요청 하나를 쏘면 휴대폰 SmartThings 앱에 알림이 뜨는
경로를 완성한다. 낙상 감지 로직은 나중에 이 위에 얹는다.

소요 시간: 30분 내외

---

## 전체 구조

```
  RDK X3                                SmartThings 허브        휴대폰
┌──────────────────┐                   ┌────────────────┐    ┌────────┐
│ 낙상 감지 파이썬  │                   │ LAN Device     │    │  ST 앱  │
│       │          │   HTTP POST       │ Trigger V2     │    │  알림   │
│       ▼          │  ───────────────► │ (Edge Driver)  │───►│         │
│  edgebridge      │   (LAN 내부)      │                │    │         │
└──────────────────┘                   └────────────────┘    └────────┘
     같은 보드에서 둘 다 실행
```

edgebridge가 필요한 이유는, SmartThings Edge 드라이버가 보안상 **외부에서 들어오는 연결을
직접 받을 수 없기** 때문입니다. 드라이버가 edgebridge에 "나한테 알려줘"라고 등록해두면,
edgebridge가 HTTP 요청을 받아 허브로 전달합니다.

---

## 1단계 — 드라이버 설치 (5분)

휴대폰이나 PC 브라우저에서 아래 채널 초대 링크를 엽니다.

```
https://api.smartthings.com/invitation-web/accept?id=cc2197b9-2dce-4d88-b6a1-2d198a0dfdef
```

1. SmartThings 계정으로 로그인
2. 채널 등록(Enroll) 후 허브 선택
3. 드라이버 목록에서 **LAN Device Trigger V2** 설치

설치까지 몇 분 걸릴 수 있습니다.

---

## 2단계 — edgebridge 설치 및 실행 (10분)

RDK X3에 SSH로 접속해서 진행합니다.

```bash
cd ~
git clone https://github.com/toddaustin07/edgebridge.git
cd edgebridge

# 의존성
pip install requests --break-system-packages

# 실행 (우선 포그라운드로 동작 확인)
python3 edgebridge.py
```

`Server started on port 8088` 비슷한 메시지가 나오면 성공입니다.

**보드 IP를 확인해두세요.** 다음 단계에서 필요합니다.

```bash
hostname -I
```

방화벽이 켜져 있다면 포트를 열어줍니다.

```bash
sudo ufw allow 8088/tcp
```

---

## 3단계 — SmartThings 앱에서 기기 생성 (5분)

1. SmartThings 앱 → **기기 추가** → **주변 기기 검색(Scan nearby)**
2. "LAN-Triggered Device"가 생성됩니다 (보통 '할당되지 않은 방'에 들어갑니다)
3. 해당 기기를 열고 **점 3개 메뉴 → 설정**에서 아래 값을 입력합니다

| 항목 | 입력값 | 주의 |
|---|---|---|
| LAN Device Name | `falldetect` | **공백·특수문자 불가.** 코드와 정확히 일치해야 함 |
| LAN App/Device Address | 보드 IP (예: `192.168.0.100`) | 포트 없이 IP만 |
| Bridge Address | 보드 IP:8088 (예: `192.168.0.100:8088`) | **포트 포함** |
| Device icon | Switch | |

두 주소 칸의 형식이 다릅니다. 하나는 IP만, 하나는 IP:포트예요.
여기서 틀리는 경우가 가장 많습니다.

### 중요: 코드에서 127.0.0.1을 쓰면 안 됩니다

edgebridge는 들어온 요청의 **출발지 IP가 위 `LAN App/Device Address`와 일치하는지 검사**합니다.
불일치하면 `Unregistered address or invalid endpoint`로 거부합니다.

edgebridge를 같은 보드에서 돌리더라도, 루프백(`127.0.0.1`)으로 접속하면 출발지가
`127.0.0.1`이 되어 반드시 거부당합니다. **보드 자신의 LAN IP로 접속해야** 출발지도
같은 IP가 되어 통과합니다.

```bash
# 틀림 — Unregistered address
curl -X POST http://127.0.0.1:8088/falldetect/trigger

# 맞음
curl -X POST http://192.168.0.100:8088/falldetect/trigger
```

`smartthings_notifier.py`는 `bridge_host`를 비워두면 LAN IP를 자동으로 찾습니다.

기기 이름을 `falldetect` 외의 것으로 하셨다면 테스트 시 `--device` 옵션으로 지정하세요.

---

## 4단계 — 연동 테스트 (5분)

이 저장소의 `smartthings_notifier.py`와 `test_notify.py`를 보드에 올린 뒤:

```bash
pip install requests --break-system-packages

# (1) edgebridge가 응답하는지 확인
python3 test_notify.py --check

# (2) 실제 알람 1회 전송 → 휴대폰 확인
python3 test_notify.py --once

# (3) 중복 억제가 동작하는지 확인 (5회 시도 → 1회만 전송되어야 정상)
python3 test_notify.py --burst 5

# (4) 대화형: 엔터 칠 때마다 알람
python3 test_notify.py
```

**알림을 받으려면 앱에서 루틴을 하나 걸어야 합니다.**
기기가 켜지는 것만으로는 푸시가 오지 않습니다.

SmartThings 앱 → 루틴 → 추가:
- 조건(If): `falldetect` 기기가 **눌림(Pushed)**
- 동작(Then): **알림 보내기** (원하는 문구로)

이 드라이버의 기기는 켜짐/꺼짐 스위치가 아니라 **버튼(momentary)**으로 동작합니다.
루틴 조건에 "눌림"만 보이는 것이 정상이며, 낙상처럼 순간 이벤트에는 이쪽이 의미상 맞습니다.
자동으로 원복되므로 껐다 켜는 처리도 필요 없습니다.

여기에 조명 켜기, 다른 사람에게 통보 등을 추가할 수 있습니다.

---

## 5단계 — 자동 시작 등록 (5분)

전원이 나갔다 들어와도 알아서 뜨도록 systemd에 등록합니다.

```bash
sudo cp edgebridge.service /etc/systemd/system/
sudo nano /etc/systemd/system/edgebridge.service   # User/경로를 실제에 맞게 수정

sudo systemctl daemon-reload
sudo systemctl enable --now edgebridge
sudo systemctl status edgebridge
```

로그 확인:

```bash
journalctl -u edgebridge -f
```

---

## 감지 코드에 붙이기

낙상 로직에서는 이렇게 씁니다.

```python
from smartthings_notifier import NotifierConfig, SmartThingsNotifier

notifier = SmartThingsNotifier(NotifierConfig(
    bridge_host="",            # 비워두면 보드의 LAN IP를 자동 검출한다
    device_name="falldetect",
    cooldown_sec=180.0,        # 3분간 재알람 억제
))
notifier.start()

# 기동 시 한 번 점검해두면 문제를 일찍 발견할 수 있다
if not notifier.ping():
    logger.error("edgebridge에 연결할 수 없습니다 — 알림이 나가지 않습니다")

# ... 감지 루프 안에서 ...
if state == State.ALARM:
    notifier.notify()          # 논블로킹. 감지 루프를 멈추지 않는다.
```

`notify()`는 즉시 반환합니다. 전송은 백그라운드 스레드가 처리하고, 실패 시 2초·4초 간격으로
3번까지 재시도합니다. **감지 루프가 네트워크 때문에 멈추는 일은 없습니다.**

---

## 문제 해결

**`Unregistered address or invalid endpoint`**

출발지 IP가 앱에 등록된 주소와 다릅니다. `127.0.0.1` 대신 보드의 LAN IP로 접속하세요.
그래도 안 되면 앱 설정의 `LAN App/Device Address`가 `hostname -I` 값과 같은지 확인하세요.
보드 IP가 바뀌었을 수 있습니다.

**로그가 전혀 안 찍힌다 / 트리거해도 반응 없다**

이전에 `&`로 띄운 edgebridge가 아직 살아서 포트를 쥐고 있을 수 있습니다.
새로 띄운 인스턴스는 바인딩에 실패하고, 요청은 옛 프로세스로 갑니다.

```bash
pgrep -af edgebridge      # 여러 개면 문제
pkill -f edgebridge.py
python3 ~/edgebridge/edgebridge.py
```

**`--check`는 되는데 앱에 알림이 안 온다**

십중팔구 루틴을 안 걸었거나 기기 이름이 다릅니다. 앱 설정의 LAN Device Name과
`--device` 값이 **대소문자까지** 같은지 확인하세요.

**edgebridge 로그에는 요청이 찍히는데 기기 상태가 안 바뀐다**

Bridge Address에 포트(`:8088`)를 빠뜨렸을 가능성이 높습니다.
그리고 허브와 보드가 **같은 서브넷**에 있어야 합니다. 게스트 WiFi에 물려 있으면 안 됩니다.

**보드 재부팅 후 안 된다**

보드 IP가 바뀌었을 수 있습니다. 앞서 얘기한 고정 IP 설정이나 공유기 DHCP 예약을 해두세요.
앱 설정의 주소도 함께 갱신해야 합니다.

**연결이 간헐적으로 끊긴다**

`journalctl -u edgebridge -f`로 로그를 보세요. WiFi 절전 모드가 원인인 경우가 있습니다.

```bash
sudo iw dev wlan0 set power_save off
```

---

## 다음 단계

이 경로가 확정되면 그 다음은 센서입니다. 개발 순서상 LD2450 파싱 → Lepton 특징 추출 →
가속도계 → 융합 로직 순으로 가면 됩니다.

나중에 감지 로직이 안정되면 전용 Edge Driver를 작성해 `fallDetected`, `confidence` 같은
커스텀 Capability로 교체할 수 있습니다. 그때도 `smartthings_notifier.py`의 인터페이스는
그대로 두고 내부 전송부만 바꾸면 되므로, 지금 작업이 버려지지 않습니다.
