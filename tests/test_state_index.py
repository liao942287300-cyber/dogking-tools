# -*- coding: utf-8 -*-
"""
set_index() 测试（顺序游标定位）

音频分段的「跳到第 N 段」和通用的「重置计数」共用这一条路径：
  - 重置 = set_index(node, 0)
  - 跳到第 N 段 = set_index(node, N - 1)

守四件事：

  ① **不能把脏数据写成新记录** —— 节点从没跑过时 state 里没它，
     这时应该"没做事"返回 False，而不是凭空造一条 index 出来。
  ② **负数 / 非数字别越界** —— 游标是下标，负下去会 slice 出奇怪的段。
  ③ **不能碰别人的字段** —— audio_sig 要留着，否则改游标会顺带把
     "切点变了就从头" 的判断搞乱。
  ④ **改完游标，加载节点真的从那段开始** —— 只测 state 里的数字不算，
     要拿真实节点的行为验证。
"""

import importlib.util
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_nodes():
    spec = importlib.util.spec_from_file_location("dk_nodes_idx", os.path.join(HERE, "nodes.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nodes = _load_nodes()


@pytest.fixture()
def state(tmp_path):
    """每个用例一个干净的 state.json。"""
    nodes.STATE_FILE = str(tmp_path / "state.json")
    return nodes


def _seed(**records):
    nodes._save_state(records)


# ---------------------------------------------------------------- 基本写入

def test_sets_index(state):
    _seed(n1={"index": 5})
    assert nodes.set_index("n1", 2) is True
    assert nodes._load_state()["n1"]["index"] == 2


def test_zero_is_reset(state):
    _seed(n1={"index": 7})
    nodes.set_index("n1", 0)
    assert nodes._load_state()["n1"]["index"] == 0


def test_keeps_other_fields(state):
    """audio_sig 不能被抹掉 —— 它是「切点变了就从头」的判据。"""
    _seed(n1={"audio_sig": "abc123", "index": 4})
    nodes.set_index("n1", 1)
    record = nodes._load_state()["n1"]
    assert record["index"] == 1
    assert record["audio_sig"] == "abc123"


# ---------------------------------------------------------------- 边界

def test_negative_becomes_zero(state):
    _seed(n1={"index": 3})
    nodes.set_index("n1", -9)
    assert nodes._load_state()["n1"]["index"] == 0


def test_non_numeric_becomes_zero(state):
    _seed(n1={"index": 3})
    nodes.set_index("n1", "abc")
    assert nodes._load_state()["n1"]["index"] == 0


def test_missing_record_is_not_created(state):
    """没跑过的节点不该被凭空写一条记录。"""
    _seed()
    assert nodes.set_index("nope", 3) is False
    assert "nope" not in nodes._load_state()


def test_dirty_record_does_not_crash(state):
    """state.json 被手工改成 {"n1": 5} 时要静默跳过，不能 500。"""
    _seed(n1=5)
    assert nodes.set_index("n1", 2) is False
    assert nodes._load_state()["n1"] == 5


def test_empty_node_is_ignored(state):
    _seed(n1={"index": 1})
    assert nodes.set_index("", 2) is False
    assert nodes._load_state()["n1"]["index"] == 1


# ---------------------------------------------------------------- 真的影响加载

def test_cursor_actually_moves_loader(state, tmp_path):
    """
    游标设到 2（= 第 3 行），下一次加载就该吐第 3 行。

    只断言 state 里的数字不够 —— 加载节点读的是它自己的游标推进逻辑，
    两边对不上就会出现「界面说从第 3 段开始，实际还从第 1 段跑」。
    """
    path = tmp_path / "p.txt"
    path.write_text("一行\n二行\n三行\n四行\n五行\n", encoding="utf-8")

    loader = nodes.DogkingPromptLoader()
    key = "audio-node"
    loader.load(str(path), nodes.MODE_ALL, 1, 0, 1, False, unique_id=key)  # 吃掉第 1 行

    nodes.set_index(key, 2)                                                 # 跳到第 3 行
    text, line, _total, _status = loader.load(
        str(path), nodes.MODE_ALL, 1, 0, 1, False, unique_id=key
    )["result"]
    assert line == 3
    assert text == "三行"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
