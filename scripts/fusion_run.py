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
  python3 fusion_run.py --camera 8 --y16

  # 진동센서까지 (수신기 XIAO 가 USB 로 꽂혀 있어야 함)
  python3 fusion_run.py --camera 8 --y16 --impact-port /dev/ttyACM0

  # SmartThings 알림까지
  python3 fusion_run.py --camera 8 --y16 --impact-port /dev/ttyACM0 \
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

  python3 fusion_run.py --camera 8 --y16 --deghost \
      --thr 0.35 --lie-hyst 0.3 --lie-commit 2 --mac-iface wlan0 \
      --impact-port /dev/ttyACM0 --bridge <보드IP>:8088 --device falldetect

⚠ bbox 브랜치가 바뀌었다 (2026-09 확인):
  · --fall-min-hold 가 없어지고 --lie-commit (기본 2.0) 으로 대체됨
  · --mac-iface 추가 — MAC 을 device_id 로 쓴다. 이걸 쓰면 --device-id auto 불필요
  · --thr 기본값이 0.47 로 올라감
  · camera_source 시그니처가 (idx, y16, reconnect_wait, tick) 4인자로 늘어남
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
                 cooldown: float, impact_min_g: float = 0.25,
                 fusion_mode: str = "soft", escalate_g: float = 0.8,
                 no_impact_delay: float = 6.0):
        self.lock = threading.Lock()
        self.jpeg: bytes | None = None
        self.frame_ver = 0

        self.fusion_window = fusion_window
        self.require_impact = require_impact
        self.cooldown = cooldown
        self.impact_min_g = impact_min_g
        self.jpeg_quality = 80

        # 진동이 판정에 개입하는 방식
        #   off    — 참고용. 열화상 판정 그대로 (예전 기본값)
        #   soft   — 충격 있으면 승격/즉시, 없으면 지연 확인 (기본)
        #   strict — 충격 없으면 알람 안 냄 (--require-impact 와 동일)
        self.fusion_mode = "strict" if require_impact else fusion_mode
        self.escalate_g = escalate_g          # 이 이상이면 WARNING 도 낙상으로 승격
        self.no_impact_delay = no_impact_delay
        self.escalations = 0                  # 진동이 만들어낸 알람 수
        self.deferred = 0                     # 진동 없어서 지연된 건수
        self.pending: dict | None = None       # 확인 대기 중인 DANGER

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
        self.impacts: deque = deque(maxlen=12)   # 최근 충격 이력
        self.levels: deque = deque(maxlen=150)   # 실시간 진동 수준 (약 30초)
        self.batt_mv = 0
        self.batt_pct = 0
        self.batt_t = 0.0
        self.ffc_count = 0
        self.ffc_t = 0.0
        self.ffc_ctrl: str | None = None
        self.level_g = 0.0
        self.level_t = 0.0

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
            pending = dict(self.pending) if self.pending else None
        age = self.impact_age()
        return {
            "uptime": int(time.time() - self.started),
            "impact_count": self.impact_count,
            "impact_age": None if age > 1e8 else round(age, 1),
            "impact_g": round(self.last_impact_g, 2),
            "impact_rssi": self.impact_rssi,
            "impact_min_g": self.impact_min_g,
            "impact_fresh": age <= self.fusion_window,
            "impact_list": list(self.impacts),
            "level_series": list(self.levels),
            "level_g": round(self.level_g, 3),
            "deghost": (DEGHOST.status() if DEGHOST else None),
            "ffc_count": self.ffc_count,
            "ffc_age": (None if not self.ffc_t
                        else round(time.time() - self.ffc_t)),
            "level_live": bool(self.level_t and
                               time.time() - self.level_t < 3),
            "impact_batt": self.impact_batt,
            "batt_mv": self.batt_mv,
            "batt_pct": self.batt_pct,
            "batt_age": (None if not self.batt_t
                         else round(time.time() - self.batt_t)),
            "impact_link": (None if not self.last_seen_t
                            else round(time.time() - self.last_seen_t, 1)),
            "thermal_events": self.thermal_events,
            "heartbeats": self.heartbeats,
            "event_type": self.last_event_type,
            "thermal_health": self.thermal_health,
            "alarms": self.alarms,
            "suppressed": self.suppressed,
            "require_impact": self.require_impact,
            "fusion_mode": self.fusion_mode,
            "escalate_g": self.escalate_g,
            "escalations": self.escalations,
            "deferred": self.deferred,
            "no_impact_delay": self.no_impact_delay,
            "pending": (None if not pending else
                        {"reason": pending.get("reason", ""),
                         "remain": max(0.0, round(
                             pending["due"] - time.time(), 1))}),
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
                if imp is None:          # LEVEL 은 _read_impacts 안에서 처리된다
                    continue
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
                    if imp.battery_mv:
                        STATE.batt_mv = imp.battery_mv
                        STATE.batt_pct = batt_percent(imp.battery_mv)
                        STATE.batt_t = imp.t
                        STATE.impact_batt = STATE.batt_pct
                with STATE.lock:
                    STATE.impacts.appendleft({
                        "t": time.strftime("%H:%M:%S", time.localtime(imp.t)),
                        "g": round(imp.peak_g, 2),
                        "rssi": imp.rssi,
                        "dur": round(imp.duration_ms),
                    })
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


_re = __import__("re")
LEVEL_RE = _re.compile(
    r"LEVEL\s+device=(?P<dev>\S+)\s+peak=(?P<peak>[\d.]+)\s+rssi=(?P<rssi>-?\d+)")
BATT_RE = _re.compile(
    r"BATT\s+device=(?P<dev>\S+)\s+mv=(?P<mv>\d+)\s+rssi=(?P<rssi>-?\d+)")


# 리튬폴리머 방전 곡선. 전압-잔량은 선형이 아니다.
_BATT_CURVE = [(3300, 0), (3600, 5), (3700, 10), (3750, 20), (3790, 30),
               (3830, 40), (3870, 50), (3920, 60), (3970, 70), (4020, 80),
               (4100, 90), (4200, 100)]


def batt_percent(mv: int) -> int:
    if not mv:
        return 0
    if mv <= _BATT_CURVE[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(_BATT_CURVE, _BATT_CURVE[1:]):
        if mv < v1:
            return int(p0 + (mv - v0) / (v1 - v0) * (p1 - p0))
    return 100


def _read_impacts(ser):
    """수신기 시리얼을 읽는다.

    IMPACT 줄은 impact_test 의 파서로 넘기고, LEVEL(실시간 수준) 줄은
    여기서 바로 STATE 에 반영한 뒤 None 을 내보낸다. 두 줄이 같은 포트로
    섞여 오므로 한 곳에서 갈라야 한다.
    """
    from impact_test import LINE_RE, Impact

    while True:
        try:
            raw = ser.readline()
        except Exception as exc:
            STATE.note("error", f"시리얼 읽기 오류: {exc}")
            return
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith("#"):
            continue

        bt = BATT_RE.search(line)
        if bt:
            mv = int(bt.group("mv"))
            pct = batt_percent(mv)
            with STATE.lock:
                STATE.batt_mv = mv
                STATE.batt_pct = pct
                STATE.batt_t = time.time()
                STATE.last_seen_t = time.time()
                STATE.impact_rssi = int(bt.group("rssi"))
                if pct:
                    STATE.impact_batt = pct
            if mv == 0:
                STATE.note("info", "배터리 보고 — 분압 저항 미장착 (HAVE_BATT_SENSE 0)")
            else:
                STATE.note("info" if pct > 20 else "error",
                           f"배터리 {pct}% ({mv}mV)"
                           + ("  ⚠ 교체 필요" if pct <= 20 else ""))
            yield None
            continue

        lv = LEVEL_RE.search(line)
        if lv:
            g = float(lv.group("peak"))
            now = time.time()
            with STATE.lock:
                STATE.level_g = g
                STATE.level_t = now
                STATE.last_seen_t = now
                STATE.impact_rssi = int(lv.group("rssi"))
                STATE.levels.append(round(g, 3))
            yield None
            continue

        m = LINE_RE.search(line)
        if not m:
            continue
        yield Impact(
            device=m.group("dev"), seq=int(m.group("seq")),
            peak_g=float(m.group("peak")), duration_ms=float(m.group("dur")),
            rssi=int(m.group("rssi")), battery_mv=int(m.group("batt")),
            t=time.time(),
        )


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


# ──────────────────────────────────────────────────────────── FFC

def run_ffc(dev: str) -> tuple[bool, str]:
    """Lepton 셔터 보정(FFC)을 한 번 실행한다.

    PureThermal 은 FFC 를 V4L2 표준 컨트롤이 아니라 UVC 확장 유닛으로
    노출한다. 보드/펌웨어마다 이름이 달라서, 알려진 후보를 순서대로
    시도하고 되는 걸 쓴다.

    영상 스트림이 열려 있는 상태에서도 컨트롤 설정은 별개 경로라
    보통 문제없이 동작한다.
    """
    import subprocess

    candidates = ["flat_field_correction", "ffc", "run_ffc",
                  "lepton_ffc", "flat_field_correct"]
    tried = []
    for name in candidates:
        try:
            r = subprocess.run(["v4l2-ctl", "-d", dev, f"--set-ctrl={name}=1"],
                               capture_output=True, text=True, timeout=5)
        except FileNotFoundError:
            return False, "v4l2-ctl 이 없습니다 (apt install v4l-utils)"
        except subprocess.TimeoutExpired:
            return False, "v4l2-ctl 응답 없음"
        if r.returncode == 0 and "unknown" not in (r.stderr or "").lower():
            with STATE.lock:
                STATE.ffc_count += 1
                STATE.ffc_t = time.time()
                STATE.ffc_ctrl = name
            return True, f"FFC 실행 ({name})"
        tried.append(name)

    return False, ("이 장치에 FFC 컨트롤이 없습니다. "
                   f"`v4l2-ctl -d {dev} --list-ctrls` 로 실제 이름을 확인하세요 "
                   f"(시도: {', '.join(tried)})")


def ffc_timer(dev: str, interval: float) -> None:
    """주기적으로 FFC 를 실행한다. 0 이면 돌지 않는다."""
    while True:
        time.sleep(interval)
        ok, msg = run_ffc(dev)
        STATE.note("info" if ok else "error", f"주기 FFC — {msg}")


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

    if rtype != "EVENT":
        return

    # 사람이 다시 일어났다 → 대기 중이던 판정은 취소한다.
    if etype == "SAFE":
        with STATE.lock:
            had = STATE.pending is not None
            STATE.pending = None
        if had:
            STATE.note("info", "대기 중이던 판정 취소 — 스스로 일어남")
        return

    seq = payload.get("seq", "?")
    age = STATE.impact_age()
    fresh = age <= STATE.fusion_window
    strong = fresh and STATE.last_impact_g >= STATE.escalate_g
    imp_txt = (f"충격 {STATE.last_impact_g:.2f}g ({age:.1f}초 전)"
               if fresh else "충격 없음")

    # ── WARNING(누움): 강한 충격이 있으면 낙상으로 승격한다.
    if etype == "WARNING":
        if STATE.fusion_mode != "off" and strong:
            with STATE.lock:
                STATE.escalations += 1
            fire_alarm(f"누움 + {imp_txt} → 낙상 승격 (seq={seq})",
                       "높음", bridge, device)
        return

    if etype != "DANGER":
        return

    with STATE.lock:
        STATE.thermal_events += 1
    detail = f"열화상 DANGER (seq={seq})  {imp_txt}"

    if fresh or STATE.fusion_mode == "off":
        fire_alarm(detail, "높음" if fresh else "보통", bridge, device)
        return

    # 여기부터는 "열화상은 낙상이라는데 충격이 없다" 는 상황
    if STATE.fusion_mode == "strict":
        with STATE.lock:
            STATE.suppressed += 1
        STATE.note("hold", f"{detail} → 보류 (충격 필수 모드)")
        return

    # soft: 바로 버리지도, 바로 울리지도 않는다. 잠시 지켜본다.
    with STATE.lock:
        STATE.deferred += 1
        STATE.pending = {"due": time.time() + STATE.no_impact_delay,
                         "detail": detail, "reason": "충격 없음",
                         "bridge": bridge, "device": device}
    STATE.note("hold",
               f"{detail} → {STATE.no_impact_delay:.0f}초 지켜봄 "
               f"(일어나면 취소, 계속 쓰러져 있으면 알람)")


def fire_alarm(detail: str, conf: str, bridge: str | None, device: str) -> None:
    """쿨다운을 확인하고 실제로 알람을 내보낸다."""
    now = time.time()
    with STATE.lock:
        STATE.pending = None
        if now - STATE.last_alarm_t < STATE.cooldown:
            STATE.suppressed += 1
            remain = STATE.cooldown - (now - STATE.last_alarm_t)
            hold = f"{detail} → 억제 (쿨다운 {remain:.0f}초 남음)"
        else:
            hold = None
            STATE.last_alarm_t = now
            STATE.alarms += 1
    if hold:
        STATE.note("hold", hold)
        return

    STATE.note("alarm", f"★ 낙상 알람 [{conf}]  {detail}")

    if bridge:
        ok = send_to_bridge(bridge, device)
        with STATE.lock:
            STATE.bridge_ok = ok
        STATE.note("alarm" if ok else "error",
                   "SmartThings 전송 성공" if ok else "SmartThings 전송 실패")


def pending_watcher() -> None:
    """지연 판정을 감시한다. 시간이 지나도 계속 DANGER 면 알람."""
    while True:
        time.sleep(0.5)
        now = time.time()
        with STATE.lock:
            p = STATE.pending
            due = bool(p and now >= p["due"])
            if due:
                STATE.pending = None
        if not due:
            continue
        if STATE.last_event_type != "DANGER":
            STATE.note("info", "대기 중이던 판정 취소 — 상태가 풀림")
            continue
        fire_alarm(p["detail"] + " · 계속 쓰러진 상태", "보통",
                   p["bridge"], p["device"])


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
.improw{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12.5px}
.improw .t{color:var(--dim);font-variant-numeric:tabular-nums;flex-shrink:0}
.improw b{font-variant-numeric:tabular-nums;width:46px;text-align:right}
.improw .bar{flex:1;height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.improw .bar i{display:block;height:100%;border-radius:3px}
.lvhead{display:flex;align-items:baseline;gap:5px;margin-bottom:2px}
.lvhead b{font-size:26px;font-variant-numeric:tabular-nums;line-height:1.1}
.lvunit{color:var(--dim);font-size:13px;margin-right:6px}
#lvchart{width:100%;height:64px;display:block;
         background:rgba(255,255,255,.02);border-radius:6px}
.lvax{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);
      margin:2px 0 10px;font-variant-numeric:tabular-nums}
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
        <button id="ffc" class="btn">FFC</button>
        <button id="dgr" class="btn">잔상 재보정</button>
      </div>
      <p id="tres" style="font-size:12.5px;color:var(--dim);margin:0;min-height:18px"></p>
    </div>
    <div class="card">
      <h2>진동센서</h2>
      <div id="vlive">
        <div class="lvhead">
          <b id="lvnum">—</b><span class="lvunit">g</span>
          <span id="lvpill"></span>
        </div>
        <svg id="lvchart" viewBox="0 0 300 64" preserveAspectRatio="none"
             role="img" aria-label="최근 30초 진동 수준">
          <line id="lvthr" x1="0" x2="300" stroke="var(--warn)"
                stroke-width="1" stroke-dasharray="3 3" opacity=".7"/>
          <polyline id="lvline" fill="none" stroke="var(--acc)" stroke-width="2"
                    stroke-linejoin="round" stroke-linecap="round"/>
        </svg>
        <div class="lvax"><span>30초 전</span><span id="lvmax">—</span><span>지금</span></div>
      </div>
      <div id="vib"></div>
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
      row('현재 상태', s.event_type) +
      row('열화상 상태', s.thermal_health) +
      row('열화상 낙상', s.thermal_events + ' 회') +
      row('하트비트', s.heartbeats + ' 회') +
      row('알람 발송', s.alarms + ' 회') +
      row('억제/보류', s.suppressed + ' 회') +
      row('SmartThings', br) +
      row('백엔드', bk) +
      row('융합 창', s.fusion_window + ' 초') +
      (s.deghost ? row('잔상 제거',
          !s.deghost.ready ? pill('배경 학습 중 ' + s.deghost.samples + '/8','p-warn')
          : s.deghost.frozen ? pill('적용 · 배경 고정','p-ok')
          : pill('적용 중','p-ok')) : row('잔상 제거', pill('꺼짐','p-dim'))) +
      row('FFC', s.ffc_count + ' 회'
          + (s.ffc_age===null ? '' : ' · ' + s.ffc_age + '초 전')) +
      row('가동 시간', s.uptime + ' 초');

    // ── 진동센서 카드
    const MODE = {off:['참고용','p-dim'], soft:['융합 (soft)','p-ok'],
                  strict:['충격 필수','p-warn']}[s.fusion_mode] || ['—','p-dim'];
    let link;
    if(s.impact_link===null) link = pill('수신 없음','p-bad');
    else if(s.impact_link < 30) link = pill('정상 · '+s.impact_link+'초 전','p-ok');
    else link = pill(s.impact_link+'초 전','p-warn');

    let vhead =
      row('판정 개입', pill(MODE[0], MODE[1])) +
      row('최근 충격', imp) +
      row('충격 누적', s.impact_count + ' 회') +
      row('승격 알람', s.escalations + ' 회') +
      row('지연 확인', s.deferred + ' 회') +
      row('링크', link) +
      row('배터리', !s.batt_pct
          ? pill('미측정','p-dim')
          : pill(s.batt_pct + '% · ' + s.batt_mv + 'mV',
                 s.batt_pct > 40 ? 'p-ok' : s.batt_pct > 20 ? 'p-warn' : 'p-bad')
            + (s.batt_age===null ? '' : ' <span class="t">'
               + Math.round(s.batt_age/60) + '분 전</span>')) +
      row('신호 세기', s.impact_rssi ? s.impact_rssi+' dBm' : '—') +
      row('감지 문턱', s.impact_min_g + ' g / 승격 ' + s.escalate_g + ' g');

    if(s.pending) vhead += row('확인 대기',
      pill(s.pending.reason + ' · ' + s.pending.remain + '초', 'p-warn'));

    // 충격 이력 — 4g 를 100% 로 보는 가로 막대
    let bars = s.impact_list.map(function(e){
      const w = Math.max(4, Math.min(100, e.g / 2 * 100));
      const c = e.g >= s.escalate_g ? 'var(--bad)' : 'var(--ok)';
      return '<div class="improw"><span class="t">'+e.t+'</span>'
        + '<span class="bar"><i style="width:'+w.toFixed(0)+'%;background:'+c+'"></i></span>'
        + '<b>'+e.g.toFixed(2)+'g</b>'
        + '<span class="t">'+(e.rssi?e.rssi+'dBm':'')+'</span></div>';
    }).join('');
    if(!bars) bars = '<p class="t" style="margin:6px 0 0">아직 감지된 충격 없음</p>';

    document.getElementById('vib').innerHTML = vhead + bars;

    // ── 실시간 진동 수준 그래프 (최근 30초)
    const sr = s.level_series || [];
    document.getElementById('lvnum').textContent =
      s.level_live ? s.level_g.toFixed(2) : '—';
    document.getElementById('lvpill').innerHTML = s.level_live
      ? pill('실시간','p-ok')
      : pill(sr.length ? '수신 끊김' : '모니터링 꺼짐', sr.length ? 'p-bad' : 'p-dim');

    // 세로 축은 승격 문턱의 1.5배까지. 값이 그보다 크면 그만큼 늘린다.
    const top = Math.max(s.escalate_g * 1.5, Math.max(0, ...sr) * 1.1, 0.3);
    const y = v => 60 - Math.min(v / top, 1) * 56;
    document.getElementById('lvthr').setAttribute('y1', y(s.escalate_g));
    document.getElementById('lvthr').setAttribute('y2', y(s.escalate_g));
    document.getElementById('lvline').setAttribute('points',
      sr.map((v, i) => (i / Math.max(sr.length - 1, 1) * 300).toFixed(1)
                       + ',' + y(v).toFixed(1)).join(' '));
    document.getElementById('lvmax').textContent =
      sr.length ? '최대 ' + Math.max(...sr).toFixed(2) + 'g · 승격선 '
                  + s.escalate_g + 'g' : '';

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
document.getElementById('ffc').onclick = e => post('/ffc', e.target, 'FFC');
document.getElementById('dgr').onclick = e => post('/deghost-reset', e.target, '잔상 재보정');
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
        elif self.path == "/ffc":
            ok, msg = run_ffc(cfg.get("video", "/dev/video0"))
            STATE.note("info" if ok else "error", f"수동 FFC — {msg}")
            res = {"ok": ok, "msg": msg}
        elif self.path == "/deghost-reset":
            if DEGHOST is None:
                res = {"ok": False, "msg": "--deghost 가 꺼져 있습니다"}
            else:
                DEGHOST.reset()
                STATE.note("info", "잔상 배경 재추정 — 화면에서 비켜 주세요")
                res = {"ok": True, "msg": "재추정 시작 (약 12초, 사람이 없어야 정확)"}
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


# ──────────────────────────────────────────────────────────── 잔상 제거

class Deghost:
    """FFC 가 구워 넣은 고정 잔상을 빼낸다.

    잔상은 **정지된 덧셈 패턴**이다. FFC 순간의 장면이 기준면에 굳어
    이후 모든 프레임에서 빠지기 때문에, 사람 모양의 음영이 화면에
    고정으로 남는다. 다음 FFC 까지 그 패턴은 변하지 않는다.

    그래서 최근 프레임들의 **픽셀별 중앙값**을 구하면 그게 곧
    "움직이지 않는 것들"(잔상 + 벽·가구 같은 정지 열원)이 된다.
    이걸 빼면 잔상이 사라진다. 중앙값은 평균과 달리 지나가는 사람에
    끌려가지 않는다.

    ⚠ 핵심 안전장치: 사람이 누워 있거나 쓰러진 동안에는 배경을
    갱신하지 않는다. 갱신하면 가만히 있는 사람이 배경으로 흡수되어
    **정작 감지해야 할 낙상자가 화면에서 지워진다.**
    """

    def __init__(self, window_sec: float = 90.0, sample_sec: float = 1.5):
        self.samples: deque = deque(maxlen=max(8, int(window_sec / sample_sec)))
        self.sample_sec = sample_sec
        self.field = None          # 추정된 고정 패턴
        self.level = 0.0           # 원래 밝기 수준 (빼고 나서 되돌린다)
        self.last_sample = 0.0
        self.last_build = 0.0
        self.frozen = False
        self.resets = 0

    def reset(self) -> None:
        self.samples.clear()
        self.field = None
        self.resets += 1

    def apply(self, a, moving: bool):
        """a: (H,W) 원본 프레임. moving=True 면 배경 갱신을 멈춘다."""
        if a is None or a.ndim != 2:
            return a
        now = time.time()
        f = a.astype(np.float32)

        # FFC 가 방금 돌면 화면 전체 수준이 확 바뀐다. 예전 배경은 무효다.
        if self.field is not None:
            shift = abs(float(np.median(f)) - self.level)
            if shift > 120:                       # Y16 카운트 기준
                self.reset()
                STATE.note("info", "FFC 감지 — 배경 재추정 시작")

        self.frozen = moving
        if not moving and now - self.last_sample >= self.sample_sec:
            self.last_sample = now
            self.samples.append(f.copy())

        if len(self.samples) >= 8 and now - self.last_build >= self.sample_sec:
            self.last_build = now
            self.field = np.median(np.stack(self.samples), axis=0)
            self.level = float(np.median(self.field))

        if self.field is None:
            self.level = float(np.median(f))
            return a

        # 고정 패턴을 빼고, 원래 밝기 수준을 되돌린다.
        out = f - self.field + self.level
        return np.clip(out, 0, 65535).astype(a.dtype)

    def status(self) -> dict:
        return {"ready": self.field is not None,
                "samples": len(self.samples),
                "need": max(0, 8 - len(self.samples)),
                "frozen": self.frozen,
                "resets": self.resets}


DEGHOST: Deghost | None = None


def deghost_frame(a):
    """배경 갱신을 멈춰야 할 상황인지 판단해 보정을 건다."""
    if DEGHOST is None:
        return a
    # 사람이 누워 있거나(WARNING) 쓰러진(DANGER) 동안은 갱신 중지.
    # 이때 갱신하면 낙상자가 배경으로 흡수되어 화면에서 지워진다.
    lying = STATE.last_event_type in ("WARNING", "DANGER")

    # ⚠ 단, 배경이 아직 없으면 얼리지 않는다.
    # 잔상 때문에 모델이 "누워 있다"고 오판하면 상태가 DANGER 에 머물고,
    # 그러면 배경을 영영 못 만들어 잔상도 못 없앤다 — 교착에 빠진다.
    # 첫 배경은 무조건 만들고, 얼리는 건 그 다음부터다.
    freeze = lying and DEGHOST.field is not None

    # 배경이 너무 오래 굳어 있으면(온도가 변했는데도) 강제로 한 번 푼다.
    if freeze and time.time() - DEGHOST.last_sample > 300:
        freeze = False

    return DEGHOST.apply(a, freeze)


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


def lepton_frames(idx, y16, reconnect_wait=3.0, tick=0.2, *args, **kwargs):
    """lepton_live.camera_source 대체.

    시그니처가 브랜치마다 바뀌어 왔다(3인자 → 4인자). 뒤에 인자가 더
    붙어도 깨지지 않도록 *args/**kwargs 로 열어 둔다. tick 은 원본이
    프레임 없을 때 None 을 흘리는 주기인데, 여기서는 쓰지 않는다.
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
    pre.add_argument("--fusion-mode", choices=("off", "soft", "strict"),
                     default="soft",
                     help="진동센서가 판정에 개입하는 방식. "
                          "off=참고용, soft=승격/지연확인(기본), strict=충격 필수")
    pre.add_argument("--escalate-g", type=float, default=0.8,
                     help="soft 모드에서 이 이상의 충격이면 '누움'도 낙상으로 승격. "
                          "실측(마루, 2.6kg 가방 1m 낙하): 1.5m 에서 1.5g, "
                          "감쇠 A∝r^-0.54 로 5m 에서도 0.8g")
    pre.add_argument("--no-impact-delay", type=float, default=6.0,
                     help="soft 모드에서 충격 없는 낙상은 이 시간 지켜본 뒤 알람(초)")
    pre.add_argument("--deghost", action="store_true",
                     help="FFC 가 구워 넣은 고정 잔상을 빼낸다 (모델 입력에도 적용)")
    pre.add_argument("--deghost-window", type=float, default=90.0,
                     help="배경 추정에 쓸 시간 창(초). 길수록 안정적이지만 "
                          "조명·온도 변화에 늦게 따라간다")
    pre.add_argument("--ffc-interval", type=float, default=0,
                     help="이 주기(초)로 FFC 를 자동 실행한다. 0 = 끔. "
                          "Lepton 은 자체 자동 FFC 가 있으므로 보통 필요 없다")
    pre.add_argument("--impact-only", action="store_true",
                     help="열화상 없이 진동센서만 띄운다. 임계값 실측용")
    pre.add_argument("--alarm-cooldown", type=float, default=180.0)
    pre.add_argument("--jpeg-quality", type=int, default=80,
                     help="스트림 JPEG 품질 1~100. 낮추면 대역폭이 줄어 끊김이 개선된다")
    pre.add_argument("--impact-min-g", type=float, default=0.25,
                     help="이 값 미만은 충격으로 세지 않는다. 노드가 보내는 g 는 "
                          "중력을 뺀 변화량이라 정지 상태에서는 0 근처다")
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
                  known.alarm_cooldown, known.impact_min_g,
                  fusion_mode=known.fusion_mode,
                  escalate_g=known.escalate_g,
                  no_impact_delay=known.no_impact_delay)
    threading.Thread(target=pending_watcher, daemon=True).start()

    global DEGHOST
    if known.deghost:
        DEGHOST = Deghost(window_sec=known.deghost_window)

    # FFC 는 /dev/videoN 을 직접 다룬다. --camera 로 넘어온 인덱스에서 유도한다.
    video_dev = "/dev/video0"
    if "--camera" in rest:
        k = rest.index("--camera")
        if k + 1 < len(rest):
            v = rest[k + 1]
            video_dev = v if v.startswith("/dev/") else f"/dev/video{v}"
    if known.ffc_interval > 0:
        threading.Thread(target=ffc_timer,
                         args=(video_dev, known.ffc_interval),
                         daemon=True).start()
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
                  "device_id": device_id, "video": video_dev}
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

    # ── 진동센서만 (열화상 없음). 임계값을 재려고 쓰는 모드다.
    if known.impact_only:
        if not known.impact_port:
            print("오류: --impact-only 에는 --impact-port 가 필요합니다.")
            return 1
        STATE.note("info", "진동센서 단독 모드 — 열화상 없음")
        print("  대시보드에서 실시간 파형을 보며 넘어져 보세요.")
        print("  종료: Ctrl+C")
        print()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        s = STATE.snapshot()
        print()
        print(f"  충격 {s['impact_count']}회")
        for e in list(STATE.impacts)[::-1]:
            print(f"    {e['t']}  {e['g']:.2f}g  rssi={e['rssi']}dBm")
        if s["impact_count"]:
            gs = [e["g"] for e in STATE.impacts]
            print()
            print(f"  최대 {max(gs):.2f}g · 최소 {min(gs):.2f}g "
                  f"· 중앙값 {sorted(gs)[len(gs)//2]:.2f}g")
            print(f"  → --escalate-g 는 최소값의 절반쯤인 "
                  f"{min(gs)/2:.2f} 근처가 무난합니다")
        return 0

    sys.argv = ["lepton_live.py"] + rest
    try:
        import lepton_live
    except ImportError:
        print("오류: lepton_live.py 와 같은 폴더에서 실행하세요.")
        return 1

    # 프레임 형태 교정
    _orig_ftg = lepton_live.frame_to_gray
    lepton_live.frame_to_gray = (
        lambda frame, y16: _orig_ftg(deghost_frame(fix_frame(frame)), y16))

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
