"""AstrBot 人格工坊（Persona Studio）。

在对话中完成三件事：

1. 新建人格：``/人格创建 <名称> <描述>``，描述可由 LLM 扩写成完整的系统提示词；
2. 随时切换人格：``/人格切换 <名称>``，作用于当前会话（群聊 / 私聊各自独立）；
3. 用 LLM 修改人格：``/人格修改 <名称> <要求>``，让 LLM 按你的要求重写提示词。

另有 ``/人格``（列表）与 ``/人格查看``、``/人格删除`` 三个辅助指令。

依赖：仅使用 AstrBot 内置接口（``Context.persona_manager`` / ``conversation_manager``），
不需要额外第三方包。
"""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_persona_studio"
PLUGIN_VERSION = "1.0.6"

# ---------------------------------------------------------------- 常量

#: 会话人格被显式置为「不启用任何人格」时使用的哨兵值。
#: 已在 AstrBot v4.28.1 源码中核对：PersonaManager.resolve_selected_persona()
#: 对该字面量做了显式分支（`if persona_id == "[%None]": pass`），必须与上游保持一致。
NO_PERSONA = "[%None]"
#: 不允许被普通指令占用 / 覆盖、也不允许删除的人格名
RESERVED_PERSONA_IDS = {"default", NO_PERSONA}

DEFAULT_ALIASES = {"默认", "default", "重置", "reset", "默认人格"}
CURRENT_ALIASES = {"当前", "current", "本会话", "现在"}
NONE_ALIASES = {"无", "none", "关闭", "不使用", "不启用", "空"}
#: 与内置别名冲突的人格名。历史遗留的仍可正常使用 / 删除，但不再允许新建——
#: 否则会出现「能建、能看、能改，就是切不过去」的死角（/人格切换 无 会被当成关闭人格）。
ALIAS_PERSONA_NAMES = DEFAULT_ALIASES | CURRENT_ALIASES | NONE_ALIASES
#: 破坏性操作的确认旗标，可出现在参数任意位置
CONFIRM_FLAGS = {"--yes", "-y", "--confirm", "--确认"}
#: 非管理员创建的人格 → 创建者 umo 的映射（scope=global），用于归属隔离
OWNER_SP_KEY = "persona_studio_owners"
#: 修改前的旧提示词备份（scope=global），供 /人格还原 回滚
BACKUP_SP_KEY = "persona_studio_backup"
#: 单个人格最多保留的备份份数
MAX_BACKUPS = 20

MAX_PERSONA_ID_LEN = 32
MAX_RAW_INPUT_LEN = 2000
MAX_SHOW_LEN = 1500
#: /人格 列表默认最多显示多少条，避免人格多时刷屏
MAX_LIST_ITEMS = 30
#: 句子标点：首 token 里出现这些字符，基本可以断定整段是「修改要求」而不是人格名
SENTENCE_PUNCTUATION = set("，。！？、；：（）【】《》“”‘’,.!?;:()[]{}~—…")
#: LLM 工具读取单个人格提示词时的默认最大字符数
LLM_TOOL_MAX_CHARS = 4000

# 指令名与别名：只使用中文名，避免 persona / persona_* 与内置指令或其他插件冲突
CMD_LIST = ("人格", "人格列表")
CMD_CREATE = ("人格创建", "创建人格", "新建人格")
CMD_SWITCH = ("人格切换", "切换人格")
CMD_SHOW = ("人格查看", "查看人格")
CMD_EDIT = ("人格修改", "修改人格", "人格调整")
CMD_DELETE = ("人格删除", "删除人格")
CMD_RESTORE = ("人格还原", "还原人格", "人格回滚")

USAGE = (
    "🎭 人格工坊指令：\n"
    "/人格 列出所有人格\n"
    "/人格创建 <名称> <描述> 新建人格（描述会由 LLM 扩写；加 --raw 直接用原文）\n"
    "/人格切换 <名称> 切换当前会话人格（可用「默认」还原）\n"
    "/人格修改 <名称> <修改要求> 让 LLM 按你的要求重写人格（可写「当前」改本会话人格）\n"
    "/人格查看 [名称] 查看人格的系统提示词\n"
    "/人格还原 <名称> 撤销上一次修改，恢复修改前的提示词\n"
    "/人格删除 <名称> --yes 删除人格（必须带 --yes 确认）"
)

CREATE_SYSTEM_PROMPT = (
    "你是一名资深的 AI 人格（System Prompt）设计师。用户会用自然语言描述他想要的人格，"
    "你要把它扩写成一段结构清晰、可以直接作为大模型系统提示词使用的文本。\n\n"
    "输出要求：\n"
    "1. 只输出系统提示词本身。不要输出任何解释、标题、代码块标记、引号或前后缀。\n"
    "2. 用第二人称“你”指代这个 AI 角色。\n"
    "3. 内容应覆盖：角色身份与背景、性格特点、说话风格与语气、称呼与口头禅、"
    "行为准则与边界（哪些事情不做）、回复格式要求（长度、是否分点等）。\n"
    "4. 篇幅控制在 200~600 字，条理清晰、具体可执行，不要空话套话。\n"
    "5. 不要虚构与用户描述相冲突的设定；用户没提到的细节可以合理补充，但不要偏离描述。"
)

EDIT_SYSTEM_PROMPT = (
    "你是一名资深的 AI 人格（System Prompt）维护专家。用户会给你一段现有的系统提示词"
    "以及他的修改要求，你要按要求修改并输出修改后的完整系统提示词。\n\n"
    "输出要求：\n"
    "1. 只输出修改后的完整系统提示词。不要输出任何解释、差异说明、标题或代码块标记。\n"
    "2. 保留原文中没有被要求修改的部分，不要无故删减或重写。\n"
    "3. 修改要具体、可执行，能直接体现在模型行为上。\n"
    "4. 保持原文的段落结构与语种（除非用户明确要求改变）。\n"
    "5. 篇幅与原文相当，除非用户明确要求扩写或精简。"
)


def sanitize_llm_text(text: str) -> str:
    """清理 LLM 输出：去掉代码块围栏与常见前缀，得到纯提示词。"""
    if not text:
        return ""
    cleaned = text.strip()
    fence = re.match(r"^```[a-zA-Z0-9_+\-]*[ \t]*\n(.*?)\n?[ \t]*```$", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    cleaned = re.sub(
        r"^(系统提示词|系统提示|人格设定|人格提示词|System\s*Prompt|Prompt)\s*[:：]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'“”":
        cleaned = cleaned[1:-1].strip()
    return cleaned.strip()


def brief_text(text: str, limit: int = 60) -> str:
    """把长提示词压成一行摘要，用于列表展示。"""
    one_line = re.sub(r"\s+", " ", (text or "").strip())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + "…"


@register(
    PLUGIN_NAME,
    "color",
    "在对话中创建人格、随时切换人格，并可调用 LLM 修改人格",
    PLUGIN_VERSION,
)
class PersonaStudioPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}

    # ------------------------------------------------------------ 配置读取

    def _get(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:  # 配置对象异常时不阻断指令
            return default
        if value is None:
            return default
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "是", "开", "开启"}
            return bool(value)
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return value

    @property
    def _manage_requires_admin(self) -> bool:
        return self._get("manage_requires_admin", True)

    @property
    def _allow_private_chat_manage(self) -> bool:
        return self._get("allow_private_chat_manage", True)

    @property
    def _use_llm_expand(self) -> bool:
        return self._get("use_llm_expand", True)

    @property
    def _auto_switch_after_create(self) -> bool:
        return self._get("auto_switch_after_create", True)

    @property
    def _max_show_length(self) -> int:
        value = self._get("max_show_length", MAX_SHOW_LEN)
        return max(200, min(int(value), 10000))

    @property
    def _max_list_items(self) -> int:
        value = self._get("max_list_items", MAX_LIST_ITEMS)
        return max(5, min(int(value), 200))

    @property
    def _persona_llm_provider_id(self) -> str:
        """配置里指定的「人格扩写/修改」模型 id；空字符串表示跟随当前会话模型。"""
        value = self._get("persona_llm_provider", "")
        return value.strip() if isinstance(value, str) else ""

    # ------------------------------------------------------------ 通用工具

    @staticmethod
    def _arg_str(event: AstrMessageEvent, names: tuple[str, ...]) -> str:
        """取出指令后面的参数文本。

        AstrBot 在唤醒阶段已经剥掉了唤醒前缀，因此这里只需剥掉指令名本身。
        """
        msg = (event.message_str or "").strip()
        lowered = msg.lower()
        for name in sorted(names, key=len, reverse=True):
            target = name.lower()
            if lowered == target:
                return ""
            if lowered.startswith(target + " "):
                return msg[len(name) :].strip()
        # 兜底：未知写法时丢掉第一个 token
        parts = msg.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _validate_persona_id(name: str) -> str | None:
        """校验人格名称，返回错误说明；合法时返回 None。"""
        if not name:
            return "人格名称不能为空"
        if len(name) > MAX_PERSONA_ID_LEN:
            return f"人格名称太长（最多 {MAX_PERSONA_ID_LEN} 个字符）"
        if name in RESERVED_PERSONA_IDS or name.lower() in RESERVED_PERSONA_IDS:
            return f"「{name}」是保留名称，请换一个"
        if name in ALIAS_PERSONA_NAMES or name.lower() in ALIAS_PERSONA_NAMES:
            return (
                f"「{name}」与内置别名冲突（「当前」「默认」「无」等），"
                "会导致 /人格切换 永远切不到它，请换一个名字"
            )
        if re.search(r"[\s/\\:*?\"<>|]", name):
            return "人格名称不能包含空格或 / \\ : * ? \" < > | 等字符"
        return None

    @staticmethod
    def _looks_like_persona_id(token: str) -> bool:
        """判断一个 token 是否「看起来像人格名」。

        用于区分「人格名打错」与「省略人格名、直接写修改要求」：
        只有形如人格名（名称合法、长度不超限、不含句子标点）的 token 才按名字处理。
        """
        if PersonaStudioPlugin._validate_persona_id(token) is not None:
            return False
        return not any(ch in SENTENCE_PUNCTUATION for ch in token)

    # ------------------------------------------------------------ 文本 / 旗标

    def _clip_preview(self, text: str) -> str:
        """按 max_show_length 截断提示词，并显式标注已截断。

        旧版本只有 /人格查看 会标注，/人格创建 与 /人格修改 的回执是静默截断的，
        用户会以为看到的就是完整提示词。
        """
        text = text or ""
        limit = self._max_show_length
        if len(text) <= limit:
            return text
        return f"{text[:limit]}\n……（已截断，完整提示词共 {len(text)} 字）"

    @staticmethod
    def _strip_confirm_flags(text: str) -> tuple[str, bool]:
        """摘掉参数里的确认旗标，返回 (剩余文本, 是否带确认)。"""
        confirmed = False
        kept = []
        for token in text.split():
            if token.lower() in CONFIRM_FLAGS:
                confirmed = True
                continue
            kept.append(token)
        return " ".join(kept), confirmed

    @staticmethod
    def _strip_raw_flag(description: str) -> tuple[str, bool]:
        """识别任意位置的 --raw（独立 token），返回 (去掉旗标后的描述, 是否命中)。

        旧版本只认描述开头的 --raw，写在末尾会被当成正文写进人格。
        """
        if not re.search(r"(?:^|\s)--raw(?=\s|$)", description):
            return description, False
        cleaned = re.sub(r"(?:(?<=\s)|^)--raw(?=\s|$)", "", description, count=1)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        return cleaned, True

    # ------------------------------------------------------------ 归属（非管理员隔离）

    async def _load_owners(self) -> dict:
        try:
            data = await sp.global_get(OWNER_SP_KEY, {})
        except Exception as exc:
            logger.debug(f"读取人格归属失败（忽略）: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    async def _save_owners(self, owners: dict) -> None:
        try:
            await sp.global_put(OWNER_SP_KEY, owners)
        except Exception as exc:
            logger.warning(f"保存人格归属失败: {exc}")

    async def _set_owner(self, persona_id: str, umo: str) -> None:
        owners = await self._load_owners()
        owners[persona_id] = umo
        await self._save_owners(owners)

    async def _drop_owner(self, persona_id: str) -> None:
        owners = await self._load_owners()
        if owners.pop(persona_id, None) is not None:
            await self._save_owners(owners)

    async def _check_persona_ownership(
        self, event: AstrMessageEvent, persona_id: str
    ) -> str | None:
        """非管理员只能改 / 删自己创建的人格。允许时返回 None，否则返回错误说明。

        管理员不受限制。由 WebUI 或管理员创建的人格没有归属记录，一律视为
        「公共人格」，非管理员不能改 / 删——这样就不会出现「抢注名字」或
        「误删别人的人格」。
        """
        if event.is_admin():
            return None
        owner = (await self._load_owners()).get(persona_id)
        if owner is None:
            return (
                f"❌ 「{persona_id}」是公共人格（由管理员或 WebUI 创建），"
                "你不能修改或删除它。\n"
                "需要自己的版本的话，用 /人格创建 建一个你自己的。"
            )
        if owner != event.unified_msg_origin:
            return f"❌ 「{persona_id}」是其他使用者创建的人格，你不能修改或删除它。"
        return None

    # ------------------------------------------------------------ 修改前备份（可回滚）

    async def _backup_prompt(self, persona_id: str, prompt: str) -> None:
        try:
            backups = await sp.global_get(BACKUP_SP_KEY, {})
            if not isinstance(backups, dict):
                backups = {}
            stack = backups.get(persona_id)
            if not isinstance(stack, list):
                stack = []
            stack.append(prompt or "")
            backups[persona_id] = stack[-MAX_BACKUPS:]
            await sp.global_put(BACKUP_SP_KEY, backups)
        except Exception as exc:
            logger.warning(f"备份人格 {persona_id} 的旧提示词失败: {exc}")

    async def _pop_backup(self, persona_id: str) -> str | None:
        try:
            backups = await sp.global_get(BACKUP_SP_KEY, {})
            if not isinstance(backups, dict):
                return None
            stack = backups.get(persona_id)
            if not isinstance(stack, list) or not stack:
                return None
            prompt = stack.pop()
            if stack:
                backups[persona_id] = stack
            else:
                backups.pop(persona_id, None)
            await sp.global_put(BACKUP_SP_KEY, backups)
            return prompt
        except Exception as exc:
            logger.warning(f"读取人格 {persona_id} 的备份失败: {exc}")
            return None

    async def _backup_count(self, persona_id: str) -> int:
        try:
            backups = await sp.global_get(BACKUP_SP_KEY, {})
        except Exception:
            return 0
        if not isinstance(backups, dict):
            return 0
        stack = backups.get(persona_id)
        return len(stack) if isinstance(stack, list) else 0

    async def _drop_backups(self, persona_id: str) -> None:
        try:
            backups = await sp.global_get(BACKUP_SP_KEY, {})
            if isinstance(backups, dict) and backups.pop(persona_id, None) is not None:
                await sp.global_put(BACKUP_SP_KEY, backups)
        except Exception as exc:
            logger.debug(f"清理人格 {persona_id} 的备份失败（忽略）: {exc}")

    async def _all_personas(self) -> list:
        personas = await self.context.persona_manager.get_all_personas()
        return sorted(personas, key=lambda item: item.persona_id)

    async def _find_persona(self, persona_id: str):
        if not persona_id:
            return None
        for persona in await self._all_personas():
            if persona.persona_id == persona_id:
                return persona
        return None

    async def _persona_ids(self) -> list[str]:
        return [persona.persona_id for persona in await self._all_personas()]

    async def _current_persona_id(self, umo: str) -> str | None:
        """当前会话最终生效的人格 id（会话级强制人格优先）。"""
        try:
            session_config = (
                await sp.session_get(umo, "session_service_config", {}) or {}
            )
        except Exception:
            session_config = {}
        if not isinstance(session_config, dict):
            session_config = {}
        forced = session_config.get("persona_id")
        if forced:
            return forced

        try:
            conversation_manager = self.context.conversation_manager
            conversation_id = await conversation_manager.get_curr_conversation_id(umo)
            if not conversation_id:
                return None
            conversation = await conversation_manager.get_conversation(
                umo, conversation_id
            )
        except Exception as exc:
            logger.debug(f"读取当前会话人格失败: {exc}")
            return None
        return getattr(conversation, "persona_id", None)

    async def _default_persona_id(self, umo: str) -> str:
        """解析「默认」应指向的人格 id。"""
        persona_id = ""
        try:
            default_persona = await self.context.persona_manager.get_default_persona_v3(
                umo
            )
            persona_id = (default_persona or {}).get("name") or ""
        except Exception as exc:
            logger.debug(f"读取默认人格失败: {exc}")
        if persona_id and await self._find_persona(persona_id):
            return persona_id
        return NO_PERSONA

    async def _apply_persona(self, event: AstrMessageEvent, persona_id: str) -> str:
        """把人格应用到当前会话，返回会话（对话）id。"""
        umo = event.unified_msg_origin
        conversation_manager = self.context.conversation_manager

        # 会话级强制人格优先级高于对话人格，先清掉，避免切换被静默覆盖
        try:
            session_config = (
                await sp.session_get(umo, "session_service_config", {}) or {}
            )
            # 与 _current_persona_id 保持一致的防御：非 dict 时放弃清理，
            # 不要让 AttributeError 被下面的 except 吞成一条看不见的 debug 日志。
            if not isinstance(session_config, dict):
                logger.warning(
                    "session_service_config 不是 dict（实际 %s），跳过会话级人格清理",
                    type(session_config).__name__,
                )
                session_config = {}
            if session_config.get("persona_id"):
                session_config.pop("persona_id", None)
                await sp.session_put(umo, "session_service_config", session_config)
        except Exception as exc:
            # 清理失败会让「切换人格」被会话级强制人格覆盖，表现为切换不生效，
            # 这是最难排查的一类现象，因此用 warning 而非 debug。
            logger.warning(f"清理会话级人格失败（本次切换可能不生效）: {exc}")

        conversation_id = await conversation_manager.get_curr_conversation_id(umo)
        if not conversation_id:
            conversation_id = await conversation_manager.new_conversation(
                umo,
                platform_id=event.get_platform_id(),
                persona_id=persona_id,
            )
        else:
            await conversation_manager.update_conversation(
                umo,
                conversation_id=conversation_id,
                persona_id=persona_id,
            )
        return conversation_id

    def _lookup_provider(self, provider_id: str):
        """按 id 取提供商，兼容不同 AstrBot 版本（Context 方法 / provider_manager）。"""
        getter = getattr(self.context, "get_provider_by_id", None)
        if callable(getter):
            try:
                provider = getter(provider_id)
            except Exception as exc:
                logger.warning(f"按 id 获取模型 {provider_id} 失败: {exc}")
            else:
                if provider is not None:
                    return provider
        try:
            inst_map = getattr(self.context.provider_manager, "inst_map", None)
        except Exception:
            inst_map = None
        if isinstance(inst_map, dict):
            return inst_map.get(provider_id)
        return None

    async def _resolve_llm_provider(self, umo: str) -> tuple[Any, str]:
        """挑选「人格扩写 / 人格修改」用的模型。

        返回 ``(provider, note)``：note 为空表示按配置取到了模型；否则是给用户看的
        回退说明（配置的模型不存在 / 被禁用时回退为当前会话模型）。
        """
        provider_id = self._persona_llm_provider_id
        note = ""
        if provider_id:
            provider = self._lookup_provider(provider_id)
            if provider is not None:
                return provider, ""
            note = f"配置的模型 {provider_id} 不可用，已回退当前会话模型"
            logger.warning(f"配置的人格模型 {provider_id} 不可用，本次回退为当前会话模型。")
        try:
            return await self.context.get_using_provider_async(umo), note
        except Exception as exc:
            logger.warning(f"获取 LLM Provider 失败: {exc}")
            return None, note

    async def _llm_complete(
        self, umo: str, system_prompt: str, user_prompt: str
    ) -> tuple[str | None, str]:
        """调用「人格扩写/修改」用的模型，返回 (文本, 回退说明)；失败时文本为 None。"""
        provider, note = await self._resolve_llm_provider(umo)
        if provider is None:
            return None, note
        try:
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
        except Exception as exc:
            logger.warning(f"调用 LLM 失败: {exc}")
            return None, note
        text = sanitize_llm_text(getattr(response, "completion_text", "") or "")
        return (text or None), note

    async def _check_manage_permission(self, event: AstrMessageEvent) -> bool:
        """判断是否有创建 / 修改 / 删除人格的权限。"""
        if event.is_admin():
            return True
        if not self._manage_requires_admin:
            return True
        return bool(self._allow_private_chat_manage and event.is_private_chat())

    async def _unknown_persona_hint(self, name: str) -> str:
        """首 token 像人格名却查无此人时给出的提示（不修改任何人格）。"""
        if name.lower() in NONE_ALIASES:
            return (
                f"❌ 「{name}」表示不使用人格，没有可修改的提示词。\n"
                "先用 /人格切换 <名称> 启用一个人格，再修改它。"
            )
        try:
            ids = await self._persona_ids()
        except Exception:
            ids = []
        hint = "、".join(ids) if ids else "（暂无自定义人格）"
        return (
            f"❌ 找不到人格「{name}」，没有修改任何人格。\n"
            f"现有：{hint}\n"
            f"· 想改某个人格：/人格修改 <名称> <修改要求>\n"
            f"· 想改当前会话的人格：/人格修改 当前 <修改要求>"
        )

    # ------------------------------------------------------------ LLM 工具（让模型读取人格）

    @property
    def _llm_tools_enabled(self) -> bool:
        return self._get("enable_llm_tools", True)

    @property
    def _llm_tool_max_chars(self) -> int:
        value = self._get("llm_tool_max_chars", LLM_TOOL_MAX_CHARS)
        return max(200, min(int(value), 50000))

    def _llm_tool_disabled(self) -> str:
        return "人格读取工具已被管理员关闭（插件配置 enable_llm_tools）。"

    async def _llm_tool_current_id(self, event: AstrMessageEvent) -> str | None:
        try:
            return await self._current_persona_id(event.unified_msg_origin)
        except Exception as exc:
            logger.debug(f"读取当前人格失败: {exc}")
            return None

    @filter.llm_tool(name="persona_list")
    async def tool_persona_list(self, event: AstrMessageEvent) -> str:
        """列出 AstrBot 里所有可用人格（Persona）的 ID、提示词摘要与字数。

        当用户问"有哪些人格/角色"、"有没有某个角色"、想挑选或对比人格时调用本工具。
        本工具会返回全部人格的清单（这是读取所有人格的标准方式）。
        如果需要某个人格的完整系统提示词，拿到 ID 后再调用 persona_read。
        """
        if not self._llm_tools_enabled:
            return self._llm_tool_disabled()
        try:
            personas = await self._all_personas()
        except Exception as exc:
            logger.error(f"LLM 工具读取人格列表失败: {exc}")
            return f"读取人格列表失败：{exc}"

        current = await self._llm_tool_current_id(event)
        lines = [f"共 {len(personas)} 个人格："]
        if personas:
            for index, persona in enumerate(personas, start=1):
                prompt = persona.system_prompt or ""
                lines.append(
                    f"{index}. ID={persona.persona_id} | 字数={len(prompt)} | "
                    f"摘要：{brief_text(prompt)}"
                )
        else:
            lines.append("（当前没有任何人格）")

        if current == NO_PERSONA:
            lines.append("当前会话：已显式关闭人格，不使用任何人格。")
        else:
            lines.append(f"当前会话正在使用的人格 ID：{current or '（未指定，跟随全局默认）'}")
        lines.append("需要完整系统提示词时，用 persona_read 工具并传入对应 ID。")
        return "\n".join(lines)

    @filter.llm_tool(name="persona_read")
    async def tool_persona_read(self, event: AstrMessageEvent, persona_id: str) -> str:
        """读取指定人格的完整系统提示词（角色设定）。

        当用户想知道某个人格具体是怎么设定的、或需要按某人格的口吻行事时调用。
        先用 persona_list 拿到准确的人格 ID，再把 ID 传给本工具。

        Args:
            persona_id(string): 要读取的人格 ID（例如「猫娘」），可从 persona_list 获取
        """
        if not self._llm_tools_enabled:
            return self._llm_tool_disabled()
        persona_id = (persona_id or "").strip()
        if not persona_id:
            return "缺少参数 persona_id，请先调用 persona_list 获取人格 ID。"

        try:
            persona = await self._find_persona(persona_id)
        except Exception as exc:
            logger.error(f"LLM 工具读取人格 {persona_id} 失败: {exc}")
            return f"读取人格失败：{exc}"

        if persona is None:
            try:
                ids = await self._persona_ids()
            except Exception:
                ids = []
            return (
                f"找不到人格「{persona_id}」。现有 ID：{'、'.join(ids) if ids else '（无）'}。"
            )

        prompt = persona.system_prompt or ""
        limit = self._llm_tool_max_chars
        if len(prompt) > limit:
            prompt = prompt[:limit] + f"\n……（提示词共 {len(persona.system_prompt or '')} 字，此处截断）"
        return f"人格「{persona.persona_id}」的系统提示词：\n{prompt}"

    @filter.llm_tool(name="persona_current")
    async def tool_persona_current(self, event: AstrMessageEvent) -> str:
        """读取当前会话正在生效的人格（ID + 完整系统提示词）。

        当需要确认"我现在是什么角色/人格"、或当前人格的设定是什么时调用本工具。
        """
        if not self._llm_tools_enabled:
            return self._llm_tool_disabled()
        current = await self._llm_tool_current_id(event)
        if not current:
            return "当前会话没有指定人格，AstrBot 正在使用默认行为（不注入人格提示词）。"
        if current == NO_PERSONA:
            return "当前会话已显式关闭人格，不注入任何人格提示词。"

        try:
            persona = await self._find_persona(current)
        except Exception as exc:
            return f"读取当前人格失败：{exc}"
        if persona is None:
            return f"当前会话指定的人格 ID 是「{current}」，但它已经不存在了（可能已被删除）。"

        prompt = persona.system_prompt or ""
        limit = self._llm_tool_max_chars
        if len(prompt) > limit:
            prompt = prompt[:limit] + f"\n……（提示词共 {len(persona.system_prompt or '')} 字，此处截断）"
        return f"当前会话生效的人格：「{persona.persona_id}」\n系统提示词：\n{prompt}"

    # ------------------------------------------------------------ 指令实现

    @filter.command(CMD_LIST[0], alias=set(CMD_LIST[1:]))
    async def persona_list(self, event: AstrMessageEvent):
        """列出所有人格，并标注当前会话生效的人格。"""
        umo = event.unified_msg_origin
        try:
            personas = await self._all_personas()
            current = await self._current_persona_id(umo)
        except Exception as exc:
            logger.error(f"读取人格列表失败: {exc}")
            yield event.plain_result(f"❌ 读取人格列表失败：{exc}")
            return

        lines = ["🎭 可用人格："]
        if personas:
            limit = self._max_list_items
            shown = personas[:limit]
            for persona in shown:
                mark = "✅" if persona.persona_id == current else "•"
                lines.append(
                    f"{mark} {persona.persona_id} — {brief_text(persona.system_prompt)}"
                )
            hidden = len(personas) - len(shown)
            if hidden > 0:
                lines.append(
                    f"…… 还有 {hidden} 个人格未显示（最多显示 {limit} 个，"
                    f"可用 /人格查看 <名称> 直接查看）"
                )
        else:
            lines.append("（还没有自定义人格）")

        if current == NO_PERSONA:
            current_text = "（已显式关闭，不使用任何人格）"
        else:
            current_text = current or "（未指定，跟随全局默认）"
        lines.append(f"\n当前会话人格：{current_text}")
        model = self._persona_llm_provider_id or "（跟随当前会话模型）"
        lines.append(f"⚙️ 扩写/修改人格使用的模型：{model}")
        lines.append("\n" + USAGE)
        yield event.plain_result("\n".join(lines))

    @filter.command(CMD_CREATE[0], alias=set(CMD_CREATE[1:]))
    async def persona_create(self, event: AstrMessageEvent):
        """在对话中新建人格。"""
        if not await self._check_manage_permission(event):
            yield event.plain_result("❌ 你没有新建人格的权限（需要管理员）。")
            return

        raw_arg = self._arg_str(event, CMD_CREATE)
        if not raw_arg:
            yield event.plain_result(
                "用法：/人格创建 <名称> <描述>\n例如：/人格创建 猫娘 一只傲娇的猫娘，说话带喵"
            )
            return

        parts = raw_arg.split(None, 1)
        name = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""

        error = self._validate_persona_id(name)
        if error:
            yield event.plain_result(f"❌ {error}")
            return
        if not description:
            yield event.plain_result(
                f"❌ 还差一段描述哦。用法：/人格创建 {name} <这个人的性格、说话风格、设定……>"
            )
            return
        if len(description) > MAX_RAW_INPUT_LEN:
            description = description[:MAX_RAW_INPUT_LEN]

        try:
            if await self._find_persona(name):
                yield event.plain_result(
                    f"❌ 已存在同名人格「{name}」，换个名字，"
                    f"或用 /人格修改 {name} <要求> 改现有的人格。"
                )
                return
        except Exception as exc:
            yield event.plain_result(f"❌ 查询人格失败：{exc}")
            return

        # 是否用 LLM 扩写描述（--raw 可强制只用原文，位置不限）
        use_llm = self._use_llm_expand
        description, has_raw = self._strip_raw_flag(description)
        if has_raw:
            use_llm = False
        if not description:
            yield event.plain_result("❌ 描述不能为空。")
            return

        system_prompt = description
        llm_note = "（已直接使用你的描述作为提示词）"
        if use_llm:
            generated, note = await self._llm_complete(
                event.unified_msg_origin,
                CREATE_SYSTEM_PROMPT,
                f"人格名称：{name}\n用户的描述：{description}\n\n"
                "请输出这个人格的系统提示词。",
            )
            if generated:
                system_prompt = generated
                llm_note = "（已由 LLM 扩写为完整系统提示词）"
                if note:
                    llm_note = f"（已由 LLM 扩写为完整系统提示词；{note}）"
            else:
                llm_note = "（LLM 调用失败，已直接使用你的描述作为提示词）"

        try:
            await self.context.persona_manager.create_persona(
                persona_id=name,
                system_prompt=system_prompt,
            )
        except Exception as exc:
            logger.error(f"创建人格 {name} 失败: {exc}")
            yield event.plain_result(f"❌ 创建人格「{name}」失败：{exc}")
            return

        # 非管理员创建的人格记下归属：之后只有创建者本人（或管理员）能改 / 删，
        # 避免任何私聊使用者都能动全局人格库。
        if not event.is_admin():
            await self._set_owner(name, event.unified_msg_origin)

        switched = False
        if self._auto_switch_after_create:
            try:
                await self._apply_persona(event, name)
                switched = True
            except Exception as exc:
                logger.warning(f"创建后切换人格失败: {exc}")

        reply = [
            f"✅ 已创建人格「{name}」{llm_note}",
            "",
            f"📝 系统提示词预览：\n{self._clip_preview(system_prompt)}",
            "",
        ]
        if switched:
            reply.append("🎉 已自动切换到该人格，直接聊天即可生效。")
        else:
            reply.append(f"用 /人格切换 {name} 即可启用。")
        reply.append(f"不满意可以 /人格修改 {name} <你的修改要求>。")
        yield event.plain_result("\n".join(reply))

    @filter.command(CMD_SWITCH[0], alias=set(CMD_SWITCH[1:]))
    async def persona_switch(self, event: AstrMessageEvent):
        """切换当前会话使用的人格。"""
        raw_arg = self._arg_str(event, CMD_SWITCH).strip()
        if not raw_arg:
            try:
                current = await self._current_persona_id(event.unified_msg_origin)
            except Exception:
                current = None
            yield event.plain_result(
                f"当前会话人格：{current or '（未指定）'}\n"
                "用法：/人格切换 <名称>，可用「默认」还原，用 /人格 查看全部人格。"
            )
            return

        target = raw_arg
        lowered = raw_arg.lower()
        # 先查库：确实存在同名人格时，真实人格优先于内置别名。
        # 否则历史上创建的名为「无」「关闭」「当前」的人格会永远切不过去。
        try:
            exists = await self._find_persona(target)
        except Exception as exc:
            yield event.plain_result(f"❌ 查询人格失败：{exc}")
            return

        if exists is None:
            if lowered in DEFAULT_ALIASES:
                target = await self._default_persona_id(event.unified_msg_origin)
            elif lowered in NONE_ALIASES:
                target = NO_PERSONA
            else:
                ids = await self._persona_ids()
                hint = "、".join(ids) if ids else "（暂无自定义人格）"
                yield event.plain_result(
                    f"❌ 找不到人格「{target}」。\n现有：{hint}\n"
                    f"用 /人格创建 {target} <描述> 新建一个。"
                )
                return

        try:
            await self._apply_persona(event, target)
        except Exception as exc:
            logger.error(f"切换人格到 {target} 失败: {exc}")
            yield event.plain_result(f"❌ 切换人格失败：{exc}")
            return

        if target == NO_PERSONA:
            yield event.plain_result(
                "✅ 已还原为「不使用任何人格」：本会话不再注入人格提示词（等同 AstrBot 默认行为）。"
            )
            return
        yield event.plain_result(
            f"✅ 已切换到人格「{target}」，本会话后续回复都会使用它。\n"
            f"不满意可以 /人格修改 {target} <你的修改要求>。"
        )

    @filter.command(CMD_SHOW[0], alias=set(CMD_SHOW[1:]))
    async def persona_show(self, event: AstrMessageEvent):
        """查看某个人格的完整系统提示词。"""
        raw_arg = self._arg_str(event, CMD_SHOW).strip()
        lowered = raw_arg.lower()
        target = raw_arg
        persona = None

        async def _find(pid):
            """返回 (persona, 错误文本)。"""
            try:
                return await self._find_persona(pid), None
            except Exception as exc:
                return None, f"❌ 查询人格失败：{exc}"

        # 1) 先按字面名查库：真实人格优先于内置别名
        if target:
            persona, err = await _find(target)
            if err:
                yield event.plain_result(err)
                return

        # 2) 没命中再按别名解析（「当前」/「默认」）
        if persona is None and (not target or lowered in CURRENT_ALIASES):
            try:
                target = await self._current_persona_id(event.unified_msg_origin)
            except Exception:
                target = None
            if target and target != NO_PERSONA:
                persona, err = await _find(target)
                if err:
                    yield event.plain_result(err)
                    return
        elif persona is None and lowered in DEFAULT_ALIASES:
            target = await self._default_persona_id(event.unified_msg_origin)
            if target and target != NO_PERSONA:
                persona, err = await _find(target)
                if err:
                    yield event.plain_result(err)
                    return
            else:
                yield event.plain_result(
                    "❌ AstrBot 当前没有配置可查看的默认人格（全局默认是内置的"
                    "「default」，不是自定义人格）。用法：/人格查看 <名称>。"
                )
                return

        # 3) 仍未命中：区分「会话没挂人格」与「名字写错」
        if persona is None:
            if not raw_arg or lowered in CURRENT_ALIASES:
                yield event.plain_result(
                    "❌ 当前会话没有生效的人格。用法：/人格查看 <名称>，或先 /人格切换。"
                )
            else:
                yield event.plain_result(
                    f"❌ 找不到人格「{target}」，用 /人格 查看现有列表。"
                )
            return

        yield event.plain_result(
            f"🎭 人格「{persona.persona_id}」的系统提示词：\n\n"
            f"{self._clip_preview(persona.system_prompt or '')}"
        )

    @filter.command(CMD_EDIT[0], alias=set(CMD_EDIT[1:]))
    async def persona_edit(self, event: AstrMessageEvent):
        """调用 LLM 按用户要求修改人格。"""
        if not await self._check_manage_permission(event):
            yield event.plain_result("❌ 你没有修改人格的权限（需要管理员）。")
            return

        raw_arg = self._arg_str(event, CMD_EDIT)
        if not raw_arg:
            yield event.plain_result(
                "用法：/人格修改 <名称> <修改要求>\n"
                "例如：/人格修改 猫娘 说话再傲娇一点，每句结尾加“喵”\n"
                "改当前会话的人格：/人格修改 当前 <修改要求>（或整段不写名字，如 /人格修改 说话简短一点）"
            )
            return

        parts = raw_arg.split(None, 1)
        first = parts[0]
        target: str | None = None
        requirement = ""
        used_default_alias = False
        if len(parts) > 1:
            try:
                persona = await self._find_persona(first)
            except Exception as exc:
                yield event.plain_result(f"❌ 查询人格失败：{exc}")
                return
            if persona is not None:
                target = first
                requirement = parts[1].strip()
            elif first.lower() in CURRENT_ALIASES:
                requirement = parts[1].strip()
            elif first.lower() in DEFAULT_ALIASES:
                target = await self._default_persona_id(event.unified_msg_origin)
                requirement = parts[1].strip()
                used_default_alias = True
            elif first.lower() in NONE_ALIASES:
                # 「无」「关闭」等是「不使用人格」的别名，不是人格名，
                # 别让它们落进「整段当修改要求」的分支去改当前人格。
                yield event.plain_result(await self._unknown_persona_hint(first))
                return
            elif self._looks_like_persona_id(first):
                # 首 token 长得像人格名、库里却没有：多半是名字打错了。
                # 只提示，不静默把它当成「改当前会话人格」，避免误改。
                yield event.plain_result(await self._unknown_persona_hint(first))
                return
        else:
            # 只有一个 token，两种可能：人格名漏写了修改要求，或整段就是修改要求。
            # 必须先查库确认它是不是已存在的人格名：旧版本直接把它当成修改要求发给
            # LLM，会把「/人格修改 诗人」误当成「按『诗人』重写当前会话人格」并写库。
            try:
                persona = await self._find_persona(first)
            except Exception as exc:
                yield event.plain_result(f"❌ 查询人格失败：{exc}")
                return
            if persona is not None:
                # 名字存在但没写要求：只提示补全，绝不把它当作修改要求
                yield event.plain_result(
                    f"❌ 请说明要对人格「{first}」改什么。\n"
                    f"用法：/人格修改 {first} <修改要求>\n"
                    f"例如：/人格修改 {first} 说话更简洁一点\n"
                    f"想改当前会话人格的话写：/人格修改 当前 {first}"
                )
                return
            if first.lower() in CURRENT_ALIASES or first.lower() in DEFAULT_ALIASES:
                # 只写了「当前」/「默认」，没有修改要求：别把别名本身当成要求发给 LLM
                yield event.plain_result(
                    "❌ 请说明要改什么。用法：/人格修改 当前 <修改要求>"
                )
                return
            if first.lower() in NONE_ALIASES:
                # 同理，「无」「关闭」是「不使用人格」的别名，不是修改要求
                yield event.plain_result(await self._unknown_persona_hint(first))
                return
            # 既不是人格名也不是别名：整段当修改要求，作用于当前会话人格（文档约定的简写）
            requirement = raw_arg
            target = None
        if not requirement:
            # 首 token 不像人格名（含句子标点等）：整段都是修改要求，作用于当前会话人格
            requirement = raw_arg
            target = None
        if target is None:
            try:
                target = await self._current_persona_id(event.unified_msg_origin)
            except Exception as exc:
                yield event.plain_result(f"❌ 读取当前人格失败：{exc}")
                return

        if not requirement:
            yield event.plain_result("❌ 请说明要改什么。")
            return
        if len(requirement) > MAX_RAW_INPUT_LEN:
            requirement = requirement[:MAX_RAW_INPUT_LEN]

        if not target or target == NO_PERSONA:
            if used_default_alias:
                yield event.plain_result(
                    "❌ AstrBot 当前没有配置可修改的默认人格（全局默认是内置的「default」，"
                    "不是自定义人格）。请指定人格名：/人格修改 <名称> <修改要求>。"
                )
            else:
                yield event.plain_result(
                    "❌ 当前会话没有生效的人格。请指定名称：/人格修改 <名称> <修改要求>。"
                )
            return

        try:
            persona = await self._find_persona(target)
        except Exception as exc:
            yield event.plain_result(f"❌ 查询人格失败：{exc}")
            return
        if not persona:
            yield event.plain_result(f"❌ 找不到人格「{target}」，用 /人格 查看现有列表。")
            return

        # 非管理员只能改自己创建的人格
        deny = await self._check_persona_ownership(event, target)
        if deny:
            yield event.plain_result(deny)
            return

        new_prompt, note = await self._llm_complete(
            event.unified_msg_origin,
            EDIT_SYSTEM_PROMPT,
            "【现有系统提示词】\n"
            f"{persona.system_prompt}\n\n"
            "【修改要求】\n"
            f"{requirement}\n\n"
            "请输出修改后的完整系统提示词。",
        )
        if not new_prompt:
            yield event.plain_result(
                "❌ 调用 LLM 失败或返回为空，未修改人格。请检查模型配置后重试。"
            )
            return

        old_prompt = persona.system_prompt or ""
        try:
            await self.context.persona_manager.update_persona(
                target, system_prompt=new_prompt
            )
        except Exception as exc:
            logger.error(f"更新人格 {target} 失败: {exc}")
            yield event.plain_result(f"❌ 更新人格「{target}」失败：{exc}")
            return

        # 更新成功后再记录旧版本，避免失败时留下无意义的备份
        await self._backup_prompt(target, old_prompt)

        reply = (
            f"✅ 已按你的要求用 LLM 重写人格「{target}」。\n\n"
            f"📝 新的系统提示词：\n{self._clip_preview(new_prompt)}\n\n"
            f"不满意可以继续 /人格修改 {target} <新的要求>；"
            f"改坏了用 /人格还原 {target} 恢复上一次的版本。"
        )
        if note:
            reply += f"\n⚠️ {note}"
        yield event.plain_result(reply)

    @filter.command(CMD_RESTORE[0], alias=set(CMD_RESTORE[1:]))
    async def persona_restore(self, event: AstrMessageEvent):
        """撤销上一次修改，恢复人格在上一次修改前的提示词。"""
        if not await self._check_manage_permission(event):
            yield event.plain_result("❌ 你没有修改人格的权限（需要管理员）。")
            return

        target = self._arg_str(event, CMD_RESTORE).strip()
        if not target:
            yield event.plain_result(
                "用法：/人格还原 <名称>\n"
                "恢复该人格上一次被 /人格修改 覆盖之前的提示词（可连续还原多步）。"
            )
            return
        if target.lower() in CURRENT_ALIASES:
            try:
                target = await self._current_persona_id(event.unified_msg_origin)
            except Exception as exc:
                yield event.plain_result(f"❌ 读取当前人格失败：{exc}")
                return
        if not target or target == NO_PERSONA:
            yield event.plain_result(
                "❌ 当前会话没有生效的人格。请指定名称：/人格还原 <名称>。"
            )
            return

        try:
            persona = await self._find_persona(target)
        except Exception as exc:
            yield event.plain_result(f"❌ 查询人格失败：{exc}")
            return
        if not persona:
            yield event.plain_result(f"❌ 找不到人格「{target}」。")
            return

        deny = await self._check_persona_ownership(event, target)
        if deny:
            yield event.plain_result(deny)
            return

        count = await self._backup_count(target)
        if count == 0:
            yield event.plain_result(
                f"❌ 人格「{target}」没有可还原的历史版本"
                "（只有经过 /人格修改 的人格才会留下备份）。"
            )
            return

        prompt = await self._pop_backup(target)
        if prompt is None:
            yield event.plain_result(f"❌ 读取人格「{target}」的历史版本失败。")
            return

        try:
            await self.context.persona_manager.update_persona(
                target, system_prompt=prompt
            )
        except Exception as exc:
            logger.error(f"还原人格 {target} 失败: {exc}")
            # 还原失败就把备份放回去，别弄丢历史版本
            await self._backup_prompt(target, prompt)
            yield event.plain_result(f"❌ 还原人格「{target}」失败：{exc}")
            return

        remain = await self._backup_count(target)
        reply = (
            f"✅ 已把人格「{target}」还原到上一次修改前的版本。\n\n"
            f"📝 当前系统提示词：\n{self._clip_preview(prompt)}"
        )
        if remain:
            reply += f"\n\n（还可以继续还原 {remain} 步）"
        yield event.plain_result(reply)

    @filter.command(CMD_DELETE[0], alias=set(CMD_DELETE[1:]))
    async def persona_delete(self, event: AstrMessageEvent):
        """删除人格。"""
        if not await self._check_manage_permission(event):
            yield event.plain_result("❌ 你没有删除人格的权限（需要管理员）。")
            return

        raw_arg = self._arg_str(event, CMD_DELETE).strip()
        target, confirmed = self._strip_confirm_flags(raw_arg)
        if not target:
            yield event.plain_result("用法：/人格删除 <名称> --yes")
            return
        if target in RESERVED_PERSONA_IDS or target.lower() in RESERVED_PERSONA_IDS:
            yield event.plain_result(f"❌ 「{target}」是保留人格，不能删除。")
            return

        try:
            persona = await self._find_persona(target)
        except Exception as exc:
            yield event.plain_result(f"❌ 查询人格失败：{exc}")
            return
        if not persona:
            yield event.plain_result(f"❌ 找不到人格「{target}」。")
            return

        deny = await self._check_persona_ownership(event, target)
        if deny:
            yield event.plain_result(deny)
            return

        # 删除不可恢复，必须显式确认
        if not confirmed:
            prompt = persona.system_prompt or ""
            yield event.plain_result(
                f"⚠️ 即将删除人格「{target}」，此操作不可恢复。\n"
                f"提示词共 {len(prompt)} 字，摘要：{brief_text(prompt)}\n\n"
                f"确认请发送：/人格删除 {target} --yes"
            )
            return

        try:
            await self.context.persona_manager.delete_persona(target)
        except Exception as exc:
            logger.error(f"删除人格 {target} 失败: {exc}")
            yield event.plain_result(f"❌ 删除人格「{target}」失败：{exc}")
            return

        # 清理归属与备份记录，避免残留脏数据
        await self._drop_owner(target)
        await self._drop_backups(target)

        note = ""
        try:
            current = await self._current_persona_id(event.unified_msg_origin)
        except Exception:
            current = None
        if current == target:
            note = "\n⚠️ 当前会话正在使用它，已回退为不使用人格设定，可 /人格切换 其他人格。"

        yield event.plain_result(f"✅ 已删除人格「{target}」。{note}")
