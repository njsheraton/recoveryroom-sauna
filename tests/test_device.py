import asyncio

import pytest

from conftest import run
from recoveryroom_sauna import protocol as P
from recoveryroom_sauna.device import ConnState, LanDeviceClient, WritesDisabledError
from recoveryroom_sauna.simulator import Faults, SimulatedSauna, SimulatorServer


async def wait_until(pred, timeout=8.0, step=0.05):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def fast_client(port, **kw):
    return LanDeviceClient("127.0.0.1", port, heartbeat_interval=0.2, poll_interval=0.2,
                           read_timeout=1.0, backoff_initial=0.1, backoff_max=0.3, **kw)


def test_connects_logs_in_and_observes_fresh_state():
    async def main():
        sim = SimulatorServer(sauna=SimulatedSauna(power=True, target_f=176, timer_seconds=1800, current_f=140), tick_interval=0.1)
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            assert await wait_until(lambda: c.snapshot().fresh)
            snap = c.snapshot()
            assert snap.state == ConnState.CONNECTED and snap.observation.report.power
            assert snap.observation.report.target_c == 80 and snap.observation.report.current_c == 60
            assert snap.to_dict()["writesEnabled"] is False
            # observation ids advance with each new report
            first = snap.observation.id
            obs = await c.wait_for_newer_observation(first, 3.0)
            assert obs is not None and obs.id > first
            assert sim.control_log == []           # nothing was ever written
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_fragmented_and_coalesced_responses_still_decode():
    for faults in (Faults(fragment_bytes=3), Faults(coalesce=True)):
        async def main(faults=faults):
            sim = SimulatorServer(faults=faults, tick_interval=0.1)
            await sim.start()
            c = fast_client(sim.port)
            await c.start()
            try:
                assert await wait_until(lambda: c.snapshot().fresh)
                assert c.snapshot().protocol_errors == 0
            finally:
                await c.stop(); await sim.stop()
        run(main())


def test_reconnects_after_connection_drop():
    async def main():
        sim = SimulatorServer(tick_interval=0.1)
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            assert await wait_until(lambda: c.snapshot().fresh)
            await sim.drop_all_connections()
            assert await wait_until(lambda: c.snapshot().reconnects >= 1 and c.snapshot().fresh)
            assert sim.connections_accepted >= 2
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_silent_device_is_not_fresh_and_open_socket_is_not_health():
    async def main():
        sim = SimulatorServer(faults=Faults(silent=True), tick_interval=0.1)
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            await asyncio.sleep(1.5)
            snap = c.snapshot()
            assert not snap.fresh and snap.observation is None
            assert snap.state != ConnState.CONNECTED
            assert sim.connections_accepted >= 1
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_state_silence_after_login_goes_stale_then_reconnects():
    async def main():
        sim = SimulatorServer(tick_interval=0.1)
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            assert await wait_until(lambda: c.snapshot().fresh)
            sim.faults.drop_state = True
            # pongs keep flowing but no state: within read timeout the client must drop it.
            from recoveryroom_sauna import device as D
            old = D.LAN_FRESHNESS_SECONDS
            D.LAN_FRESHNESS_SECONDS = 0.6
            try:
                assert await wait_until(lambda: c.snapshot().reconnects >= 1, timeout=6)
            finally:
                D.LAN_FRESHNESS_SECONDS = old
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_login_rejection_is_reported_not_fresh():
    async def main():
        sim = SimulatorServer(faults=Faults(reject_login=True), tick_interval=0.1)
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            assert await wait_until(lambda: "rejected" in (c.snapshot().last_error or ""))
            assert not c.snapshot().fresh
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_garbage_causes_protocol_error_and_reconnect():
    async def main():
        sim = SimulatorServer(faults=Faults(garbage_before_response=b"\xff\xff"), tick_interval=0.1)
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            assert await wait_until(lambda: c.snapshot().protocol_errors >= 1)
            assert not c.snapshot().fresh
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_impossible_state_is_rejected():
    async def main():
        sim = SimulatorServer(sauna=SimulatedSauna(timer_seconds=24 * 3600), tick_interval=0.1)   # set_hour=24 impossible
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            assert await wait_until(lambda: c.snapshot().protocol_errors >= 1)
            assert c.snapshot().observation is None
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_control_writes_are_refused():
    async def main():
        sim = SimulatorServer(tick_interval=0.1)
        await sim.start()
        c = fast_client(sim.port)
        await c.start()
        try:
            assert await wait_until(lambda: c.snapshot().fresh)
            with pytest.raises(WritesDisabledError):
                await c.send_control(P.build_flag_control(P.Flag.POWER, True))
            with pytest.raises(NotImplementedError):
                await fast_client(sim.port, writes_enabled=True).send_control(b"\x00")
            await asyncio.sleep(0.3)
            assert sim.control_log == []
        finally:
            await c.stop(); await sim.stop()
    run(main())


def test_refresh_requests_new_read_without_writing():
    async def main():
        sim = SimulatorServer(tick_interval=0.1)
        await sim.start()
        c = LanDeviceClient("127.0.0.1", sim.port, heartbeat_interval=0.5, poll_interval=5.0, read_timeout=2.0)
        await c.start()
        try:
            assert await wait_until(lambda: c.snapshot().fresh)
            before = c.snapshot().observation.id
            c.request_refresh()
            obs = await c.wait_for_newer_observation(before, 2.0)
            assert obs is not None and obs.id == before + 1
            assert sim.control_log == []
        finally:
            await c.stop(); await sim.stop()
    run(main())
