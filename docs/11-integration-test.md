# 최종 통합 점검

열화상 AI + 무선 진동센서 + SmartThings 알림을 한 번에 돌리고 검증하는 절차.

> LD2450 은 사고로 사망하여 이번 점검에서 제외했습니다. 교체 후에는
> `fusion_run.py` 에 레이더 입력을 추가하면 됩니다(구조는 이미 열려 있음).

---

## 구성

```
  Lepton 3.0 ──USB──┐
                    │   fusion_run.py
                    ├──►  lepton_live.py (bbox 브랜치)
  진동 노드          │       │ 지속 낙상 판정 (--fall-hold)
   └─ESP-NOW─┐      │       ▼
             │      │    융합: 열화상 낙상 + 최근 충격
  수신기 XIAO ┘──USB─┘       │
                            ▼
                       edgebridge ──► SmartThings 허브 ──► 폰 알림
```

**융합 지점**은 `lepton_live.py` 의 웹훅 호출입니다. 그 코드는 이미
"FALL 이 `--fall-hold` 초 동안 유지됨" 을 확인한 뒤에만 웹훅을 부르므로,
`fusion_run.py` 는 그 시점에 진동센서 신호를 함께 보고 최종 결정을 내립니다.

친구분 저장소 파일은 **한 줄도 수정하지 않습니다.**

---

## 0. 준비 확인

```bash
cd ~/fall-detection && git branch --show-current    # experiment-bbox 여야 함
cd ~/fall-detection-setup && git pull

# 스크립트를 모델 저장소로 복사 (lepton_live.py 와 같은 폴더여야 함)
cp ~/fall-detection-setup/scripts/fusion_run.py  ~/fall-detection/
cp ~/fall-detection-setup/scripts/impact_test.py ~/fall-detection/
```

브랜치가 다르면:

```bash
cd ~/fall-detection
git fetch origin experiment-bbox && git checkout experiment-bbox
```

**하드웨어 연결**

- [ ] Lepton(PureThermal) USB 연결 → `v4l2-ctl --list-devices` 로 인덱스 확인
- [ ] 수신기 XIAO USB 연결 → `ls /dev/ttyACM*`
- [ ] 진동 노드 전원 (USB 또는 배터리), **외장 안테나 장착 확인**
- [ ] `sudo systemctl status edgebridge` → active

---

## 1. 단계별 점검

한꺼번에 켜지 말고 하나씩 확인하세요. 문제가 생겼을 때 원인이 명확해집니다.

### 1-1. 열화상만

```bash
cd ~/fall-detection && source .venv/bin/activate
python3 fusion_run.py --camera 9 --y16 --thr 0.4
```

브라우저에서 `http://<보드IP>:8090`

- [ ] 열화상 영상이 보인다
- [ ] SAFE / LIED / FALL 상태가 바뀐다
- [ ] 넘어져보면 로그에 `열화상 낙상` 이 남는다

### 1-2. 진동센서 추가

```bash
python3 fusion_run.py --camera 9 --y16 --thr 0.4 \
  --impact-port /dev/ttyACM0
```

- [ ] 대시보드 "최근 충격" 이 갱신된다
- [ ] 바닥을 두드리면 로그에 `충격 N.NNg` 가 뜬다
- [ ] 신호 세기가 **-70dBm 이상**이다

### 1-3. SmartThings 연결

```bash
python3 fusion_run.py --camera 9 --y16 --thr 0.4 \
  --impact-port /dev/ttyACM0 \
  --bridge $(hostname -I | awk '{print $1}'):8088 \
  --device falldetect
```

- [ ] 낙상 시 `SmartThings 전송 성공` 이 뜬다
- [ ] 휴대폰에 알림이 온다

> `edgebridge 거부` 가 뜨면 앱의 `LAN App/Device Address` 가 지금 보드 IP 와
> 같은지 확인하세요. 보드 IP 가 바뀌었으면 앱 설정도 고쳐야 합니다.

---

## 2. 융합 동작 확인

### 충격 필수 모드

```bash
python3 fusion_run.py ... --require-impact
```

이 모드에서는 **열화상이 낙상이라고 해도 최근 충격이 없으면 알람을 보내지 않습니다.**

- [ ] 진동 노드를 멀리 치우고 넘어져본다 → `보류 (--require-impact)` 가 뜬다
- [ ] 노드 근처에서 넘어져본다 → 알람이 나간다

오탐을 크게 줄이지만 **놓칠 위험도 커집니다.** 카펫 위나 노드에서 먼 곳의
낙상은 충격이 약해 감지되지 않을 수 있어요.

### 융합 창

```bash
--fusion-window 10      # 기본값
```

열화상 낙상 판정 시점 기준으로 이 시간 안의 충격을 같은 사건으로 봅니다.
`--fall-hold`(기본 3초)를 감안하면 실제 충격은 판정보다 3~5초 앞서므로,
10초면 충분하고 너무 길면 무관한 충격을 엮게 됩니다.

### 쿨다운

```bash
--alarm-cooldown 180    # 기본 3분
```

---

## 3. 검증 데이터 수집

**이게 이번 점검의 핵심입니다.** 각 동작을 **10회씩** 반복하고 기록하세요.

| 동작 | 기대 | 감지됨 | 비고 |
|---|---|---|---|
| 앞으로 넘어지기 | 알람 | ___/10 | |
| 옆으로 넘어지기 | 알람 | ___/10 | |
| 뒤로 넘어지기 | 알람 | ___/10 | |
| 천천히 눕기 | 알람 없음 | ___/10 | LIED 표시만 |
| 바닥에 앉기 | 알람 없음 | ___/10 | |
| 물건 줍기 | 알람 없음 | ___/10 | |
| 물건 떨어뜨리기 | 알람 없음 | ___/10 | 충격만 기록 |
| 그냥 걷기 | 무반응 | ___/10 | |

**재현율**(넘어짐을 잡는 비율)과 **오탐**(정상 활동에서 울린 횟수)을 나눠 세세요.
낙상 감지에서는 **놓치는 쪽이 훨씬 위험**하므로, 오탐이 조금 늘더라도
재현율을 우선하는 게 맞습니다.

### 임계값 조정

| 증상 | 대응 |
|---|---|
| 낙상을 놓친다 | `--thr` 을 낮춘다 (0.4 → 0.3) |
| 오탐이 많다 | `--thr` 을 올린다, 또는 `--require-impact` |
| 눕기를 낙상으로 본다 | `--fall-hold` 를 늘린다, `--lie-aspect` 조정 |
| 알람이 너무 늦다 | `--fall-hold` 를 줄인다 (3 → 2) |

`--thr 0.4` 는 새 모델의 검증 최적값입니다(AP 0.974, Recall 0.953).
다만 학습 데이터가 FLIR One 고해상도라 Lepton 에서는 재조정이 필요할 수 있습니다.

---

## 4. 상시 가동 등록

검증이 끝난 뒤에만 하세요. 튜닝 중에는 수동 실행이 편합니다.

```bash
sudo tee /etc/systemd/system/fall.service > /dev/null <<EOF
[Unit]
Description=Fall Detection (thermal + impact + SmartThings)
After=network-online.target edgebridge.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/fall-detection
ExecStartPre=/bin/sleep 20
ExecStart=$HOME/fall-detection/.venv/bin/python fusion_run.py \\
  --camera 9 --y16 --thr 0.4 \\
  --impact-port /dev/ttyACM0 \\
  --bridge $(hostname -I | awk '{print $1}'):8088 --device falldetect
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now fall.service
journalctl -u fall.service -f
```

> 카메라와 시리얼 장치 번호가 재부팅 시 바뀔 수 있습니다. 자주 바뀌면
> udev 규칙으로 고정 심볼릭 링크를 만드세요.

---

## 5. 문제 해결

| 증상 | 원인과 대응 |
|---|---|
| 프레임 없음 / `Killed` | `--y16` 유무 확인. 인덱스는 `v4l2-ctl --list-devices` |
| `select() timeout` | PureThermal 재연결. 다른 프로세스가 카메라 점유 중인지 `fuser /dev/video9` |
| 진동 수신 안 됨 | 수신기 USB, 두 XIAO 의 **외장 안테나**, 채널 일치 |
| `edgebridge 거부` | 보드 LAN IP 와 앱 설정 불일치 |
| `No existing registrations` | edgebridge 재시작으로 등록 유실 → 앱에서 기기 재생성 |
| 알림 안 옴 (기기는 켜짐) | 앱 루틴 미생성. If: 눌림(Pushed) / Then: 알림 |
| 추론이 느림 | `--stride 2` |
| 대시보드 안 열림 | 포트 8090, 보드 IP 확인. `--web-port` 로 변경 가능 |

---

## 6. 이 시스템의 한계 — 반드시 인지할 것

**검증 지표는 모델 학습 데이터 기준입니다.** AP 0.974 는 FLIR One 고해상도
데이터셋에서의 수치이고, Lepton 160×120 + 실제 방 환경에서의 성능은
**여러분이 3장에서 직접 측정한 값**이 진실입니다.

**사각지대가 있습니다.** 카메라 화각 밖, 가구에 가려진 위치, 화장실 등은
감지되지 않습니다. 진동센서도 노드에서 멀면 충격이 약해집니다.

**보조 수단입니다.** 실제 고령자 보호에 쓰신다면 응급 호출 버튼 같은
수동 수단을 반드시 병행하세요. 이 시스템만 믿으면 안 됩니다.
