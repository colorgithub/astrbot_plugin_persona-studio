#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人格工坊离线回归测试（不依赖 astrbot）。

用 ``stub_astrbot`` 替代 astrbot 接口，直接驱动 PersonaStudioPlugin 的指令处理器。
Python 3.8+ 直接跑，不需要安装任何东西：

    python tests/test_offline.py
"""

from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import stub_astrbot as stub  # noqa: E402

stub.install_stub()
MOD = stub.load_plugin(os.path.join(HERE, os.pardir, "main.py"), "plugin_offline")

UMO_A = "aiocqhttp:private:10001"
UMO_B = "aiocqhttp:private:20002"
BASE = [("诗人", "你是一位浪漫的诗人。"), ("猫娘", "你是一只傲娇的猫娘。")]

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok and detail:
        print(f"        {detail}")


def build(personas=None, current="猫娘", default_name="default", config=None):
    pm = stub.FakePersonaManager(personas if personas is not None else BASE, default_name)
    cm = stub.FakeConversationManager(current)
    ctx = stub.FakeContext(pm, cm)
    plugin = MOD.PersonaStudioPlugin(ctx, config or {})
    return plugin, pm, cm


async def call(plugin, handler, msg, admin=True, umo=UMO_A):
    ev = stub.FakeEvent(msg, umo=umo, admin=admin)
    out = []
    async for r in getattr(plugin, handler)(ev):
        out.append(r[1])
    return "\n".join(out)


def no_write(pm):
    return not pm.updated and not pm.created and not pm.deleted


def rewrote(pm, persona_id):
    return len(pm.updated) == 1 and pm.updated[0][0] == persona_id


async def main():
    # ================= 参数解析（v1.0.5） =================

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_edit", "人格修改 诗人")
    check(
        "单 token = 已存在人格名时只提示、不写库",
        no_write(pm) and "请说明要对人格「诗人」改什么" in out,
        f"写库={pm.updated} 回复={out[:80]}",
    )

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_edit", "人格修改 诗人 说话要押韵")
    check("名称 + 要求：正常改指定人格", rewrote(pm, "诗人"), f"写库={pm.updated}")

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_edit", "人格修改 说话简短一点")
    check("单 token 非人格名：整段当要求改当前人格", rewrote(pm, "猫娘"), f"写库={pm.updated}")

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_edit", "人格修改 诗入 说话要押韵")
    check("人格名打错 + 有要求：只提示不写库", no_write(pm) and "找不到人格「诗入」" in out)

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_edit", "人格修改 当前")
    check("「当前」漏写要求：只提示", no_write(pm) and "请说明要改什么" in out)

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_edit", "人格修改 当前 更简洁")
    check("「当前」+ 要求：改当前人格", rewrote(pm, "猫娘"), f"写库={pm.updated}")

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_edit", "人格修改 无 更简洁")
    check("「无」是别名，不是人格名，也不是修改要求", no_write(pm) and "没有可修改的提示词" in out)

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_edit", "人格修改 说话更简洁， 别用感叹号")
    check("首 token 含标点：整段当要求", rewrote(pm, "猫娘"), f"写库={pm.updated}")

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_edit", "人格修改")
    check("空参数：给用法", no_write(pm) and "用法：/人格修改" in out)

    plugin, pm, cm = build(default_name="default")
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_edit", "人格修改 默认 更简洁")
    check(
        "「默认」+ 要求（全局默认是内置 default）：明确说明无默认可改",
        no_write(pm) and "没有配置可修改的默认人格" in out,
        out[:100],
    )

    plugin, pm, cm = build(default_name="诗人")
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_edit", "人格修改 默认 更简洁")
    check("「默认」+ 要求（默认是真实人格）：正常改", rewrote(pm, "诗人"), f"写库={pm.updated}")

    # ================= 别名与同名人格（v1.0.6 ①） =================

    plugin, pm, cm = build(BASE + [("无", "我是名为「无」的人格")])
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_switch", "人格切换 无")
    check("存在同名人格时 /人格切换 无 真的切到它", cm.convs[UMO_A].persona_id == "无",
          f"实际={cm.convs[UMO_A].persona_id!r}")

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_switch", "人格切换 无")
    check("无同名人格时 /人格切换 无 仍表示关闭人格", cm.convs[UMO_A].persona_id == "[%None]",
          f"实际={cm.convs[UMO_A].persona_id!r}")

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_create", "人格创建 关闭 一段描述")
    check("新建时拒绝与别名冲突的名字",
          "与内置别名冲突" in out and not any(p.persona_id == "关闭" for p in pm.personas))

    # ================= 健壮性（v1.0.6 ②） =================

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    stub.SP.store[("umo", UMO_A, "session_service_config")] = ["脏数据", "不是", "dict"]
    out = await call(plugin, "persona_switch", "人格切换 诗人")
    check("session_service_config 非 dict 时不崩且切换仍生效",
          cm.convs[UMO_A].persona_id == "诗人" and "已切换到人格「诗人」" in out,
          f"实际={cm.convs[UMO_A].persona_id!r}")

    # ================= /人格查看 默认（v1.0.6 ③） =================

    plugin, pm, cm = build(default_name="诗人")
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_show", "人格查看 默认")
    check("默认是真实人格时 /人格查看 默认 能看到它", "你是一位浪漫的诗人。" in out, out[:100])

    plugin, pm, cm = build(default_name="default")
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_show", "人格查看 默认")
    check("默认是内置 default 时给出明确说明", "没有配置可查看的默认人格" in out, out[:100])

    # ================= 列表上限（v1.0.6 ④） =================

    plugin, pm, cm = build(
        [(f"人格{i:03d}", "提示词" * 20) for i in range(60)], config={"max_list_items": 30}
    )
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_list", "人格")
    check("人格过多时列表被截断并提示剩余数量",
          "还有 30 个人格未显示" in out and out.count("—") == 30,
          f"条目数={out.count('—')}")

    # ================= 截断标记（v1.0.6 ⑤） =================

    plugin, pm, cm = build([], config={"use_llm_expand": False, "max_show_length": 200})
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_create", "人格创建 长人格 " + "很长的设定。" * 100)
    check("/人格创建 的预览带「已截断」标记", "已截断" in out, out[-80:])

    plugin, pm, cm = build(
        [("长人格", "很长的设定。" * 100)], config={"max_show_length": 200}
    )
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_show", "人格查看 长人格")
    check("/人格查看 的截断标记仍然存在", "已截断" in out, out[-80:])

    # ================= --raw 位置（v1.0.6 ⑥） =================

    plugin, pm, cm = build([], config={"use_llm_expand": True})
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_create", "人格创建 客服 你是一个专业的客服 --raw")
    got = [p.system_prompt for p in pm.personas if p.persona_id == "客服"]
    check("--raw 写在末尾也生效且不写进正文",
          got and got[0] == "你是一个专业的客服", f"落库={got}")

    plugin, pm, cm = build([], config={"use_llm_expand": True})
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_create", "人格创建 客服2 --raw 你是一个专业的客服")
    got = [p.system_prompt for p in pm.personas if p.persona_id == "客服2"]
    check("--raw 写在开头仍然有效", got and got[0] == "你是一个专业的客服", f"落库={got}")

    # ================= 归属隔离（v1.0.6 ⑦） =================

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await cm.new_conversation(UMO_B, persona_id="猫娘")
    await call(plugin, "persona_create", "人格创建 私有人格 我的私人设定",
               admin=False, umo=UMO_A)

    out = await call(plugin, "persona_edit", "人格修改 私有人格 改一下",
                     admin=False, umo=UMO_B)
    check("别人不能改非管理员创建的人格", "其他使用者创建" in out, out[:80])

    out = await call(plugin, "persona_delete", "人格删除 私有人格 --yes",
                     admin=False, umo=UMO_B)
    check("别人不能删非管理员创建的人格", "其他使用者创建" in out, out[:80])

    out = await call(plugin, "persona_edit", "人格修改 私有人格 改一下",
                     admin=False, umo=UMO_A)
    check("创建者本人可以改", "已按你的要求用 LLM 重写人格" in out, out[:80])

    out = await call(plugin, "persona_edit", "人格修改 诗人 改一下", admin=False, umo=UMO_A)
    check("非管理员不能改公共人格", "公共人格" in out, out[:80])

    out = await call(plugin, "persona_edit", "人格修改 诗人 改一下", admin=True, umo=UMO_A)
    check("管理员不受归属限制", "已按你的要求用 LLM 重写人格" in out, out[:80])

    # ================= 删除确认 / 修改回滚（v1.0.6 ⑧） =================

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_delete", "人格删除 诗人")
    check("不带 --yes 时只提示、不删除",
          "不可恢复" in out and any(p.persona_id == "诗人" for p in pm.personas), out[:80])

    out = await call(plugin, "persona_delete", "人格删除 诗人 --yes")
    check("带 --yes 才真的删除",
          "已删除人格「诗人」" in out and not any(p.persona_id == "诗人" for p in pm.personas))

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    await call(plugin, "persona_edit", "人格修改 诗人 更简洁")
    after_edit = [p.system_prompt for p in pm.personas if p.persona_id == "诗人"][0]
    out = await call(plugin, "persona_restore", "人格还原 诗人")
    after_restore = [p.system_prompt for p in pm.personas if p.persona_id == "诗人"][0]
    check("/人格修改 后可 /人格还原 回滚",
          after_edit == "[REWRITTEN-BY-LLM]"
          and after_restore == "你是一位浪漫的诗人。"
          and "已把人格「诗人」还原" in out,
          f"修改后={after_edit!r} 还原后={after_restore!r}")

    plugin, pm, cm = build()
    await cm.new_conversation(UMO_A, persona_id="猫娘")
    out = await call(plugin, "persona_restore", "人格还原 诗人")
    check("没有备份时给出明确提示", "没有可还原的历史版本" in out, out[:80])

    # ================= 汇总 =================
    failed = [n for n, ok in RESULTS if not ok]
    print()
    print("=" * 70)
    print(f"通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}")
    for n in failed:
        print(f"  FAIL: {n}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
