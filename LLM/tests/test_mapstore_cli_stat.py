# -*- coding: utf-8 -*-
r"""cli 通道（ssh.exe/scp.exe 子进程）的 stat 解析测试。

背景（2026-09-19 真机实测）：GNU ``stat -c`` **不解释**反斜杠转义，``stat -c "%Y\t%s"``
输出的是字面 ``1789309341\t124``；而 ``find -printf "%f\t%T@\t%s\n"`` 会解释。
旧实现把 ``stat`` 的输出按真 TAB 切分 → 永远切不出两段 → ``stat()`` 恒为 ``None``
→ ``read_with_meta()`` 把**每一张图**都判成"远程文件不存在"，cli 通道下地图**只列得出、
读不出**。本文件锁死这个分隔符口径。
"""
import pytest

from LLM.maps import mapstore


def _transport(monkeypatch, stdout: bytes):
    tr = mapstore._CliTransport()
    captured = {}

    def fake_run(cmd, stdin=None):
        captured["cmd"] = cmd
        return stdout

    monkeypatch.setattr(tr, "run", fake_run)
    return tr, captured


def test_cli_stat_parses_colon_separator(monkeypatch):
    tr, captured = _transport(monkeypatch, b"1789309341:124\n")
    assert tr.stat("/home/sunrise/Robot/ros2_car/maps/my_map3.yaml") == (1789309341.0, 124)
    # 命令里不能再用 \t：GNU stat -c 会原样吐出来
    assert "%Y:%s" in captured["cmd"]
    assert "\\t" not in captured["cmd"]


def test_cli_stat_reads_no_trailing_newline(monkeypatch):
    """远端也可能没有换行（strip 后照样要能解析）。"""
    tr, _ = _transport(monkeypatch, b"1789309341:124")
    assert tr.stat("/x.pgm") == (1789309341.0, 124)


def test_cli_stat_none_when_absent(monkeypatch):
    """文件不存在：`... || true` 下 stdout 为空 → None（不是抛异常）。"""
    tr, _ = _transport(monkeypatch, b"")
    assert tr.stat("/nope.pgm") is None


@pytest.mark.parametrize("bad", [b"garbage", b"1:2:3", b"abc:def"])
def test_cli_stat_none_on_malformed(monkeypatch, bad):
    tr, _ = _transport(monkeypatch, bad)
    assert tr.stat("/x.pgm") is None


def test_cli_stat_still_none_for_literal_backslash_t(monkeypatch):
    """防回归实拍样本：旧格式若被谁改回来，这里会解析失败（而不是悄悄当成两段）。"""
    tr, _ = _transport(monkeypatch, b"1789309341\\t124\n")
    assert tr.stat("/x.pgm") is None
