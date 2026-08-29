"""Steam 愿望单价格监控插件。

功能:
- 监控单个游戏或整个 Steam 愿望单的价格
- 后台定时轮询,折扣达到阈值(或触及插件观测到的历史最低价)时主动推送到绑定会话
- 消息附带游戏头图、原价、现价、折扣率与史低标记

指令:
- /sw bind        绑定当前会话接收推送
- /sw unbind      解绑当前会话
- /sw add <链接/ID>        添加单游戏监控
- /sw import <愿望单链接/ID> 导入整个愿望单
- /sw remove <链接/ID>     移除监控
- /sw list        查看监控列表与当前价格
- /sw check       立即全量检查并推送
- /sw status      查看运行状态
"""

import asyncio
import time

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .steam_api import (
    GamePrice,
    SteamAPI,
    SteamAPIError,
    extract_appid,
    extract_wishlist_steamid,
)
from .storage import Storage

STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"
LIST_PAGE_SIZE = 20
HELP_TEXT = """Steam 愿望单监控 指令:
/sw bind - 绑定当前会话接收推送
/sw unbind - 解绑当前会话
/sw add <商店链接或AppID> - 添加单游戏监控
/sw import <愿望单链接或SteamID> - 导入整个公开愿望单
/sw remove <商店链接或AppID> - 移除监控
/sw list - 查看监控列表与当前价格
/sw check - 立即全量检查并推送
/sw status - 查看运行状态"""


@register(
    "astrbot_plugin_steam_wishlist",
    "xiaowan138",
    "Steam 愿望单/单游戏价格监控,折扣达阈值或史低时自动推送",
    "0.1.0",
    "https://github.com/xiaowan138/astrbot_plugin_steam_wishlist",
)
class SteamWishlistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.storage = Storage(StarTools.get_data_dir("astrbot_plugin_steam_wishlist"))
        self.api = SteamAPI(
            region=config.get("region", "cn"),
            language=config.get("language", "schinese"),
            delay=float(config.get("request_delay_seconds", 1.5)),
        )
        self._loop_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._next_check_ts: float | None = None
        self._checking = False
        # 热重载时 __init__ 运行于已启动的事件循环,直接拉起后台任务;
        # 冷启动时循环尚未就绪,交给 on_astrbot_loaded 生命周期钩子
        try:
            asyncio.get_running_loop()
            self._start_loop_task()
        except RuntimeError:
            pass

    async def terminate(self):
        for task in self._bg_tasks:
            task.cancel()
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        await self.api.close()
        self.storage.save()

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if self._config_enabled() and (self._loop_task is None or self._loop_task.done()):
            self._start_loop_task()
            logger.info("Steam 愿望单监控: 后台轮询已随框架启动")

    # ==================== 指令 ====================

    @filter.command("sw")
    async def handle_sw(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """Steam 愿望单价格监控主指令,发送 /sw help 查看用法"""
        sub = sub.lower().strip()
        arg = arg.strip()
        if not sub:
            yield event.plain_result(HELP_TEXT)
        elif sub in ("help", "帮助"):
            yield event.plain_result(HELP_TEXT)
        elif sub == "bind":
            yield event.plain_result(self._cmd_bind(event))
        elif sub == "unbind":
            yield event.plain_result(self._cmd_unbind(event))
        elif sub == "status":
            yield event.plain_result(self._cmd_status())
        elif sub == "list":
            yield event.plain_result(self._cmd_list())
        elif sub == "add":
            async for r in self._cmd_add(event, arg):
                yield r
        elif sub == "import":
            async for r in self._cmd_import(event, arg):
                yield r
        elif sub in ("remove", "rm", "删除"):
            yield event.plain_result(self._cmd_remove(arg))
        elif sub == "check":
            async for r in self._cmd_check(event):
                yield r
        else:
            yield event.plain_result(f"未知子命令「{sub}」。{HELP_TEXT}")

    def _cmd_bind(self, event: AstrMessageEvent) -> str:
        umo = event.unified_msg_origin
        if self.storage.add_binding(umo):
            self.storage.save()
            return "已绑定当前会话,折扣消息将推送到这里。"
        return "当前会话已绑定过。"

    def _cmd_unbind(self, event: AstrMessageEvent) -> str:
        if self.storage.remove_binding(event.unified_msg_origin):
            self.storage.save()
            return "已解绑当前会话。"
        return "当前会话尚未绑定。"

    def _cmd_status(self) -> str:
        cfg_targets = [t for t in self.config.get("push_targets", []) if t]
        total_bindings = len(set(self.storage.bindings) | set(cfg_targets))
        state = "运行中" if self._loop_task and not self._loop_task.done() else "已停止"
        if self._config_enabled():
            state += f"(间隔 {self.config.get('check_interval_hours', 6)} 小时)"
        else:
            state = "自动检查已关闭(仅手动 /sw check)"
        next_line = ""
        if self._next_check_ts:
            wait_min = max(0, int((self._next_check_ts - time.time()) / 60))
            next_line = f"下次检查: 约 {wait_min} 分钟后\n"
        return (
            f"Steam 愿望单监控\n"
            f"监控游戏: {len(self.storage.games)} 个\n"
            f"推送绑定: {total_bindings} 个会话\n"
            f"折扣阈值: {self.config.get('discount_threshold', 30)}%\n"
            f"区域/语言: {self.config.get('region', 'cn')} / {self.config.get('language', 'schinese')}\n"
            f"后台任务: {state}\n{next_line}"
        )

    def _cmd_list(self) -> str:
        if not self.storage.games:
            return "监控列表为空,使用 /sw add <链接或ID> 或 /sw import <愿望单> 添加。"
        lines = []
        items = sorted(
            self.storage.games.items(), key=lambda kv: kv[1].get("added_at", 0), reverse=True
        )
        for appid, game in items:
            state = self.storage.price_state.get(appid, {})
            if state.get("discount_percent"):
                price_part = f"· -{state['discount_percent']}% → {state.get('final_formatted', '?')}"
            elif state.get("last_seen_final") is None and state.get("final_formatted"):
                price_part = f"· {state['final_formatted']}"
            else:
                price_part = "· 暂无价格数据"
            lines.append(f"{game['name']} ({appid}) {price_part}")
        shown = lines[:LIST_PAGE_SIZE]
        omitted = len(lines) - len(shown)
        text = "监控列表:\n" + "\n".join(shown)
        if omitted > 0:
            text += f"\n... 及另外 {omitted} 个游戏"
        return text

    async def _cmd_add(self, event: AstrMessageEvent, arg: str):
        if not arg:
            yield event.plain_result("用法: /sw add <商店链接或AppID>")
            return
        appid = extract_appid(arg)
        if appid is None:
            yield event.plain_result("无法识别游戏,请提供 Steam 商店链接或纯数字 AppID。")
            return
        if str(appid) in self.storage.games:
            yield event.plain_result(f"「{self.storage.games[str(appid)]['name']}」已在监控中。")
            return
        try:
            price = await self.api.fetch_app_price(appid)
        except SteamAPIError as e:
            yield event.plain_result(f"查询失败: {e}")
            return
        self.storage.add_game(appid, price.name)
        self._init_state(appid, price)
        self.storage.save()
        if price.has_price:
            if price.discount_percent:
                desc = (
                    f"原价 {price.initial_formatted},现价 {price.final_formatted}"
                    f"(-{price.discount_percent}%),当前就有折扣,请自行把握"
                )
            else:
                desc = f"当前售价 {price.final_formatted}(无折扣)"
            yield event.plain_result(f"已添加监控:「{price.name}」\n{desc}。")
        else:
            yield event.plain_result(
                f"已添加监控:「{price.name}」({price.final_formatted}),该游戏暂无付费价格,仅在出现售价变动时跟踪。"
            )

    async def _cmd_import(self, event: AstrMessageEvent, arg: str):
        if not arg:
            yield event.plain_result(
                "用法: /sw import <愿望单链接或SteamID>\n"
                "示例: /sw import https://store.steampowered.com/wishlist/profiles/76561198xxxx/\n"
                "或自定义URL: /sw import https://store.steampowered.com/wishlist/id/yourname/"
            )
            return
        steamid = extract_wishlist_steamid(arg)
        if steamid is None:
            yield event.plain_result("无法识别愿望单,请提供完整愿望单链接或 17 位 SteamID。")
            return
        yield event.plain_result("正在拉取愿望单,请稍候...")
        try:
            if steamid.startswith("vanity:"):
                steamid = await self.api.resolve_vanity(steamid[7:])
            wishlist = await self.api.fetch_wishlist(steamid)
        except SteamAPIError as e:
            yield event.plain_result(f"拉取愿望单失败: {e}")
            return
        if not wishlist:
            yield event.plain_result("该愿望单为空或不可见(Steam 愿望单需设为公开)。")
            return
        added, skipped = 0, 0
        for appid_str in wishlist:
            if appid_str in self.storage.games:
                skipped += 1
                continue
            # 官方愿望单接口不返回游戏名,先以 AppID 占位,首次价格检查时回填
            self.storage.add_game(int(appid_str), f"AppID {appid_str}")
            added += 1
        self.storage.save()
        tip = (
            "\n游戏名称与价格基线正在后台建立(较大的愿望单需要几分钟),完成后如有符合阈值的折扣会自动推送。"
            if added
            else ""
        )
        yield event.plain_result(
            f"愿望单导入完成: 新增 {added} 个,已存在 {skipped} 个,当前共监控 {len(self.storage.games)} 个。{tip}"
        )
        if added:
            # 后台建立价格基线,避免阻塞指令(愿望单较大时逐个请求较慢)
            task = asyncio.create_task(self._bootstrap_prices(list(wishlist.keys())))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    def _cmd_remove(self, arg: str) -> str:
        if not arg:
            return "用法: /sw remove <商店链接或AppID>"
        appid = extract_appid(arg)
        if appid is None:
            return "无法识别,请提供 Steam 商店链接或纯数字 AppID。"
        name = self.storage.remove_game(appid)
        if name is None:
            return f"AppID {appid} 不在监控列表中。"
        self.storage.save()
        return f"已移除监控:「{name}」。"

    async def _cmd_check(self, event: AstrMessageEvent):
        if not self.storage.games:
            yield event.plain_result("监控列表为空,无可检查项。")
            return
        if self._checking:
            yield event.plain_result("已有检查正在进行,请稍候。")
            return
        yield event.plain_result(f"开始检查 {len(self.storage.games)} 个游戏,可能需要一些时间...")
        result = await self._check_all(push=True)
        if result is None:
            yield event.plain_result("检查被取消。")
            return
        total, on_sale, pushed, failed = result
        yield event.plain_result(
            f"检查完成: {total} 个游戏,当前打折 {on_sale} 个,本次推送 {pushed} 条,失败 {failed} 个。"
        )

    # ==================== 后台轮询 ====================

    def _config_enabled(self) -> bool:
        return bool(self.config.get("enable_auto_check", True))

    def _start_loop_task(self):
        self._loop_task = asyncio.create_task(self._check_loop())

    async def _check_loop(self):
        while True:
            try:
                interval = max(1, int(self.config.get("check_interval_hours", 6))) * 3600
                await asyncio.sleep(interval)
                if not self.storage.games:
                    continue
                await self._check_all(push=True)
            except asyncio.CancelledError:
                return
            except Exception as e:  # 后台任务兜底,单轮异常不允许终止循环
                logger.error(f"Steam 愿望单监控轮询异常: {e}")
                await asyncio.sleep(60)

    async def _bootstrap_prices(self, appid_list: list[str]):
        """为刚导入的游戏建立价格基线(不推送),完成后保存。"""
        try:
            await self._check_all(push=False, only=appid_list)
        except Exception as e:
            logger.warning(f"Steam 愿望单监控: 价格基线建立失败 {e}")

    async def _check_all(
        self, push: bool, only: list[str] | None = None
    ) -> tuple[int, int, int, int] | None:
        """全量检查。返回 (总数, 打折数, 推送数, 失败数),被取消时返回 None。"""
        if self._checking:
            return None
        self._checking = True
        total = on_sale = pushed = failed = 0
        try:
            targets = only or list(self.storage.games.keys())
            total = len(targets)
            for appid_str in targets:
                game = self.storage.games.get(appid_str)
                if not game:
                    continue
                try:
                    price = await self.api.fetch_app_price(int(appid_str))
                except (SteamAPIError, aiohttp.ClientError) as e:
                    failed += 1
                    logger.warning(f"Steam 愿望单监控: AppID {appid_str} 查询失败 {e}")
                    continue
                except asyncio.CancelledError:
                    raise
                # 占位名回填或游戏改名同步
                if game.get("name") != price.name:
                    self.storage.games[appid_str]["name"] = price.name
                outcome = self._update_state_and_decide(int(appid_str), price)
                if outcome is None:
                    failed += 1
                    continue
                should_push, is_on_sale = outcome
                if is_on_sale:
                    on_sale += 1
                if push and should_push:
                    await self._push_discount(price)
                    pushed += 1
                self.storage.save()
            return total, on_sale, pushed, failed
        finally:
            self._checking = False

    def _init_state(self, appid: int, price: GamePrice):
        """添加游戏时建立价格基线: 记录当前价并作为观测期最低价,不触发推送。"""
        now = int(time.time())
        state = {
            "last_seen_final": price.final_cents,
            "last_pushed_final": None,
            "min_final": price.final_cents,
            "min_final_at": now,
            "initial_cents": price.initial_cents,
            "discount_percent": price.discount_percent,
            "final_formatted": price.final_formatted,
            "initial_formatted": price.initial_formatted,
            "header_image": price.header_image,
            "last_check_at": now,
        }
        self.storage.set_state(appid, state)

    def _update_state_and_decide(self, appid: int, price: GamePrice) -> tuple[bool, bool] | None:
        """更新单个游戏的价格状态。

        返回 (是否应推送, 是否在打折);数据异常返回 None。
        """
        state = self.storage.get_state(appid)
        now = int(time.time())
        if not price.has_price:
            # 免费游戏/无售价: 仅刷新观测信息
            state.update(
                {
                    "discount_percent": 0,
                    "final_formatted": price.final_formatted,
                    "initial_formatted": "",
                    "header_image": price.header_image,
                    "last_check_at": now,
                }
            )
            state.pop("last_seen_final", None)
            self.storage.set_state(appid, state)
            return False, False

        old_seen = state.get("last_seen_final")
        old_min = state.get("min_final")
        is_new_lowest = old_min is None or price.final_cents <= old_min
        is_on_sale = price.discount_percent > 0

        should_push = False
        if state and is_on_sale:
            threshold = int(self.config.get("discount_threshold", 30))
            price_dropped = old_seen is not None and price.final_cents < old_seen
            threshold_ok = price.discount_percent >= threshold
            lowest_only_ok = (not self.config.get("notify_lowest_only", False)) or is_new_lowest
            already_pushed = price.final_cents == state.get("last_pushed_final")
            should_push = price_dropped and threshold_ok and lowest_only_ok and not already_pushed

        if price.discount_percent == 0:
            # 当前已回到原价: 清除推送记录,使下次同价折扣也能再次提醒
            state["last_pushed_final"] = None
        elif should_push:
            state["last_pushed_final"] = price.final_cents

        state.update(
            {
                "last_seen_final": price.final_cents,
                "min_final": min(price.final_cents, old_min) if old_min is not None else price.final_cents,
                "min_final_at": now if (is_new_lowest and old_min != price.final_cents) else state.get("min_final_at", now),
                "initial_cents": price.initial_cents,
                "discount_percent": price.discount_percent,
                "final_formatted": price.final_formatted,
                "initial_formatted": price.initial_formatted,
                "header_image": price.header_image,
                "last_check_at": now,
            }
        )
        self.storage.set_state(appid, state)
        return should_push, is_on_sale

    # ==================== 推送 ====================

    def _all_bindings(self) -> list[str]:
        cfg_targets = [t for t in self.config.get("push_targets", []) if t]
        return list(dict.fromkeys(self.storage.bindings + cfg_targets))

    async def _push_discount(self, price: GamePrice):
        bindings = self._all_bindings()
        if not bindings:
            return
        state = self.storage.get_state(price.appid)
        is_lowest = state.get("min_final") is not None and (
            price.final_cents <= state["min_final"]
        )
        lowest_line = "\n📉 观测期内历史最低价!" if is_lowest else ""
        text = (
            f"🎮 Steam 折扣提醒\n"
            f"「{price.name}」\n"
            f"💰 {price.final_formatted}(原价 {price.initial_formatted})-{price.discount_percent}%"
            f"{lowest_line}\n"
            f"🔗 {STORE_PAGE_URL.format(appid=price.appid)}"
        )
        for umo in bindings:
            try:
                chain = MessageChain()
                if price.header_image:
                    chain.url_image(price.header_image)
                chain.message(text)
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.warning(f"Steam 愿望单监控: 推送到 {umo} 失败 {e}")
