"""集成测试：插件 + AstrBot 真实 PersonaManager / ConversationManager / SQLite / SharedPreferences。

目的：证明「对话里新建的人格真的写进 AstrBot 数据库」「切换真的能在
PersonaManager.resolve_selected_persona 里生效」「LLM 修改真的改到库里」。

运行：python tests/test_integration.py（需 pip install astrbot，使用临时数据库，不影响真实数据）
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="astrbot_int_root_"))
sys.dont_write_bytecode = True  # 不要在插件目录留下 __pycache__

import astrbot.api  # noqa: F401,E402  先导入 api，避免 persona_mgr 的循环导入
import astrbot.core.conversation_mgr as conv_mgr_mod
import astrbot.core.persona_mgr as persona_mgr_mod
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.utils.shared_preferences import SharedPreferences

PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  -> {detail}"))


class StubACM:
    """PersonaManager 只需要 default_conf 和 get_conf(umo)。"""

    def __init__(self, conf=None):
        self.default_conf = conf or {}
        self.conf = conf or {}

    def get_conf(self, umo=None):
        return self.conf


class FakeProvider:
    def __init__(self, replies):
        self.replies = list(replies)

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        resp = type("R", (), {})()
        resp.completion_text = self.replies.pop(0) if self.replies else ""
        return resp


class FakeContext:
    def __init__(self, persona_manager, conversation_manager, provider):
        self.persona_manager = persona_manager
        self.conversation_manager = conversation_manager
        self.provider = provider

    async def get_using_provider_async(self, umo=None):
        return self.provider

    def get_config(self, umo=None):
        return {}


class FakeEvent:
    def __init__(self, msg, umo="qq:group:100", admin=True, private=False):
        self.message_str = msg
        self.unified_msg_origin = umo
        self._admin = admin
        self._private = private

    def is_admin(self):
        return self._admin

    def is_private_chat(self):
        return self._private

    def get_platform_id(self):
        return "qq"

    def plain_result(self, text):
        return MessageEventResult().message(text)


def load_plugin():
    spec = importlib.util.spec_from_file_location("persona_studio_int", PLUGIN_MAIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["persona_studio_int"] = module
    spec.loader.exec_module(module)
    return module


async def call(handler, event):
    texts = []
    async for result in handler(event):
        texts.append(result if isinstance(result, str) else result.get_plain_text())
    return "\n".join(texts)


async def main():
    module = load_plugin()
    tmpdir = tempfile.mkdtemp(prefix="persona_studio_")
    db = SQLiteDatabase(str(Path(tmpdir) / "astrbot.db"))
    await db.initialize()
    sp = SharedPreferences(db, json_storage_path=str(Path(tmpdir) / "sp.json"))

    # 让框架内部也走这份临时 sp，避免污染真实数据目录
    persona_mgr_mod.sp = sp
    conv_mgr_mod.sp = sp
    module.sp = sp

    acm = StubACM()
    persona_manager = PersonaManager(db, acm)
    await persona_manager.initialize()
    conversation_manager = ConversationManager(db)

    umo = "qq:group:100"
    provider = FakeProvider(["```\n你是一只傲娇的猫娘，说话必须带“喵”。\n```"])
    context = FakeContext(persona_manager, conversation_manager, provider)
    plugin = module.PersonaStudioPlugin(context, {})

    print("== 1. 对话中新建人格 -> 真的写进 AstrBot 数据库 ==")
    text = await call(plugin.persona_create, FakeEvent("人格创建 猫娘 一只傲娇的猫"))
    check("指令回执成功", "已创建人格「猫娘」" in text, text)
    row = await db.get_persona_by_id("猫娘")
    check("DB 里存在该人格", row is not None and row.persona_id == "猫娘")
    check("DB 提示词为 LLM 清洗后的内容",
          row.system_prompt == "你是一只傲娇的猫娘，说话必须带“喵”。", row.system_prompt)
    check("PersonaManager 缓存已刷新",
          any(p.persona_id == "猫娘" for p in persona_manager.personas))

    print("== 2. 自动切换 -> 真的落到对话记录里 ==")
    cid = await conversation_manager.get_curr_conversation_id(umo)
    check("已创建对话", bool(cid), str(cid))
    conv = await db.get_conversation_by_id(cid)
    check("对话 persona_id 落库", conv.persona_id == "猫娘", str(conv.persona_id))

    print("== 3. 切换真的在 AstrBot 人格解析逻辑里生效 ==")
    pid, persona, forced, webchat = await persona_manager.resolve_selected_persona(
        umo=umo,
        conversation_persona_id=conv.persona_id,
        platform_name="qq",
        provider_settings=None,
    )
    check("resolve 选中的人格 id", pid == "猫娘", str(pid))
    check("resolve 返回人格对象", persona is not None and persona["name"] == "猫娘",
          str(persona))
    check("resolve 拿到正确提示词",
          persona and persona["prompt"] == "你是一只傲娇的猫娘，说话必须带“喵”。",
          str(persona and persona["prompt"]))

    print("== 4. 新建第二个人格 + 随时切换 ==")
    provider.replies.append("你是一位严谨的代码审查员。")
    text = await call(plugin.persona_create, FakeEvent("人格创建 审查员 严格挑毛病"))
    check("第二个人格创建成功", "已创建人格「审查员」" in text, text)
    text = await call(plugin.persona_switch, FakeEvent("人格切换 猫娘"))
    check("切回猫娘", "已切换到人格「猫娘」" in text, text)
    cid = await conversation_manager.get_curr_conversation_id(umo)
    conv = await db.get_conversation_by_id(cid)
    check("切换后 DB 生效", conv.persona_id == "猫娘", str(conv.persona_id))
    _, persona, _, _ = await persona_manager.resolve_selected_persona(
        umo=umo, conversation_persona_id=conv.persona_id, platform_name="qq"
    )
    check("解析结果跟着切换", persona["name"] == "猫娘", str(persona))

    print("== 5. 列表能看到库里的人格 ==")
    text = await call(plugin.persona_list, FakeEvent("人格"))
    check("列表含猫娘", "猫娘" in text and "审查员" in text, text)
    check("列表标注当前人格", "✅ 猫娘" in text, text)

    print("== 6. 会话级强制人格会被切换清掉 ==")
    await sp.session_put(umo, "session_service_config", {"persona_id": "审查员"})
    _, persona, forced, _ = await persona_manager.resolve_selected_persona(
        umo=umo, conversation_persona_id=conv.persona_id, platform_name="qq"
    )
    check("强制人格优先（前提校验）", forced == "审查员" and persona["name"] == "审查员",
          str(persona))
    text = await call(plugin.persona_switch, FakeEvent("人格切换 猫娘"))
    _, persona, forced, _ = await persona_manager.resolve_selected_persona(
        umo=umo, conversation_persona_id=conv.persona_id, platform_name="qq"
    )
    check("切换后强制人格被清除、人格变为猫娘",
          not forced and persona["name"] == "猫娘", f"forced={forced} persona={persona}")

    print("== 7. LLM 修改人格 -> 真的改到库里 ==")
    provider.replies.append("你是一只温柔的猫娘，说话轻声细语，每句带“喵”。")
    text = await call(plugin.persona_edit, FakeEvent("人格修改 猫娘 温柔一点"))
    check("修改回执", "已按你的要求用 LLM 重写人格「猫娘」" in text, text)
    row = await db.get_persona_by_id("猫娘")
    check("DB 提示词已更新",
          row.system_prompt.startswith("你是一只温柔的猫娘"), row.system_prompt)
    _, persona, _, _ = await persona_manager.resolve_selected_persona(
        umo=umo, conversation_persona_id=conv.persona_id, platform_name="qq"
    )
    check("解析到的是修改后的人格",
          persona["prompt"].startswith("你是一只温柔的猫娘"), persona["prompt"])

    print("== 8. 「默认」解析：配置里存在真实默认人格 ==")
    acm.conf = {
        "agent_runner": {
            "runner_type": "local",
            "config": {"persona": {"persona_id": "审查员"}},
        }
    }
    text = await call(plugin.persona_switch, FakeEvent("人格切换 默认"))
    check("切到配置的默认人格", "已切换到人格「审查员」" in text, text)
    cid = await conversation_manager.get_curr_conversation_id(umo)
    conv = await db.get_conversation_by_id(cid)
    check("默认人格落库", conv.persona_id == "审查员", str(conv.persona_id))

    print("== 9. 查看 ==")
    text = await call(plugin.persona_show, FakeEvent("人格查看 猫娘"))
    check("查看显示库里最新提示词", "温柔的猫娘" in text, text)

    print("== 10. 删除 -> 真的从库里移除 ==")
    text = await call(plugin.persona_delete, FakeEvent("人格删除 审查员"))
    check("删除回执", "已删除人格「审查员」" in text, text)
    check("DB 里已不存在", await db.get_persona_by_id("审查员") is None)
    check("PersonaManager 缓存已刷新",
          all(p.persona_id != "审查员" for p in persona_manager.personas))

    print("== 11. WebUI 侧创建的人格也能被聊天切换（互通性） ==")
    await persona_manager.create_persona(persona_id="后台人格", system_prompt="你是后台创建的")
    text = await call(plugin.persona_switch, FakeEvent("人格切换 后台人格"))
    check("能切到后台创建的人格", "已切换到人格「后台人格」" in text, text)

    print("== 12. 提示词长度展示截断 ==")
    await persona_manager.update_persona("后台人格", system_prompt="长" * 5000)
    plugin.config = {"max_show_length": 300}
    text = await call(plugin.persona_show, FakeEvent("人格查看 后台人格"))
    check("超长提示词被截断", "已截断" in text and len(text) < 800, str(len(text)))

    print("== 13. LLM 读取人格工具（按 AstrBot 真实调用约定） ==")
    from astrbot.core.provider.register import llm_tools

    tools = {t.name: t for t in llm_tools.func_list if t.name.startswith("persona_")}
    check("三个读取工具已注册进 AstrBot 工具表",
          set(tools) == {"persona_list", "persona_read", "persona_current"},
          str(set(tools)))
    check("persona_read 的 JSON schema 有 persona_id",
          tools["persona_read"].parameters["properties"]["persona_id"]["type"] == "string",
          str(tools["persona_read"].parameters))
    check("persona_list 无必填参数",
          tools["persona_list"].parameters["properties"] == {},
          str(tools["persona_list"].parameters))

    # star_manager 装载插件时会把 handler 绑成 functools.partial(raw_handler, 插件实例)，
    # 这里复刻同样的绑定方式，验证「event + kwargs」的真实调用约定可用。
    import functools

    bound_list = functools.partial(tools["persona_list"].handler, plugin)
    bound_read = functools.partial(tools["persona_read"].handler, plugin)
    bound_current = functools.partial(tools["persona_current"].handler, plugin)

    result = await bound_list(FakeEvent("人格"))
    check("persona_list 真实调用返回全部人格",
          "猫娘" in result and "后台人格" in result and "ID=" in result, result[:120])
    result = await bound_read(FakeEvent("人格"), persona_id="猫娘")
    check("persona_read 真实调用读到库里的提示词",
          "温柔的猫娘" in result, result[:120])
    result = await bound_read(FakeEvent("人格"), persona_id="不存在的")
    check("persona_read 真实调用给出候选", "找不到人格" in result, result[:120])
    result = await bound_current(FakeEvent("人格"))
    check("persona_current 真实调用返回当前人格",
          "后台人格" in result and "长" in result, result[:120])

    await db.engine.dispose()
    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
