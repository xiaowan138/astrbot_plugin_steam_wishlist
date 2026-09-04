"""Steam 愿望单价格监控插件。

功能:
- 监控单个游戏或整个 Steam 愿望单的价格
- 后台定时轮询,折扣达到阈值(或触及插件观测到的历史最低价)时主动推送到绑定会话
- 基础价下调(非促销降价)同样提醒
- 一轮检查中多个游戏同时达标时聚合为一条消息,防止刷屏
- 按游戏名搜索添加 / 愿望单同步 / 疑似下架游戏自动清理

指令:
- /sw bind        绑定当前会话接收推送
- /sw unbind      解绑当前会话
- /sw search <游戏名>          搜索游戏
- /sw add <序号/链接/AppID>    添加单游戏监控
- /sw import <愿望单链接/ID>   导入整个愿望单
- /sw sync        同步已导入愿望单的新增/移除
- /sw remove <链接/AppID>      移除监控
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

try:
    # GreedyStr 用于接收指令剩余文本(如多词游戏名),仅在较新版本 AstrBot 的内部路径提供
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # 旧版本无 GreedyStr 时退化为单 token 参数
    GreedyStr = str

from .steam_api import (
    GamePrice,
    SteamAPI,
    SteamAPIError,
    AppNotFoundError,
    extract_appid,
    extract_wishlist_steamid,
)
from .storage import MANUAL_SOURCE, Storage, steam_source

STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"
LIST_PAGE_SIZE = 20
# 单轮检查推送数超过该值时,合并为一条聚合消息防刷屏
AGGREGATE_LIMIT = 3
# appdetails 连续返回「不存在」达到该次数后,视为已下架并自动移除
DELIST_REMOVE_THRESHOLD = 3
# /sw search 结果的有效期(秒),期间 /sw add <序号> 可直接按序号添加
SEARCH_TTL = 300
# 插件启动后先等一小段时间再做首轮检查,避免重启后长时间收不到推送
FIRST_CHECK_DELAY = 90
# 批量检查时每处理多少个游戏落一次盘(进程崩溃时的状态丢失上限)
SAVE_EVERY = 25

HELP_TEXT = """Steam 愿望单监控 指令:
/sw search <游戏名> - 搜索游戏,如 /sw search portal 2
/sw add <序号/链接/AppID> - 添加单游戏监控(序号来自最近一次搜索)
/sw import <愿望单链接或SteamID> - 导入整个公开愿望单
/sw sync - 同步已导入愿望单的新增/移除
/sw remove <链接或AppID> - 移除监控
/sw list - 查看监控列表与当前价格
/sw check - 立即全量检查并推送
/sw bind / unbind - 绑定/解绑当前会话接收推送
/sw status - 查看运行状态"""


@register(
    "astrbot_plugin_steam_wishlist",
    "xiaowan138",
    "Steam 愿望单/单游戏价格监控,折扣达阈值或史低时自动推送",
    "0.2.0",
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
        # umo -> (搜索时间戳, [appid, ...]) 仅存内存,重启失效
        self._last_search: dict[str, tuple[float, list[int]]] = {}
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
    async def handle_sw(self, event: AstrMessageEvent, sub: str = "", arg: GreedyStr = ""):
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
        elif sub == "search":
            async for r in self._cmd_search(event, arg):
                yield r
        elif sub == "add":
            async for r in self._cmd_add(event, arg):
                yield r
        elif sub == "import":
            async for r in self._cmd_import(event, arg):
                yield r
        elif sub == "sync":
            async for r in self._cmd_sync(event):
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
        sync_line = ""
        if self.storage.wishlist_sources:
            sync_line = f"愿望单同步源: {len(self.storage.wishlist_sources)} 个(使用 /sw sync 同步)\n"
        return (
            f"Steam 愿望单监控\n"
            f"监控游戏: {len(self.storage.games)} 个\n"
            f"推送绑定: {total_bindings} 个会话\n"
            f"折扣阈值: {self.config.get('discount_threshold', 30)}%\n"
            f"区域/语言: {self.config.get('region', 'cn')} / {self.config.get('language', 'schinese')}\n"
            f"{sync_line}"
            f"后台任务: {state}\n{next_line}"
        )

    def _cmd_list(self) -> str:
        if not self.storage.games:
            return "监控列表为空,使用 /sw search <游戏名> 搜索后添加,或 /sw import <愿望单> 导入。"
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

    async def _cmd_search(self, event: AstrMessageEvent, arg: str):
        if not arg:
            yield event.plain_result("用法: /sw search <游戏名>\n示例: /sw search portal 2")
            return
        try:
            results = await self.api.search_games(arg)
        except SteamAPIError as e:
            yield event.plain_result(f"搜索失败: {e}")
            return
        if not results:
            yield event.plain_result(f"未找到与「{arg}」相关的游戏,换个关键词试试?")
            return
        self._last_search[event.unified_msg_origin] = (
            time.time(),
            [r["appid"] for r in results],
        )
        lines = [f"搜索「{arg}」结果:"]
        for i, r in enumerate(results, 1):
            price_part = f" - {r['price_display']}" if r["price_display"] else ""
            lines.append(f"{i}. {r['name']}{price_part} (AppID {r['appid']})")
        lines.append("使用 /sw add <序号> 添加监控,如 /sw add 1")
        yield event.plain_result("\n".join(lines))

    async def _cmd_add(self, event: AstrMessageEvent, arg: str):
        if not arg:
            yield event.plain_result("用法: /sw add <序号/商店链接/AppID>\n可先 /sw search <游戏名> 再按序号添加。")
            return
        appid = self._resolve_appid(event, arg)
        if appid is None:
            yield event.plain_result(
                "无法识别游戏。请提供 Steam 商店链接、纯数字 AppID,"
                "或先 /sw search 后使用序号添加。"
            )
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
        self.storage.undismiss(appid)
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

    def _resolve_appid(self, event: AstrMessageEvent, arg: str) -> int | None:
        """解析添加目标: 优先按最近搜索的序号,其次商店链接/纯数字 AppID。"""
        arg = arg.strip()
        if arg.isdigit():
            last = self._last_search.get(event.unified_msg_origin)
            if last and time.time() - last[0] <= SEARCH_TTL:
                idx = int(arg)
                if 1 <= idx <= len(last[1]):
                    return last[1][idx - 1]
        return extract_appid(arg)

    async def _cmd_import(self, event: AstrMessageEvent, arg: str):
        if not arg:
            yield event.plain_result(
                "用法: /sw import <愿望单链接或SteamID>\n"
                "示例: /sw import https://steamcommunity.com/profiles/76561198xxxx/wishlist\n"
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
        added, skipped, dismissed_cnt = 0, 0, 0
        for appid_str in wishlist:
            if appid_str in self.storage.games:
                skipped += 1
                continue
            if appid_str in self.storage.dismissed:
                dismissed_cnt += 1
                continue
            # 官方愿望单接口不返回游戏名,先以 AppID 占位,首次价格检查时回填
            self.storage.add_game(int(appid_str), f"AppID {appid_str}", source=steam_source(steamid))
            added += 1
        self.storage.add_wishlist_source(steamid)
        self.storage.save()
        tip = (
            "\n游戏名称与价格基线正在后台建立(较大的愿望单需要几分钟),完成后如有符合阈值的折扣会自动推送。"
            if added
            else ""
        )
        note = f",跳过已手动移除 {dismissed_cnt} 个" if dismissed_cnt else ""
        yield event.plain_result(
            f"愿望单导入完成: 新增 {added} 个,已存在 {skipped} 个{note},"
            f"当前共监控 {len(self.storage.games)} 个。{tip}\n"
            f"之后可随时使用 /sw sync 同步该愿望单的新增/移除。"
        )
        if added:
            # 后台建立价格基线,避免阻塞指令(愿望单较大时逐个请求较慢)
            task = asyncio.create_task(self._bootstrap_prices(list(wishlist.keys())))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _cmd_sync(self, event: AstrMessageEvent):
        sources = list(self.storage.wishlist_sources)
        if not sources:
            yield event.plain_result("尚未导入过愿望单,请先用 /sw import <愿望单链接> 导入。")
            return
        yield event.plain_result(f"正在同步 {len(sources)} 个愿望单,请稍候...")
        added_appids: list[str] = []
        removed_names: list[str] = []
        failed = 0
        for sid in sources:
            try:
                wishlist = await self.api.fetch_wishlist(sid)
            except SteamAPIError as e:
                failed += 1
                logger.warning(f"Steam 愿望单监控: 同步愿望单 {sid} 失败 {e}")
                continue
            wl_set = set(wishlist.keys())
            for appid_str in wishlist:
                if appid_str in self.storage.games or appid_str in self.storage.dismissed:
                    continue
                self.storage.add_game(int(appid_str), f"AppID {appid_str}", source=steam_source(sid))
                added_appids.append(appid_str)
            # 愿望单中已删除的游戏停止监控;手动添加的不受影响
            for appid_str, game in list(self.storage.games.items()):
                if game.get("source") == steam_source(sid) and appid_str not in wl_set:
                    self.storage.remove_game(int(appid_str))
                    removed_names.append(game.get("name") or appid_str)
        self.storage.save()
        if added_appids:
            task = asyncio.create_task(self._bootstrap_prices(added_appids))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        lines = [f"同步完成: 新增 {len(added_appids)} 个,移除 {len(removed_names)} 个。"]
        if failed:
            lines.append(f"有 {failed} 个愿望单同步失败(可能被限流),可稍后重试。")
        if removed_names:
            shown = "、".join(removed_names[:10]) + (" 等" if len(removed_names) > 10 else "")
            lines.append(f"已停止监控(愿望单中已删除): {shown}")
        if added_appids:
            lines.append("新增游戏的价格基线正在后台建立。")
        yield event.plain_result("\n".join(lines))

    def _cmd_remove(self, arg: str) -> str:
        if not arg:
            return "用法: /sw remove <链接或AppID>"
        appid = extract_appid(arg)
        if appid is None:
            return "无法识别,请提供 Steam 商店链接或纯数字 AppID。"
        game = self.storage.remove_game(appid)
        if game is None:
            return f"AppID {appid} 不在监控列表中。"
        # 来自愿望单的游戏被手动移除时记录,避免 /sw sync 又自动加回来
        if game.get("source") and game["source"] != MANUAL_SOURCE:
            self.storage.dismiss(appid)
        self.storage.save()
        return f"已移除监控:「{game['name']}」。"

    async def _cmd_check(self, event: AstrMessageEvent):
        if not self.storage.games:
            yield event.plain_result("监控列表为空,无可检查项。")
            return
        if self._checking:
            yield event.plain_result("已有检查正在进行(可能是后台轮询),请稍候。")
            return
        yield event.plain_result(f"开始检查 {len(self.storage.games)} 个游戏,可能需要一些时间...")
        result = await self._check_all(push=True)
        if result is None:
            yield event.plain_result("已有检查正在进行(可能是后台轮询),请稍候再试。")
            return
        total, on_sale, pushed, failed, removed = result
        msg = (
            f"检查完成: {total} 个游戏,当前打折 {on_sale} 个,本次推送 {pushed} 条,失败 {failed} 个。"
        )
        if removed:
            msg += f"\n已自动移除 {removed} 个疑似下架的游戏。"
        yield event.plain_result(msg)

    # ==================== 后台轮询 ====================

    def _config_enabled(self) -> bool:
        return bool(self.config.get("enable_auto_check", True))

    def _start_loop_task(self):
        self._loop_task = asyncio.create_task(self._check_loop())

    async def _check_loop(self):
        first_round = True
        while True:
            try:
                if first_round:
                    # 启动后先做一轮快速检查,避免重启后长时间收不到推送
                    interval = FIRST_CHECK_DELAY
                    first_round = False
                else:
                    interval = max(1, int(self.config.get("check_interval_hours", 6))) * 3600
                self._next_check_ts = time.time() + interval
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
    ) -> tuple[int, int, int, int, int] | None:
        """全量检查。

        返回 (总数, 打折数, 推送数, 失败数, 自动移除数);已有检查进行中返回 None。
        """
        if self._checking:
            return None
        self._checking = True
        total = on_sale = failed = removed_cnt = 0
        to_push: list[tuple[GamePrice, str]] = []
        try:
            targets = only or list(self.storage.games.keys())
            total = len(targets)
            since_save = 0
            for appid_str in targets:
                game = self.storage.games.get(appid_str)
                if not game:
                    continue
                try:
                    price = await self.api.fetch_app_price(int(appid_str))
                except AppNotFoundError:
                    # Steam 明确表示该 AppID 不存在: 连续多次后视为已下架,自动清理
                    state = self.storage.get_state(int(appid_str))
                    state["fail_count"] = state.get("fail_count", 0) + 1
                    self.storage.set_state(int(appid_str), state)
                    if state["fail_count"] >= DELIST_REMOVE_THRESHOLD:
                        removed = self.storage.remove_game(int(appid_str))
                        if removed:
                            removed_cnt += 1
                            logger.info(
                                f"Steam 愿望单监控: AppID {appid_str}「{removed['name']}」"
                                f"连续 {state['fail_count']} 次查询不存在,已自动移除"
                            )
                    since_save += 1
                    if since_save >= SAVE_EVERY:
                        self.storage.save()
                        since_save = 0
                    continue
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
                should_push, is_on_sale, reason = outcome
                if is_on_sale:
                    on_sale += 1
                if push and should_push:
                    to_push.append((price, reason))
                since_save += 1
                if since_save >= SAVE_EVERY:
                    self.storage.save()
                    since_save = 0
            self.storage.save()
            if push and to_push:
                await self._push_results(to_push)
            return total, on_sale, len(to_push), failed, removed_cnt
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
            "base_final": price.final_cents if price.discount_percent == 0 else None,
            "prev_base_final": None,
            "initial_cents": price.initial_cents,
            "discount_percent": price.discount_percent,
            "final_formatted": price.final_formatted,
            "initial_formatted": price.initial_formatted,
            "header_image": price.header_image,
            "last_check_at": now,
            "fail_count": 0,
        }
        self.storage.set_state(appid, state)

    def _update_state_and_decide(self, appid: int, price: GamePrice) -> tuple[bool, bool, str]:
        """更新单个游戏的价格状态。

        返回 (是否应推送, 是否在打折, 推送原因 discount/price_cut)。
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
                    "fail_count": 0,
                }
            )
            state.pop("last_seen_final", None)
            self.storage.set_state(appid, state)
            return False, False, ""

        old_seen = state.get("last_seen_final")
        old_min = state.get("min_final")
        old_base = state.get("base_final")
        is_new_lowest = old_min is None or price.final_cents <= old_min
        is_on_sale = price.discount_percent > 0

        should_push = False
        reason = ""
        if state:
            if is_on_sale:
                threshold = int(self.config.get("discount_threshold", 30))
                price_dropped = old_seen is not None and price.final_cents < old_seen
                threshold_ok = price.discount_percent >= threshold
                lowest_only_ok = (not self.config.get("notify_lowest_only", False)) or is_new_lowest
                already_pushed = price.final_cents == state.get("last_pushed_final")
                should_push = price_dropped and threshold_ok and lowest_only_ok and not already_pushed
                if should_push:
                    reason = "discount"
            elif old_base is not None and price.final_cents < old_base:
                # 非促销状态下的基础价下调(厂商砍价),不适用折扣阈值,直接提醒
                should_push = True
                reason = "price_cut"

        if price.discount_percent == 0:
            # 记录供推送展示的旧基础价,再更新当前基础价
            if old_base is not None and price.final_cents != old_base:
                state["prev_base_final"] = old_base
            state["base_final"] = price.final_cents
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
                "fail_count": 0,
            }
        )
        self.storage.set_state(appid, state)
        return should_push, is_on_sale, reason

    # ==================== 推送 ====================

    def _all_bindings(self) -> list[str]:
        cfg_targets = [t for t in self.config.get("push_targets", []) if t]
        return list(dict.fromkeys(self.storage.bindings + cfg_targets))

    async def _push_results(self, items: list[tuple[GamePrice, str]]):
        """少量推送用带图消息;超过阈值合并为一条聚合消息防刷屏。"""
        if len(items) <= AGGREGATE_LIMIT:
            for price, reason in items:
                await self._push_discount(price, reason)
        else:
            await self._push_aggregated(items)

    async def _push_discount(self, price: GamePrice, reason: str):
        bindings = self._all_bindings()
        if not bindings:
            return
        state = self.storage.get_state(price.appid)
        if reason == "price_cut":
            old_base = state.get("prev_base_final")
            old_price_part = f"(原价 ¥ {old_base / 100:g})" if old_base else ""
            text = (
                f"✂️ Steam 基础价下调提醒\n"
                f"「{price.name}」\n"
                f"💰 新基础价 {price.final_formatted}{old_price_part} - 厂商降价,非促销折扣\n"
                f"🔗 {STORE_PAGE_URL.format(appid=price.appid)}"
            )
        else:
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

    async def _push_aggregated(self, items: list[tuple[GamePrice, str]]):
        """多游戏同时达标时合并为一条纯文本消息。"""
        bindings = self._all_bindings()
        if not bindings:
            return
        lines = [f"🎮 Steam 折扣提醒({len(items)} 个游戏)"]
        for price, reason in items:
            state = self.storage.get_state(price.appid)
            url = STORE_PAGE_URL.format(appid=price.appid)
            if reason == "price_cut":
                lines.append(f"✂️「{price.name}」基础价下调 → {price.final_formatted}  {url}")
            else:
                is_lowest = state.get("min_final") is not None and (
                    price.final_cents <= state["min_final"]
                )
                tag = " 📉史低" if is_lowest else ""
                lines.append(
                    f"「{price.name}」-{price.discount_percent}% → {price.final_formatted}{tag}  {url}"
                )
        text = "\n".join(lines)
        for umo in bindings:
            try:
                chain = MessageChain()
                chain.message(text)
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.warning(f"Steam 愿望单监控: 聚合推送到 {umo} 失败 {e}")
