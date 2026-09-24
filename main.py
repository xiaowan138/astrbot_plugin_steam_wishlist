"""Steam 愿望单价格监控插件。

功能:
- 监控单个游戏或整个 Steam 愿望单的价格
- 后台定时轮询,折扣达到阈值(或触及插件观测到的历史最低价)时主动推送到绑定会话
- 基础价下调(非促销降价)与愿望单游戏发售同样提醒
- 一轮检查中多个游戏同时达标时聚合为一条消息,防止刷屏
- 按游戏名搜索添加 / 愿望单同步 / 来源管理 / 疑似下架游戏自动清理

指令:
- /sw bind / unbind        绑定/解绑当前会话接收推送
- /sw search <游戏名>      搜索游戏
- /sw add <序号/链接/AppID>   添加单游戏监控
- /sw import <愿望单链接/ID>  导入整个愿望单
- /sw sources              查看已导入的愿望单来源
- /sw unlink <序号>        移除某个愿望单来源(可选连带其游戏)
- /sw sync                 同步已导入愿望单的新增/移除
- /sw remove <链接/AppID>  移除监控
- /sw list                 查看监控列表与当前价格
- /sw history <链接/AppID> 查看某游戏的价格走势
- /sw export               导出监控列表(Markdown)
- /sw check                立即全量检查并推送
- /sw status               查看运行状态
"""

import asyncio
import time

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
    format_money,
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
# 自动检查被配置关闭时,轮询任务进入休眠并周期性复查配置(便于 WebUI 开关即时生效)
DISABLED_POLL_INTERVAL = 300

HELP_TEXT = """Steam 愿望单监控 指令:
/sw search <游戏名> - 搜索游戏,如 /sw search portal 2
/sw add <序号/链接/AppID> - 添加单游戏监控(序号来自最近一次搜索)
/sw import <愿望单链接或SteamID> - 导入整个公开愿望单
/sw sources - 查看已导入的愿望单来源
/sw unlink <序号> - 移除某个愿望单来源
/sw sync - 同步已导入愿望单的新增/移除
/sw remove <链接/AppID> - 移除监控
/sw list - 查看监控列表与当前价格
/sw history <链接/AppID> - 查看某游戏的价格走势
/sw export - 导出监控列表(Markdown)
/sw check - 立即全量检查并推送
/sw bind / unbind - 绑定/解绑当前会话接收推送
/sw status - 查看运行状态"""


@register(
    "astrbot_plugin_steam_wishlist",
    "xiaowan138",
    "Steam 愿望单/单游戏价格监控,折扣达阈值或史低时自动推送",
    "0.3.0",
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
            self._maybe_start_loop()
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
        if self._maybe_start_loop():
            logger.info("Steam 愿望单监控: 后台轮询已随框架启动")

    # ==================== 指令 ====================

    @filter.command("sw")
    async def handle_sw(self, event: AstrMessageEvent, sub: str = "", arg: GreedyStr = ""):
        """Steam 愿望单价格监控主指令,发送 /sw help 查看用法"""
        sub = sub.lower().strip()
        arg = arg.strip()
        if not sub or sub in ("help", "帮助"):
            yield event.plain_result(HELP_TEXT)
        elif sub == "bind":
            yield event.plain_result(self._cmd_bind(event))
        elif sub == "unbind":
            yield event.plain_result(self._cmd_unbind(event))
        elif sub == "status":
            yield event.plain_result(self._cmd_status())
        elif sub == "list":
            yield event.plain_result(self._cmd_list())
        elif sub == "export":
            yield event.plain_result(self._cmd_export())
        elif sub in ("sources", "来源"):
            yield event.plain_result(self._cmd_sources())
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
        elif sub == "unlink":
            async for r in self._cmd_unlink(event, arg):
                yield r
        elif sub in ("remove", "rm", "删除"):
            yield event.plain_result(self._cmd_remove(event, arg))
        elif sub == "history":
            yield event.plain_result(self._cmd_history(event, arg))
        elif sub == "check":
            async for r in self._cmd_check(event):
                yield r
        else:
            yield event.plain_result(f"未知子命令「{sub}」。{HELP_TEXT}")

    def _deny_if_not_admin(self, event: AstrMessageEvent) -> str | None:
        """破坏性/影响全局的命令在开启限制时仅管理员可用。返回拒绝文案或 None。"""
        if not self.config.get("admin_only", False):
            return None
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin) and is_admin():
            return None
        return "该命令仅限管理员使用(可在插件配置中关闭「仅管理员可操作」)。"

    def _cmd_bind(self, event: AstrMessageEvent) -> str:
        denied = self._deny_if_not_admin(event)
        if denied:
            return denied
        umo = event.unified_msg_origin
        if self.storage.add_binding(umo):
            self.storage.save()
            return "已绑定当前会话,折扣消息将推送到这里。"
        return "当前会话已绑定过。"

    def _cmd_unbind(self, event: AstrMessageEvent) -> str:
        denied = self._deny_if_not_admin(event)
        if denied:
            return denied
        if self.storage.remove_binding(event.unified_msg_origin):
            self.storage.save()
            return "已解绑当前会话。"
        return "当前会话尚未绑定。"

    def _cmd_status(self) -> str:
        cfg_targets = [t for t in self.config.get("push_targets", []) if t]
        total_bindings = len(set(self.storage.bindings) | set(cfg_targets))
        if not self._config_enabled():
            state = "自动检查已关闭(仅手动 /sw check)"
        elif self._loop_task and not self._loop_task.done():
            state = f"运行中(间隔 {self.config.get('check_interval_hours', 6)} 小时)"
        else:
            state = "已停止"
        next_line = ""
        if self._next_check_ts:
            wait_min = max(0, int((self._next_check_ts - time.time()) / 60))
            next_line = f"下次检查: 约 {wait_min} 分钟后\n"
        sync_line = ""
        if self.storage.wishlist_sources:
            sync_line = (
                f"愿望单来源: {len(self.storage.wishlist_sources)} 个"
                f"(使用 /sw sources 查看、/sw sync 同步)\n"
            )
        return (
            f"Steam 愿望单监控\n"
            f"监控游戏: {len(self.storage.games)} 个\n"
            f"推送绑定: {total_bindings} 个会话\n"
            f"折扣阈值: {self.config.get('discount_threshold', 30)}%\n"
            f"区域/语言: {self.config.get('region', 'cn')} / {self.config.get('language', 'schinese')}\n"
            f"{sync_line}"
            f"后台任务: {state}\n{next_line}"
        )

    def _price_line(self, appid: str) -> str:
        """列表/导出共用的单行价格描述。

        金额统一走 format_money,保证与史低、历史价的渲染风格一致。
        """
        state = self.storage.price_state.get(appid, {})
        currency = state.get("currency", "")
        current = format_money(state.get("last_seen_final"), currency) or state.get("final_formatted")
        if state.get("discount_percent"):
            line = f"-{state['discount_percent']}% → {current or '?'}"
        elif current:
            # 含免费游戏等无 price_overview、只有现成文本的情况
            line = current
        else:
            line = "暂无价格数据"
        min_text = format_money(state.get("min_final"), currency)
        if min_text:
            line += f"(史低 {min_text})"
        return line

    def _cmd_list(self) -> str:
        if not self.storage.games:
            return "监控列表为空,使用 /sw search <游戏名> 搜索后添加,或 /sw import <愿望单> 导入。"
        items = sorted(
            self.storage.games.items(), key=lambda kv: kv[1].get("added_at", 0), reverse=True
        )
        lines = [
            f"{game['name']} ({appid}) · {self._price_line(appid)}" for appid, game in items
        ]
        shown = lines[:LIST_PAGE_SIZE]
        omitted = len(lines) - len(shown)
        text = f"监控列表({len(lines)} 个):\n" + "\n".join(shown)
        if omitted > 0:
            text += f"\n... 及另外 {omitted} 个游戏(完整列表可用 /sw export)"
        return text

    def _cmd_export(self) -> str:
        if not self.storage.games:
            return "监控列表为空,无可导出内容。"
        items = sorted(
            self.storage.games.items(), key=lambda kv: kv[1].get("added_at", 0), reverse=True
        )
        stamp = time.strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# Steam 监控列表({len(items)} 个)",
            f"导出时间: {stamp}",
            "",
            "| 游戏 | AppID | 现价 | 折扣 | 观测史低 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for appid, game in items:
            state = self.storage.price_state.get(appid, {})
            currency = state.get("currency", "")
            price = format_money(state.get("last_seen_final"), currency) or state.get(
                "final_formatted"
            ) or "暂无数据"
            discount = f"-{state['discount_percent']}%" if state.get("discount_percent") else "-"
            lowest = format_money(state.get("min_final"), currency) or "-"
            name = str(game.get("name", "")).replace("|", "\\|")
            lines.append(f"| {name} | {appid} | {price} | {discount} | {lowest or '-'} |")
        return "\n".join(lines)

    def _cmd_sources(self) -> str:
        if not self.storage.wishlist_sources:
            return "尚未导入过愿望单,请先用 /sw import <愿望单链接> 导入。"
        items = sorted(
            self.storage.wishlist_sources.items(), key=lambda kv: kv[1].get("added_at", 0)
        )
        lines = [f"已导入的愿望单来源({len(items)} 个):"]
        for i, (steamid, meta) in enumerate(items, 1):
            count = sum(
                1 for g in self.storage.games.values() if g.get("source") == steam_source(steamid)
            )
            label = meta.get("label") or "未命名"
            lines.append(f"{i}. {label} · SteamID {steamid} · 关联 {count} 个游戏")
        lines.append("使用 /sw unlink <序号> 移除某个来源")
        return "\n".join(lines)

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
        denied = self._deny_if_not_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
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
        if price.coming_soon:
            yield event.plain_result(
                f"已添加监控:「{price.name}」\n该游戏尚未发售"
                f"(预计 {price.release_date or '时间未定'}),发售时会推送提醒。"
            )
        elif price.has_price:
            if price.discount_percent:
                desc = (
                    f"原价 {price.initial_text},现价 {price.final_text}"
                    f"(-{price.discount_percent}%),当前就有折扣,请自行把握"
                )
            else:
                desc = f"当前售价 {price.final_text}(无折扣)"
            yield event.plain_result(f"已添加监控:「{price.name}」\n{desc}。")
        else:
            yield event.plain_result(
                f"已添加监控:「{price.name}」({price.final_text}),该游戏暂无付费价格,仅在出现售价变动时跟踪。"
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
        denied = self._deny_if_not_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
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
        label = ""
        try:
            if steamid.startswith("vanity:"):
                label = steamid[7:]
                steamid = await self.api.resolve_vanity(label)
            wishlist = await self.api.fetch_wishlist(steamid)
        except SteamAPIError as e:
            yield event.plain_result(f"拉取愿望单失败: {e}")
            return
        if not wishlist:
            yield event.plain_result("该愿望单为空或不可见(Steam 愿望单需设为公开)。")
            return
        added_appids: list[str] = []
        skipped = dismissed_cnt = 0
        for appid_str in wishlist:
            if appid_str in self.storage.games:
                skipped += 1
                continue
            if appid_str in self.storage.dismissed:
                dismissed_cnt += 1
                continue
            # 官方愿望单接口不返回游戏名,先以 AppID 占位,首次价格检查时回填
            self.storage.add_game(int(appid_str), f"AppID {appid_str}", source=steam_source(steamid))
            added_appids.append(appid_str)
        already_imported = steamid in self.storage.wishlist_sources
        self.storage.add_wishlist_source(steamid, label)
        self.storage.save()
        note = f",跳过已手动移除 {dismissed_cnt} 个" if dismissed_cnt else ""
        reimport = "(该愿望单此前已导入过)" if already_imported else ""
        tip = (
            "\n游戏名称与价格基线正在后台建立(较大的愿望单需要几分钟),完成后如有符合阈值的折扣会自动推送。"
            if added_appids
            else ""
        )
        yield event.plain_result(
            f"愿望单导入完成{reimport}: 新增 {len(added_appids)} 个,已存在 {skipped} 个{note},"
            f"当前共监控 {len(self.storage.games)} 个。{tip}\n"
            f"之后可随时使用 /sw sync 同步该愿望单的新增/移除。"
        )
        if added_appids:
            self._spawn_bootstrap(added_appids)

    def _spawn_bootstrap(self, appids: list[str]):
        """后台建立价格基线,避免阻塞指令(愿望单较大时逐个请求较慢)。"""
        task = asyncio.create_task(self._bootstrap_prices(appids))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _cmd_sync(self, event: AstrMessageEvent):
        denied = self._deny_if_not_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
        sources = list(self.storage.wishlist_sources.keys())
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
            self._spawn_bootstrap(added_appids)
        lines = [f"同步完成: 新增 {len(added_appids)} 个,移除 {len(removed_names)} 个。"]
        if failed:
            lines.append(f"有 {failed} 个愿望单同步失败(可能被限流),可稍后重试。")
        if removed_names:
            shown = "、".join(removed_names[:10]) + (" 等" if len(removed_names) > 10 else "")
            lines.append(f"已停止监控(愿望单中已删除): {shown}")
        if added_appids:
            lines.append("新增游戏的价格基线正在后台建立。")
        yield event.plain_result("\n".join(lines))

    async def _cmd_unlink(self, event: AstrMessageEvent, arg: str):
        """移除某个愿望单来源;可选择同时移除由它导入的游戏。"""
        denied = self._deny_if_not_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not self.storage.wishlist_sources:
            yield event.plain_result("尚未导入过愿望单,无可移除的来源。")
            return
        items = sorted(
            self.storage.wishlist_sources.items(), key=lambda kv: kv[1].get("added_at", 0)
        )
        if not arg:
            yield event.plain_result("用法: /sw unlink <序号> [all]\n" + self._cmd_sources())
            return
        parts = arg.split()
        if not parts[0].isdigit() or not (1 <= int(parts[0]) <= len(items)):
            yield event.plain_result(f"序号无效,请输入 1 - {len(items)} 之间的数字。\n" + self._cmd_sources())
            return
        steamid, meta = items[int(parts[0]) - 1]
        purge = len(parts) > 1 and parts[1].lower() in ("all", "全部")
        removed_games = 0
        if purge:
            for appid_str, game in list(self.storage.games.items()):
                if game.get("source") == steam_source(steamid):
                    self.storage.remove_game(int(appid_str))
                    removed_games += 1
        self.storage.remove_wishlist_source(steamid)
        self.storage.save()
        label = meta.get("label") or steamid
        msg = f"已移除愿望单来源「{label}」。"
        if purge:
            msg += f"\n同时移除了由它导入的 {removed_games} 个游戏。"
        else:
            msg += "\n由它导入的游戏仍保留在监控中(如需一并移除,使用 /sw unlink <序号> all)。"
        yield event.plain_result(msg)

    def _cmd_remove(self, event: AstrMessageEvent, arg: str) -> str:
        denied = self._deny_if_not_admin(event)
        if denied:
            return denied
        if not arg:
            return "用法: /sw remove <链接或AppID>"
        # 与 /sw add 保持一致: 纯数字优先按最近搜索的序号解析
        appid = self._resolve_appid(event, arg)
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

    def _cmd_history(self, event: AstrMessageEvent, arg: str) -> str:
        if not arg:
            return "用法: /sw history <链接或AppID>\n可先用 /sw list 查看监控中的 AppID。"
        appid = self._resolve_appid(event, arg)
        if appid is None:
            return "无法识别,请提供 Steam 商店链接或纯数字 AppID。"
        key = str(appid)
        game = self.storage.games.get(key)
        if not game:
            return f"AppID {appid} 不在监控列表中。"
        state = self.storage.price_state.get(key, {})
        history = state.get("history") or []
        currency = state.get("currency", "")
        header = f"「{game['name']}」价格走势"
        if not history:
            return (
                f"{header}\n暂无历史记录,插件会在每次价格检查时记录一个观测点"
                f"(当前价 {state.get('final_formatted') or '暂无数据'})。"
            )
        lines = [header, f"共 {len(history)} 个观测点(最多保留最近 60 个):"]
        for point in history[-15:]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(point.get("t", 0)))
            price = format_money(point.get("p"), currency) or "?"
            discount = f" (-{point['d']}%)" if point.get("d") else ""
            lines.append(f"{stamp}  {price}{discount}")
        if len(history) > 15:
            lines.append(f"... 及更早的 {len(history) - 15} 个观测点")
        min_cents = state.get("min_final")
        if min_cents is not None:
            lines.append(f"观测期最低价: {format_money(min_cents, currency)}")
        return "\n".join(lines)

    async def _cmd_check(self, event: AstrMessageEvent):
        denied = self._deny_if_not_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
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
        total, on_sale, pushed, failed, removed, released = result
        msg = (
            f"检查完成: {total} 个游戏,当前打折 {on_sale} 个,本次推送 {pushed} 条,失败 {failed} 个。"
        )
        if released:
            msg += f"\n其中 {released} 个游戏已发售,已推送提醒。"
        if removed:
            msg += f"\n已自动移除 {removed} 个疑似下架的游戏。"
        yield event.plain_result(msg)

    # ==================== 后台轮询 ====================

    def _config_enabled(self) -> bool:
        return bool(self.config.get("enable_auto_check", True))

    def _maybe_start_loop(self) -> bool:
        """启动轮询任务(幂等),返回本次是否真正启动。

        注意: 即使 enable_auto_check 为 false 也会启动该任务,因为任务内部会先检查配置并休眠。
        这样在 WebUI 里重新打开开关时无需重启插件即可恢复自动检查;
        关闭状态下它不会发起任何 Steam 请求。
        """
        if self._loop_task and not self._loop_task.done():
            return False
        self._loop_task = asyncio.create_task(self._check_loop())
        return True

    async def _check_loop(self):
        first_round = True
        while True:
            try:
                if not self._config_enabled():
                    # 配置关闭: 不检查,但保持任务存活并定期复查,便于 WebUI 开关即时生效
                    self._next_check_ts = None
                    await asyncio.sleep(DISABLED_POLL_INTERVAL)
                    first_round = True
                    continue
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
    ) -> tuple[int, int, int, int, int, int] | None:
        """全量检查。

        返回 (总数, 打折数, 推送数, 失败数, 自动移除数, 发售数);已有检查进行中返回 None。
        """
        if self._checking:
            return None
        self._checking = True
        total = on_sale = failed = removed_cnt = released_cnt = 0
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
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # 单个游戏的超时/网络抖动只记为一次失败,不影响其余游戏
                    failed += 1
                    logger.warning(f"Steam 愿望单监控: AppID {appid_str} 查询失败 {e}")
                    continue
                # 占位名回填或游戏改名同步
                if game.get("name") != price.name:
                    self.storage.games[appid_str]["name"] = price.name
                should_push, is_on_sale, reason = self._update_state_and_decide(
                    int(appid_str), price, record_push=push
                )
                if is_on_sale:
                    on_sale += 1
                if reason == "released":
                    released_cnt += 1
                if push and should_push:
                    to_push.append((price, reason))
                since_save += 1
                if since_save >= SAVE_EVERY:
                    self.storage.save()
                    since_save = 0
            self.storage.save()
            if push and to_push:
                await self._push_results(to_push)
            return total, on_sale, len(to_push), failed, removed_cnt, released_cnt
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
            "currency": price.currency,
            "coming_soon": price.coming_soon,
            "release_date": price.release_date,
            "last_check_at": now,
            "fail_count": 0,
        }
        if price.has_price:
            state["history"] = [{"t": now, "p": price.final_cents, "d": price.discount_percent}]
        self.storage.set_state(appid, state)

    def _update_state_and_decide(
        self, appid: int, price: GamePrice, record_push: bool = True
    ) -> tuple[bool, bool, str]:
        """更新单个游戏的价格状态。

        返回 (是否应推送, 是否在打折, 推送原因 discount/price_cut/released)。

        record_push=False 用于建立价格基线的场景(/sw import 后台初始化):
        此时只记录观测值,不能把当前价格写进 last_pushed_final,
        否则这次真实发生的降价会被误判为「已推送」而永久丢失通知。
        """
        state = self.storage.get_state(appid)
        now = int(time.time())
        notify_release = bool(self.config.get("enable_release_notify", True))

        # 发售提醒: 此前标记为未发售,现在已发售
        released = bool(state and state.get("coming_soon") and not price.coming_soon)

        if not price.has_price:
            # 免费游戏/未发售/无售价: 仅刷新观测信息
            state.update(
                {
                    "discount_percent": 0,
                    "final_formatted": price.final_formatted,
                    "initial_formatted": "",
                    "header_image": price.header_image,
                    "coming_soon": price.coming_soon,
                    "release_date": price.release_date,
                    "last_check_at": now,
                    "fail_count": 0,
                }
            )
            state.pop("last_seen_final", None)
            self.storage.set_state(appid, state)
            if released and notify_release:
                return True, False, "released"
            return False, False, ""

        old_seen = state.get("last_seen_final")
        old_min = state.get("min_final")
        old_base = state.get("base_final")
        is_new_lowest = old_min is None or price.final_cents <= old_min
        is_on_sale = price.discount_percent > 0

        should_push = False
        reason = ""
        if released and notify_release:
            should_push = True
            reason = "released"
        elif state:
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
        elif should_push and record_push and reason == "discount":
            state["last_pushed_final"] = price.final_cents

        # 观测期最低价(金额统一在展示时用 format_money 渲染)
        if is_new_lowest:
            state["min_final"] = price.final_cents
            if old_min != price.final_cents:
                state["min_final_at"] = now
        elif old_min is not None:
            state["min_final"] = old_min
        state.setdefault("min_final_at", now)

        state.update(
            {
                "last_seen_final": price.final_cents,
                "initial_cents": price.initial_cents,
                "discount_percent": price.discount_percent,
                "final_formatted": price.final_formatted,
                "initial_formatted": price.initial_formatted,
                "header_image": price.header_image,
                "currency": price.currency,
                "coming_soon": price.coming_soon,
                "release_date": price.release_date,
                "last_check_at": now,
                "fail_count": 0,
            }
        )
        self.storage.set_state(appid, state)
        self.storage.append_history(appid, price.final_cents, price.discount_percent, now)
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

    def _push_text(self, price: GamePrice, reason: str) -> str:
        state = self.storage.get_state(price.appid)
        currency = price.currency or state.get("currency", "")
        url = STORE_PAGE_URL.format(appid=price.appid)
        if reason == "released":
            if price.discount_percent:
                price_line = (
                    f"💰 {price.final_text}(原价 {price.initial_text})"
                    f"-{price.discount_percent}%"
                )
            else:
                price_line = f"💰 当前价格 {price.final_text}"
            return (
                f"🎉 Steam 发售提醒\n"
                f"「{price.name}」已发售\n"
                f"{price_line}\n"
                f"🔗 {url}"
            )
        if reason == "price_cut":
            old_base = state.get("prev_base_final")
            old_part = f"(原价 {format_money(old_base, currency)})" if old_base else ""
            return (
                f"✂️ Steam 基础价下调提醒\n"
                f"「{price.name}」\n"
                f"💰 新基础价 {price.final_text}{old_part} - 厂商降价,非促销折扣\n"
                f"🔗 {url}"
            )
        is_lowest = state.get("min_final") is not None and price.final_cents <= state["min_final"]
        lowest_line = "\n📉 观测期内历史最低价!" if is_lowest else ""
        return (
            f"🎮 Steam 折扣提醒\n"
            f"「{price.name}」\n"
            f"💰 {price.final_text}(原价 {price.initial_text})-{price.discount_percent}%"
            f"{lowest_line}\n"
            f"🔗 {url}"
        )

    async def _push_discount(self, price: GamePrice, reason: str):
        bindings = self._all_bindings()
        if not bindings:
            return
        text = self._push_text(price, reason)
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
            if reason == "released":
                lines.append(f"🎉「{price.name}」已发售 → {price.final_text}  {url}")
            elif reason == "price_cut":
                lines.append(f"✂️「{price.name}」基础价下调 → {price.final_text}  {url}")
            else:
                is_lowest = state.get("min_final") is not None and (
                    price.final_cents <= state["min_final"]
                )
                tag = " 📉史低" if is_lowest else ""
                lines.append(
                    f"「{price.name}」-{price.discount_percent}% → {price.final_text}{tag}  {url}"
                )
        text = "\n".join(lines)
        for umo in bindings:
            try:
                chain = MessageChain()
                chain.message(text)
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.warning(f"Steam 愿望单监控: 聚合推送到 {umo} 失败 {e}")
