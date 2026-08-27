#!/usr/bin/env python3
"""
최종 통합 실행 — 열화상 AI + 무선 진동센서 + SmartThings 알림

lepton_live.py(experiment-bbox 브랜치)를 그대로 구동하되,
 · HUD 화면을 가로채 브라우저 대시보드로 내보내고
 · 웹훅 호출을 가로채 진동센서 신호와 융합한 뒤 SmartThings 로 보낸다.

친구분 저장소의 파일은 한 줄도 수정하지 않는다.

  lepton_live.py 와 같은 폴더에서 실행할 것.

사용법:

  # 열화상만 (진동센서 없이)
  python3 fusion_run.py --camera 9 --y16

  # 진동센서까지 (수신기 XIAO 가 USB 로 꽂혀 있어야 함)
  python3 fusion_run.py --camera 9 --y16 --impact-port /dev/ttyACM0

  # SmartThings 알림까지
  python3 fusion_run.py --camera 9 --y16 --impact-port /dev/ttyACM0 \
      --bridge 172.30.1.33:8088 --device falldetect

  # 백엔드 서버는 기본으로 켜져 있다 (cherry-fall.duckdns.org).
  # 다른 주소로 바꾸려면 --webhook <URL>, 끄려면 --webhook none

  # 백엔드가 device_id 로 보드 MAC 을 요구하면 auto 로 자동 입력
  python3 fusion_run.py ... --device-id auto          # 예: 1C:DB:D4:F0:DE:44
  python3 fusion_run.py ... --device-id auto:lower    # 소문자
  python3 fusion_run.py ... --device-id auto:plain    # 콜론 없이
  python3 fusion_run.py ... --device-id auto:eth0     # 인터페이스 지정

  # 진동 충격이 있어야만 알람 (오탐 최소화)
  python3 fusion_run.py ... --require-impact

  → 대시보드: http://<보드IP>:8090

lepton_live.py 의 옵션을 그대로 넘길 수 있다. 최신 bbox 브랜치 권장 조합:

  python3 fusion_run.py --camera 9 --y16 \
      --collapse-trigger --thr 0.35 --lie-hyst 0.3 --fall-min-hold 5 \
      --impact-port /dev/ttyACM0 --bridge <보드IP>:8088 --device falldetect
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

# ──────────────────────────────────────────────────────────── 상태

class State:
    """모든 스레드가 공유하는 상태. 락으로 보호한다."""

    def __init__(self, fusion_window: float, require_impact: bool,
                 cooldown: float, impact_min_g: float = 1.5):
        self.lock = threading.Lock()
        self.jpeg: bytes | None = None
        self.frame_ver = 0

        self.fusion_window = fusion_window
        self.require_impact = require_impact
        self.cooldown = cooldown
        self.impact_min_g = impact_min_g
        self.jpeg_quality = 80

        self.last_impact_t = 0.0
        self.last_impact_g = 0.0
        self.impact_count = 0
        self.impact_rssi = 0
        self.impact_batt = 0
        self.last_seen_t = 0.0     # 하트비트 포함 마지막 수신 (생존 확인용)

        self.thermal_events = 0      # DANGER EVENT 횟수
        self.heartbeats = 0
        self.last_event_type = "—"   # SAFE | WARNING | DANGER
        self.last_report = "—"
        self.thermal_health = "—"
        self.alarms = 0              # 실제로 SmartThings 로 보낸 횟수
        self.suppressed = 0
        self.last_alarm_t = -1e9
        self.log: deque = deque(maxlen=40)

        self.bridge_ok: bool | None = None
        self.backend_url: str | None = None
        self.backend_ok: bool | None = None
        self.backend_sent = 0
        self.backend_fail = 0
        self.seq = 100000            # 수동 테스트용 seq (실 감지와 겹치지 않게)
        self.started = time.time()

    def put_frame(self, img) -> None:
        ok, buf = cv2.imencode(".jpg", img,
                               [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()
                self.frame_ver += 1

    def get_frame(self, since: int = -1) -> tuple[bytes | None, int]:
        """since 이후의 새 프레임만 돌려준다.

        Lepton 은 9fps 인데 예전에는 스트림이 20fps 로 같은 프레임을 계속
        재전송했다. 대역폭만 두 배로 쓰고 화면은 나아지지 않는다.
        """
        with self.lock:
            if self.frame_ver == since:
                return None, since
            return self.jpeg, self.frame_ver

    def note(self, kind: str, msg: str) -> None:
        entry = {"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": msg}
        with self.lock:
            self.log.appendleft(entry)
        print(f"  [{entry['t']}] {msg}", flush=True)

    def impact_age(self) -> float:
        return time.time() - self.last_impact_t if self.last_impact_t else 1e9

    def snapshot(self) -> dict:
        with self.lock:
            log = list(self.log)
        age = self.impact_age()
        return {
            "uptime": int(time.time() - self.started),
            "impact_count": self.impact_count,
            "impact_age": None if age > 1e8 else round(age, 1),
            "impact_g": round(self.last_impact_g, 2),
            "impact_rssi": self.impact_rssi,
            "impact_min_g": self.impact_min_g,
            "impact_fresh": age <= self.fusion_window,
            "thermal_events": self.thermal_events,
            "heartbeats": self.heartbeats,
            "event_type": self.last_event_type,
            "thermal_health": self.thermal_health,
            "alarms": self.alarms,
            "suppressed": self.suppressed,
            "require_impact": self.require_impact,
            "fusion_window": self.fusion_window,
            "bridge_ok": self.bridge_ok,
            "backend_url": self.backend_url,
            "backend_ok": self.backend_ok,
            "backend_sent": self.backend_sent,
            "backend_fail": self.backend_fail,
            "log": log,
        }


STATE: State | None = None

# 백엔드 서버 기본 주소. --webhook 로 덮어쓸 수 있고,
# --webhook none 을 주면 백엔드 전송을 끈다.
DEFAULT_BACKEND = "https://cherry-fall.duckdns.org/api/device/data"


# ──────────────────────────────────────────────────────────── 진동센서

IMPACT_RE = None   # impact_test 에서 가져온다


def impact_reader(port: str, baud: int) -> None:
    """수신기 XIAO 의 시리얼을 읽어 충격 이벤트를 STATE 에 반영한다."""
    try:
        import serial
    except ImportError:
        STATE.note("error", "pyserial 없음 — 진동센서 비활성 (pip install pyserial)")
        return

    while True:
        try:
            ser = serial.Serial(port, baud, timeout=1)
        except OSError as exc:
            STATE.note("error", f"진동 수신기 {port} 열기 실패: {exc}")
            time.sleep(5)
            continue

        STATE.note("info", f"진동 수신기 연결: {port}")
        try:
            for imp in _read_impacts(ser):
                # 노드의 주기 하트비트는 중력(약 1.0g)만 싣고 온다.
                # 이걸 충격으로 세면 항상 "최근 충격 있음" 이 되어 융합이 무의미해진다.
                if imp.peak_g < STATE.impact_min_g:
                    with STATE.lock:
                        STATE.impact_rssi = imp.rssi
                        STATE.last_seen_t = imp.t
                    continue
                with STATE.lock:
                    STATE.last_impact_t = imp.t
                    STATE.last_impact_g = imp.peak_g
                    STATE.impact_count += 1
                    STATE.impact_rssi = imp.rssi
                    STATE.impact_batt = imp.battery_mv // 30 if imp.battery_mv else 0
                STATE.note("impact",
                           f"충격 {imp.peak_g:.2f}g  rssi={imp.rssi}dBm  ({imp.device})")
        except Exception as exc:
            STATE.note("error", f"진동 수신 중단: {exc}")
        finally:
            try:
                ser.close()
            except Exception:
                pass
        time.sleep(2)   # 재연결


def _read_impacts(ser):
    from impact_test import read_impacts
    return read_impacts(ser, echo_comments=False)


# ──────────────────────────────────────────────────────────── 융합 판정

def send_to_bridge(bridge: str, device: str) -> bool:
    import urllib.request
    url = f"http://{bridge}/{device}/trigger"
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=4) as r:
            body = (r.read(200) or b"").decode("utf-8", "replace")
            if "unregistered" in body.lower() or "invalid endpoint" in body.lower():
                STATE.note("error",
                           "edgebridge 거부 — 앱의 LAN App/Device Address 가 "
                           "이 보드 IP 와 같은지, 기기 이름이 정확한지 확인하세요")
                return False
            return 200 <= r.status < 300
    except Exception as exc:
        STATE.note("error", f"edgebridge 전송 실패: {exc}")
        return False


def send_to_backend(payload: dict, retries: int = 0, backoff: float = 1.0) -> None:
    """친구분 스키마 그대로 실제 백엔드 서버로 전송한다.

    lepton_live 의 원본 post_json 을 그대로 쓰므로 재시도·백오프 동작이 같다.
    (원본은 백그라운드 스레드에서 보내므로 여기서 블로킹되지 않는다.)
    """
    url = STATE.backend_url
    if not url:
        return
    fn = getattr(STATE, "_orig_post_json", None)
    if fn is None:
        return
    with STATE.lock:
        STATE.backend_sent += 1
    try:
        fn(url, payload, retries=retries, backoff=backoff)
        with STATE.lock:
            STATE.backend_ok = True
    except Exception as exc:
        with STATE.lock:
            STATE.backend_fail += 1
            STATE.backend_ok = False
        STATE.note("error", f"백엔드 전송 실패: {exc}")


def ping_bridge(bridge: str) -> tuple[bool, str]:
    """edgebridge 가 살아 있는지만 확인한다. 기기를 트리거하지 않는다."""
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(f"http://{bridge}/", timeout=3) as r:
            return True, f"응답 {r.status}"
    except urllib.error.HTTPError as e:
        # 루트 경로는 404 를 주는 게 정상 — 서버가 살아 있다는 뜻
        return True, f"응답 {e.code} (정상)"
    except Exception as exc:
        return False, str(exc)


def build_test_payload(device_id: str) -> dict | None:
    """실제 감지와 동일한 형식의 DANGER 페이로드를 만든다."""
    builder = getattr(STATE, "_build_payload", None)
    if builder is None:
        return None
    now = time.time()
    with STATE.lock:
        STATE.seq += 1
        seq = STATE.seq
    return builder(
        device_id=device_id, seq=seq, now=now,
        report_type="EVENT", event_type="DANGER",
        sensor_health={"vibrator": "UNKNOWN", "radar": "UNKNOWN", "thermal": "OK"},
        battery_pct=100, rssi=-55, uptime_sec=int(now - STATE.started),
    )


def manual_test(bridge: str | None, device: str, device_id: str) -> dict:
    """대시보드의 테스트 버튼.

    감지와 무관하게 (1) SmartThings 트리거와 (2) 백엔드 DANGER JSON 을
    실제 낙상과 똑같이 한 번 내보낸다.
    """
    parts, ok_any = [], False

    # 1) SmartThings
    if bridge:
        ok = send_to_bridge(bridge, device)
        with STATE.lock:
            STATE.bridge_ok = ok
            if ok:
                STATE.alarms += 1
                STATE.last_alarm_t = time.time()
        parts.append("SmartThings " + ("성공" if ok else "실패"))
        ok_any = ok_any or ok
    else:
        parts.append("SmartThings 미설정")

    # 2) 백엔드 서버 (친구분 스키마 그대로)
    if STATE.backend_url:
        payload = build_test_payload(device_id)
        if payload is None:
            parts.append("백엔드 페이로드 생성 실패")
        else:
            send_to_backend(payload, retries=2, backoff=1.0)
            parts.append(f"백엔드 DANGER 전송 (seq={payload.get('seq')})")
            ok_any = True
    else:
        parts.append("백엔드 미설정")

    msg = " · ".join(parts)
    STATE.note("alarm" if ok_any else "error", f"수동 테스트 — {msg}")
    return {"ok": ok_any, "msg": msg}


def on_webhook(payload: dict, bridge: str | None, device: str,
               retries: int = 0, backoff: float = 1.0) -> None:
    """lepton_live 의 백엔드 전송을 가로챈다.

    experiment-bbox 최신 버전은 이 경로로 두 가지를 보낸다:
      · HEARTBEAT — 5초 주기 생존 신고 (event_type = SAFE/WARNING/DANGER)
      · EVENT     — 사람 상태가 바뀐 순간 (DANGER = 낙상)

    알람은 EVENT + DANGER 일 때만 낸다. 하트비트는 대시보드 표시에만 쓴다.
    """
    rtype = payload.get("report_type", "?")
    etype = payload.get("event_type", "?")
    health = payload.get("sensor_health", {})

    with STATE.lock:
        STATE.last_event_type = etype
        STATE.last_report = rtype
        STATE.thermal_health = health.get("thermal", "?")
        STATE.heartbeats += (1 if rtype == "HEARTBEAT" else 0)

    # 백엔드가 지정돼 있으면 하트비트·이벤트를 그대로 중계한다.
    # 친구분 스키마와 재시도 정책을 손대지 않는다.
    send_to_backend(payload, retries=retries, backoff=backoff)

    if rtype == "EVENT":
        STATE.note("info", f"상태 변화 → {etype}"
                   + (f"  → 백엔드 전송" if STATE.backend_url else ""))

    # 낙상(DANGER) 진입 순간에만 융합 판정
    if not (rtype == "EVENT" and etype == "DANGER"):
        return

    now = time.time()
    age = STATE.impact_age()
    fresh = age <= STATE.fusion_window

    with STATE.lock:
        STATE.thermal_events += 1

    detail = f"열화상 DANGER (seq={payload.get('seq', '?')})"

    if fresh:
        detail += f" + 충격 {STATE.last_impact_g:.2f}g ({age:.1f}초 전)"
    else:
        detail += "  충격 없음"

    # 충격 필수 모드에서 충격이 없으면 보류
    if STATE.require_impact and not fresh:
        with STATE.lock:
            STATE.suppressed += 1
        STATE.note("hold", f"{detail} → 보류 (--require-impact)")
        return

    # 쿨다운
    if now - STATE.last_alarm_t < STATE.cooldown:
        with STATE.lock:
            STATE.suppressed += 1
        remain = STATE.cooldown - (now - STATE.last_alarm_t)
        STATE.note("hold", f"{detail} → 억제 (쿨다운 {remain:.0f}초 남음)")
        return

    with STATE.lock:
        STATE.last_alarm_t = now
        STATE.alarms += 1

    conf = "높음" if fresh else "보통"
    STATE.note("alarm", f"★ 낙상 알람 [{conf}]  {detail}")

    if bridge:
        ok = send_to_bridge(bridge, device)
        with STATE.lock:
            STATE.bridge_ok = ok
        STATE.note("alarm" if ok else "error",
                   "SmartThings 전송 성공" if ok else "SmartThings 전송 실패")


# ──────────────────────────────────────────────────────────── 대시보드

_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>낙상 감지 통합</title><style>
:root{--bg:#141317;--panel:#1d1c21;--line:#2e2c33;--ink:#e8e6e1;--dim:#8a867f;
      --ok:#7fb08c;--warn:#d9a05b;--bad:#d97066;--acc:#7aa9cc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;padding:16px}
.wrap{max-width:1000px;margin:0 auto;display:grid;grid-template-columns:1fr 320px;gap:16px}
@media(max-width:820px){.wrap{grid-template-columns:1fr}}
img{width:100%;border-radius:10px;background:#000;image-rendering:pixelated}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
h2{font-size:13px;margin:0 0 10px;color:var(--dim);text-transform:uppercase;
   letter-spacing:.06em;font-weight:600}
.row{display:flex;justify-content:space-between;padding:5px 0;font-size:14px;
     border-bottom:1px solid var(--line)}
.row:last-child{border:0}
.row b{font-variant-numeric:tabular-nums;font-weight:600}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
.p-ok{background:rgba(127,176,140,.18);color:var(--ok)}
.p-warn{background:rgba(217,160,91,.18);color:var(--warn)}
.p-bad{background:rgba(217,112,102,.18);color:var(--bad)}
.p-dim{background:rgba(138,134,127,.15);color:var(--dim)}
ul{list-style:none;padding:0;margin:0;max-height:340px;overflow-y:auto;font-size:13px}
li{padding:6px 0;border-bottom:1px solid var(--line);display:flex;gap:8px}
li span.t{color:var(--dim);font-variant-numeric:tabular-nums;flex-shrink:0}
li.alarm{color:var(--bad);font-weight:600}
li.impact{color:var(--acc)}
li.hold{color:var(--warn)}
li.error{color:var(--bad)}
li.info{color:var(--dim)}
.btn{flex:1;padding:9px 10px;border-radius:7px;border:1px solid var(--line);
     background:#26242b;color:var(--ink);font-size:13px;font-weight:600;cursor:pointer;
     font-family:inherit}
.btn:hover{background:#302e37}
.btn:active{transform:translateY(1px)}
.btn-primary{background:rgba(217,112,102,.16);border-color:rgba(217,112,102,.4);color:var(--bad)}
.btn-primary:hover{background:rgba(217,112,102,.26)}
.btn:disabled{opacity:.5;cursor:default}
</style></head><body>
<div class="wrap">
  <div>
    <img src="/stream" alt="thermal">
    <p style="font-size:12.5px;color:var(--dim);margin:10px 2px">
      SAFE 정상 · LIED 누움 · FALL 낙상 &nbsp;|&nbsp; 하단 막대 = EMA 확률,
      상자 = 자세 판정 영역
    </p>
  </div>
  <div style="display:flex;flex-direction:column;gap:16px">
    <div class="card">
      <h2>상태</h2>
      <div id="stat"></div>
    </div>
    <div class="card">
      <h2>SmartThings 점검</h2>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <button id="ping" class="btn">연결 확인</button>
        <button id="test" class="btn btn-primary">테스트 알림</button>
      </div>
      <p id="tres" style="font-size:12.5px;color:var(--dim);margin:0;min-height:18px"></p>
    </div>
    <div class="card">
      <h2>이벤트</h2>
      <ul id="log"></ul>
    </div>
  </div>
</div>
<script>
function row(k,v){return '<div class="row"><span>'+k+'</span><b>'+v+'</b></div>'}
function pill(t,c){return '<span class="pill '+c+'">'+t+'</span>'}
async function tick(){
  try{
    const s = await (await fetch('/status')).json();
    let imp;
    if(s.impact_age===null) imp = pill('없음','p-dim');
    else if(s.impact_fresh) imp = pill(s.impact_g+'g · '+s.impact_age+'초 전','p-ok');
    else imp = pill(s.impact_age+'초 전','p-dim');

    let br;
    if(s.bridge_ok===null) br = pill('미전송','p-dim');
    else br = s.bridge_ok ? pill('정상','p-ok') : pill('실패','p-bad');

    let bk;
    if(!s.backend_url) bk = pill('미설정','p-dim');
    else if(s.backend_fail) bk = pill(s.backend_sent+'건 · 실패 '+s.backend_fail,'p-warn');
    else bk = pill(s.backend_sent+'건 전송','p-ok');

    document.getElementById('stat').innerHTML =
      row('최근 충격', imp) +
      row('충격 누적', s.impact_count + ' 회') +
      row('신호 세기', s.impact_rssi ? s.impact_rssi+' dBm' : '—') +
      row('현재 상태', s.event_type) +
      row('열화상 상태', s.thermal_health) +
      row('열화상 낙상', s.thermal_events + ' 회') +
      row('하트비트', s.heartbeats + ' 회') +
      row('알람 발송', s.alarms + ' 회') +
      row('억제/보류', s.suppressed + ' 회') +
      row('SmartThings', br) +
      row('백엔드', bk) +
      row('융합 창', s.fusion_window + ' 초') +
      row('충격 필수', s.require_impact ? pill('ON','p-warn') : pill('OFF','p-dim')) +
      row('가동 시간', s.uptime + ' 초');

    document.getElementById('log').innerHTML = s.log.map(e =>
      '<li class="'+e.kind+'"><span class="t">'+e.t+'</span><span>'+e.msg+'</span></li>'
    ).join('') || '<li class="info">아직 이벤트 없음</li>';
  }catch(e){}
}
async function post(path, btn, label){
  const el = document.getElementById('tres');
  btn.disabled = true; el.textContent = label + ' 중...';
  try{
    const r = await (await fetch(path, {method:'POST'})).json();
    el.textContent = (r.ok ? '✓ ' : '✗ ') + label + ': ' + r.msg;
    el.style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
  }catch(e){
    el.textContent = '✗ 요청 실패'; el.style.color = 'var(--bad)';
  }
  btn.disabled = false; tick();
}
document.getElementById('ping').onclick = e => post('/ping', e.target, '연결 확인');
document.getElementById('test').onclick = e => post('/test', e.target, '테스트 알림');
setInterval(tick, 1000); tick();
</script></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            body = json.dumps(STATE.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path != "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
        self.end_headers()
        ver = -1
        try:
            while True:
                j, ver = STATE.get_frame(ver)
                if j is None:
                    time.sleep(0.02)      # 새 프레임 대기
                    continue
                self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(j)}\r\n\r\n".encode())
                self.wfile.write(j)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        cfg = getattr(self.server, "cfg", {})
        if self.path == "/test":
            res = manual_test(cfg.get("bridge"), cfg.get("device", "falldetect"),
                              cfg.get("device_id", "pi_node_01"))
        elif self.path == "/ping":
            b = cfg.get("bridge")
            if not b:
                res = {"ok": False, "msg": "--bridge 미지정"}
            else:
                ok, msg = ping_bridge(b)
                with STATE.lock:
                    STATE.bridge_ok = ok
                STATE.note("info" if ok else "error", f"edgebridge 연결 확인: {msg}")
                res = {"ok": ok, "msg": msg}
        else:
            self.send_error(404)
            return

        body = json.dumps(res).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# ──────────────────────────────────────────────────────────── 카메라

def fix_frame(frame):
    """PureThermal Y16 이 (1, N) 평면 버퍼로 올 때 (120,160) 으로 되돌린다."""
    a = np.asarray(frame)
    if a.ndim >= 2 and a.shape[0] not in (0, 1):
        return a[:120] if a.shape[0] == 122 else a
    flat = a.reshape(-1)
    if flat.dtype == np.uint8 and flat.size in (160 * 120 * 2, 160 * 122 * 2):
        flat = flat.view(np.uint16)
    n = flat.size
    for h, w in ((120, 160), (122, 160)):
        if n == h * w:
            img = flat.reshape(h, w)
            return img[:120] if h == 122 else img
    for h, w, c in ((120, 160, 3), (122, 160, 3)):
        if n == h * w * c:
            img = flat.reshape(h, w, c)
            return img[:120] if h == 122 else img
    return a


def lepton_frames(idx, y16, reconnect_wait=3.0):
    """lepton_live.camera_source 대체.

    최신 bbox 브랜치는 (idx, y16, reconnect_wait) 3인자로 부른다.
    카메라가 빠지면 reconnect_wait 초 간격으로 재연결을 시도한다.
    """
    while True:
        try:
            yield from _one_session(idx, y16)
        except RuntimeError as exc:
            STATE.note("error", f"{exc}")
        STATE.note("info", f"{reconnect_wait:.0f}초 후 카메라 재연결 시도")
        time.sleep(max(reconnect_wait, 1.0))


def _one_session(idx, y16):
    cap = cv2.VideoCapture(int(idx), cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"/dev/video{idx} 를 열 수 없습니다")
    if y16:
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"Y16 "))
    else:
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)

    deadline = time.time() + 3.0
    first = None
    while time.time() < deadline:
        ok, fr = cap.read()
        if ok and fr is not None:
            first = fr
            break
        time.sleep(0.05)
    if first is None:
        cap.release()
        raise RuntimeError(
            f"/dev/video{idx} 에서 프레임을 못 받았습니다.\n"
            "  PureThermal 을 다시 꽂거나 --y16 유무를 바꿔보세요.")
    print(f"  카메라 열림: /dev/video{idx}  frame={np.asarray(first).shape}")

    fails = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                fails += 1
                if fails > 200:
                    STATE.note("error", "카메라 프레임 중단")
                    return
                time.sleep(0.02)
                continue
            fails = 0
            yield frame
    finally:
        cap.release()


# ──────────────────────────────────────────────────────────── 메인

def read_mac(iface: str) -> str | None:
    """인터페이스의 MAC 주소를 읽는다."""
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            m = f.read().strip()
        return m if m and m != "00:00:00:00:00:00" else None
    except OSError:
        return None


def resolve_device_id(value: str, iface_pref: str = "") -> str:
    """--device-id 가 auto/mac 이면 보드 MAC 으로 치환한다.

    백엔드가 device_id 로 보드 MAC 을 요구하는 경우, 손으로 적다가
    한 글자만 틀려도 매칭이 안 된다. 자동으로 읽어 넣는 편이 안전하다.

    형식 지정:
      auto        기본 인터페이스(현재 통신 중)의 MAC, 대문자 콜론 구분
      auto:eth0   특정 인터페이스 지정
      auto:lower  소문자
      auto:plain  콜론 없이
    """
    if not value or value.split(":")[0].lower() not in ("auto", "mac"):
        return value

    opts = [o.lower() for o in value.split(":")[1:]]
    lower = "lower" in opts
    plain = "plain" in opts or "nocolon" in opts
    named = [o for o in opts if o not in ("lower", "plain", "nocolon")]

    order = ([named[0]] if named else
             ([iface_pref] if iface_pref else []) + ["wlan0", "eth0", "end0"])
    mac = None
    for i in order:
        mac = read_mac(i)
        if mac:
            break
    if not mac:
        print("  ⚠ MAC 을 읽지 못했습니다. --device-id 를 직접 지정하세요.")
        return value

    mac = mac.lower() if lower else mac.upper()
    if plain:
        mac = mac.replace(":", "")
    return mac


def lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "<보드IP>"
    finally:
        s.close()


def main() -> int:
    global STATE

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--web-port", type=int, default=8090)
    pre.add_argument("--impact-port", default=None,
                     help="진동 수신기 시리얼 (예: /dev/ttyACM0). 생략하면 열화상만")
    pre.add_argument("--impact-baud", type=int, default=115200)
    pre.add_argument("--bridge", default=None,
                     help="edgebridge 주소 IP:PORT (예: 172.30.1.33:8088)")
    pre.add_argument("--device", default="falldetect",
                     help="SmartThings LAN Device Name")
    pre.add_argument("--fusion-window", type=float, default=10.0,
                     help="열화상 낙상 전후 이 시간 안의 충격을 같은 사건으로 본다(초)")
    pre.add_argument("--require-impact", action="store_true",
                     help="충격이 있어야만 알람 (오탐 최소화, 놓칠 위험 증가)")
    pre.add_argument("--alarm-cooldown", type=float, default=180.0)
    pre.add_argument("--jpeg-quality", type=int, default=80,
                     help="스트림 JPEG 품질 1~100. 낮추면 대역폭이 줄어 끊김이 개선된다")
    pre.add_argument("--impact-min-g", type=float, default=1.5,
                     help="이 값 미만은 충격으로 세지 않는다. 노드의 주기 하트비트는 "
                          "중력(약 1.0g)만 실어 오므로 걸러내야 한다")
    known, rest = pre.parse_known_args()

    if "--display" not in rest:
        rest.append("--display")

    # --webhook <URL> 이 주어지면 그 주소를 실제 백엔드로 본다.
    # lepton_live 에게는 내부 표식을 넘겨 post_json 이 호출되게만 하고,
    # 실제 전송은 fusion_run 이 원본 post_json 으로 대신 수행한다.
    backend_url = DEFAULT_BACKEND
    if "--webhook" in rest:
        i = rest.index("--webhook")
        if i + 1 < len(rest):
            v = rest[i + 1]
            if v.lower() in ("none", "off", ""):
                backend_url = None
            elif not v.startswith("fusion://"):
                backend_url = v
            rest[i + 1] = "fusion://internal"
    else:
        rest += ["--webhook", "fusion://internal"]

    # device-id 는 lepton_live 로도 넘기고 우리도 알아야 한다 (테스트 페이로드용)
    device_id = "pi_node_01"
    if "--device-id" in rest:
        j = rest.index("--device-id")
        if j + 1 < len(rest):
            device_id = resolve_device_id(rest[j + 1])
            rest[j + 1] = device_id      # lepton_live 에도 치환된 값을 넘긴다

    STATE = State(known.fusion_window, known.require_impact,
                  known.alarm_cooldown, known.impact_min_g)
    STATE.backend_url = backend_url
    STATE.jpeg_quality = max(1, min(100, known.jpeg_quality))

    # GUI 호출을 대시보드로 돌린다
    cv2.imshow = lambda name, img: STATE.put_frame(img)
    cv2.waitKey = lambda delay=0: -1
    cv2.destroyAllWindows = lambda: None
    cv2.namedWindow = lambda *a, **k: None

    # ThreadingHTTPServer 여야 한다. 단일 스레드면 MJPEG 스트림이 스레드를
    # 영원히 붙잡아 /status 폴링과 버튼(POST)이 처리되지 않는다.
    server = ThreadingHTTPServer(("0.0.0.0", known.web_port), Handler)
    server.daemon_threads = True
    server.cfg = {"bridge": known.bridge, "device": known.device,
                  "device_id": device_id}
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print()
    print(f"  대시보드:  http://{lan_ip()}:{known.web_port}")
    print(f"  진동센서:  {known.impact_port or '없음 (열화상만)'}")
    print(f"  SmartThings: {known.bridge + '/' + known.device if known.bridge else '없음'}")
    print(f"  백엔드 서버: {backend_url or '없음 (--webhook none)'}")
    print(f"  device_id:   {device_id}")
    print(f"  융합 창: {known.fusion_window}초"
          f"{'  · 충격 필수' if known.require_impact else ''}")
    print(f"  종료: Ctrl+C")
    print()

    if known.impact_port:
        threading.Thread(target=impact_reader,
                         args=(known.impact_port, known.impact_baud),
                         daemon=True).start()

    sys.argv = ["lepton_live.py"] + rest
    try:
        import lepton_live
    except ImportError:
        print("오류: lepton_live.py 와 같은 폴더에서 실행하세요.")
        return 1

    # 프레임 형태 교정
    _orig_ftg = lepton_live.frame_to_gray
    lepton_live.frame_to_gray = lambda frame, y16: _orig_ftg(fix_frame(frame), y16)

    # 카메라 열기 교체 (PureThermal 다중 백엔드 탐색 회피)
    lepton_live.camera_source = lepton_frames

    # 웹훅 가로채기 — 여기가 융합 지점이다.
    # 최신 bbox 브랜치의 시그니처: (url, payload, timeout, retries, backoff)
    STATE._orig_post_json = lepton_live.post_json     # 실제 전송에 재사용
    lepton_live.post_json = (
        lambda url, payload, timeout=4, retries=0, backoff=1.0:
        on_webhook(payload, known.bridge, known.device, retries, backoff))

    # 진동센서 목업 자리에 실제 값을 채워 넣는다.
    # lepton_live 의 build_payload 는 --health-vibrator / --rssi / --battery-pct 를
    # 그대로 싣는데, 우리는 수신기에서 받은 진짜 값을 알고 있다.
    _orig_build = lepton_live.build_payload

    def _build(**kw):
        if known.impact_port:
            with STATE.lock:
                seen = STATE.last_seen_t
            fresh = (time.time() - seen) <= 60 if seen else False
            kw["sensor_health"] = dict(kw.get("sensor_health", {}))
            kw["sensor_health"]["vibrator"] = "OK" if fresh else "FAIL"
            with STATE.lock:
                if STATE.impact_rssi:
                    kw["rssi"] = STATE.impact_rssi
                if STATE.impact_batt:
                    kw["battery_pct"] = max(0, min(100, STATE.impact_batt))
        return _orig_build(**kw)

    lepton_live.build_payload = _build
    STATE._build_payload = _build                    # 테스트 버튼이 같은 형식을 쓰도록

    # --webhook 에 아무 값이나 넣어야 lepton_live 가 post_json 을 호출한다
    if "--webhook" not in rest:
        sys.argv += ["--webhook", "fusion://internal"]

    STATE.note("info", "감지 시작")
    try:
        lepton_live.main()
    except KeyboardInterrupt:
        pass
    finally:
        s = STATE.snapshot()
        print()
        print(f"  열화상 낙상 {s['thermal_events']}회 · 충격 {s['impact_count']}회 "
              f"· 알람 {s['alarms']}회 · 억제 {s['suppressed']}회 "
              f"· 백엔드 {s['backend_sent']}건(실패 {s['backend_fail']})")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
