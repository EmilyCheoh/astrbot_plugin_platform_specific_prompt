"""PlatformPrompt: QQ / Den 专属提示词槽。

- System：每轮附加；不清理。
- First：与 RecursionLog 一样，以 contexts 数量判断首轮；不清理。
- Before / After：与 PromptTags 一样，清理历史旧标签再注入。
"""

import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


SLOT_COUNT = 10
PLATFORM_IDS = {"Abyss": "qq", "Abyss_Den": "den"}
POSITIONS = {
    "system_prompt",
    "first_message",
    "user_message_before",
    "user_message_after",
}
RECURRING_POSITIONS = {"user_message_before", "user_message_after"}
TAG_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
POSITION_LABELS = {
    "system_prompt": "system",
    "first_message": "first",
    "user_message_before": "before",
    "user_message_after": "after",
}


@register(
    "PlatformPrompt",
    "FelisAbyssalis",
    "按 QQ / Den 平台注入独立提示词",
    "1.0.0",
    "",
)
class PlatformPrompt(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info("[PlatformPrompt] 已加载：10 个平台提示词槽")

    @staticmethod
    def _event_platform(event: AstrMessageEvent) -> str | None:
        return PLATFORM_IDS.get(event.get_platform_id())

    def _slots(self) -> list[dict[str, Any]]:
        """始终从配置读取，命令开关和面板修改都能生效。"""
        result = []
        for number in range(1, SLOT_COUNT + 1):
            raw = self.config.get(f"slot_{number}", {})
            if not isinstance(raw, dict):
                raw = {}
            result.append(
                {
                    "number": number,
                    "remark": str(raw.get("remark", "") or "").strip(),
                    "enabled": raw.get("enabled", False) is True,
                    "platform": str(raw.get("platform", "den") or "").strip().lower(),
                    "position": str(raw.get("injection_position", "system_prompt") or "").strip(),
                    "tag_name": str(raw.get("tag_name", "") or "").strip(),
                    "content": str(raw.get("content", "") or "").strip(),
                }
            )
        return result

    @staticmethod
    def _valid(slot: dict[str, Any]) -> bool:
        return (
            slot["platform"] in ("qq", "den")
            and slot["position"] in POSITIONS
            and bool(TAG_NAME_RE.fullmatch(slot["tag_name"]))
            and bool(slot["content"])
        )

    @staticmethod
    def _tag(slot: dict[str, Any]) -> str:
        name = slot["tag_name"]
        return f"<{name}>\n{slot['content']}\n</{name}>"

    @staticmethod
    def _strip_tag(text: str, pattern: re.Pattern[str]) -> str:
        if not pattern.search(text):
            return text
        text = pattern.sub("", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    @classmethod
    def _clean_contexts(cls, req: ProviderRequest, patterns: list[re.Pattern[str]]) -> None:
        """只清理每轮模式的旧标签；绝不清理首轮模式和 System。"""
        if not patterns:
            return

        def clean(text: str) -> str:
            for pattern in patterns:
                text = cls._strip_tag(text, pattern)
            return text

        if isinstance(req.prompt, str):
            req.prompt = clean(req.prompt)

        if not req.contexts:
            return

        updated = []
        for message in req.contexts:
            if isinstance(message, str):
                content = clean(message)
                if content == message or content:
                    updated.append(content)
                continue
            if not isinstance(message, dict):
                updated.append(message)
                continue

            content = message.get("content")
            if isinstance(content, str):
                cleaned = clean(content)
                if cleaned == content:
                    updated.append(message)
                elif cleaned:
                    updated.append({**message, "content": cleaned})
                # 标签独占的空消息不再保留。
                continue

            if isinstance(content, list):
                new_parts = []
                changed = False
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        cleaned = clean(part["text"])
                        if cleaned != part["text"]:
                            changed = True
                            if cleaned:
                                new_parts.append({**part, "text": cleaned})
                            continue
                    new_parts.append(part)
                if new_parts or not changed:
                    updated.append({**message, "content": new_parts} if changed else message)
                continue

            updated.append(message)
        req.contexts = updated

    @filter.on_llm_request(priority=1)
    async def cleanup(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 PromptTags 注入之前，去除 PlatformPrompt 的旧每轮标签。"""
        if self._event_platform(event) is None:
            return
        patterns: list[re.Pattern[str]] = []
        for slot in self._slots():
            # disabled 的槽也要清理上一轮残留；两平台槽都要清理，避免分支带入。
            if slot["position"] not in RECURRING_POSITIONS:
                continue
            if not TAG_NAME_RE.fullmatch(slot["tag_name"]):
                continue
            name = slot["tag_name"]
            patterns.append(re.compile(rf"<{re.escape(name)}>.*?</{re.escape(name)}>", re.DOTALL))
        try:
            self._clean_contexts(req, patterns)
        except Exception:
            logger.error("[PlatformPrompt] 清理失败", exc_info=True)

    @filter.on_llm_request(priority=-497)
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """-497：System 位于 RecursionLog(-498) 前面。"""
        platform = self._event_platform(event)
        if platform is None:
            return

        try:
            threshold = max(0, int(self.config.get("initial_context_count", 2)))
            first_turn = len(req.contexts or []) <= threshold
            blocks = {
                "system_prompt": [],
                "user_message_before": [],
                "user_message_after": [],
            }
            for slot in self._slots():
                if not slot["enabled"] or slot["platform"] != platform:
                    continue
                if not self._valid(slot):
                    logger.warning("[PlatformPrompt] Slot %s 内容或配置无效，跳过", slot["number"])
                    continue
                position = slot["position"]
                if position == "first_message":
                    if not first_turn:
                        continue
                    position = "user_message_before"
                blocks[position].append(self._tag(slot))

            if blocks["system_prompt"]:
                block = "\n\n".join(blocks["system_prompt"])
                req.system_prompt = f"{req.system_prompt.rstrip()}\n\n{block}" if req.system_prompt else block
            if blocks["user_message_before"]:
                block = "\n\n".join(blocks["user_message_before"])
                req.prompt = f"{block}\n\n{req.prompt}" if req.prompt else block
            if blocks["user_message_after"]:
                block = "\n\n".join(blocks["user_message_after"])
                req.prompt = f"{req.prompt}\n\n{block}" if req.prompt else block
        except Exception:
            logger.error("[PlatformPrompt] 注入失败", exc_info=True)

    @staticmethod
    def _line(slot: dict[str, Any]) -> str:
        state = "ON" if slot["enabled"] else "OFF"
        name = slot["remark"] or slot["tag_name"] or "未命名"
        position = POSITION_LABELS.get(slot["position"], "无效位置")
        return f"#{slot['number']} [{state}] [{slot['platform'].upper()}] {name} · {position}"

    @staticmethod
    def _detail(slot: dict[str, Any]) -> str:
        return (
            f"#{slot['number']} · {slot['remark'] or '未命名'}\n"
            f"状态：{'ON' if slot['enabled'] else 'OFF'}\n"
            f"平台：{slot['platform']}\n"
            f"位置：{slot['position']}\n"
            f"标签：{slot['tag_name'] or '(未填)'}\n"
            f"内容：\n{slot['content'] or '(未填)'}"
        )

    @staticmethod
    def _help(platform: str | None) -> str:
        return (
            f"🐈‍⬛ PlatformPrompt · 当前平台：{platform or '其他'}\n"
            "/pp list|ls [den|qq] · 列出 Slot\n"
            "/pp check|view [den|qq|编号] · 查看完整内容\n"
            "/pp preview · 预览当前平台的注入块\n"
            "/pp on|off <编号|den|qq> · 快速开关"
        )

    def _set_enabled(self, numbers: list[int], enabled: bool) -> int:
        """一并保存；写入失败时还原内存配置。"""
        old_values = {f"slot_{n}": self.config.get(f"slot_{n}") for n in numbers}
        changed = 0
        for n in numbers:
            key = f"slot_{n}"
            raw = dict(self.config.get(key) or {})
            if raw.get("enabled", False) is enabled:
                continue
            raw["enabled"] = enabled
            self.config[key] = raw
            changed += 1
        if changed:
            try:
                self.config.save_config()
            except Exception:
                for key, old in old_values.items():
                    if old is None:
                        self.config.pop(key, None)
                    else:
                        self.config[key] = old
                raise
        return changed

    @filter.command("pp")
    async def pp(self, event: AstrMessageEvent, action: str = "", target: str = ""):
        """PlatformPrompt：查看 / 快速开关（内容在面板编辑）。"""
        platform = self._event_platform(event)
        slots = self._slots()
        action = action.lower().strip()
        target = str(target).lower().strip()

        if action in ("", "help"):
            yield event.plain_result(self._help(platform))
            return

        if action in ("list", "ls"):
            if target not in ("", "qq", "den"):
                yield event.plain_result("用法：/pp list [den|qq]")
                return
            selected = [s for s in slots if not target or s["platform"] == target]
            header = f"🐈‍⬛ PlatformPrompt · {target.upper() if target else 'ALL'}"
            yield event.plain_result(header + "\n" + ("\n".join(map(self._line, selected)) if selected else "（没有 Slot）"))
            return

        if action in ("check", "view"):
            if target.isdecimal() and 1 <= int(target) <= SLOT_COUNT:
                yield event.plain_result(self._detail(slots[int(target) - 1]))
                return
            if target not in ("", "qq", "den"):
                yield event.plain_result("用法：/pp check [den|qq|1~10]")
                return
            chosen = target or platform
            if not chosen:
                yield event.plain_result("当前平台不是 QQ / Den；请指定 den 或 qq。")
                return
            selected = [s for s in slots if s["enabled"] and s["platform"] == chosen]
            yield event.plain_result(
                f"🐈‍⬛ PlatformPrompt · {chosen.upper()} · 已启用\n\n"
                + ("\n\n---\n\n".join(map(self._detail, selected)) if selected else "（没有已启用 Slot）")
            )
            return

        if action == "preview":
            if target:
                yield event.plain_result("用法：/pp preview")
                return
            if not platform:
                yield event.plain_result("当前平台不是 QQ / Den，没有注入内容。")
                return
            selected = [s for s in slots if s["enabled"] and s["platform"] == platform and self._valid(s)]
            if not selected:
                yield event.plain_result("🐈‍⬛ 当前平台没有可注入的 Slot。")
                return
            items = []
            for s in selected:
                timing = "仅首轮" if s["position"] == "first_message" else "每轮"
                items.append(f"#{s['number']} · {s['position']} · {timing}\n{self._tag(s)}")
            yield event.plain_result("🐈‍⬛ PlatformPrompt · 注入预览（不触发模型）\n\n" + "\n\n".join(items))
            return

        if action in ("on", "off"):
            enabled = action == "on"
            if target in ("qq", "den"):
                numbers = [s["number"] for s in slots if s["platform"] == target]
            elif target.isdecimal() and 1 <= int(target) <= SLOT_COUNT:
                numbers = [int(target)]
            else:
                yield event.plain_result("用法：/pp on|off <1~10|den|qq>")
                return
            try:
                changed = self._set_enabled(numbers, enabled)
            except Exception:
                logger.error("[PlatformPrompt] 配置保存失败", exc_info=True)
                yield event.plain_result("🐈‍⬛ 配置保存失败，状态没有改变。")
                return
            yield event.plain_result(
                f"🐈‍⬛ 已{'启用' if enabled else '关闭'} {target} "
                f"（{changed} 个 Slot 状态改变，已保存）"
            )
            return

        yield event.plain_result(self._help(platform))
