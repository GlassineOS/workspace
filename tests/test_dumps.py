# SPDX-License-Identifier: GPL-2.0-or-later
"""The dump here is written by hand in the format's shape. No real dump is ever a test input in this repository."""
import json
import os
import stat
from pathlib import Path

import pytest

TIME = 1800000000.5
# Written from the kernel's format strings, not captured on a device.
PREEMPT_LINE = "a5xx: hang detect: hw rb {hw} (CP_RB_BASE 1000), cur rb {cur}, next rb {next}, preempt state {state}\n"
BLAME_LINE = "a5xx: hw is on rb {ring}, not rb {cur}: blaming rb {ring}\n"
FAULT_LINE = "gpu fault ring {ring} fence 1a status 00000000 rb 0010/0020 ib1 0000000000001000/0020 ib2 0000000000002000/0010\n"


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
    log = PREEMPT_LINE.format(hw=2, cur=2, next=-1, state=3) + FAULT_LINE.format(ring=2)
    id_ = add(ws, "--log", ws.file("dmesg", log))
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


def logged(ws, text):
    return record(ws, add(ws, "--log", ws.file("dmesg", text)))["dump"]


def test_a_preempt_line_with_no_next_ring_still_gives_the_preempt_state(ws):
    d = logged(ws, PREEMPT_LINE.format(hw=1, cur=-1, next=-1, state=2))
    assert d["preempt_state"] == 2 and "ring" not in d


def test_the_ring_is_the_one_the_kernel_blames_not_cur_rb(ws):
    d = logged(ws, PREEMPT_LINE.format(hw=1, cur=0, next=1, state=2) + BLAME_LINE.format(ring=1, cur=0) + FAULT_LINE.format(ring=1))
    assert (d["ring"], d["preempt_state"]) == (1, 2)


def test_a_lockup_names_the_ring(ws):
    assert logged(ws, "a5xx: hangcheck detected gpu lockup rb 3!\n")["ring"] == 3


def test_a_ring_of_minus_one_is_unknown(ws):
    d = logged(ws, FAULT_LINE.format(ring=-1))
    assert "ring" not in d
    assert "ring=unknown" in ws.tool("dumps", "group").stdout


def test_a_log_with_two_faults_leaves_what_they_disagree_on_unknown_and_says_so(ws):
    log = ws.file("dmesg", PREEMPT_LINE.format(hw=0, cur=0, next=-1, state=1) + FAULT_LINE.format(ring=0)
                  + PREEMPT_LINE.format(hw=2, cur=2, next=-1, state=3) + FAULT_LINE.format(ring=2))
    done = ws.tool("dumps", "add", write_dump(ws), "--mesa", "26.0", "--profile", "short", "--ran", "x", "--log", log)
    d = record(ws, done.stdout.strip())["dump"]
    assert "ring" not in d and "preempt_state" not in d
    assert "more than one ring (0, 2)" in done.stderr and "--ring says which" in done.stderr
    assert "more than one preempt_state (1, 3)" in done.stderr and "--preempt-state says which" in done.stderr


def test_a_lockup_and_a_later_fault_on_another_ring_dont_mix(ws):
    d = logged(ws, "a5xx: hangcheck detected gpu lockup rb 1!\n" + PREEMPT_LINE.format(hw=2, cur=2, next=0, state=3) + FAULT_LINE.format(ring=2))
    assert "ring" not in d and d["preempt_state"] == 3


def test_a_value_repeated_in_a_log_is_one_value(ws):
    line = PREEMPT_LINE.format(hw=2, cur=2, next=-1, state=3)
    d = logged(ws, line + FAULT_LINE.format(ring=2) + line)
    assert (d["ring"], d["preempt_state"]) == (2, 3)


def test_a_flag_settles_what_a_log_disagrees_on(ws):
    log = ws.file("dmesg", FAULT_LINE.format(ring=0) + FAULT_LINE.format(ring=2))
    done = ws.tool("dumps", "add", write_dump(ws), "--mesa", "26.0", "--profile", "short", "--ran", "x", "--log", log, "--ring", "2")
    assert record(ws, done.stdout.strip())["dump"]["ring"] == 2 and "more than one ring" not in done.stderr


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


def add_ram(ws, *more, until="2999-01-01", build="kernel-build", image="DDRCS0_0.BIN", log="Booting Linux\n", code=0):
    return ws.tool("dumps", "add-ram", ws.file(image, f"raw {image}\n"), "--build", build, "--kernel-log", ws.file("klog", log),
                   "--until", until, "--ran", "froze overnight", *more, code=code)


def test_adding_a_ram_dump_again_with_the_same_date_and_build_changes_nothing_and_says_so(ws):
    ws.add("kernel-build", keep="always")
    add_ram(ws)
    before = manifest(ws)
    out = add_ram(ws).stdout
    assert out.count("already stored") == 2 and "kept until 2999-01-01" in out
    assert manifest(ws) == before and (ws.tmp / "DDRCS0_0.BIN").exists() and (ws.tmp / "klog").exists()


@pytest.mark.parametrize("change", [{"until": "2000-01-01"}, {"build": "other-build"}, {"log": "another log\n"}])
def test_adding_a_ram_image_again_with_another_date_build_or_log_is_refused(ws, change):
    ws.add("kernel-build", keep="always")
    ws.add("other-build", keep="always")
    add_ram(ws)
    before = manifest(ws)
    err = add_ram(ws, **change, code=1).stderr
    assert "already stored" in err and "kernel-build" in err and "nothing was changed" in err
    assert manifest(ws) == before and (ws.tmp / "DDRCS0_0.BIN").exists() and (ws.tmp / "klog").exists()


def test_a_ram_log_stored_with_another_build_is_refused_for_a_new_image(ws):
    ws.add("kernel-build", keep="always")
    ws.add("other-build", keep="always")
    add_ram(ws)
    before = manifest(ws)
    err = add_ram(ws, build="other-build", image="DDRCS1_0.BIN", code=1).stderr
    assert "already stored with build kernel-build" in err
    assert manifest(ws) == before and (ws.tmp / "DDRCS1_0.BIN").exists()


def test_a_second_image_can_join_a_stored_ram_log(ws):
    ws.add("kernel-build", keep="always")
    add_ram(ws)
    add_ram(ws, image="DDRCS1_0.BIN")
    assert sorted(e["dump"]["kind"] for e in manifest(ws)) == ["ram", "ram", "ram-log"]


def test_a_stored_dump_is_read_only_and_the_folder_private(ws):
    id_ = add(ws)
    assert stat.S_IMODE(Path(ws.art("path", id_).stdout.strip()).stat().st_mode) == 0o400
    assert stat.S_IMODE((ws.artifacts / "dumps").stat().st_mode) == 0o700


def test_show_refuses_what_isnt_a_dump(ws):
    ws.add("a.bin")
    assert "isn't a dump" in ws.tool("dumps", "show", "a.bin", code=1).stderr
