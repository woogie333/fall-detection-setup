#!/usr/bin/env python3
"""
SmartThings 연동 테스트 도구 — 센서 없이 검증한다.

낙상 감지 로직을 만들기 전에, "보드에서 신호를 쏘면 폰에 알림이 뜬다"는
경로 전체가 동작하는지 먼저 확정하기 위한 스크립트.

사용법:

  # 1. edgebridge 서버가 살아 있는지만 확인
  python3 test_notify.py --check

  # 2. 알람 1회 전송 — 폰에서 알림 확인
  python3 test_notify.py --once

  # 3. 대화형 모드: 엔터 칠 때마다 알람 발생
  python3 test_notify.py

  # 4. 쿨다운 동작 확인 (연속 5회 시도 → 1회만 나가야 정상)
  python3 test_notify.py --burst 5

  # 5. 다른 보드/기기 이름 지정
  python3 test_notify.py --host 192.168.0.100 --device falldetect
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from smartthings_notifier import NotifierConfig, SmartThingsNotifier


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_check(notifier: SmartThingsNotifier) -> int:
    print("edgebridge 연결 확인 중...")
    if notifier.ping():
        print("  OK — 서버가 응답합니다.")
        print()
        print("다음 단계: python3 test_notify.py --once 로 실제 알람을 보내보세요.")
        return 0

    cfg = notifier.config
    print("  실패 — 서버에 연결할 수 없습니다.")
    print()
    print("확인할 것:")
    print(f"  1. edgebridge가 실행 중인가?   ps aux | grep edgebridge")
    print(f"  2. 주소가 맞는가?              현재 설정: {cfg.bridge_host}:{cfg.bridge_port}")
    print(f"  3. 포트가 열려 있는가?         ss -tlnp | grep {cfg.bridge_port}")
    print(f"  4. 방화벽                      sudo ufw allow {cfg.bridge_port}/tcp")
    return 1


def cmd_once(notifier: SmartThingsNotifier, force: bool) -> int:
    print(f"'{notifier.config.device_name}' 트리거 전송...")
    accepted = notifier.notify(force=force)
    if not accepted:
        print("  쿨다운으로 억제되었습니다. --force 로 무시할 수 있습니다.")
        return 1

    # 재시도까지 모두 끝날 때까지 대기 (최대 backoff 2+4=6초 + 타임아웃 여유)
    notifier.wait_idle(timeout=20.0)

    if notifier.stat_sent > 0:
        print("  전송 성공. 휴대폰의 SmartThings 앱을 확인하세요.")
        print()
        print("알림이 안 뜬다면:")
        print("  - 기기 이름이 앱 설정과 정확히 일치하는지 (대소문자 포함)")
        print("  - 해당 기기에 알림 루틴을 걸어두었는지")
        return 0

    print("  전송 실패. 위 로그를 확인하세요.")
    return 1


def cmd_burst(notifier: SmartThingsNotifier, count: int) -> int:
    print(f"{count}회 연속 전송 시도 — 쿨다운이 동작하면 1회만 나가야 정상입니다.")
    print()
    for i in range(1, count + 1):
        accepted = notifier.notify()
        mark = "전송" if accepted else "억제"
        print(f"  {i}/{count}  {mark}")
        time.sleep(0.3)

    notifier.wait_idle(timeout=20.0)
    print()
    print(f"결과: 실제 전송 {notifier.stat_sent}건, 억제 {notifier.stat_suppressed}건")
    if notifier.stat_sent == 1:
        print("  정상입니다.")
        return 0
    print("  예상과 다릅니다. cooldown_sec 설정을 확인하세요.")
    return 1


def cmd_interactive(notifier: SmartThingsNotifier) -> int:
    print("대화형 모드입니다.")
    print("  엔터      → 알람 전송")
    print("  f + 엔터  → 쿨다운 무시하고 강제 전송")
    print("  r + 엔터  → 쿨다운 초기화")
    print("  q + 엔터  → 종료")
    print()

    while True:
        try:
            line = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if line == "q":
            break
        if line == "r":
            notifier.reset_cooldown()
            print("  쿨다운 초기화됨")
            continue

        notifier.notify(force=(line == "f"))

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SmartThings edgebridge 연동 테스트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--host",
        default="",
        help="edgebridge 서버 주소. 생략하면 보드의 LAN IP를 자동 검출합니다. "
        "(127.0.0.1은 edgebridge가 거부하므로 쓰지 마세요)",
    )
    parser.add_argument("--port", type=int, default=8088, help="포트 (기본: 8088)")
    parser.add_argument(
        "--device",
        default="falldetect",
        help="SmartThings 앱에 설정한 LAN Device Name (기본: falldetect)",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=180.0,
        help="중복 알람 억제 시간(초). 테스트 시 0으로 두면 편합니다.",
    )
    parser.add_argument("--check", action="store_true", help="연결 확인만 수행")
    parser.add_argument("--once", action="store_true", help="1회 전송 후 종료")
    parser.add_argument("--force", action="store_true", help="쿨다운 무시")
    parser.add_argument("--burst", type=int, metavar="N", help="N회 연속 전송 (쿨다운 검증)")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = NotifierConfig(
        bridge_host=args.host,
        bridge_port=args.port,
        device_name=args.device,
        cooldown_sec=args.cooldown,
    )

    notifier = SmartThingsNotifier(config)

    print()
    print(f"  대상: http://{config.bridge_host}:{config.port_str()}/{args.device}/trigger")
    print(f"  앱의 'LAN App/Device Address'에 {config.bridge_host} 가 들어 있어야 합니다.")
    print()

    if args.check:
        return cmd_check(notifier)

    with notifier:
        if args.once:
            return cmd_once(notifier, args.force)
        if args.burst:
            return cmd_burst(notifier, args.burst)
        return cmd_interactive(notifier)


if __name__ == "__main__":
    sys.exit(main())
