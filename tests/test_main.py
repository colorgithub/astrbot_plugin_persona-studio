"""人格工坊插件行为测试：用真实 astrbot.api 接口 + 假 Context（无需真实数据库）。

运行：python tests/test_main.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# 必须在导入 astrbot 之前设置：AstrBot 会在 <root>/data 下创建运行目录，
# 这里指向临时目录，避免污染插件目录或真实 AstrBot 数据目录。
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="astrbot_test_root_"))
sys.dont_write_bytecode = True  # 不要在插件目录留下 __pycache__

import astrbot.api as abapi  # noqa: E402
from astrbot.core.message.message_event_result import MessageEventResult  # noqa: E402

PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"


# ----------------------------------------------------------------- 假对象

class FakePersona:
    def __init__(self, persona_id, system_prompt):
        self.persona_id = persona_id
        self.system_prompt = system_prompt
        self.begin_dialogs = []
        self.tools = None


class FakePersonaManager:
    def __init__(self):
        self.personas = []

    async def get_all_personas(self):
        return list(self.personas)

    async def create_persona(self, persona_id, system_prompt, **kwargs):
        if any(p.persona_id == persona_id for p in self.personas):
            raise ValueError(f"Persona with ID {persona_id} already exists.")
        p = FakePersona(persona_id, system_prompt)
        self.personas.append(p)
        return p

    async def update_persona(self, persona_id, system_prompt=None, **kwargs):
        for p in self.personas:
            if p.persona_id == persona_id:
                if system_prompt is not None:
                    p.system_prompt = system_prompt
                return p
        raise ValueError("not found")

    async def delete_persona(self, persona_id):
        self.personas = [p for p in self.personas if p.persona_id != persona_id]

    async def get_default_persona_v3(self, umo=None):
        return {"name": "default"}


class FakeConversation:
    def __init__(self, cid, persona_id=None):
        self.cid = cid
        self.persona_id = persona_id


class FakeConversationManager:
    def __init__(self):
        self.convs = {}
        self.current = {}
        self.seq = 0

    async def get_curr_conversation_id(self, umo):
        return self.current.get(umo)

    async def get_conversation(self, umo, cid):
        return self.convs.get(cid)

    async def new_conversation(self, umo, platform_id=None, content=None, title=None,
                               persona_id=None):
        self.seq += 1
        cid = f"conv-{self.seq}"
        self.convs[cid] = FakeConversation(cid, persona_id)
        self.current[umo] = cid
        return cid

    async def update_conversation(self, umo, conversation_id=None, history=None,
                                  title=None, persona_id=None, token_usage=None):
        if not conversation_id:
            conversation_id = self.current.get(umo)
        conv = self.convs.get(conversation_id)
        if conv is not None and persona_id is not None:
            conv.persona_id = persona_id


class FakeProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append((system_prompt, prompt))
        text = self.replies.pop(0) if self.replies else "（无回复）"
        if isinstance(text, Exception):
            raise text
        resp = type("R", (), {})()
        resp.completion_text = text
        return resp


class FakeContext:
    def __init__(self, config=None, provider=None, provider_map=None):
        self.persona_manager = FakePersonaManager()
        self.conversation_manager = FakeConversationManager()
        self.provider = provider
        self.provider_map = dict(provider_map or {})
        self.config = config or {}

    async def get_using_provider_async(self, umo=None):
        return self.provider

    def get_provider_by_id(self, provider_id):
        return self.provider_map.get(provider_id)

    def get_config(self, umo=None):
        return self.config


class FakeSP:
    def __init__(self):
        self.data = {}

    async def session_get(self, umo, key=None, default=None):
        return self.data.get((umo, key), default)

    async def session_put(self, umo, key, value):
        self.data[(umo, key)] = value


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


# ----------------------------------------------------------------- 装载插件

def load_plugin():
    spec = importlib.util.spec_from_file_location("persona_studio_main", PLUGIN_MAIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["persona_studio_main"] = module
    spec.loader.exec_module(module)
    return module


async def call(handler, event):
    """执行 async generator handler，返回拼接后的文本。"""
    texts = []
    async for result in handler(event):
        if isinstance(result, str):
            texts.append(result)
        else:
            texts.append(result.get_plain_text())
    return "\n".join(texts)


def make_plugin(module, config=None, provider=None, provider_map=None):
    context = FakeContext(config=config, provider=provider, provider_map=provider_map)
    plugin = module.PersonaStudioPlugin(context, config or {})
    fake_sp = FakeSP()
    module.sp = fake_sp  # 插件内部调用的是模块级 sp
    plugin.sp = fake_sp
    plugin._fake_sp = fake_sp
    return plugin, context


# ----------------------------------------------------------------- 用例

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  -> {detail}"))


async def main():
    module = load_plugin()
    print("== 1. 模块导入与注册 ==")
    check("插件类可导入", hasattr(module, "PersonaStudioPlugin"))
    from astrbot.core.star.star import star_registry
    names = [m.name for m in star_registry]
    check("register 装饰器注册成功", module.PLUGIN_NAME in names, str(names))

    print("== 2. 纯函数 ==")
    check(
        "sanitize 去代码块",
        module.sanitize_llm_text("```markdown\n你是猫娘喵\n```") == "你是猫娘喵",
        module.sanitize_llm_text("```markdown\n你是猫娘喵\n```"),
    )
    check(
        "sanitize 去前缀",
        module.sanitize_llm_text("系统提示词：你是猫娘") == "你是猫娘",
    )
    check("brief_text 截断", module.brief_text("a" * 100, 10).endswith("…"))
    check("名称校验-非法字符", module.PersonaStudioPlugin._validate_persona_id("a b") is not None)
    check("名称校验-保留名", module.PersonaStudioPlugin._validate_persona_id("default") is not None)
    check("名称校验-正常", module.PersonaStudioPlugin._validate_persona_id("猫娘") is None)
    check("名称校验-过长", module.PersonaStudioPlugin._validate_persona_id("x" * 40) is not None)

    print("== 3. 参数解析（唤醒前缀已由框架剥掉） ==")
    ev = FakeEvent("人格创建 猫娘 一只傲娇猫")
    check("_arg_str 中文指令", module.PersonaStudioPlugin._arg_str(ev, module.CMD_CREATE) == "猫娘 一只傲娇猫")
    ev = FakeEvent("新建人格 猫娘 abc")
    check("_arg_str 中文别名", module.PersonaStudioPlugin._arg_str(ev, module.CMD_CREATE) == "猫娘 abc")
    ev = FakeEvent("人格切换")
    check("_arg_str 无参数", module.PersonaStudioPlugin._arg_str(ev, module.CMD_SWITCH) == "")

    # 只保留中文指令名/别名：不再注册 persona、personas、persona_* 等英文别名
    all_cmd_names = module.CMD_LIST + module.CMD_CREATE + module.CMD_SWITCH + module.CMD_SHOW + module.CMD_EDIT + module.CMD_DELETE
    english = [n for n in all_cmd_names if n.isascii()]
    check("不再注册任何英文别名", english == [], str(english))
    check("不再有 persona / persona_* 别名",
          not any(n.lower().startswith("persona") for n in all_cmd_names),
          str([n for n in all_cmd_names if n.lower().startswith("persona")]))
    check("中文指令名保留", {"人格", "人格创建", "人格切换", "人格查看", "人格修改", "人格删除"} <= set(all_cmd_names),
          str(all_cmd_names))

    # 命令过滤器上也不应再挂英文别名
    from astrbot.core.star.star_handler import star_handlers_registry

    registered_aliases = set()
    for md in star_handlers_registry.star_handlers_map.values():
        for f in md.event_filters:
            for alias in getattr(f, "alias", set()) or set():
                registered_aliases.add(alias)
    check("命令过滤器里没有 persona* 别名",
          not any(a.lower().startswith("persona") for a in registered_aliases),
          str(sorted(a for a in registered_aliases if a.lower().startswith("persona"))))

    print("== 4. 列表（空） ==")
    provider = FakeProvider([])
    plugin, ctx = make_plugin(module, provider=provider)
    text = await call(plugin.persona_list, FakeEvent("人格"))
    check("空列表提示", "还没有自定义人格" in text, text)
    check("显示指令帮助", "/人格创建" in text)

    print("== 5. 创建人格（LLM 扩写 + 自动切换） ==")
    provider = FakeProvider(["```\n你是一只傲娇的猫娘，说话带喵。\n```"])
    plugin, ctx = make_plugin(module, provider=provider)
    event = FakeEvent("人格创建 猫娘 一只傲娇的猫，说话带喵")
    text = await call(plugin.persona_create, event)
    check("创建成功提示", "已创建人格「猫娘」" in text, text)
    check("走 LLM 扩写", "已由 LLM 扩写" in text, text)
    check("提示词已清洗", "```" not in text, text)
    personas = await ctx.persona_manager.get_all_personas()
    check("落库 1 条", len(personas) == 1, str([p.persona_id for p in personas]))
    check("提示词正确", personas[0].system_prompt == "你是一只傲娇的猫娘，说话带喵。", personas[0].system_prompt)
    cid = await ctx.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
    check("自动切换（新建对话）", ctx.conversation_manager.convs[cid].persona_id == "猫娘")
    check("自动切换提示", "已自动切换" in text, text)

    print("== 6. 重名 / 非法名 / 缺参数 ==")
    text = await call(plugin.persona_create, FakeEvent("人格创建 猫娘 再来一次"))
    check("重名拦截", "已存在同名人格" in text, text)
    text = await call(plugin.persona_create, FakeEvent("人格创建 a/b 描述"))
    check("非法名拦截", "不能包含" in text, text)
    text = await call(plugin.persona_create, FakeEvent("人格创建"))
    check("缺参数给用法", "用法" in text, text)
    text = await call(plugin.persona_create, FakeEvent("人格创建 新人格"))
    check("缺描述提示", "还差一段描述" in text, text)

    print("== 7. --raw 跳过 LLM ==")
    provider = FakeProvider([])
    plugin2, ctx2 = make_plugin(module, provider=provider)
    text = await call(plugin2.persona_create, FakeEvent("人格创建 客服 --raw 你是专业客服"))
    check("--raw 直接用原文", "已直接使用你的描述" in text, text)
    check("--raw 未调用 LLM", provider.calls == [], str(provider.calls))
    personas = await ctx2.persona_manager.get_all_personas()
    check("--raw 提示词", personas[0].system_prompt == "你是专业客服", personas[0].system_prompt)

    print("== 8. LLM 失败回退 ==")
    provider = FakeProvider([RuntimeError("boom")])
    plugin3, ctx3 = make_plugin(module, provider=provider)
    text = await call(plugin3.persona_create, FakeEvent("人格创建 兜底 随便描述"))
    check("LLM 失败仍创建", "已创建人格「兜底」" in text and "LLM 调用失败" in text, text)

    print("== 9. 切换人格 ==")
    await ctx.persona_manager.create_persona(persona_id="诗人", system_prompt="你是诗人")
    text = await call(plugin.persona_switch, FakeEvent("人格切换 诗人"))
    check("切换成功", "已切换到人格「诗人」" in text, text)
    cid = await ctx.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
    check("会话人格已更新", ctx.conversation_manager.convs[cid].persona_id == "诗人")
    text = await call(plugin.persona_switch, FakeEvent("人格切换 不存在"))
    check("切换不存在提示", "找不到人格" in text, text)
    text = await call(plugin.persona_switch, FakeEvent("人格切换"))
    check("无参数显示当前", "当前会话人格：诗人" in text, text)

    print("== 10. 切换默认 / 关闭 ==")
    text = await call(plugin.persona_switch, FakeEvent("人格切换 默认"))
    check("默认别名：配置里无真实默认人格时回退为不使用人格",
          "不使用任何人格" in text, text)
    cid = await ctx.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
    check("默认落为 [%None]（配置无真实默认人格）",
          ctx.conversation_manager.convs[cid].persona_id == module.NO_PERSONA,
          ctx.conversation_manager.convs[cid].persona_id)

    # 配置里存在真实默认人格时，「默认」应指向它
    async def _default_is_poet(umo=None):
        return {"name": "诗人"}

    ctx.persona_manager.get_default_persona_v3 = _default_is_poet
    text = await call(plugin.persona_switch, FakeEvent("人格切换 默认"))
    check("默认别名：指向配置的默认人格", "已切换到人格「诗人」" in text, text)
    cid = await ctx.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
    check("默认人格落库", ctx.conversation_manager.convs[cid].persona_id == "诗人")

    text = await call(plugin.persona_switch, FakeEvent("人格切换 关闭"))
    check("关闭提示", "不使用任何人格" in text, text)

    print("== 11. 会话级强制人格 ==")
    umo = event.unified_msg_origin
    fake_sp = module.sp  # 插件实际读取的模块级 sp
    fake_sp.data[(umo, "session_service_config")] = {"persona_id": "强制"}
    forced = await plugin._current_persona_id(umo)
    check("读取时强制人格优先", forced == "强制", str(forced))
    text = await call(plugin.persona_list, FakeEvent("人格"))
    check("列表标注强制人格", "当前会话人格：强制" in text, text)
    await call(plugin.persona_switch, FakeEvent("人格切换 诗人"))
    svc = fake_sp.data[(umo, "session_service_config")]
    check("切换时清掉强制人格", "persona_id" not in svc, str(svc))

    print("== 12. 查看 ==")
    text = await call(plugin.persona_show, FakeEvent("人格查看 诗人"))
    check("查看到提示词", "你是诗人" in text, text)
    text = await call(plugin.persona_show, FakeEvent("人格查看"))
    check("省略名称看当前", "你是诗人" in text, text)
    text = await call(plugin.persona_show, FakeEvent("人格查看 查无此人"))
    check("不存在提示", "找不到人格" in text, text)

    print("== 13. LLM 修改人格 ==")
    provider = FakeProvider(["你是一位浪漫的诗人，说话简洁，每句都要押韵。"])
    plugin4, ctx4 = make_plugin(module, provider=provider)
    await ctx4.persona_manager.create_persona(persona_id="诗人", system_prompt="你是诗人")
    await ctx4.conversation_manager.new_conversation("qq:group:100", "qq", persona_id="诗人")
    text = await call(plugin4.persona_edit, FakeEvent("人格修改 诗人 说话要押韵"))
    check("修改成功", "已按你的要求用 LLM 重写人格「诗人」" in text, text)
    p = await ctx4.persona_manager.get_all_personas()
    check("提示词已更新", p[0].system_prompt.startswith("你是一位浪漫的诗人"), p[0].system_prompt)
    check("system prompt 用了修改模板",
          "维护专家" in provider.calls[0][0], provider.calls[0][0][:40])
    check("用户 prompt 含原文与要求",
          "你是诗人" in provider.calls[0][1] and "说话要押韵" in provider.calls[0][1])

    print("== 14. 修改（省略人格名 -> 当前人格） ==")
    provider = FakeProvider(["你是诗人，说话简短。"])
    plugin5, ctx5 = make_plugin(module, provider=provider)
    await ctx5.persona_manager.create_persona(persona_id="诗人", system_prompt="你是诗人")
    await ctx5.conversation_manager.new_conversation("qq:group:100", "qq", persona_id="诗人")
    text = await call(plugin5.persona_edit, FakeEvent("人格修改 说话简短一点"))
    check("省略名称作用于当前人格", "重写人格「诗人」" in text, text)

    print("== 14b. 修改：人格名打错只提示，不静默改当前人格 ==")
    provider_b = FakeProvider(["你是一位简洁的诗人。", "你是诗人，说话简短。"])
    plugin5b, ctx5b = make_plugin(module, provider=provider_b)
    await ctx5b.persona_manager.create_persona(persona_id="诗人", system_prompt="你是诗人")
    await ctx5b.conversation_manager.new_conversation("qq:group:100", "qq", persona_id="诗人")

    text = await call(plugin5b.persona_edit, FakeEvent("人格修改 诗入 说话要押韵"))
    check("打错名字给出提示", "找不到人格「诗入」" in text, text)
    check("打错名字不调用 LLM", provider_b.calls == [], str(provider_b.calls))
    p5b = await ctx5b.persona_manager.get_all_personas()
    check("打错名字时库中提示词不变", p5b[0].system_prompt == "你是诗人", p5b[0].system_prompt)
    check("提示里给出「当前」写法", "/人格修改 当前" in text, text)

    text = await call(plugin5b.persona_edit, FakeEvent("人格修改 当前 说话简短一点"))
    check("显式「当前」仍可改本会话人格", "重写人格「诗人」" in text, text)

    text = await call(plugin5b.persona_edit, FakeEvent("人格修改 当前"))
    check("只写「当前」提醒补要求", "请说明要改什么" in text, text)

    text = await call(plugin5b.persona_edit, FakeEvent("人格修改 无 说话简短一点"))
    check("「无」没有可修改的提示词", "没有可修改的提示词" in text, text)

    text = await call(plugin5b.persona_edit, FakeEvent("人格修改 说话更简洁， 别用感叹号"))
    check("首 token 含标点仍按修改要求处理", "重写人格「诗人」" in text, text)

    print("== 14c. 配置指定「人格扩写/修改」用的模型 ==")
    session_provider = FakeProvider(["会话模型写的"])
    custom_provider = FakeProvider(["专用模型写的", "你是一只猫娘。"])
    cfg_model = {"persona_llm_provider": "wb2api/deepseek-v4.1-flash"}
    plugin5c, ctx5c = make_plugin(
        module, config=cfg_model, provider=session_provider,
        provider_map={"wb2api/deepseek-v4.1-flash": custom_provider},
    )
    await ctx5c.persona_manager.create_persona(persona_id="诗人", system_prompt="你是诗人")
    await ctx5c.conversation_manager.new_conversation("qq:group:100", "qq", persona_id="诗人")
    text = await call(plugin5c.persona_edit, FakeEvent("人格修改 诗人 更简洁"))
    check("修改走配置指定的模型", len(custom_provider.calls) == 1 and session_provider.calls == [],
          f"custom={len(custom_provider.calls)} session={len(session_provider.calls)}")
    check("模型可用时不出现回退提示", "已回退" not in text, text)
    text = await call(plugin5c.persona_list, FakeEvent("人格"))
    check("人格列表显示配置的模型", "wb2api/deepseek-v4.1-flash" in text, text)
    check("配置模型的结果落库",
          (await ctx5c.persona_manager.get_all_personas())[0].system_prompt == "专用模型写的",
          (await ctx5c.persona_manager.get_all_personas())[0].system_prompt)
    text = await call(plugin5c.persona_create, FakeEvent("人格创建 猫娘 一只猫"))
    check("创建扩写也走配置指定的模型", len(custom_provider.calls) == 2, str(len(custom_provider.calls)))
    check("创建提示词来自配置模型", "你是一只猫娘。" in text, text)

    session2 = FakeProvider(["会话模型兜底"])
    plugin5d, ctx5d = make_plugin(
        module, config={"persona_llm_provider": "不存在/模型"},
        provider=session2, provider_map={},
    )
    await ctx5d.persona_manager.create_persona(persona_id="诗人", system_prompt="你是诗人")
    await ctx5d.conversation_manager.new_conversation("qq:group:100", "qq", persona_id="诗人")
    text = await call(plugin5d.persona_edit, FakeEvent("人格修改 诗人 更简洁"))
    check("配置的模型不存在时回退会话模型",
          len(session2.calls) == 1 and "重写人格「诗人」" in text, text)
    check("模型不可用时明确提示回退", "已回退当前会话模型" in text, text)
    text = await call(plugin5d.persona_create, FakeEvent("人格创建 备用 一只猫"))
    check("创建时的回退也会提示", "已回退当前会话模型" in text, text)
    check("留空时跟随会话模型", plugin5d._persona_llm_provider_id == "", plugin5d._persona_llm_provider_id)
    plugin5e, _ = make_plugin(module, config={"persona_llm_provider": "  a/b  "}, provider=FakeProvider([]))
    check("配置项两侧空格被忽略", plugin5e._persona_llm_provider_id == "a/b", plugin5e._persona_llm_provider_id)

    print("== 15. 修改：当前无人格时报错 ==")
    plugin6, ctx6 = make_plugin(module, provider=FakeProvider(["x"]))
    text = await call(plugin6.persona_edit, FakeEvent("人格修改 随便改改"))
    check("无人格提示", "没有生效的人格" in text, text)
    text = await call(plugin6.persona_edit, FakeEvent("人格修改"))
    check("修改缺参数给用法", "用法" in text, text)

    print("== 16. 修改：LLM 失败不写库 ==")
    plugin7, ctx7 = make_plugin(module, provider=FakeProvider([RuntimeError("boom")]))
    await ctx7.persona_manager.create_persona(persona_id="诗人", system_prompt="你是诗人")
    text = await call(plugin7.persona_edit, FakeEvent("人格修改 诗人 改成别的"))
    personas = await ctx7.persona_manager.get_all_personas()
    check("LLM 失败不改库", personas[0].system_prompt == "你是诗人", personas[0].system_prompt)
    check("LLM 失败提示", "调用 LLM 失败" in text, text)

    print("== 17. 删除 ==")
    text = await call(plugin.persona_delete, FakeEvent("人格删除 诗人"))
    check("删除成功", "已删除人格「诗人」" in text, text)
    ids = [p.persona_id for p in await ctx.persona_manager.get_all_personas()]
    check("库中已移除", "诗人" not in ids, str(ids))
    text = await call(plugin.persona_delete, FakeEvent("人格删除 诗人"))
    check("删除不存在提示", "找不到人格" in text, text)
    text = await call(plugin.persona_delete, FakeEvent("人格删除 default"))
    check("保留名不可删", "不能删除" in text, text)
    text = await call(plugin.persona_delete, FakeEvent("人格删除"))
    check("删除缺参数给用法", "用法" in text, text)

    print("== 18. 删除当前使用中的人格 ==")
    await ctx.persona_manager.create_persona(persona_id="临时", system_prompt="x")
    await call(plugin.persona_switch, FakeEvent("人格切换 临时"))
    text = await call(plugin.persona_delete, FakeEvent("人格删除 临时"))
    check("提示当前会话回退", "当前会话正在使用它" in text, text)

    print("== 19. 权限 ==")
    cfg_admin = {"manage_requires_admin": True, "allow_private_chat_manage": True}
    plugin_a, _ = make_plugin(module, config=cfg_admin, provider=FakeProvider([]))
    text = await call(plugin_a.persona_create, FakeEvent("人格创建 甲 描述", admin=False))
    check("群聊非管理员被拦", "没有新建人格的权限" in text, text)
    text = await call(plugin_a.persona_create, FakeEvent("人格创建 甲 描述", admin=True))
    check("管理员放行", "已创建人格「甲」" in text, text)
    text = await call(plugin_a.persona_create,
                     FakeEvent("人格创建 乙 描述", admin=False, private=True))
    check("私聊非管理员放行", "已创建人格「乙」" in text, text)

    cfg_no_private = {"manage_requires_admin": True, "allow_private_chat_manage": False}
    plugin_b, _ = make_plugin(module, config=cfg_no_private, provider=FakeProvider([]))
    text = await call(plugin_b.persona_create,
                     FakeEvent("人格创建 丙 描述", admin=False, private=True))
    check("关闭私聊放行后被拦", "没有新建人格的权限" in text, text)

    cfg_open = {"manage_requires_admin": False}
    plugin_c, _ = make_plugin(module, config=cfg_open, provider=FakeProvider([]))
    text = await call(plugin_c.persona_create, FakeEvent("人格创建 丁 描述", admin=False))
    check("关闭管理员限制后放行", "已创建人格「丁」" in text, text)
    text = await call(plugin_c.persona_switch, FakeEvent("人格切换 丁", admin=False))
    check("切换不受权限限制", "已切换到人格「丁」" in text, text)

    print("== 20. 无 Provider 时的行为 ==")
    plugin_d, _ = make_plugin(module, provider=None)
    text = await call(plugin_d.persona_create, FakeEvent("人格创建 戊 描述"))
    check("无模型时仍创建（回退原文）", "已创建人格「戊」" in text and "LLM 调用失败" in text, text)
    text = await call(plugin_d.persona_edit, FakeEvent("人格修改 戊 改一下"))
    check("无模型时修改被拒", "调用 LLM 失败" in text, text)

    print("== 21. 配置项边界 ==")
    plugin_e, _ = make_plugin(module, config={"max_show_length": 5}, provider=FakeProvider([]))
    check("max_show_length 下限兜底", plugin_e._max_show_length == 200, str(plugin_e._max_show_length))
    plugin_f, _ = make_plugin(module, config={"max_show_length": 999999}, provider=FakeProvider([]))
    check("max_show_length 上限兜底", plugin_f._max_show_length == 10000, str(plugin_f._max_show_length))
    plugin_g, _ = make_plugin(module, config={"manage_requires_admin": "false"})
    check("字符串布尔解析", plugin_g._manage_requires_admin is False)
    plugin_h, _ = make_plugin(module, config=None)
    check("无配置用默认值", plugin_h._manage_requires_admin is True and plugin_h._use_llm_expand is True)

    print("== 22. 空描述 / 超长输入 ==")
    plugin_i, _ = make_plugin(module, provider=FakeProvider([]))
    text = await call(plugin_i.persona_create, FakeEvent("人格创建 己 --raw"))
    check("--raw 后无描述被拦", "描述不能为空" in text or "还差一段描述" in text, text)
    long_desc = "啊" * 3000
    text = await call(plugin_i.persona_create, FakeEvent(f"人格创建 庚 {long_desc}"))
    check("超长描述被截断后仍创建", "已创建人格「庚」" in text, text[:80])
    p = [x for x in await plugin_i.context.persona_manager.get_all_personas() if x.persona_id == "庚"]
    check("落库长度被裁剪", len(p[0].system_prompt) <= module.MAX_RAW_INPUT_LEN, str(len(p[0].system_prompt)))

    print("== 23. LLM 读取人格工具（persona_list / persona_read / persona_current） ==")
    plugin_t, ctx_t = make_plugin(module, provider=FakeProvider([]))
    await ctx_t.persona_manager.create_persona(
        persona_id="猫娘", system_prompt="你是一只傲娇的猫娘，说话必须带“喵”。"
    )
    await ctx_t.persona_manager.create_persona(
        persona_id="诗人", system_prompt="你是一位浪漫的诗人。"
    )
    ev_t = FakeEvent("人格")

    text = await plugin_t.tool_persona_list(ev_t)
    check("persona_list 列出全部人格", "猫娘" in text and "诗人" in text, text)
    check("persona_list 带 ID/字数/摘要",
          "ID=猫娘" in text and "字数=" in text and "摘要：" in text, text)
    check("persona_list 提示当前会话", "当前会话正在使用的人格 ID" in text, text)
    check("persona_list 指引用 persona_read", "persona_read" in text, text)

    text = await plugin_t.tool_persona_read(ev_t, persona_id="猫娘")
    check("persona_read 返回完整提示词",
          "你是一只傲娇的猫娘，说话必须带“喵”。" in text, text)
    text = await plugin_t.tool_persona_read(ev_t, persona_id="查无此人")
    check("persona_read 找不到时给出候选 ID", "找不到人格" in text and "猫娘" in text, text)
    text = await plugin_t.tool_persona_read(ev_t, persona_id="  ")
    check("persona_read 空参数提示", "缺少参数" in text, text)

    text = await plugin_t.tool_persona_current(ev_t)
    check("persona_current 未指定时如实说明", "没有指定人格" in text, text)

    await call(plugin_t.persona_switch, FakeEvent("人格切换 猫娘"))
    text = await plugin_t.tool_persona_current(FakeEvent("人格"))
    check("persona_current 返回当前人格与完整提示词",
          "猫娘" in text and "你是一只傲娇的猫娘" in text, text)
    text = await plugin_t.tool_persona_list(FakeEvent("人格"))
    check("persona_list 标注当前人格", "当前会话正在使用的人格 ID：猫娘" in text, text)

    plugin_t2, ctx_t2 = make_plugin(module, provider=FakeProvider([]))
    await ctx_t2.persona_manager.create_persona(persona_id="临时", system_prompt="x")
    await call(plugin_t2.persona_switch, FakeEvent("人格切换 关闭"))
    text = await plugin_t2.tool_persona_current(FakeEvent("人格"))
    check("persona_current 识别已关闭人格", "已显式关闭人格" in text, text)

    plugin_t3, ctx_t3 = make_plugin(module, provider=FakeProvider([]))
    await ctx_t3.persona_manager.create_persona(persona_id="被删", system_prompt="x")
    await ctx_t3.conversation_manager.new_conversation("qq:group:100", "qq", persona_id="被删")
    await ctx_t3.persona_manager.delete_persona("被删")
    text = await plugin_t3.tool_persona_current(FakeEvent("人格"))
    check("persona_current 识别已删除人格", "已经不存在" in text, text)

    print("== 24. LLM 工具的开关与截断配置 ==")
    plugin_off, _ = make_plugin(
        module, config={"enable_llm_tools": False}, provider=FakeProvider([])
    )
    for name, result in (
        ("persona_list", await plugin_off.tool_persona_list(FakeEvent("人格"))),
        ("persona_read", await plugin_off.tool_persona_read(FakeEvent("人格"), persona_id="x")),
        ("persona_current", await plugin_off.tool_persona_current(FakeEvent("人格"))),
    ):
        check(f"关闭后 {name} 返回禁用提示", "已被管理员关闭" in result, result)

    plugin_cap, ctx_cap = make_plugin(
        module, config={"llm_tool_max_chars": 200}, provider=FakeProvider([])
    )
    check("llm_tool_max_chars 下限兜底", plugin_cap._llm_tool_max_chars == 200,
          str(plugin_cap._llm_tool_max_chars))
    plugin_cap_big, _ = make_plugin(
        module, config={"llm_tool_max_chars": 999999}, provider=FakeProvider([])
    )
    check("llm_tool_max_chars 上限兜底", plugin_cap_big._llm_tool_max_chars == 50000,
          str(plugin_cap_big._llm_tool_max_chars))

    await ctx_cap.persona_manager.create_persona(persona_id="长人格", system_prompt="长" * 1000)
    text = await plugin_cap.tool_persona_read(FakeEvent("人格"), persona_id="长人格")
    check("超长提示词按配置截断", "此处截断" in text and len(text) < 600, str(len(text)))

    print("== 25. LLM 工具的 docstring / schema 契约 ==")
    from astrbot.core.provider.register import llm_tools as _llm_tools

    registered = {t.name: t for t in _llm_tools.func_list if t.name.startswith("persona_")}
    check("三个工具已注册到 AstrBot",
          set(registered) == {"persona_list", "persona_read", "persona_current"},
          str(set(registered)))
    check("persona_read 参数 schema 正确",
          registered["persona_read"].parameters["properties"].get("persona_id", {}).get("type")
          == "string",
          str(registered["persona_read"].parameters))
    check("无参工具 schema 为空对象",
          registered["persona_list"].parameters["properties"] == {},
          str(registered["persona_list"].parameters))
    check("工具描述不含残留的 Args 段",
          not registered["persona_current"].description.strip().endswith("Args:"),
          registered["persona_current"].description)

    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败用例：")
        for name in FAIL:
            print("  -", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
