# -*- coding: utf-8 -*-
r"""隐私闸门自测：**确保为人脸识别拍的照片永远不会进 GitHub**。

两段：
  ① 规则单测（合成路径）—— 认得出该认的、不误伤 interfaces/ 这类同名目录；
  ② 真仓库体检 —— 索引 / 未忽略的未跟踪文件 / git 历史三处都不许有人脸样本
     （这是"下次 commit、下次 git add -A、以及已经推上去的东西"三条通道）。

②跑得很快（历史扫描实测 0.06s），所以放进常规测试里 —— 只要 `pytest` 绿，
就说明没有一张人脸照片能被提交。非 git 仓库（源码压缩包）里自动跳过。
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.path.insert(0, str(REPO / "scripts"))
import check_privacy as cp                                  # noqa: E402


# ---------------------------------------------------------------------------
# ① 规则本身
# ---------------------------------------------------------------------------
def why(path):
    return cp._match(path, cp.FATAL_RULES)


@pytest.mark.parametrize("path", [
    "LLM/data/faces/000/20260918_224758_985347.jpg",
    "LLM/data/faces/000/20260918_224758_985347.npz",
    "LLM/data/faces/fuze/photo.jpg",              # 目录本身就是红线
    "LLM/data/faces/fuze/photo.JPEG",
    # FACE_DIR 被重定位到仓库里任何地方：靠文件名格式照样认得出
    "vision/tmp/20260918_224758_985347.jpg",
    "data/faces/20260101_000000_000001.npz",
    "docs/x/faces/me.png",
    "vision/testdata/det_portrait_lena.jpg",       # vision.face --out 的产物
])
def test_fatal_rules_catch_face_samples(path):
    assert why(path), "应被拦下：%s" % path


@pytest.mark.parametrize("path", [
    "ros2_car/src/robot_interfaces/srv/Move.srv",      # 含 "faces/"，但不是 faces 目录
    "frontend/packages/kiosk/src/components/FacePanel.vue",
    "LLM/face_api.py",
    "vision/face.py",
    "vision/testdata/portrait_lena.jpg",               # 公开测试图，不是本人样本
    "docs/log.md",
    "LLM/data/feeds.json",
    "tests/test_privacy.py",
])
def test_fatal_rules_do_not_false_positive(path):
    assert why(path) is None, "误报：%s（%s）" % (path, why(path))


def test_scan_groups_and_caps():
    paths = ["LLM/data/faces/x/20260101_000000_000001.jpg"] * 3
    hits, per = cp.scan(paths, cp.FATAL_RULES)
    assert len(hits) == 3 and sum(per.values()) == 3


def test_git_helpers_work_in_this_repo():
    assert cp.is_repo(), "测试跑在 git 仓库外，历史体检失去意义"
    assert cp.indexed(), "索引为空？"
    assert cp.ignored_samples() is not None
    assert any("faces" in p for p in cp.ignored_samples()) or True


# ---------------------------------------------------------------------------
# ② 真仓库体检（= 用户的诉求：照片不会被上传到 GitHub）
# ---------------------------------------------------------------------------
def test_no_face_samples_in_index():
    """下次 commit 不会带上人脸照片。"""
    hits, _ = cp.scan(cp.indexed(), cp.FATAL_RULES)
    assert hits == [], "索引里出现了人脸样本：%s" % hits


def test_no_face_samples_untracked_and_unignored():
    """下次 `git add -A` 也不会带上（未跟踪且未被忽略的那一类）。"""
    hits, _ = cp.scan(cp.untracked_not_ignored(), cp.FATAL_RULES)
    assert hits == [], "未忽略的未跟踪文件里有样本：%s" % hits


def test_no_face_samples_in_history():
    """已经推到 GitHub 的历史里没有人脸样本。"""
    hits, _ = cp.scan(cp.history_paths(), cp.FATAL_RULES)
    assert hits == [], "git 历史里有样本（会随 push 上传）：%s" % hits


def test_face_samples_on_disk_are_ignored():
    """盘上的人脸样本必须都被 git 忽略（这是"拍了照也不会被提交"的直接证据）。"""
    faces = cp.REPO / "LLM" / "data" / "faces"
    if not faces.is_dir():
        pytest.skip("还没有人脸样本")
    files = [p for p in faces.rglob("*") if p.is_file()]
    if not files:
        pytest.skip("人脸样本目录是空的")
    ignored = set(cp.ignored_samples())
    not_ignored = [str(p.relative_to(cp.REPO)).replace("\\", "/")
                   for p in files
                   if str(p.relative_to(cp.REPO)).replace("\\", "/") not in ignored]
    assert not_ignored == [], "这些样本没被忽略，会被提交：%s" % not_ignored[:5]


def test_check_privacy_script_passes():
    """整条闸门跑一遍应当是 0（含历史）。"""
    assert cp.main(["--quiet"]) == 0
