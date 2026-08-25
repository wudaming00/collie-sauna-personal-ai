"""The interview demo is a clean profile and a reversible launcher, not seeded user state."""


def test_prepare_without_launch_uses_only_the_isolated_profile_and_restores_env(tmp_path, monkeypatch):
    from harness import demo_ready

    profile = tmp_path / "interview-profile"
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(demo_ready, "_MANIFEST", str(manifest))
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "ordinary-profile"))
    monkeypatch.setenv("COLLIE_SAUNA_DIR", str(tmp_path / "ordinary-sauna"))

    assert demo_ready.prepare(state_dir=str(profile), launch=False) == 0
    assert (profile / "personal.db").exists()
    # The authoritative check is the real process environment; no-launch callers keep their scope.
    import os
    assert os.environ["COLLIE_STATE_DIR"] == str(tmp_path / "ordinary-profile")
    assert os.environ["COLLIE_SAUNA_DIR"] == str(tmp_path / "ordinary-sauna")
    assert manifest.exists()


def test_readiness_rejects_any_foreign_or_test_data(tmp_path, monkeypatch):
    from harness import demo_ready, demo_seed
    from harness.executive import Executive
    from harness.personal_state import PersonalState
    from harness.sauna import SaunaClient

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    state = PersonalState(str(tmp_path / "personal.db"))
    executive = Executive(state)
    sauna = SaunaClient(state, cloud_dir=str(tmp_path / "sauna"))
    demo_seed.seed(state, executive, sauna)
    state.add_task("[Test] should never be shown tomorrow")

    checks = demo_ready.scenario_checks(state, sauna)
    failed = {row["label"] for row in checks if row["level"] == "fail"}
    assert "profile contains demo data only" in failed
    assert "no test labels can leak into the interview" in failed
    state.close()


def test_demo_server_stop_targets_only_the_recorded_pid(monkeypatch):
    from harness import demo_ready

    killed = []
    monkeypatch.setattr(demo_ready.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(demo_ready, "_wait_for", lambda predicate, wanted, timeout=8.0: True)

    assert demo_ready._stop_server({"server_pid": 4242, "port": 8878})
    assert killed == [(4242, demo_ready.signal.SIGTERM)]
    assert not demo_ready._stop_server({"port": 8878})


def test_no_launch_does_not_orphan_an_active_demo_manifest(tmp_path, monkeypatch):
    from harness import demo_ready

    profile = tmp_path / "interview-profile"
    manifest = tmp_path / "manifest.json"
    active = {"active": True, "state_dir": "running", "server_pid": 77, "port": 8878}
    manifest.write_text(__import__("json").dumps(active), encoding="utf-8")
    monkeypatch.setattr(demo_ready, "_MANIFEST", str(manifest))

    assert demo_ready.prepare(state_dir=str(profile), launch=False) == 0
    assert __import__("json").loads(manifest.read_text(encoding="utf-8")) == active


def test_windowless_server_breaks_away_from_short_lived_launcher(monkeypatch):
    from harness import wallpaper

    calls = []
    class Child:
        pid = 4321
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Child()

    monkeypatch.setattr(wallpaper.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wallpaper, "pythonw", lambda: r"C:\Collie\pythonw.exe")
    monkeypatch.setattr(wallpaper, "_pkg_parent", lambda: r"C:\Collie\pkg")
    monkeypatch.setattr(wallpaper, "_collie_home", lambda: r"C:\Collie\state")
    monkeypatch.setattr(wallpaper.subprocess, "Popen", popen)

    assert wallpaper.start_server_windowless(8878) == 4321
    flags = calls[0][1]["creationflags"]
    assert flags & 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
    assert flags & 0x00000008  # DETACHED_PROCESS
    assert flags & 0x08000000  # CREATE_NO_WINDOW
    assert calls[0][1]["close_fds"] is True


def test_windowless_server_falls_back_when_parent_job_forbids_breakaway(monkeypatch):
    from harness import wallpaper

    flags = []
    class Child:
        pid = 98
    def popen(_command, **kwargs):
        flags.append(kwargs["creationflags"])
        if len(flags) == 1:
            raise OSError("breakaway denied")
        return Child()

    monkeypatch.setattr(wallpaper.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wallpaper, "pythonw", lambda: "pythonw")
    monkeypatch.setattr(wallpaper, "_pkg_parent", lambda: "pkg")
    monkeypatch.setattr(wallpaper, "_collie_home", lambda: "state")
    monkeypatch.setattr(wallpaper.subprocess, "Popen", popen)

    assert wallpaper.start_server_windowless(8878) == 98
    assert flags[0] & 0x01000000 and not (flags[1] & 0x01000000)
    assert flags[1] & 0x00000008 and flags[1] & 0x08000000
