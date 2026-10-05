# -*- coding: utf-8 -*-
"""离线测试用的 astrbot 桩件。

环境里装不上 astrbot（或 Python 版本不够）时，用它替代
``astrbot.api`` / ``astrbot.api.event`` / ``astrbot.api.star``，
直接驱动 ``PersonaStudioPlugin`` 的指令处理器。

只做接口形状的模拟，不实现 AstrBot 的真实行为。
"""

from __future__ import annotations

import importlib.util
import sys
import types

# --------------------------------------------------------------------------
# astrbot 桩件
# --------------------------------------------------------------------------


class _Filter:
    """替代 ``astrbot.api.event.filter``，只记录注册信息，装饰器原样返回函数。"""

    def __init__(self):
        self.commands = {}
        self.tools = {}

    def command(self, name=None, alias=None, **kwargs):
        def deco(fn):
            self.commands[fn.__name__] = {"name": name, "alias": set(alias or ())}
            return fn

        return deco

    def llm_tool(self, name=None, **kwargs):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return deco


class _Logger:
    def __init__(self):
        self.lines = []

    def _log(self, level, msg, *args, **kwargs):
        self.lines.append((level, str(msg)))

    def debug(self, msg, *a, **k):
        self._log("debug", msg)

    def info(self, msg, *a, **k):
        self._log("info", msg)

    def warning(self, msg, *a, **k):
        self._log("warning", msg)

    def error(self, msg, *a, **k):
        self._log("error", msg)


class _SP:
    """替代 astrbot.api.sp（SharedPreferences），session / global 两个作用域。"""

    def __init__(self):
        self.store = {}

    async def session_get(self, umo, key, default=None):
        return self.store.get(("umo", umo, key), default)

    async def session_put(self, umo, key, value):
        self.store[("umo", umo, key)] = value

    async def global_get(self, key, default=None):
        return self.store.get(("global", key), default)

    async def global_put(self, key, value):
        self.store[("global", key)] = value


class _Star:
    def __init__(self, context):
        self.context = context


def _register(*args, **kwargs):
    def deco(cls):
        return cls

    return deco


FILTER = _Filter()
LOGGER = _Logger()
SP = _SP()


def install_stub():
    """把桩件塞进 sys.modules，必须在 import main.py 之前调用。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = LOGGER
    api.sp = SP

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.filter = FILTER

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.register = _register

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


def load_plugin(path, module_name="plugin_under_test"):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# 假 Context / 假 LLM
# --------------------------------------------------------------------------


class FakePersona:
    def __init__(self, persona_id, system_prompt):
        self.persona_id = persona_id
        self.system_prompt = system_prompt


class FakePersonaManager:
    def __init__(self, personas, default_name="default"):
        self.personas = [FakePersona(i, p) for i, p in personas]
        self.default_name = default_name
        self.created = []
        self.updated = []
        self.deleted = []

    async def get_all_personas(self):
        return list(self.personas)

    async def create_persona(self, persona_id, system_prompt, **kwargs):
        if any(p.persona_id == persona_id for p in self.personas):
            raise ValueError(f"Persona with ID {persona_id} already exists.")
        p = FakePersona(persona_id, system_prompt)
        self.personas.append(p)
        self.created.append(persona_id)
        return p

    async def update_persona(self, persona_id, system_prompt=None, **kwargs):
        for p in self.personas:
            if p.persona_id == persona_id:
                p.system_prompt = system_prompt
                self.updated.append((persona_id, system_prompt))
                return p
        raise ValueError("not found")

    async def delete_persona(self, persona_id):
        self.personas = [p for p in self.personas if p.persona_id != persona_id]
        self.deleted.append(persona_id)

    async def get_default_persona_v3(self, umo=None):
        # AstrBot 返回 Personality(TypedDict)
        return {"name": self.default_name}


class FakeConversation:
    def __init__(self, cid, persona_id):
        self.conversation_id = cid
        self.persona_id = persona_id


class FakeConversationManager:
    def __init__(self, current_persona_id=None):
        self.convs = {}
        self.updated = []
        self.created = []

    async def get_curr_conversation_id(self, umo):
        c = self.convs.get(umo)
        return c.conversation_id if c else None

    async def get_conversation(self, umo, cid):
        return self.convs.get(umo)

    async def new_conversation(self, umo, platform_id=None, persona_id=None, **kw):
        c = FakeConversation("conv-1", persona_id)
        self.convs[umo] = c
        self.created.append((umo, persona_id))
        return c.conversation_id

    async def update_conversation(self, umo, conversation_id=None, persona_id=None, **kw):
        c = self.convs.get(umo)
        if c is None:
            c = FakeConversation(conversation_id or "conv-1", persona_id)
            self.convs[umo] = c
        else:
            c.persona_id = persona_id
        self.updated.append((umo, conversation_id, persona_id))


class FakeResponse:
    def __init__(self, text):
        self.completion_text = text


class FakeProvider:
    """假 LLM：返回可辨识的标记，便于判断到底有没有调用 / 写库。"""

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        return FakeResponse("[REWRITTEN-BY-LLM]")


class FakeContext:
    def __init__(self, persona_manager, conversation_manager, provider=None):
        self.persona_manager = persona_manager
        self.conversation_manager = conversation_manager
        self._provider = provider or FakeProvider()

    def get_provider_by_id(self, provider_id):
        return self._provider

    async def get_using_provider_async(self, umo):
        return self._provider


class FakeEvent:
    def __init__(self, message_str, umo="aiocqhttp:private:10001", admin=True):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self._admin = admin

    def is_admin(self):
        return self._admin

    def is_private_chat(self):
        return True

    def get_platform_id(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return ("plain", text)
