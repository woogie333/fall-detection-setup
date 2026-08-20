#!/usr/bin/env python3
"""
lepton_live.py 의 HUD 화면을 브라우저로 스트리밍한다.

카메라는 한 프로세스만 열 수 있으므로 camera_test.py 와 동시에 못 돌린다.
이 스크립트는 lepton_live 를 그대로 실행하되, 화면 출력만 가로채서
MJPEG 로 내보낸다. 원본 저장소 파일은 수정하지 않는다.

사용법 (lepton_live.py 와 같은 폴더에서):

    python3 web_view.py --camera 8
    python3 web_view.py --camera 8 --webhook http://172.30.1.33:8088/falldetect/trigger
    python3 web_view.py --camera 8 --web-port 8090

    → 브라우저에서 http://<보드IP>:8090

lepton_live.py 의 모든 옵션을 그대로 쓸 수 있다 (--thr, --stride, --y16 ...).
--display 는 자동으로 붙으므로 지정할 필요 없다.

동작 원리: lepton_live 가 HUD 를 그릴 때 부르는 cv2.imshow 를 가로채
프레임을 웹 서버로 넘긴다. GUI 가 없는 SSH 환경에서도 문제없다.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import cv2
import numpy as np


# ---------------------------------------------------------------- 프레임 교정

def fix_frame(frame):
    """1차원 버퍼로 온 프레임을 (H, W) 이미지로 되돌린다.

    PureThermal + V4L2 에서 Y16 으로 캡처하면 OpenCV 가 프레임을 (1, N)
    평면 버퍼로 넘기는 경우가 있다. 그대로 두면 lepton_live 의 letterbox
    코드가 N 을 한 변으로 착각해 수만 픽셀짜리 이미지를 만들려다
    메모리를 터뜨린다(프로세스가 Killed 됨).

    Lepton 3 은 160x120. Y16 의 160x122 변형은 아래 2줄이 텔레메트리라 잘라낸다.
    """
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


# ---------------------------------------------------------------- 프레임 허브

class FrameHub:
    """최신 프레임 한 장만 유지. 느린 클라이언트가 있어도 지연이 쌓이지 않는다."""

    def __init__(self) -> None:
        self._jpeg: bytes | None = None
        self._lock = threading.Lock()
        self.count = 0

    def put_bgr(self, img) -> None:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        with self._lock:
            self._jpeg = buf.tobytes()
            self.count += 1

    def get(self) -> bytes | None:
        with self._lock:
            return self._jpeg


HUB = FrameHub()

_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>fall detection live</title>
<style>
 body{margin:0;background:#111;color:#bbb;font-family:system-ui,-apple-system,sans-serif;
      display:flex;flex-direction:column;align-items:center;gap:14px;padding:18px}
 img{max-width:100%;width:512px;image-rendering:pixelated;border-radius:10px;background:#000}
 p{font-size:13px;margin:0;color:#777;text-align:center;line-height:1.6}
 b{color:#ddd}
</style></head><body>
 <img src="/stream" alt="live">
 <p><b>SAFE</b> 정상 &nbsp;·&nbsp; <b>LIED</b> 누움 &nbsp;·&nbsp; <b>FALL</b> 낙상 감지<br>
    하단 막대는 EMA 확률. 종료는 보드에서 Ctrl+C.</p>
</body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                jpeg = HUB.get()
                if jpeg is None:
                    time.sleep(0.05)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- 메인

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
    # --web-port 만 여기서 처리하고 나머지 인자는 lepton_live 로 그대로 넘긴다.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--web-port", type=int, default=8090)
    known, rest = pre.parse_known_args()

    if "--display" not in rest:
        rest.append("--display")

    # GUI 호출을 웹 스트리밍으로 치환.
    # lepton_live 를 import 하기 전에 바꿔야 한다.
    cv2.imshow = lambda name, img: HUB.put_bgr(img)
    cv2.waitKey = lambda delay=0: -1
    cv2.destroyAllWindows = lambda: None
    cv2.namedWindow = lambda *a, **k: None

    server = HTTPServer(("0.0.0.0", known.web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print()
    print(f"  브라우저:  http://{lan_ip()}:{known.web_port}")
    print(f"  종료:      Ctrl+C")
    print()

    sys.argv = ["lepton_live.py"] + rest

    try:
        import lepton_live
    except ImportError:
        print("오류: lepton_live.py 와 같은 폴더에서 실행하세요.")
        return 1

    # lepton_live 가 프레임을 만지기 직전에 형태를 교정한다.
    # 원본 파일은 수정하지 않고, import 된 함수만 감싼다.
    _orig_frame_to_gray = lepton_live.frame_to_gray
    _reported = {"done": False}

    def _patched(frame, y16):
        fixed = fix_frame(frame)
        if not _reported["done"]:
            print(f"  프레임 형태: {np.asarray(frame).shape} -> {fixed.shape} "
                  f"({fixed.dtype})")
            _reported["done"] = True
        return _orig_frame_to_gray(fixed, y16)

    lepton_live.frame_to_gray = _patched

    try:
        lepton_live.main()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
