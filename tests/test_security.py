import json
import os

import pytest

from recoveryroom_sauna.security import SecurityStateError, SessionStore, read_private_json, write_private_json


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def store(tmp_path, clock=None, **kw):
    return SessionStore(tmp_path / "private", clock=clock or Clock(), **kw)


def test_private_dir_and_files_have_owner_only_permissions(tmp_path):
    s = store(tmp_path)
    s.open_pairing()
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "private" / "pairing.json").stat().st_mode & 0o777 == 0o600
    data = json.loads((tmp_path / "private" / "pairing.json").read_text())
    assert "pin" not in data and len(data["pin_hash"]) == 64


def test_pin_pairing_is_single_use_and_closes(tmp_path):
    s = store(tmp_path)
    pin, _ = s.open_pairing()
    assert s.pairing_open()
    token = s.redeem_pin(pin, "shop mac")
    assert token and len(token) > 32
    assert not s.pairing_open()
    assert s.redeem_pin(pin, "again") is None
    sess = s.authenticate(token)
    assert sess is not None and sess.label == "shop mac"
    raw = (tmp_path / "private" / "sessions.json").read_text()
    assert token not in raw                                  # only hashes on disk


def test_wrong_pin_attempts_are_limited(tmp_path):
    s = store(tmp_path, pin_lifetime_minutes=10, max_attempts=3)
    pin, _ = s.open_pairing()
    wrong = "000000" if pin != "000000" else "111111"
    for _ in range(3):
        assert s.redeem_pin(wrong, "x") is None
    assert not s.pairing_open()
    assert s.redeem_pin(pin, "x") is None                    # locked out even with the right PIN


def test_pin_expires(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock=clock, pin_lifetime_minutes=10)
    pin, _ = s.open_pairing()
    clock.t += 601
    assert not s.pairing_open() and s.redeem_pin(pin, "x") is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "12345a", 123456, None])
def test_malformed_pin_rejected(tmp_path, bad):
    s = store(tmp_path)
    s.open_pairing()
    assert s.redeem_pin(bad, "x") is None


def test_session_expiry_and_revocation(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock=clock, session_lifetime_days=1)
    pin, _ = s.open_pairing()
    t1 = s.redeem_pin(pin, "a")
    pin, _ = s.open_pairing()
    t2 = s.redeem_pin(pin, "b")
    assert len(s.list_sessions()) == 2
    sid = s.authenticate(t1).id
    assert s.revoke(sid) and s.authenticate(t1) is None and s.authenticate(t2) is not None
    assert not s.revoke("nope")
    clock.t += 86401
    assert s.authenticate(t2) is None and s.list_sessions() == []


def test_bogus_tokens_rejected(tmp_path):
    s = store(tmp_path)
    assert s.authenticate(None) is None
    assert s.authenticate("x" * 200) is None
    assert s.authenticate("not-a-real-token") is None


def test_corrupt_sessions_file_fails_closed(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    p = priv / "sessions.json"
    p.write_text("{corrupt")
    os.chmod(p, 0o600)
    with pytest.raises(SecurityStateError, match="corrupt"):
        store(tmp_path)
    assert p.read_text() == "{corrupt"                      # evidence preserved


def test_world_readable_private_file_refused(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    p = priv / "sessions.json"
    p.write_text(json.dumps({"version": 1, "sessions": {}}))
    os.chmod(p, 0o644)
    with pytest.raises(SecurityStateError, match="readable by others"):
        store(tmp_path)


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p = tmp_path / "priv" / "x.json"
    write_private_json(p, {"a": 1})
    write_private_json(p, {"a": 2})
    assert read_private_json(p) == {"a": 2}
    assert sorted(os.listdir(p.parent)) == ["x.json"]
