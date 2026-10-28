"""Deterministic simulated Clearlight sauna.

Speaks the provisional LAN protocol over a real TCP socket so the device
client, the dashboard and the tests exercise the same code path they will use
against the shop unit. The thermal model is a crude stand-in for software
testing only; it says nothing about how the real cabin heats.

Fault injection knobs let tests prove parser and freshness behaviour:
fragmented writes, coalesced responses, silence, dropped pongs, garbage.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import protocol as P

log = logging.getLogger("rr.simulator")

AMBIENT_F = 68


@dataclass
class SimulatedSauna:
    """Pure model. Advance with tick(seconds)."""

    power: bool = False
    internal_light: bool = False
    external_light: bool = False
    preset_enabled: bool = False
    celsius_display: bool = True
    target_f: int = 176           # 80C
    timer_seconds: int = 0
    current_f: float = AMBIENT_F
    serial: int = 0x2A
    heart: int = 0
    heat_rate_f_per_min: float = 2.5
    cool_rate_f_per_min: float = 1.0

    @property
    def set_hour(self) -> int:
        return self.timer_seconds // 3600

    @property
    def set_minute(self) -> int:
        return (self.timer_seconds % 3600) // 60

    def tick(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.power:
            self.timer_seconds = max(0, self.timer_seconds - int(seconds))
            if self.timer_seconds == 0:
                self.power = False           # hardware timer expiry switches heating off
            else:
                self.current_f = min(self.target_f, self.current_f + self.heat_rate_f_per_min * seconds / 60)
        if not self.power:
            self.current_f = max(AMBIENT_F, self.current_f - self.cool_rate_f_per_min * seconds / 60)
        self.heart = (self.heart + 1) & 0xFF

    def state_payload(self) -> bytes:
        return P.build_state_payload(
            power=self.power,
            internal_light=self.internal_light,
            external_light=self.external_light,
            preset_enabled=self.preset_enabled,
            celsius_display=self.celsius_display,
            target_f=self.target_f,
            set_hour=self.set_hour,
            set_minute=self.set_minute,
            serial=self.serial,
            current_f=int(round(self.current_f)),
            heart=self.heart,
        )

    def apply_control(self, payload: bytes) -> bool:
        """Apply a 0x0093 control payload. Returns True if understood."""
        if len(payload) != P.CONTROL_PAYLOAD_LEN or payload[0] != 0x01:
            return False
        kind = payload[1]
        if kind == P.ControlKind.FLAGS:
            mask, value = payload[2], payload[3]
            if mask == P.Flag.POWER:
                self.power = bool(value & P.Flag.POWER)
                if self.power and self.timer_seconds == 0:
                    self.timer_seconds = 60 * 60
            elif mask == P.Flag.INTERNAL_LIGHT:
                self.internal_light = bool(value & P.Flag.INTERNAL_LIGHT)
            elif mask == P.Flag.EXTERNAL_LIGHT:
                self.external_light = bool(value & P.Flag.EXTERNAL_LIGHT)
            else:
                return False
            return True
        if kind == P.ControlKind.TARGET:
            self.target_f = payload[7]
            return True
        if kind == P.ControlKind.TIMER:
            self.timer_seconds = payload[9] * 60
            return True
        return False


@dataclass
class Faults:
    """Fault injection. All default to healthy behaviour."""

    fragment_bytes: int = 0        # >0: split every write into chunks of this size
    coalesce: bool = False         # buffer responses and flush them together
    silent: bool = False           # accept connections, never respond
    drop_pong: bool = False        # do not answer heartbeats
    drop_state: bool = False       # do not answer state requests
    reject_login: bool = False
    garbage_before_response: bytes = b""
    reject_control: bool = False   # ACK but do not apply control (ACK != confirmation)
    no_ack: bool = False           # apply control but never ACK (uncertain outcome)


PASSCODE_PAYLOAD = b"\x00\x0a" + b"SIMPASS001"   # two-byte length wrapper + 10 bytes


class SimulatorServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        sauna: SimulatedSauna | None = None,
        faults: Faults | None = None,
        tick_interval: float = 1.0,
        push_state_every: float | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.sauna = sauna or SimulatedSauna()
        self.faults = faults or Faults()
        self.tick_interval = tick_interval
        self.push_state_every = push_state_every
        self._server: asyncio.AbstractServer | None = None
        self._tick_task: asyncio.Task | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.control_log: list[tuple[int, bytes]] = []
        self.connections_accepted = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._tick_task = asyncio.create_task(self._ticker())
        log.info("simulator listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.drop_all_connections()
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except asyncio.TimeoutError:
                pass

    async def drop_all_connections(self) -> None:
        for w in list(self._writers):
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass

    async def _ticker(self) -> None:
        pushed = 0.0
        while True:
            await asyncio.sleep(self.tick_interval)
            self.sauna.tick(self.tick_interval)
            if self.push_state_every:
                pushed += self.tick_interval
                if pushed >= self.push_state_every:
                    pushed = 0.0
                    for w in list(self._writers):
                        await self._write(w, P.encode_frame(P.Cmd.STATE_RSP, self.sauna.state_payload()))

    async def _write(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        f = self.faults
        if f.silent:
            return
        if f.garbage_before_response:
            data = f.garbage_before_response + data
        try:
            if f.fragment_bytes > 0:
                for i in range(0, len(data), f.fragment_bytes):
                    writer.write(data[i:i + f.fragment_bytes])
                    await writer.drain()
                    await asyncio.sleep(0.005)
            else:
                writer.write(data)
                await writer.drain()
        except (ConnectionError, RuntimeError):
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections_accepted += 1
        self._writers.add(writer)
        decoder = P.FrameDecoder()
        authenticated = False
        pending: list[bytes] = []
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                try:
                    frames = decoder.feed(data)
                except P.ProtocolError as exc:
                    log.warning("simulator: dropping client after protocol error: %s", exc)
                    break
                for frame in frames:
                    responses = self._respond(frame, authenticated)
                    if frame.cmd == P.Cmd.LOGIN_REQ and responses and P.login_succeeded(responses[0][len(P.FRAME_PREFIX) + 1 + 3:]):
                        authenticated = True
                    pending.extend(responses)
                if self.faults.coalesce:
                    await self._write(writer, b"".join(pending))
                else:
                    for r in pending:
                        await self._write(writer, r)
                pending.clear()
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except Exception:
                pass

    def _respond(self, frame: P.Frame, authenticated: bool) -> list[bytes]:
        f = self.faults
        if frame.cmd == P.Cmd.PASSCODE_REQ:
            return [P.encode_frame(P.Cmd.PASSCODE_RSP, PASSCODE_PAYLOAD)]
        if frame.cmd == P.Cmd.LOGIN_REQ:
            ok = (frame.payload == PASSCODE_PAYLOAD) and not f.reject_login
            return [P.encode_frame(P.Cmd.LOGIN_RSP, b"\x00" if ok else b"\x01")]
        if not authenticated:
            return []
        if frame.cmd == P.Cmd.PING:
            return [] if f.drop_pong else [P.encode_frame(P.Cmd.PONG, b"")]
        if frame.cmd == P.Cmd.STATE_REQ:
            return [] if f.drop_state else [P.encode_frame(P.Cmd.STATE_RSP, self.sauna.state_payload())]
        if frame.cmd == P.Cmd.CONTROL_REQ:
            self.control_log.append((frame.seq or 0, frame.payload))
            if not f.reject_control:
                self.sauna.apply_control(frame.payload)
            if f.no_ack:
                return []
            return [P.encode_frame(P.Cmd.CONTROL_ACK, b"\x00", seq=frame.seq)]
        log.info("simulator: unexpected command %s", frame.cmd_name)
        return []
