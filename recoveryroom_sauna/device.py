"""Persistent read-only LAN session with the sauna.

Responsibilities in this phase:
  * connect, obtain passcode, log in, heartbeat, poll state
  * decode state into timestamped Observations with a monotonically
    increasing observation id
  * track freshness (an open socket without a recent valid state packet is
    NOT healthy)
  * reconnect with bounded backoff on any error or silence

Explicitly NOT in this phase: transmitting control frames. send_control()
refuses unless writes are enabled, and nothing in this build enables them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from . import protocol as P

log = logging.getLogger("rr.device")

HEARTBEAT_INTERVAL = 4.0
POLL_INTERVAL = 5.0
LAN_FRESHNESS_SECONDS = 20.0
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
LOGIN_TIMEOUT = 6.0
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 30.0


class WritesDisabledError(RuntimeError):
    """Raised when any code path attempts a control write without commissioning approval."""


class ConnState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"       # authenticated; health still requires a fresh report


@dataclass(frozen=True)
class Observation:
    id: int
    report: P.StateReport
    monotonic: float
    wall_utc: datetime

    def age(self, now_monotonic: float | None = None) -> float:
        return (now_monotonic if now_monotonic is not None else time.monotonic()) - self.monotonic


@dataclass
class DeviceSnapshot:
    state: ConnState
    route: str
    host: str
    port: int
    observation: Observation | None
    last_error: str | None
    reconnects: int
    frames_received: int
    protocol_errors: int
    writes_enabled: bool
    last_pong_monotonic: float | None
    now_monotonic: float = field(default_factory=time.monotonic)

    @property
    def sample_age(self) -> float | None:
        return None if self.observation is None else self.observation.age(self.now_monotonic)

    @property
    def fresh(self) -> bool:
        age = self.sample_age
        return age is not None and age <= LAN_FRESHNESS_SECONDS and self.state == ConnState.CONNECTED

    def to_dict(self) -> dict:
        obs = self.observation
        return {
            "route": self.route,
            "connection": self.state.value,
            "host": self.host,
            "port": self.port,
            "fresh": self.fresh,
            "freshnessLimitSeconds": LAN_FRESHNESS_SECONDS,
            "sampleAgeSeconds": None if self.sample_age is None else round(self.sample_age, 1),
            "observationId": None if obs is None else obs.id,
            "observedAtUtc": None if obs is None else obs.wall_utc.isoformat(),
            "report": None if obs is None else obs.report.to_dict(),
            "lastError": self.last_error,
            "reconnects": self.reconnects,
            "framesReceived": self.frames_received,
            "protocolErrors": self.protocol_errors,
            "writesEnabled": self.writes_enabled,
        }


class LanDeviceClient:
    """asyncio client. Create inside a running loop; call start()/stop()."""

    def __init__(
        self,
        host: str,
        port: int = 12416,
        *,
        route: str = "lan",
        writes_enabled: bool = False,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        poll_interval: float = POLL_INTERVAL,
        read_timeout: float = READ_TIMEOUT,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
    ) -> None:
        self.host = host
        self.port = port
        self.route = route
        self._writes_enabled = writes_enabled
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        self.read_timeout = read_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        self._lock = threading.Lock()
        self._state = ConnState.DISCONNECTED
        self._observation: Observation | None = None
        self._obs_counter = 0
        self._last_error: str | None = None
        self._reconnects = -1
        self._frames_received = 0
        self._protocol_errors = 0
        self._last_pong: float | None = None

        self._writer: asyncio.StreamWriter | None = None
        self._run_task: asyncio.Task | None = None
        self._stopping = False
        self._refresh_event: asyncio.Event | None = None
        self._obs_changed: asyncio.Condition | None = None

    # ------------------------------------------------------------------ public

    @property
    def writes_enabled(self) -> bool:
        return self._writes_enabled

    def snapshot(self) -> DeviceSnapshot:
        with self._lock:
            return DeviceSnapshot(
                state=self._state,
                route=self.route,
                host=self.host,
                port=self.port,
                observation=self._observation,
                last_error=self._last_error,
                reconnects=max(0, self._reconnects),
                frames_received=self._frames_received,
                protocol_errors=self._protocol_errors,
                writes_enabled=self._writes_enabled,
                last_pong_monotonic=self._last_pong,
            )

    async def start(self) -> None:
        self._refresh_event = asyncio.Event()
        self._obs_changed = asyncio.Condition()
        self._stopping = False
        self._run_task = asyncio.create_task(self._run(), name="rr-device")

    async def stop(self) -> None:
        self._stopping = True
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_writer()
        self._set_state(ConnState.DISCONNECTED)

    def request_refresh(self) -> None:
        """Ask for a fresh state read now. Never changes device state."""
        if self._refresh_event is not None:
            self._refresh_event.set()

    async def wait_for_newer_observation(self, after_id: int, timeout: float) -> Observation | None:
        """Wait until the observation id advances past after_id (a real new report)."""
        assert self._obs_changed is not None
        deadline = time.monotonic() + timeout
        async with self._obs_changed:
            while True:
                obs = self._observation
                if obs is not None and obs.id > after_id:
                    return obs
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._obs_changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return None

    async def send_control(self, payload: bytes) -> None:
        """Transmit a control frame. REFUSED in this build.

        Phase 3 will implement a serialised command worker with intent
        persistence, bounded ACK and readback waits, and outcome
        classification (rejected / confirmed / uncertain). Until commissioning
        enables writes, this method must never transmit.
        """
        if not self._writes_enabled:
            raise WritesDisabledError("control writes are disabled (commissioning.writesEnabled is false)")
        raise NotImplementedError("control path is not implemented in the read-only build")

    # ----------------------------------------------------------------- internal

    def _set_state(self, state: ConnState, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            if error is not None:
                self._last_error = error

    async def _close_writer(self) -> None:
        w, self._writer = self._writer, None
        if w is not None:
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), 1.0)
            except Exception:
                pass

    async def _run(self) -> None:
        backoff = self.backoff_initial
        while not self._stopping:
            with self._lock:
                self._reconnects += 1
            try:
                await self._session()
                backoff = self.backoff_initial
            except asyncio.CancelledError:
                raise
            except P.ProtocolError as exc:
                with self._lock:
                    self._protocol_errors += 1
                self._set_state(ConnState.DISCONNECTED, f"protocol error: {exc}")
                log.warning("device: protocol error, reconnecting: %s", exc)
            except (OSError, asyncio.TimeoutError, ConnectionError, EOFError) as exc:
                self._set_state(ConnState.DISCONNECTED, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
                log.info("device: connection lost (%s), reconnecting in %.1fs", exc or type(exc).__name__, backoff)
            except Exception as exc:  # defensive: never let the worker die silently
                self._set_state(ConnState.DISCONNECTED, f"unexpected: {exc!r}")
                log.exception("device: unexpected error")
            finally:
                await self._close_writer()
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(self.backoff_max, backoff * 2)

    async def _session(self) -> None:
        self._set_state(ConnState.CONNECTING)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT
        )
        self._writer = writer
        decoder = P.FrameDecoder()

        async def read_frames() -> list[P.Frame]:
            data = await asyncio.wait_for(reader.read(4096), self.read_timeout)
            if not data:
                raise EOFError("connection closed by sauna")
            frames = decoder.feed(data)
            with self._lock:
                self._frames_received += len(frames)
            return frames

        async def send(cmd: int, payload: bytes = b"") -> None:
            writer.write(P.encode_frame(cmd, payload))
            await writer.drain()

        async def expect(cmd: int, timeout: float) -> P.Frame:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError(f"timed out waiting for {P.Cmd(cmd).name}")
                for frame in await asyncio.wait_for(read_frames(), remaining):
                    if frame.cmd == cmd:
                        return frame
                    self._handle_unsolicited(frame)

        # --- authenticate (read-only handshake) ---
        self._set_state(ConnState.AUTHENTICATING)
        await send(P.Cmd.PASSCODE_REQ)
        passcode = await expect(P.Cmd.PASSCODE_RSP, LOGIN_TIMEOUT)
        # Passcode material is session-local only: never logged, never persisted.
        await send(P.Cmd.LOGIN_REQ, P.login_payload_from_passcode_response(passcode.payload))
        login = await expect(P.Cmd.LOGIN_RSP, LOGIN_TIMEOUT)
        if not P.login_succeeded(login.payload):
            raise ConnectionError("sauna rejected LAN login")
        self._set_state(ConnState.CONNECTED, error=None)
        with self._lock:
            self._last_error = None
        log.info("device: authenticated to %s:%s", self.host, self.port)

        # --- steady state: heartbeat + poll + reader ---
        async def heartbeat() -> None:
            while True:
                await send(P.Cmd.PING)
                await asyncio.sleep(self.heartbeat_interval)

        async def poller() -> None:
            assert self._refresh_event is not None
            while True:
                await send(P.Cmd.STATE_REQ, P.STATE_REQUEST_PAYLOAD)
                self._refresh_event.clear()
                try:
                    await asyncio.wait_for(self._refresh_event.wait(), self.poll_interval)
                except asyncio.TimeoutError:
                    pass

        async def reader_loop() -> None:
            last_report = time.monotonic()
            while True:
                try:
                    frames = await read_frames()
                except asyncio.TimeoutError:
                    frames = []
                for frame in frames:
                    if frame.cmd == P.Cmd.STATE_RSP:
                        await self._record_state(frame.payload)
                        last_report = time.monotonic()
                    else:
                        self._handle_unsolicited(frame)
                if time.monotonic() - last_report > LAN_FRESHNESS_SECONDS:
                    raise asyncio.TimeoutError("no valid state report within freshness limit")

        tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(poller()), asyncio.create_task(reader_loop())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()          # re-raise the first failure
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _handle_unsolicited(self, frame: P.Frame) -> None:
        if frame.cmd == P.Cmd.PONG:
            with self._lock:
                self._last_pong = time.monotonic()
        elif frame.cmd == P.Cmd.CONTROL_ACK:
            log.warning("device: unexpected control ACK seq=%s in read-only mode", frame.seq)
        else:
            log.debug("device: ignoring frame %s (%d bytes)", frame.cmd_name, len(frame.payload))

    async def _record_state(self, payload: bytes) -> None:
        report = P.parse_state_payload(payload)   # raises ProtocolError on impossible state
        assert self._obs_changed is not None
        async with self._obs_changed:
            with self._lock:
                self._obs_counter += 1
                self._observation = Observation(
                    id=self._obs_counter,
                    report=report,
                    monotonic=time.monotonic(),
                    wall_utc=datetime.now(timezone.utc),
                )
            self._obs_changed.notify_all()


class DeviceThread:
    """Runs a LanDeviceClient on its own event loop in a background thread so
    the synchronous HTTP server can read snapshots."""

    def __init__(self, client_factory) -> None:
        self._factory = client_factory
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="rr-device-loop", daemon=True)
        self._client: LanDeviceClient | None = None
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._client = self._factory()
        self._loop.run_until_complete(self._client.start())
        self._ready.set()
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        if self._client is not None:
            fut = asyncio.run_coroutine_threadsafe(self._client.stop(), self._loop)
            try:
                fut.result(5)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    @property
    def client(self) -> LanDeviceClient:
        assert self._client is not None
        return self._client

    def snapshot(self) -> DeviceSnapshot:
        return self.client.snapshot()

    def request_refresh(self) -> None:
        self._loop.call_soon_threadsafe(self.client.request_refresh)

    def wait_for_newer_observation(self, after_id: int, timeout: float) -> Observation | None:
        fut = asyncio.run_coroutine_threadsafe(self.client.wait_for_newer_observation(after_id, timeout), self._loop)
        return fut.result(timeout + 1)
