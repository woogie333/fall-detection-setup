"""
SmartThings 알림 전송 모듈

toddaustin07/edgebridge + LAN Device Trigger V2 드라이버 조합으로
SmartThings 허브에 낙상 이벤트를 전달한다.

설계 의도:
  - 낙상 감지 루프를 절대 블로킹하지 않는다 (전송은 백그라운드 스레드)
  - 네트워크 실패 시 재시도하되, 무한정 쌓이지 않는다
  - 중복 알람을 억제한다 (같은 사건으로 폰이 20번 울리면 안 됨)
  - edgebridge가 죽어 있어도 감지 시스템은 계속 돈다

의존성:
    pip install requests --break-system-packages
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class NotifierConfig:
    """edgebridge 연동 설정."""

    bridge_host: str = ""
    """edgebridge 서버 주소.

    주의: 같은 보드에서 edgebridge를 돌리더라도 127.0.0.1을 쓰면 안 된다.
    edgebridge는 요청의 출발지 IP가 SmartThings 앱에 등록된
    'LAN App/Device Address'와 일치하는지 검사하며, 불일치 시
    'Unregistered address or invalid endpoint'로 거부한다.

    루프백으로 접속하면 출발지가 127.0.0.1이 되어 검사에 걸리므로,
    보드 자신의 LAN IP(예: 192.168.0.100)를 지정해야 한다.
    비워두면 아래 auto_detect_host()가 자동으로 찾는다.
    """

    bridge_port: int = 8088
    """edgebridge 기본 포트."""

    device_name: str = "falldetect"
    """SmartThings 앱의 LAN Device Trigger 설정에 입력한 이름과 반드시 일치해야 한다.
    공백/특수문자 불가."""

    timeout_sec: float = 3.0
    """HTTP 타임아웃. 짧게 잡는다 — 감지 루프보다 알림이 느려질 이유가 없다."""

    max_retries: int = 3
    """전송 실패 시 재시도 횟수."""

    retry_backoff_sec: float = 2.0
    """재시도 간격의 기준값. 시도마다 배로 늘어난다 (2s, 4s, 8s)."""

    cooldown_sec: float = 180.0
    """같은 종류의 알람을 이 시간 내에는 다시 보내지 않는다.
    설계 문서의 '최근 알람 후 3분간 재알람 억제' 규칙."""

    queue_maxsize: int = 32
    """전송 대기열 상한. 넘치면 가장 오래된 것을 버린다."""

    def port_str(self) -> str:
        return str(self.bridge_port)


def auto_detect_host() -> str:
    """이 장치의 LAN IP를 찾는다.

    실제로 패킷을 보내지 않고, 외부 주소로 향하는 소켓의 로컬 끝단을 조회해
    '기본 경로로 나갈 때 쓰는 인터페이스의 IP'를 얻는다. 인터페이스 이름
    (eth0/wlan0)을 몰라도 되고, 유선/무선이 바뀌어도 알아서 따라간다.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP라 실제 통신은 발생하지 않는다
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@dataclass
class _Event:
    name: str
    created_at: float = field(default_factory=time.monotonic)


class SmartThingsNotifier:
    """백그라운드 스레드로 edgebridge에 트리거를 전송한다.

    사용:
        notifier = SmartThingsNotifier(NotifierConfig(bridge_host="192.168.0.100"))
        notifier.start()
        ...
        notifier.notify("falldetect")     # 논블로킹, 즉시 반환
        ...
        notifier.stop()

    컨텍스트 매니저로도 쓸 수 있다:
        with SmartThingsNotifier(cfg) as notifier:
            notifier.notify("falldetect")
    """

    def __init__(self, config: Optional[NotifierConfig] = None) -> None:
        self.config = config or NotifierConfig()

        if not self.config.bridge_host:
            self.config.bridge_host = auto_detect_host()
            logger.info("bridge_host auto-detected: %s", self.config.bridge_host)
        elif self.config.bridge_host in ("127.0.0.1", "localhost"):
            logger.warning(
                "bridge_host=%s — edgebridge가 'Unregistered address'로 거부할 수 있습니다. "
                "보드의 LAN IP를 지정하세요.",
                self.config.bridge_host,
            )

        self._queue: queue.Queue[Optional[_Event]] = queue.Queue(
            maxsize=self.config.queue_maxsize
        )
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._sending = threading.Event()
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

        # 통계 — 운영 중 상태 점검용
        self.stat_sent = 0
        self.stat_failed = 0
        self.stat_suppressed = 0
        self.stat_dropped = 0

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """백그라운드 전송 스레드를 시작한다."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("notifier already running")
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._worker, name="st-notifier", daemon=True
        )
        self._thread.start()
        logger.info(
            "notifier started -> http://%s:%d/%s/trigger",
            self.config.bridge_host,
            self.config.bridge_port,
            self.config.device_name,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """대기 중인 전송을 마치고 스레드를 종료한다."""
        if self._thread is None:
            return
        self._stop_flag.set()
        try:
            self._queue.put_nowait(None)  # 종료 신호
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None
        logger.info(
            "notifier stopped (sent=%d failed=%d suppressed=%d dropped=%d)",
            self.stat_sent,
            self.stat_failed,
            self.stat_suppressed,
            self.stat_dropped,
        )

    def notify(self, name: Optional[str] = None, *, force: bool = False) -> bool:
        """알람을 전송 대기열에 넣는다. 즉시 반환한다 (논블로킹).

        Args:
            name: 트리거할 기기 이름. 생략하면 config.device_name.
            force: True면 쿨다운을 무시하고 강제 전송. 테스트용.

        Returns:
            대기열에 들어갔으면 True, 쿨다운으로 억제되었거나
            대기열이 꽉 찼으면 False.
        """
        name = name or self.config.device_name

        if not force and self._in_cooldown(name):
            self.stat_suppressed += 1
            logger.info("suppressed '%s' (cooldown)", name)
            return False

        event = _Event(name=name)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 대기열이 꽉 찼다는 건 네트워크가 오래 죽어 있었다는 뜻.
            # 오래된 것을 버리고 최신 이벤트를 넣는다 — 낡은 알람보다 최근 알람이 중요.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
                self.stat_dropped += 1
                logger.warning("queue full, dropped oldest event")
            except (queue.Empty, queue.Full):
                self.stat_dropped += 1
                return False

        # 쿨다운 타이머는 '전송 성공'이 아니라 '접수' 시점에 건다.
        # 전송이 느려도 그 사이 중복 알람이 쌓이지 않게 하기 위함.
        if not force:
            with self._lock:
                self._last_sent[name] = time.monotonic()
        return True

    def ping(self) -> bool:
        """edgebridge 서버가 살아 있는지 동기적으로 확인한다.

        기동 시 한 번 호출해서 설정이 맞는지 검증하는 용도.
        """
        url = f"http://{self.config.bridge_host}:{self.config.bridge_port}/"
        try:
            resp = requests.get(url, timeout=self.config.timeout_sec)
            # edgebridge는 루트에 대해 404를 줄 수도 있다.
            # 응답이 왔다는 것 자체가 서버가 살아 있다는 뜻.
            logger.info("edgebridge reachable (HTTP %d)", resp.status_code)
            return True
        except requests.RequestException as exc:
            logger.error("edgebridge unreachable at %s: %s", url, exc)
            return False

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """대기열이 비고 전송이 끝날 때까지 기다린다. 테스트/종료 시 유용.

        Returns:
            시간 내에 모두 처리되었으면 True.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and not self._sending.is_set():
                return True
            time.sleep(0.1)
        return False

    def reset_cooldown(self, name: Optional[str] = None) -> None:
        """쿨다운을 해제한다. 테스트나 수동 복구용."""
        with self._lock:
            if name is None:
                self._last_sent.clear()
            else:
                self._last_sent.pop(name, None)

    # ------------------------------------------------------------------
    # 내부 구현
    # ------------------------------------------------------------------

    def _in_cooldown(self, name: str) -> bool:
        with self._lock:
            last = self._last_sent.get(name)
        if last is None:
            return False
        return (time.monotonic() - last) < self.config.cooldown_sec

    def _worker(self) -> None:
        while not self._stop_flag.is_set() or not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:  # 종료 신호
                break
            self._sending.set()
            try:
                self._send_with_retry(event)
            finally:
                self._sending.clear()

    def _send_with_retry(self, event: _Event) -> None:
        url = (
            f"http://{self.config.bridge_host}:{self.config.bridge_port}"
            f"/{event.name}/trigger"
        )
        delay = self.config.retry_backoff_sec

        for attempt in range(1, self.config.max_retries + 1):
            if self._stop_flag.is_set() and attempt > 1:
                # 종료 중이면 재시도하지 않는다
                self.stat_failed += 1
                logger.warning("abandoned '%s' (shutting down)", event.name)
                return
            try:
                # lantrigger 드라이버는 POST를 기대한다
                resp = requests.post(url, timeout=self.config.timeout_sec)

                # edgebridge는 출발지 IP가 등록된 주소와 다르면 거부한다.
                # 재시도해도 결과가 같으므로 즉시 포기하고 원인을 알린다.
                body = (resp.text or "")[:200]
                if "unregistered" in body.lower() or "invalid endpoint" in body.lower():
                    self.stat_failed += 1
                    logger.error(
                        "edgebridge가 요청을 거부했습니다: %s\n"
                        "  → 앱의 'LAN App/Device Address'가 현재 출발지 IP(%s)와 "
                        "일치하는지 확인하세요. 127.0.0.1로 접속하면 반드시 실패합니다.\n"
                        "  → 기기 이름 '%s'이 앱 설정과 정확히 같은지도 확인하세요.",
                        body.strip(),
                        self.config.bridge_host,
                        event.name,
                    )
                    return

                if 200 <= resp.status_code < 300:
                    latency = time.monotonic() - event.created_at
                    self.stat_sent += 1
                    logger.info(
                        "sent '%s' (attempt %d, %.0fms)",
                        event.name,
                        attempt,
                        latency * 1000,
                    )
                    return
                logger.warning(
                    "bridge returned HTTP %d for '%s' (attempt %d)",
                    resp.status_code,
                    event.name,
                    attempt,
                )
            except requests.RequestException as exc:
                logger.warning(
                    "send failed for '%s' (attempt %d): %s", event.name, attempt, exc
                )

            if attempt < self.config.max_retries:
                # 종료 신호가 오면 즉시 빠져나올 수 있도록 wait 사용
                if self._stop_flag.wait(timeout=delay):
                    return
                delay *= 2

        self.stat_failed += 1
        logger.error(
            "giving up on '%s' after %d attempts", event.name, self.config.max_retries
        )

    # ------------------------------------------------------------------

    def __enter__(self) -> "SmartThingsNotifier":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
