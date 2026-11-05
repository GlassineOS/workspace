# SPDX-License-Identifier: GPL-2.0-or-later
"""The dump here is written by hand in the format's shape. No real dump is ever a test input in this repository."""
import json
import os
import stat
from pathlib import Path

import pytest

TIME = 1800000000.5


def dump_text(rbbm="0x12345678", ib1=0x20, ib2=0x10, comm="app", kernel="7.0-test", time=TIME, revision="509 (05000900)"):
    return f"""kgsl-or-msm dump
kernel: {kernel}
module: msm
time: {time}
comm: {comm}
cmdline: /usr/bin/{comm} --flag
revision: {revision}
rbbm-status: {rbbm}
ringbuffer:
  - id: 0
    rptr: 1
bos:
  - iova: 0x1000
registers:
  - {{ offset: 0x2c84, value: 0x{ib1:x} }}
  - {{ offset: 0x2c90, value: 0x{ib2:x} }}
registers-hlsq:
  - {{ offset: 0x2c84, value: 0xdead }}
"""


def write_dump(ws, name="devcd", **kw):
    path = ws.tmp / name
    path.write_text(dump_text(**kw))
    return path


def add(ws, *args, name="devcd", code=0, **kw):
    done = ws.tool("dumps", "add", write_dump(ws, name, **kw), "--mesa", "26.0", "--profile", "short", "--ran", "tapped a link", *args, code=code)
    return done.stdout.split("\n")[0].strip()


def record(ws, id_):
    for line in (ws.artifacts / "dumps" / "manifest.jsonl").read_text().splitlines():
        entry = json.loads(line)
        if entry["id"] == id_:
            return entry
    raise AssertionError(id_)


def fake_decoder(ws, mesa="26.0", *more):
    path = ws.tmp / f"fake-{mesa}"
    path.write_text('#!/bin/sh\necho "decoder ' + mesa + ' args: $*"\nwhile [ $# -gt 0 ]; do [ "$1" = -f ] && head -n 1 "$2"; shift; done\n')
    path.chmod(0o755)
    ws.tool("dumps", "decoder", path, "--mesa", mesa, *more)
    return path


def test_add_stores_a_fault_with_what_the_dump_says_and_what_it_cant(ws):
    id_ = add(ws, "--log", ws.file("dmesg", "hw rb 2, cur rb 2, next rb 0, preempt state 3\n"))
    e = record(ws, id_)
    d = e["dump"]
    assert (d["kernel"], d["app"], d["time"], d["time_from"]) == ("7.0-test", "app", TIME, "dump")
    assert (d["ran"], d["mesa"], d["profile"]) == ("tapped a link", "26.0", "short")
    assert (d["rbbm_status"], d["ib1_rem"], d["ib2_rem"], d["ring"], d["preempt_state"]) == ("0x12345678", "0x20", "0x10", 2, 3)
    assert e["class"] == "user-data" and e["keep"] == "always" and e["name"] == f"gpu-{id_}"
    assert not (ws.tmp / "devcd").exists()
    assert Path(ws.art("path", id_).stdout.strip()).is_relative_to(ws.artifacts / "dumps")
    assert (ws.tmp / "dmesg").exists()  # the log may cover a second dump of the event
    assert ws.art("path", d["log"]).stdout


def test_mesa_profile_and_what_ran_are_required(ws):
    path = write_dump(ws)
    for missing in (["--profile", "p", "--ran", "r"], ["--mesa", "m", "--ran", "r"], ["--mesa", "m", "--profile", "p"]):
        ws.tool("dumps", "add", path, *missing, code=2)
    assert path.exists()


def test_a_field_the_dump_lacks_is_unknown_and_flags_can_supply_it(ws):
    id_ = add(ws)
    assert "ring" not in record(ws, id_)["dump"]
    assert "ring=unknown preempt=unknown" in ws.tool("dumps", "group").stdout
    other = add(ws, "--ring", "1", "--preempt-state", "0", "--app", "innocent", "--kernel", "other", rbbm="0x1")
    d = record(ws, other)["dump"]
    assert (d["ring"], d["preempt_state"], d["app"], d["kernel"]) == (1, 0, "innocent", "other")


def test_a_file_that_isnt_a_dump_is_stored_with_unknowns_and_says_so(ws):
    path = ws.file("truncated", "cut off\n")
    done = ws.tool("dumps", "add", path, "--mesa", "unknown", "--profile", "unknown", "--ran", "x")
    assert "rbbm_status" in done.stderr
    d = record(ws, done.stdout.strip())["dump"]
    assert d["time_from"] == "file date"


def test_other_gpu_families_leave_the_registers_unknown(ws):
    assert "ib1_rem" not in record(ws, add(ws, revision="630 (06030000)"))["dump"]


def test_show_is_one_command_and_keeps_nothing(ws):
    id_ = add(ws)
    fake_decoder(ws)
    before = sorted(p.name for p in (ws.artifacts / "dumps").iterdir())
    out = ws.tool("dumps", "show", id_).stdout
    assert out.startswith("decoder 26.0 args: -f ") and "kgsl-or-msm dump" in out
    assert "-v" in ws.tool("dumps", "show", id_, "--verbose").stdout.split("\n")[0]
    assert sorted(p.name for p in (ws.artifacts / "dumps").iterdir()) == before


def test_show_refuses_the_wrong_mesa_unless_told(ws):
    id_ = add(ws)
    fake_decoder(ws, "25.1")
    err = ws.tool("dumps", "show", id_, code=1).stderr
    assert "no decoder crashdec-mesa-26.0" in err and "crashdec-mesa-25.1" in err
    assert "decoder 25.1" in ws.tool("dumps", "show", id_, "--decoder", "crashdec-mesa-25.1").stdout


def test_show_says_when_the_decoder_fails_or_its_file_is_gone(ws):
    id_ = add(ws)
    bad = ws.file("bad", "#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    ws.tool("dumps", "decoder", bad, "--mesa", "26.0")
    assert "exited 3" in ws.tool("dumps", "show", id_, code=1).stderr
    decoder = Path(ws.art("path", "crashdec-mesa-26.0").stdout.strip())
    decoder.unlink()
    assert "missing" in ws.tool("dumps", "show", id_, code=1).stderr


def test_a_decoder_must_be_executable_and_is_an_artifact_with_its_version(ws):
    ws.tool("dumps", "decoder", ws.file("plain"), "--mesa", "1", code=1)
    fake_decoder(ws, "26.0", "--image", "alpine:test")
    e = json.loads(ws.art("find", "--json", "crashdec").stdout)
    assert e["name"] == "crashdec-mesa-26.0" and e["decoder"] == {"mesa": "26.0", "image": "alpine:test"}
    assert e["folder"] == "dumps" and os.access(ws.art("path", e["name"]).stdout.strip(), os.X_OK)


def test_an_image_decoder_runs_in_a_container_with_no_network_and_read_only_mounts(ws, monkeypatch):
    bin_dir = ws.tmp / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\necho "docker $*"\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    id_ = add(ws)
    fake_decoder(ws, "26.0", "--image", "alpine:test")
    out = ws.tool("dumps", "show", id_).stdout
    assert "--network none" in out and ":/dump:ro" in out and ":/usr/local/bin/crashdec:ro" in out
    assert "alpine:test crashdec -f /dump" in out


def test_group_fingerprints_and_keeps_ib2_and_shader_hash_out_of_it(ws):
    state = ["--ring", "2", "--preempt-state", "3"]
    a = add(ws, *state, "--shader-hash", "abcd", ib2=1)
    b = add(ws, *state, ib2=2, time=TIME + 1000)
    c = add(ws, *state, ib1=0x99, time=TIME + 2000)
    d = add(ws, *state, rbbm="0x1", time=TIME + 3000)
    out = ws.tool("dumps", "group").stdout
    assert "rbbm=0x12345678 ib1_rem=0x20 ring=2 preempt=3: 2" in out
    assert "ib1_rem=0x99 ring=2 preempt=3: 1" in out and "rbbm=0x00000001" in out
    assert "shader candidate abcd" in out and "ib2_rem=0x1" in out and "ib2_rem=0x2" in out
    rows = json.loads(ws.tool("dumps", "group", "--json").stdout)
    assert [sorted(x["id"] for x in r["dumps"]) for r in rows if len(r["dumps"]) == 2] == [sorted([a, b])]
    assert {c, d} <= {x["id"] for r in rows for x in r["dumps"]}


def test_dumps_close_in_time_are_one_event_and_a_link_can_be_named(ws):
    first = add(ws)
    second = add(ws, time=TIME + 5, comm="innocent")
    far = add(ws, time=TIME + 5000)
    named = add(ws, "--event-with", far, time=TIME + 90000)
    other_kernel = add(ws, kernel="other", time=TIME + 1)
    event = lambda i: record(ws, i)["dump"]["event"]
    assert event(first) == first and event(second) == first
    assert event(far) == far and event(named) == far and event(other_kernel) == other_kernel
    assert f"event {first}" in ws.tool("dumps", "group").stdout


def test_event_with_must_be_a_gpu_dump(ws):
    ws.add("a.bin")
    add(ws, "--event-with", "a.bin", code=1)


def ram(ws, *more, until="2999-01-01", images=("DDRCS0_0.BIN", "DDRCS1_0.BIN")):
    ws.add("kernel-build", keep="always")
    paths = [ws.file(name, f"raw {name}\n") for name in images]
    return ws.tool("dumps", "add-ram", *paths, "--build", "kernel-build", "--kernel-log", ws.file("klog", "Booting Linux\n"),
                   "--until", until, "--ran", "froze overnight", *more)


def manifest(ws):
    return [json.loads(line) for line in (ws.artifacts / "dumps" / "manifest.jsonl").read_text().splitlines()]


def test_a_ram_dump_keeps_its_log_always_and_its_images_until_the_date(ws):
    ram(ws)
    by_kind = {}
    for e in manifest(ws):
        by_kind.setdefault(e["dump"]["kind"], []).append(e)
    [log] = by_kind["ram-log"]
    images = by_kind["ram"]
    assert len(images) == 2 and log["keep"] == "always" and all(e["keep"] == "2999-01-01" for e in images)
    assert all(e["dump"]["build"] == "kernel-build" and e["dump"]["log"] == log["name"] for e in images)
    assert log["from"] == ["kernel-build"]


def test_a_ram_dump_is_its_own_kind_and_not_a_gpu_fault(ws):
    ram(ws)
    assert ws.tool("dumps", "group").stdout == ""
    log = next(e for e in manifest(ws) if e["dump"]["kind"] == "ram-log")
    image = next(e for e in manifest(ws) if e["dump"]["kind"] == "ram")
    for ref in (log["id"], image["id"]):
        out = ws.tool("dumps", "show", ref).stdout
        assert "Booting Linux" in out and "kernel-build" in out and "froze overnight" in out


def test_a_raw_image_goes_after_its_date_while_the_log_and_build_stay(ws):
    ram(ws, until="2000-01-01")
    image = next(e for e in manifest(ws) if e["dump"]["kind"] == "ram")
    log = next(e for e in manifest(ws) if e["dump"]["kind"] == "ram-log")
    assert image["name"] in ws.art("find", "--expired").stdout
    ws.art("rm", image["name"], "--why", "its date passed")
    assert "removed" in ws.tool("dumps", "show", log["id"]).stdout
    err = ws.art("rm", log["name"], "--why", "x", code=1).stderr
    assert "keep: always" in err
    assert "artifact " + log["name"] in ws.art("rm", "kernel-build", "--why", "x", code=1).stderr


def test_a_ram_dump_with_an_unknown_build_moves_nothing(ws):
    image, klog = ws.file("img"), ws.file("klog", "x")
    ws.tool("dumps", "add-ram", image, "--build", "nope", "--kernel-log", klog, "--until", "2999-01-01", "--ran", "x", code=1)
    assert image.exists() and klog.exists()


@pytest.mark.parametrize("log,until", [("", "2999-01-01"), ("x", "soon")])
def test_a_ram_dump_needs_a_log_and_a_date(ws, log, until):
    ws.add("kernel-build")
    image, klog = ws.file("img"), ws.file("klog", log)
    ws.tool("dumps", "add-ram", image, "--build", "kernel-build", "--kernel-log", klog, "--until", until, "--ran", "x", code=1)
    assert image.exists() and klog.exists()


def test_a_ram_dump_with_a_bad_image_name_moves_nothing(ws):
    ws.add("kernel-build")
    image, klog = ws.file("bad name"), ws.file("klog", "x")
    ws.tool("dumps", "add-ram", image, "--build", "kernel-build", "--kernel-log", klog, "--until", "2999-01-01", "--ran", "x", code=1)
    assert image.exists() and klog.exists()


def test_a_stored_dump_is_read_only_and_the_folder_private(ws):
    id_ = add(ws)
    assert stat.S_IMODE(Path(ws.art("path", id_).stdout.strip()).stat().st_mode) == 0o400
    assert stat.S_IMODE((ws.artifacts / "dumps").stat().st_mode) == 0o700


def test_show_refuses_what_isnt_a_dump(ws):
    ws.add("a.bin")
    assert "isn't a dump" in ws.tool("dumps", "show", "a.bin", code=1).stderr
