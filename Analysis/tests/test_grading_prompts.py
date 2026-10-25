"""档位提示词与锚文的漂移守卫。

为什么要有这组测试：两份提示词（`prompts_grading.txt` 内部版、`prompt_three_grades.md`
对外独立版）的判据表都是**逐字抄袭** `specs.py` 里 `ANCHORS` 的原文，界面下拉框悬停
显示的也是同一段字。四处必须同时改，否则会出现"提示词教模型填 3 档、界面却说 3 档是
另一回事"——这种错不会让任何断言变红，只会让填出来的档位从此没人信。

历史：锚文改过两次（渗透区间 20-40 调整到 10-30、政策 5 档从"强力支持"改成
"强驱动正在兑现"），两次都漏改了文档。所以这里对账，不再靠记性。

另外守住独立版的存在理由：`prompt_three_grades.md` 必须能单独贴给任意 AI 使用，
一旦它引用了仓库里的文件、模块或异常名，就退化成了只能在本地读的草稿。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from stockframe.specs import ANCHORS

ROOT = Path(__file__).resolve().parent.parent

SOURCES = {
    "prompts_grading.txt": ROOT / "prompts_grading.txt",
    "prompt_three_grades.md": ROOT / "prompt_three_grades.md",
}

STANDALONE = "prompt_three_grades.md"

FIELDS = ("macro.policy_stance", "industry.penetration_space", "company.logic_strength")


def _text(name: str) -> str:
    return SOURCES[name].read_text(encoding="utf-8")


def _discriminator(anchor: str) -> str:
    """取一条锚文里"能区分档位"的那一段。

    渗透空间的锚文以区间区分（`10-30%`），其余以档位标签区分（冒号前的"强支持"）。
    """
    band = re.search(r"[><]?\d+\s*[-–]\s*\d+%|[><]\s*\d+%", anchor)
    if band:
        return band.group(0).replace(" ", "").replace("–", "-")
    return anchor.split("：")[0]


@pytest.mark.parametrize("name", list(SOURCES))
@pytest.mark.parametrize("key", ["policy_stance", "penetration_space", "logic_strength"])
def test_every_anchor_is_quoted(name, key):
    """每条锚文的关键判别词都必须出现在提示词里，五个档位一个都不能少。"""
    text = _text(name)
    missing = [a for a in ANCHORS[key].values() if _discriminator(a) not in text]
    assert not missing, f"{name} 没有同步 {key} 的锚文：{missing}"


@pytest.mark.parametrize("name", list(SOURCES))
@pytest.mark.parametrize("field", FIELDS)
def test_field_id_is_named(name, field):
    """提示词必须点名真实回填键，否则落盘时只能靠人猜该写哪一层。"""
    assert field in _text(name), f"{name} 未点名字段 {field}"


@pytest.mark.parametrize("name", list(SOURCES))
def test_json_blocks_parse(name):
    """回填片段必须是可直接粘贴的合法 JSON——复制时报错等于没有片段。"""
    blocks = re.findall(r"```json\n(.*?)```", _text(name), re.S)
    assert len(blocks) >= 3, f"{name} 应有至少 3 个回填片段，实际 {len(blocks)} 个"
    payloads = []
    for block in blocks:
        payloads.append(json.loads(block))
    flat = json.dumps(payloads, ensure_ascii=False)
    for field in FIELDS:
        assert field.split(".")[1] in flat, f"{name} 缺少 {field} 的回填片段"


def test_weight_chain_matches_specs():
    """§零 的"一档值多少分"是实测值，不是估算：按 specs 权重重算一遍对账。"""
    from stockframe.specs import DIMENSIONS

    def indicator_weight(dim_id: str, field: str) -> float:
        for dim in DIMENSIONS:
            if dim.id != dim_id:
                continue
            for factor in dim.factors:
                for ind in factor.indicators:
                    if ind.field == field:
                        return dim.weight_mid * factor.weight * ind.weight
        raise AssertionError(f"找不到 {field}")

    per_level = {  # 1-5 档换算后每档 2.5 分，见 specs.rating_bands
        "macro.policy_stance": 0.05,
        "industry.penetration_space": 0.05,
        "company.logic_strength": 0.11,
    }
    for field, expect in per_level.items():
        dim_id = field.split(".")[0]
        actual = 2.5 * indicator_weight(dim_id, field)
        assert abs(actual - expect) <= 0.005, (field, actual, expect)


def test_standalone_prompt_has_no_repo_references():
    """独立版不能出现"见某某文件""某某异常"，否则它就不是能直接粘贴的提示词。"""
    text = _text(STANDALONE)
    banned = [
        "prompts_grading",
        "prompts.txt",
        "src/stockframe",
        "engine.run",
        "ContractError",
        "LookAheadError",
        "ProvenanceError",
        "WordingError",
        ".py",
        "tests/",
        "pytest",
    ]
    hits = [b for b in banned if b in text]
    assert not hits, f"{STANDALONE} 仍引用仓库内部对象：{hits}"


def test_standalone_prompt_covers_logic_essence():
    """一句话本质那一节必须带上三件东西：回填键、200 字硬限、与档位的双向校验。

    这三条缺任何一条，本质就会退化成"写句漂亮话"：没有键回填不了，没有字数限制会被
    输入框截成半句，没有档位约束则文字与档位各说各话。
    """
    text = _text(STANDALONE)
    assert "company.logic_essence" in text, "缺回填键"
    assert "200 字" in text, "缺字数硬限"
    assert '"logic_essence"' in text, "缺回填 JSON 片段"
    for rule in ("最高 4 档", "强制 1 档", "最高 2 档"):
        assert rule in text, f"缺档位约束：{rule}"
