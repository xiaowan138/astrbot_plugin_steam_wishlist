"""v0.3.0 验证脚本: 6 个 bug 修复 + 6 个新功能。

运行: python _test_v3.py
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))

from steam_api import (
    AppNotFoundError,
    GamePrice,
    HTTPStatusError,
    SteamAPIError,
    format_money,
)


def build_mocks():
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    core_mod = types.ModuleType("astrbot.core")
    core_star_mod = types.ModuleType("astrbot.core.star")
    core_filter_mod = types.ModuleType("astrbot.core.star.filter")
    core_command_mod = types.ModuleType("astrbot.core.star.filter.command")

    sent = []

    class FakeMessageChain:
        def __init__(self):
            self.texts = []
            self.images = []

        def message(self, t):
            self.texts.append(t)

        def url_image(self, u):
            self.images.append(u)

    class FakeFilter:
        @staticmethod
        def command(*a, **k):
            return lambda f: f

        @staticmethod
        def on_astrbot_loaded():
            return lambda f: f

    class FakeContext:
        async def send_message(self, umo, chain):
            sent.append((umo, "\n".join(chain.texts)))

    class FakeStar:
        def __init__(self, context):
            self.context = context

    class GreedyStr(str):
        pass

    event_mod.filter = FakeFilter()
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = FakeMessageChain
    star_mod.register = lambda *a, **k: (lambda cls: cls)
    star_mod.Star = FakeStar
    star_mod.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path(tempfile.mkdtemp()))
    star_mod.Context = FakeContext
    api_mod.AstrBotConfig = dict
    api_mod.logger = types.SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None
    )
    core_command_mod.GreedyStr = GreedyStr
    astrbot_mod.api = api_mod
    api_mod.event = event_mod
    astrbot_mod.core = core_mod
    core_mod.star = core_star_mod
    core_star_mod.filter = core_filter_mod
    core_filter_mod.command = core_command_mod
    api_mod.star = star_mod
    for name, mod in {
        "astrbot": astrbot_mod,
        "astrbot.api": api_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.core": core_mod,
        "astrbot.core.star": core_star_mod,
        "astrbot.core.star.filter": core_filter_mod,
        "astrbot.core.star.filter.command": core_command_mod,
    }.items():
        sys.modules[name] = mod
    return FakeContext(), sent


def load_plugin_module():
    pkg = types.ModuleType("plugin_pkg")
    pkg.__path__ = [str(PLUGIN_DIR)]
    pkg.__package__ = "plugin_pkg"
    sys.modules["plugin_pkg"] = pkg
    spec = importlib.util.spec_from_file_location("plugin_pkg.main", str(PLUGIN_DIR / "main.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugin_pkg"
    spec.loader.exec_module(mod)
    return mod


def make_price(appid, final_cents, discount, initial=10000, name=None, currency="CNY",
               coming_soon=False, release_date=""):
    return GamePrice(
        appid=appid,
        name=name or f"Game{appid}",
        header_image=f"https://img/{appid}.jpg",
        currency=currency,
        initial_cents=initial,
        final_cents=final_cents,
        discount_percent=discount,
        initial_formatted=f"¥ {initial / 100:g}",
        final_formatted=f"¥ {final_cents / 100:g}" if final_cents is not None else "暂无售价",
        coming_soon=coming_soon,
        release_date=release_date,
    )


class FakeEvent:
    def __init__(self, umo="umo:test", admin=False):
        self.unified_msg_origin = umo
        self._admin = admin

    def is_admin(self):
        return self._admin

    def plain_result(self, t):
        return t


def check(label, cond, detail=""):
    if not cond:
        print(f"  ✗ {label} {detail}")
        raise AssertionError(label)
    print(f"  ✓ {label}")


async def main():
    ctx, sent = build_mocks()
    main_mod = load_plugin_module()

    # main.py 以包内相对导入加载 steam_api,与脚本顶部的同名模块是两个实例,
    # 会导致 except HTTPStatusError 抓不到脚本抛出的异常。这里统一到包内实例。
    pkg_api = sys.modules["plugin_pkg.steam_api"]
    g = globals()
    for name in ("GamePrice", "format_money", "SteamAPIError", "AppNotFoundError", "HTTPStatusError"):
        g[name] = getattr(pkg_api, name)

    from storage import Storage, steam_source

    def make_plugin(**over):
        cfg = {
            "discount_threshold": 30,
            "notify_lowest_only": False,
            "enable_auto_check": False,
            "enable_release_notify": True,
            "admin_only": False,
            "push_targets": [],
        }
        cfg.update(over)
        p = main_mod.SteamWishlistPlugin(ctx, cfg)
        if p._loop_task:
            p._loop_task.cancel()
        p.storage = Storage(Path(tempfile.mkdtemp()))
        return p

    # ==================================================================
    print("\n【Bug 1】导入愿望单不再吞掉已监控游戏的降价")
    print("-" * 60)
    p = make_plugin()
    p.storage.add_game(100, "OldGame")
    p._init_state(100, make_price(100, 10000, 0))
    p._update_state_and_decide(100, make_price(100, 10000, 0))

    # (a) record_push=False 不得写 last_pushed_final
    p._update_state_and_decide(100, make_price(100, 4000, 60), record_push=False)
    st = p.storage.get_state(100)
    check("record_push=False 时不写 last_pushed_final", st.get("last_pushed_final") is None,
          f"got {st.get('last_pushed_final')}")

    # (b) /sw import 只把新增游戏交给后台 bootstrap
    captured = []
    p._spawn_bootstrap = lambda appids: captured.append(list(appids))

    class ImportAPI:
        async def fetch_wishlist(self, steamid):
            return {"100": "100", "200": "200", "300": "300"}

        async def close(self):
            pass

    p.api = ImportAPI()
    replies = [r async for r in p._cmd_import(FakeEvent(), "76561198000000001")]
    check("导入后只 bootstrap 新增的 2 个(不含已监控的 100)",
          captured == [["200", "300"]], f"got {captured}")
    check("导入结果文案正确", "新增 2 个" in replies[-1], replies[-1])

    # (c) 端到端: 愿望单里已监控的游戏不进入 bootstrap,其降价仍能正常推送
    p2 = make_plugin()
    p2.storage.add_game(100, "OldGame")
    p2._init_state(100, make_price(100, 10000, 0))
    p2._update_state_and_decide(100, make_price(100, 10000, 0))
    p2.storage.add_binding("umo:test")
    captured2 = []
    p2._spawn_bootstrap = lambda appids: captured2.append(list(appids))

    class ImportAPI2:
        async def fetch_wishlist(self, steamid):
            return {"100": "100"}  # 愿望单里只有那个已在监控的游戏

        async def close(self):
            pass

    p2.api = ImportAPI2()
    [r async for r in p2._cmd_import(FakeEvent(), "76561198000000001")]
    check("已监控游戏不进入 bootstrap 轮次", captured2 == [], f"got {captured2}")

    class DropAPI:
        async def fetch_app_price(self, appid):
            return make_price(appid, 4000, 60)

        async def close(self):
            pass

    p2.api = DropAPI()
    sent.clear()
    result = await p2._check_all(push=True)
    check("之后真实检查仍能推送该游戏的降价", result[2] == 1 and len(sent) == 1,
          f"pushed={result[2]}, sent={len(sent)}")

    # ==================================================================
    print("\n【Bug 2】单个请求超时不再中断整轮检查")
    print("-" * 60)
    p3 = make_plugin()
    for i in range(5):
        p3.storage.add_game(900 + i, f"Game{900 + i}")
        p3._init_state(900 + i, make_price(900 + i, 10000, 0))
    p3.storage.add_binding("umo:test")

    class TimeoutAPI:
        def __init__(self):
            self.calls = []

        async def fetch_app_price(self, appid):
            self.calls.append(appid)
            if appid == 902:
                raise SteamAPIError("Steam 接口响应超时")
            if appid >= 903:
                return make_price(appid, 4000, 60)
            return make_price(appid, 10000, 0)

        async def close(self):
            pass

    p3.api = TimeoutAPI()
    sent.clear()
    result = await p3._check_all(push=True)
    total, on_sale, pushed, failed, removed, released = result
    check("全部 5 个游戏都被检查", p3.api.calls == [900, 901, 902, 903, 904], p3.api.calls)
    check("失败数记为 1", failed == 1, f"got {failed}")
    check("超时之后的 903/904 仍成功推送", pushed == 2 and len(sent) == 2,
          f"pushed={pushed}, sent={len(sent)}")

    # ==================================================================
    print("\n【Bug 3】/sw list 正确显示无折扣游戏的现价")
    print("-" * 60)
    p4 = make_plugin()
    p4.storage.add_game(620, "Portal 2")
    p4._init_state(620, make_price(620, 7000, 0))
    p4._update_state_and_decide(620, make_price(620, 7000, 0))
    out = p4._cmd_list()
    check("显示现价 ¥70 而非「暂无价格数据」", "¥70" in out and "暂无价格数据" not in out, out)
    # 打折的游戏
    p4._update_state_and_decide(620, make_price(620, 3500, 50))
    out = p4._cmd_list()
    check("打折游戏显示折扣与现价", "-50% → ¥35" in out, out)
    # 免费游戏
    p5 = make_plugin()
    p5.storage.add_game(570, "Dota 2")
    free = GamePrice(appid=570, name="Dota 2", header_image="", currency="", initial_cents=None,
                     final_cents=None, discount_percent=0, initial_formatted="",
                     final_formatted="免费")
    p5._init_state(570, free)
    p5._update_state_and_decide(570, free)
    out = p5._cmd_list()
    check("免费游戏显示「免费」", "免费" in out, out)

    # ==================================================================
    print("\n【Bug 4】enable_auto_check=false 时不发起任何检查")
    print("-" * 60)
    p6 = make_plugin(enable_auto_check=False)
    p6.storage.add_game(620, "Portal 2")
    calls = []
    orig = p6._check_all

    async def spy(*a, **k):
        calls.append(1)
        return await orig(*a, **k)

    p6._check_all = spy
    p6._maybe_start_loop()
    await asyncio.sleep(0.05)
    check("关闭状态下 _next_check_ts 为空(不排检查)", p6._next_check_ts is None,
          f"got {p6._next_check_ts}")
    check("关闭状态下未调用 _check_all", not calls, f"calls={len(calls)}")
    check("状态文案提示已关闭", "自动检查已关闭" in p6._cmd_status(), p6._cmd_status())
    p6._loop_task.cancel()

    # 打开开关后(无需重启)恢复
    p6.config["enable_auto_check"] = True
    p6._next_check_ts = None
    p6._loop_task = asyncio.create_task(p6._check_loop())
    await asyncio.sleep(0.05)
    check("重新开启后立即排定检查(约 90 秒后)",
          p6._next_check_ts is not None and 60 < p6._next_check_ts - time.time() <= 90,
          f"got {p6._next_check_ts}")
    p6._loop_task.cancel()

    # ==================================================================
    print("\n【Bug 5】价格文本按区域货币渲染,不再硬编码 ¥")
    print("-" * 60)
    check("CNY → ¥", format_money(5000, "CNY") == "¥50", format_money(5000, "CNY"))
    check("USD → $", format_money(5000, "USD") == "$50", format_money(5000, "USD"))
    check("未知货币退化为「金额 代码」", format_money(5000, "XYZ") == "50 XYZ",
          format_money(5000, "XYZ"))
    check("None → 空串", format_money(None, "CNY") == "")

    p7 = make_plugin()
    p7.storage.add_game(1, "USGame")
    p7._init_state(1, make_price(1, 10000, 0, currency="USD"))
    p7._update_state_and_decide(1, make_price(1, 10000, 0, currency="USD"))
    p7._update_state_and_decide(1, make_price(1, 7000, 0, currency="USD"))
    pushed, _, reason = p7._update_state_and_decide(1, make_price(1, 7000, 0, currency="USD"))
    text = p7._push_text(make_price(1, 7000, 0, currency="USD"), "price_cut")
    check("基础价下调文案使用 $ 而非 ¥", "$100" in text and "¥" not in text, text)

    # ==================================================================
    print("\n【Bug 6】resolve_vanity 走节流通道并正确映射 404")
    print("=" * 60)
    api_mod = sys.modules["plugin_pkg.steam_api"]
    api = api_mod.SteamAPI(region="cn", delay=0.2)

    async def fake_get_404(url, params=None):
        raise HTTPStatusError(404, "Steam 接口返回 HTTP 404")

    async def fake_get_ok(url, params=None):
        return '<html>{"steamid":"76561198010217378"}</html>'

    api._throttled_get = fake_get_404
    try:
        await api.resolve_vanity("nobody")
        check("404 应抛错", False)
    except SteamAPIError as e:
        check("404 → 友好提示", "找不到自定义URL" in str(e), str(e))
    api._throttled_get = fake_get_ok
    sid = await api.resolve_vanity("someone")
    check("正常解析出 SteamID", sid == "76561198010217378", sid)
    await api.close()

    # ==================================================================
    print("\n【功能 1】/sw list 显示观测期史低")
    print("-" * 60)
    p8 = make_plugin()
    p8.storage.add_game(620, "Portal 2")
    p8._init_state(620, make_price(620, 10000, 0))
    p8._update_state_and_decide(620, make_price(620, 4000, 60))   # 史低 ¥40
    p8._update_state_and_decide(620, make_price(620, 6000, 40))   # 回到 ¥60
    out = p8._cmd_list()
    check("列表含史低标记", "史低 ¥40" in out, out)

    # ==================================================================
    print("\n【功能 2】/sw sources 与 /sw unlink")
    print("-" * 60)
    p9 = make_plugin()
    p9.storage.add_wishlist_source("76561198000000001", label="alice")
    p9.storage.add_wishlist_source("76561198000000002")
    p9.storage.add_game(100, "FromAlice", source=steam_source("76561198000000001"))
    p9.storage.add_game(200, "Manual")

    src = p9._cmd_sources()
    check("来源列表含自定义名与 SteamID", "alice" in src and "76561198000000001" in src, src)
    check("来源列表统计关联游戏数", "关联 1 个游戏" in src, src)

    # 不带 all: 只解除来源
    replies = [r async for r in p9._cmd_unlink(FakeEvent(), "1")]
    check("unlink 解除来源", "76561198000000001" not in p9.storage.wishlist_sources)
    check("unlink 默认保留游戏", "100" in p9.storage.games)
    check("提示可用 all 一并移除", "all" in replies[-1], replies[-1])

    # 带 all: 连带移除
    p9.storage.add_wishlist_source("76561198000000003")
    p9.storage.add_game(300, "FromCarol", source=steam_source("76561198000000003"))
    replies = [r async for r in p9._cmd_unlink(FakeEvent(), "2 all")]
    check("unlink all 移除来源关联游戏", "300" not in p9.storage.games)
    check("unlink all 不动手动游戏", "200" in p9.storage.games)
    check("unlink all 文案报告移除数量", "1 个游戏" in replies[-1], replies[-1])

    replies = [r async for r in p9._cmd_unlink(FakeEvent(), "99")]
    check("序号越界给出提示", "序号无效" in replies[-1], replies[-1])

    # ==================================================================
    print("\n【功能 3】发售提醒")
    print("-" * 60)
    p10 = make_plugin()
    p10.storage.add_game(400, "ComingSoon")
    p10._init_state(400, make_price(400, None, 0, coming_soon=True, release_date="2026 Q2"))
    p10._update_state_and_decide(400, make_price(400, None, 0, coming_soon=True))
    push, _, reason = p10._update_state_and_decide(400, make_price(400, None, 0, coming_soon=True))
    check("未发售期间不推送", push is False, f"got {push}")

    push, _, reason = p10._update_state_and_decide(
        400, make_price(400, 7000, 0, coming_soon=False, release_date="2026-09-01")
    )
    check("发售后推送且原因为 released", push is True and reason == "released", f"{push}/{reason}")
    push, _, reason = p10._update_state_and_decide(
        400, make_price(400, 7000, 0, coming_soon=False)
    )
    check("发售提醒只发一次", push is False, f"got {push}")

    text = p10._push_text(make_price(400, 7000, 0, coming_soon=False), "released")
    check("发售文案正确", "已发售" in text and "¥70" in text, text)

    # 打折发售时带折扣信息
    text = p10._push_text(make_price(400, 3500, 50, coming_soon=False), "released")
    check("打折发售时文案含折扣", "-50%" in text, text)

    # 开关关闭时不推送
    p11 = make_plugin(enable_release_notify=False)
    p11.storage.add_game(400, "ComingSoon")
    p11._init_state(400, make_price(400, None, 0, coming_soon=True))
    p11._update_state_and_decide(400, make_price(400, None, 0, coming_soon=True))
    push, _, reason = p11._update_state_and_decide(
        400, make_price(400, 7000, 0, coming_soon=False)
    )
    check("关闭发售提醒后不推送", push is False and reason == "", f"{push}/{reason}")

    # ==================================================================
    print("\n【功能 4】权限控制")
    print("-" * 60)
    p12 = make_plugin(admin_only=True)
    check("非管理员 bind 被拒", "仅限管理员" in p12._cmd_bind(FakeEvent(admin=False)))
    check("管理员 bind 放行", "已绑定" in p12._cmd_bind(FakeEvent(admin=True)))
    check("非管理员 remove 被拒", "仅限管理员" in p12._cmd_remove(FakeEvent(admin=False), "620"))
    check("查询类 list 不受限", "监控列表为空" in p12._cmd_list())
    check("查询类 export 不受限", "为空" in p12._cmd_export())
    p13 = make_plugin(admin_only=False)
    check("开关关闭时普通用户可 bind", "已绑定" in p13._cmd_bind(FakeEvent(admin=False)))

    # ==================================================================
    print("\n【功能 5】/sw export 导出 Markdown")
    print("-" * 60)
    p14 = make_plugin()
    p14.storage.add_game(620, "Portal 2")
    p14._init_state(620, make_price(620, 10000, 0))
    p14._update_state_and_decide(620, make_price(620, 3500, 50))
    md = p14._cmd_export()
    check("导出为 Markdown 表格", "| 游戏 | AppID | 现价 | 折扣 | 观测史低 |" in md, md[:200])
    check("导出含折扣与史低", "-50%" in md and "¥35" in md, md)
    check("导出含游戏名", "Portal 2" in md, md)
    check("空列表给出提示", "为空" in make_plugin()._cmd_export())

    # ==================================================================
    print("\n【功能 6】/sw history 价格走势")
    print("-" * 60)
    p15 = make_plugin()
    p15.storage.add_game(620, "Portal 2")
    p15._init_state(620, make_price(620, 10000, 0))
    for cents, disc in ((10000, 0), (7000, 30), (3500, 65), (7000, 30)):
        p15._update_state_and_decide(620, make_price(620, cents, disc))
    hist = p15._cmd_history(FakeEvent(), "620")
    check("走势含观测点数量", "个观测点" in hist, hist)
    check("走势含最低价", "观测期最低价: ¥35" in hist, hist)
    check("走势含多次价格", "¥70" in hist and "¥35" in hist, hist)
    check("不在监控中的游戏给出提示", "不在监控列表中" in p15._cmd_history(FakeEvent(), "999"))
    check("无参数时给出用法", "用法" in p15._cmd_history(FakeEvent(), ""))

    # 相同价格不重复记录
    before = len(p15.storage.get_state(620)["history"])
    p15._update_state_and_decide(620, make_price(620, 7000, 30))
    after = len(p15.storage.get_state(620)["history"])
    check("相同价格不重复写入历史", before == after, f"{before} → {after}")

    # ==================================================================
    print("\n【兼容性】v0.2.0 旧数据自动升级")
    print("-" * 60)
    with tempfile.TemporaryDirectory() as d:
        legacy = {
            "games": {"620": {"name": "Portal 2", "added_at": 1, "source": "manual"}},
            "price_state": {},
            "bindings": ["umo:old"],
            "wishlist_sources": ["76561198000000009"],
            "dismissed": ["999"],
        }
        (Path(d) / "data.json").write_text(json.dumps(legacy), encoding="utf-8")
        s = Storage(Path(d))
        check("旧 list 格式来源升级为 dict", isinstance(s.wishlist_sources, dict))
        check("旧来源数据保留", "76561198000000009" in s.wishlist_sources)
        check("旧监控列表保留", "620" in s.games)
        check("旧绑定保留", s.bindings == ["umo:old"])
        s.save()
        s2 = Storage(Path(d))
        check("升级后重新加载正常", "76561198000000009" in s2.wishlist_sources)

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())