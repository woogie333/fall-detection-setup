"""
HLK-LD2450 24GHz mmWave 레이더 파서.

UART 256000 8N1, 10Hz. 최대 3명의 x/y 좌표(mm)와 속도(cm/s)를 준다.

프레임 구조 (30 bytes):
    AA FF 03 00 | 타깃1 (8B) | 타깃2 (8B) | 타깃3 (8B) | 55 CC

타깃 8바이트:
    x(int16, mm) y(int16, mm) speed(int16, cm/s) resolution(uint16, mm)

⚠️ 부호 인코딩이 2의 보수가 아니다. 최상위 비트가 1이면 양수, 0이면 음수다.
   이걸 일반적인 2의 보수로 처리하면 좌표가 통째로 뒤집힌다.

⚠️ LD2450 은 수평 평면상의 2D 좌표만 준다. 높이(z)는 측정하지 않으므로
   "아래로 떨어졌다"를 이 센서 단독으로는 알 수 없다. 낙상 판정에서의 역할은
   사람의 존재·위치와 "넘어진 뒤 그 자리에 계속 있다"를 확인하는 것이다.
   또한 대상이 완전히 정지하면 트랙을 놓치는 경향이 있다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, List, Optional

HEADER = b"\xAA\xFF\x03\x00"
TAIL = b"\x55\xCC"
FRAME_LEN = 30
BAUD = 256000


@dataclass
class Target:
    x_mm: int          # 좌우 (센서 정면 기준, 오른쪽이 양수)
    y_mm: int          # 거리 (센서에서 멀어지는 방향이 양수)
    speed_cms: int     # 시선 방향 속도. 음수 = 접근
    res_mm: int        # 거리 분해능

    @property
    def distance_mm(self) -> float:
        return (self.x_mm ** 2 + self.y_mm ** 2) ** 0.5

    @property
    def angle_deg(self) -> float:
        """정면을 0도로 한 좌우 각도. 오른쪽이 양수."""
        import math
        return math.degrees(math.atan2(self.x_mm, max(self.y_mm, 1)))


@dataclass
class Frame:
    targets: List[Target]
    t: float

    @property
    def count(self) -> int:
        return len(self.targets)


def decode_signed(raw: int) -> int:
    """LD2450 의 부호 인코딩을 푼다.

    최상위 비트 1 = 양수, 0 = 음수. 2의 보수가 아니다.

        0x8000 | v  ->  +v
        v            ->  -v
    """
    if raw & 0x8000:
        return raw - 0x8000
    return -raw


def parse_target(b: bytes) -> Optional[Target]:
    """8바이트를 타깃으로. 미사용 슬롯(전부 0x00)이면 None."""
    if b == b"\x00" * 8:
        return None
    x = decode_signed(int.from_bytes(b[0:2], "little"))
    y = decode_signed(int.from_bytes(b[2:4], "little"))
    s = decode_signed(int.from_bytes(b[4:6], "little"))
    r = int.from_bytes(b[6:8], "little")
    return Target(x_mm=x, y_mm=y, speed_cms=s, res_mm=r)


def parse_frame(payload: bytes) -> Frame:
    """헤더/테일을 뺀 24바이트에서 타깃 목록을 만든다."""
    targets = []
    for i in range(3):
        t = parse_target(payload[i * 8:(i + 1) * 8])
        if t is not None:
            targets.append(t)
    return Frame(targets=targets, t=time.monotonic())


def open_serial(port: str, baud: int = BAUD):
    """시리얼 포트를 연다.

    256000 은 비표준 속도라 드라이버가 거부할 수 있다. pyserial 이 실패하면
    termios 의 BOTHER 로 커스텀 속도를 직접 설정한다.
    """
    import serial

    try:
        return serial.Serial(port, baud, timeout=1)
    except (ValueError, OSError) as exc:
        raise RuntimeError(
            f"{port} 를 {baud} baud 로 열지 못했습니다: {exc}\n"
            f"  - USB-TTL 어댑터를 쓰고 있다면 대부분 문제없습니다.\n"
            f"  - 보드 내장 UART 라면 비표준 속도를 지원하지 않을 수 있습니다.\n"
            f"  - 권한 문제일 수도 있습니다:  sudo usermod -aG dialout $USER (재로그인)"
        ) from exc


def read_frames(ser) -> Iterator[Frame]:
    """시리얼에서 프레임을 계속 읽어 내보낸다. 헤더로 재동기화한다."""
    buf = bytearray()
    while True:
        chunk = ser.read(64)
        if not chunk:
            continue
        buf.extend(chunk)

        while True:
            i = buf.find(HEADER)
            if i < 0:
                # 헤더 일부가 걸쳐 있을 수 있으니 꼬리 3바이트는 남긴다
                if len(buf) > 3:
                    del buf[:-3]
                break
            if len(buf) - i < FRAME_LEN:
                del buf[:i]
                break

            frame = bytes(buf[i:i + FRAME_LEN])
            del buf[:i + FRAME_LEN]

            if frame[-2:] != TAIL:
                continue  # 깨진 프레임 — 버리고 다음 헤더로
            yield parse_frame(frame[4:28])
