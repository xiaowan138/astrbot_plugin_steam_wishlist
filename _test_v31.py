"""v0.3.1 验证: 配置健壮性、检查途中移除、限流退避,以及新增功能。

独立运行: python _test_v31.py
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


def build_mocks():
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    core_mod = types.ModuleType("astrbot.core")
    core_star_mod = types.ModuleType("astrbot.core.star")
    core_filter_mod = types.ModuleType("astrbot.core.star.filter")
    core_command_mod = types.ModuleType("astrbot.core.star.filter.command")

    sent: list[tuple[str, str]] = []
    images: list[str] = []

    class FakeMessageChain:
        def __init__(self):
            self.texts = []

        def message(self, t):
            self.texts.append(t)

        def url_image(self, u):
            images.append(u)

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
    return FakeContext(), sent, images


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


class FakeSteamAPI:
    """测试替身基类: 提供 _check_all 收尾时会调用的退避钩子。"""

    def reset_backoff(self):
        pass


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


async def collect(agen):
    return [item async for item in agen]


async def main():
    ctx, sent, images = build_mocks()
    main_mod = load_plugin_module()
    pkg_api = sys.modules["plugin_pkg.steam_api"]
    GamePrice = pkg_api.GamePrice
    RateLimitError = pkg_api.RateLimitError
    SteamAPI = pkg_api.SteamAPI
    format_money = pkg_api.format_money
    from storage import Storage

    def make_price(appid, final, disc, initial=10000, name=None, coming=False,
                   release_date="", currency="CNY", image=""):
        return GamePrice(
            appid=appid,
            name=name or f"Game{appid}",
            header_image=image,
            currency=currency,
            initial_cents=initial,
            final_cents=final,
            discount_percent=disc,
            initial_formatted=f"¥ {initial / 100:g}" if initial is not None else "",
            final_formatted=f"¥ {final / 100:g}" if final is not None else "暂无售价",
            coming_soon=coming,
            release_date=release_date,
        )

    def make_plugin(**over):
        cfg = {
            "discount_threshold": 30,
            "notify_lowest_only": False,
            "enable_auto_check": False,
            "enable_release_notify": True,
            "admin_only": False,
            "push_with_image": True,
            "push_targets": [],
        }
        cfg.update(over)
        p = main_mod.SteamWishlistPlugin(ctx, cfg)
        if p._loop_task:
            p._loop_task.cancel()
        p.storage = Storage(Path(tempfile.mkdtemp()))
        return p

    # ==================================================================
    print("=" * 60)
    print("【Bug 7】非法/空配置不再让检查中断或插件加载失败")
    print("-" * 60)
    pa = make_plugin(discount_threshold=None, check_interval_hours=None,
                     request_delay_seconds=None, push_targets=None,
                     enable_auto_check="false")
    check("request_delay_seconds=None 时插件仍能加载(回退 1.5)", pa.api.delay == 1.5,
          f"got {pa.api.delay}")
    check("字符串 \"false\" 被正确识别为关闭", pa._config_enabled() is False)
    check("check_interval_hours=None 回退 6 小时",
          pa._config_int("check_interval_hours", 6, minimum=1) == 6)
    check("discount_threshold 越界值被夹取到 0-100",
          make_plugin(discount_threshold=999)._config_int(
              "discount_threshold", 30, minimum=0, maximum=100) == 100)
    check("push_targets=None 时 /sw status 不报错", "Steam 愿望单监控" in pa._cmd_status())

    for i in range(3):
        pa.storage.add_game(700 + i, f"Game{700 + i}")
        pa._init_state(700 + i, make_price(700 + i, 10000, 0))

    class API_A(FakeSteamAPI):
        def __init__(self):
            self.calls = []

        async def fetch_app_price(self, appid):
            self.calls.append(appid)
            return make_price(appid, 4000, 60)

        async def close(self):
            pass

    pa.api = API_A()
    pa.storage.add_binding("umo:test")
    sent.clear()
    result = await pa._check_all(push=True)
    check("discount_threshold=None 时 3 个游戏全部被检查", pa.api.calls == [700, 701, 702],
          pa.api.calls)
    check("整轮未抛异常且推送正常", result[2] == 3 and len(sent) == 3,
          f"pushed={result[2]}, sent={len(sent)}")

    # ==================================================================
    print()
    print("=" * 60)
    print("【Bug 8】检查途中 /sw remove 不再中断整轮、不再复活状态")
    print("-" * 60)
    pb = make_plugin()
    # 占位名场景: 回填名字必然走到那处赋值,是原 KeyError 的触发路径
    pb.storage.add_game(800, "AppID 800")
    pb.storage.add_game(801, "AppID 801")
    pb._init_state(800, make_price(800, 10000, 0))
    pb._init_state(801, make_price(801, 10000, 0))
    pb.storage.add_binding("umo:test")

    class API_B(FakeSteamAPI):
        def __init__(self, plugin):
            self.plugin = plugin
            self.calls = []

        async def fetch_app_price(self, appid):
            self.calls.append(appid)
            if appid == 800:
                # 请求飞行途中用户移除了该游戏
                self.plugin.storage.remove_game(800)
                await asyncio.sleep(0)
            return make_price(appid, 4000, 60)

        async def close(self):
            pass

    pb.api = API_B(pb)
    sent.clear()
    try:
        result_b = await pb._check_all(push=True)
        raised = None
    except Exception as e:  # noqa: BLE001
        raised = e
        result_b = None
    check("整轮不再抛异常", raised is None, repr(raised))
    check("被移除游戏之后的 801 仍被检查", 801 in pb.api.calls, pb.api.calls)
    check("已移除游戏的 price_state 未被复活", "800" not in pb.storage.price_state)
    check("801 的降价仍推送成功", result_b[2] == 1 and len(sent) == 1,
          f"pushed={result_b[2]}, sent={len(sent)}")

    # ==================================================================
    print()
    print("=" * 60)
    print("【Bug 9】only=[] 不再退化为全量检查")
    print("-" * 60)
    pd = make_plugin()
    pd.storage.add_game(950, "Game950")
    pd.storage.add_game(951, "Game951")

    class API_D(FakeSteamAPI):
        def __init__(self):
            self.calls = []

        async def fetch_app_price(self, appid):
            self.calls.append(appid)
            return make_price(appid, 10000, 0)

        async def close(self):
            pass

    pd.api = API_D()
    await pd._check_all(push=False, only=[])
    check("only=[] 时一个游戏都不检查", pd.api.calls == [], pd.api.calls)

    # ==================================================================
    print()
    print("=" * 60)
    print("【Bug 10】无绑定会话时不再谎报推送条数")
    print("-" * 60)
    pe = make_plugin()
    pe.storage.add_game(960, "Game960")
    pe._init_state(960, make_price(960, 10000, 0))
    pe._update_state_and_decide(960, make_price(960, 10000, 0))

    class API_E(FakeSteamAPI):
        async def fetch_app_price(self, appid):
            return make_price(appid, 4000, 60)

        async def close(self):
            pass

    pe.api = API_E()
    sent.clear()
    result_e = await pe._check_all(push=True)
    check("无绑定会话时实际发送 0 条", len(sent) == 0)
    check("返回的推送数为 0 而非 1", result_e[2] == 0, f"got {result_e[2]}")

    # ==================================================================
    print()
    print("=" * 60)
    print("【Bug 11】unbind 不受 admin_only 限制,普通用户能自行退订")
    print("-" * 60)
    pf = make_plugin(admin_only=True)
    normal = FakeEvent(admin=False)
    check("普通用户 bind 被拒绝", "仅限管理员" in pf._cmd_bind(normal))
    pf.storage.add_binding("umo:test")
    check("普通用户仍可 unbind 自己", pf._cmd_unbind(normal) == "已解绑当前会话。")
    check("解绑确实生效", pf.storage.bindings == [])

    # ==================================================================
    print()
    print("=" * 60)
    print("【Bug 12】429 限流: 自动退避 + 本轮提前结束")
    print("-" * 60)

    class FakeResp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {}

    class FakeSession:
        def __init__(self, status):
            self.status = status

        def get(self, url, params=None):
            return FakeResp(self.status)

        @property
        def closed(self):
            return False

    api = SteamAPI(region="cn", delay=0.2)
    api._session = FakeSession(429)
    try:
        await api._throttled_get("https://example.invalid/x")
        check("429 应抛 RateLimitError", False)
    except RateLimitError:
        check("429 抛出 RateLimitError", True)
    check("请求间隔翻倍(0.2 → 0.4)", api.delay == 0.4, f"got {api.delay}")
    check("设置了冷却期", api._cooldown_until > 0)
    api.reset_backoff()
    check("reset_backoff 恢复配置间隔", api.delay == 0.2 and api._cooldown_until == 0)

    pg = make_plugin()
    for i in range(4):
        pg.storage.add_game(970 + i, f"Game{970 + i}")
        pg._init_state(970 + i, make_price(970 + i, 10000, 0))

    class API_G(FakeSteamAPI):
        def __init__(self):
            self.calls = []

        async def fetch_app_price(self, appid):
            self.calls.append(appid)
            if appid == 971:
                raise RateLimitError("被限流")
            return make_price(appid, 4000, 60)

        async def close(self):
            pass

    pg.api = API_G()
    pg.storage.add_binding("umo:test")
    sent.clear()
    result_g = await pg._check_all(push=True)
    check("被限流后停止本轮,不再请求后续游戏", pg.api.calls == [970, 971], pg.api.calls)
    check("跳过数正确(剩余 2 个)", result_g[6] == 2, f"got {result_g[6]}")
    check("限流前已成功的游戏仍推送", result_g[2] == 1 and len(sent) == 1,
          f"pushed={result_g[2]}, sent={len(sent)}")

    # ==================================================================
    print()
    print("=" * 60)
    print("【功能 7】/sw restore 查看并恢复被移除的愿望单游戏")
    print("-" * 60)
    ph = make_plugin()
    ph.storage.add_binding("umo:test")
    ph.storage.add_wishlist_source("76561198000000001", "我的愿望单")
    from storage import steam_source
    for appid in (100, 101):
        ph.storage.add_game(appid, f"Game{appid}", steam_source("76561198000000001"))
        ph._init_state(appid, make_price(appid, 10000, 0))

    check("移除愿望单游戏时记录了名字",
          "已移除监控" in ph._cmd_remove(FakeEvent(), "100"))
    check("进入 dismissed", ph.storage.dismissed == ["100"])
    listing = ph._cmd_restore(FakeEvent(), "")
    check("restore 列表含游戏名", "Game100" in listing, listing)
    check("restore 列表含序号", "1. Game100" in listing, listing)

    ph._spawn_bootstrap = lambda appids: None
    check("按序号恢复成功", "已恢复 1 个" in ph._cmd_restore(FakeEvent(), "1"))
    check("恢复后回到监控列表", "100" in ph.storage.games)
    check("恢复后清除移除记录", ph.storage.dismissed == [])

    ph._cmd_remove(FakeEvent(), "101")
    check("全部恢复成功", "已恢复 1 个" in ph._cmd_restore(FakeEvent(), "all"))
    check("全部恢复后记录清空", ph.storage.dismissed == [])
    check("无记录时给出提示", "没有" in ph._cmd_restore(FakeEvent(), ""))
    # 先造一条移除记录,再验证「序号越界」提示(空列表走的是另一条分支)
    ph.storage.dismiss(999, "Ghost")
    check("序号越界给出提示", "无效" in ph._cmd_restore(FakeEvent(), "9"))
    check("admin_only 下普通用户 restore 被拒",
          "仅限管理员" in make_plugin(admin_only=True)._cmd_restore(FakeEvent(admin=False), ""))

    # ==================================================================
    print()
    print("=" * 60)
    print("【功能 8】/sw list 区分「未发售」与「待建立基线」")
    print("-" * 60)
    pi = make_plugin()
    pi.storage.add_game(500, "ComingGame")
    pi.storage.add_game(501, "FreshGame")
    pi._init_state(500, make_price(500, None, 0, initial=None, coming=True,
                                  release_date="2026 Q2"))
    pi._update_state_and_decide(500, make_price(500, None, 0, initial=None, coming=True,
                                                release_date="2026 Q2"))
    out = pi._cmd_list()
    check("未发售游戏显示发售时间", "未发售(2026 Q2)" in out, out)
    check("尚未检查的游戏显示待建立基线", "待建立价格基线" in out, out)
    check("不再出现「暂无价格数据」", "暂无价格数据" not in out, out)

    # ==================================================================
    print()
    print("=" * 60)
    print("【功能 9】/sw stats 与 /sw top")
    print("-" * 60)
    pj = make_plugin()
    pj.storage.add_game(600, "Sale70")
    pj.storage.add_game(601, "Sale50")
    pj.storage.add_game(602, "FullPrice")
    pj.storage.add_game(603, "ComingSoon")
    pj._init_state(600, make_price(600, 10000, 0))
    pj._update_state_and_decide(600, make_price(600, 3000, 70))
    pj._init_state(601, make_price(601, 10000, 0))
    pj._update_state_and_decide(601, make_price(601, 5000, 50))
    pj._init_state(602, make_price(602, 10000, 0))
    pj._init_state(603, make_price(603, None, 0, initial=None, coming=True))

    stats = pj._cmd_stats()
    check("stats 显示监控总数", "监控总数: 4 个" in stats, stats)
    check("stats 显示未发售数", "未发售 1" in stats, stats)
    check("stats 显示打折数", "当前打折: 2 个" in stats, stats)
    check("stats 显示达阈值数", "达到阈值(30%): 2 个" in stats, stats)
    check("stats 显示最大折扣", "最大 -70%" in stats, stats)

    top = pj._cmd_top("")
    check("top 按折扣降序,70% 在 50% 之前",
          top.index("Sale70") < top.index("Sale50"), top)
    check("top 不含未打折游戏", "FullPrice" not in top, top)
    check("top 不含未发售游戏", "ComingSoon" not in top, top)
    check("top 显示折扣与价格", "-70% → ¥30" in top, top)
    check("top 条数参数生效", "Sale50" not in pj._cmd_top("1"))
    check("top 非法参数给出提示", "无效" in pj._cmd_top("abc"))

    # ==================================================================
    print()
    print("=" * 60)
    print("【功能 10】/sw add 支持一次多个 AppID")
    print("-" * 60)
    pk = make_plugin()

    class API_K(FakeSteamAPI):
        async def fetch_app_price(self, appid):
            if appid == 999:
                raise pkg_api.AppNotFoundError("不存在")
            return make_price(appid, 10000, 0)

        async def close(self):
            pass

    pk.api = API_K()
    out = await collect(pk._cmd_add(FakeEvent(), "620 570"))
    check("批量添加两个都成功", "成功 2 个,失败 0 个" in out[0], out[0])
    check("两个都进入监控列表", "620" in pk.storage.games and "570" in pk.storage.games)
    check("批量回执含每个游戏", "✓ Game620" in out[0] and "✓ Game570" in out[0], out[0])

    out = await collect(pk._cmd_add(FakeEvent(), "620 999"))
    check("重复的记为失败并说明原因", "已在监控中" in out[0], out[0])
    check("查询失败的记为失败", "查询失败" in out[0], out[0])

    out = await collect(pk._cmd_add(FakeEvent(), "621"))
    check("单个添加仍返回详细回执", "已添加监控" in out[0], out[0])

    # ==================================================================
    print()
    print("=" * 60)
    print("【功能 11】/sw import 支持自定义别名")
    print("-" * 60)
    pl = make_plugin()

    class API_L(FakeSteamAPI):
        async def fetch_wishlist(self, steamid):
            return {"700": "700"}

        async def close(self):
            pass

    pl.api = API_L()
    pl._spawn_bootstrap = lambda appids: None
    out = await collect(pl._cmd_import(FakeEvent(), "76561198000000002 我的主号"))
    check("别名导入成功", "导入" in out[-1], out[-1])
    check("来源记录了自定义别名",
          pl.storage.wishlist_sources["76561198000000002"]["label"] == "我的主号",
          pl.storage.wishlist_sources)
    out = await collect(pl._cmd_import(FakeEvent(), "76561198000000002 新名字"))
    check("重新导入可重命名来源",
          pl.storage.wishlist_sources["76561198000000002"]["label"] == "新名字",
          pl.storage.wishlist_sources)
    check("/sw sources 显示别名", "新名字" in pl._cmd_sources())

    # ==================================================================
    print()
    print("=" * 60)
    print("【功能 12】push_with_image 可关闭头图")
    print("-" * 60)
    pm = make_plugin(push_with_image=False)
    pm.storage.add_game(400, "Game400")
    pm.storage.add_binding("umo:test")
    pm._init_state(400, make_price(400, 10000, 0, image="http://img/400.jpg"))
    pm._update_state_and_decide(400, make_price(400, 10000, 0, image="http://img/400.jpg"))

    class API_M(FakeSteamAPI):
        async def fetch_app_price(self, appid):
            return make_price(appid, 3000, 70, image="http://img/400.jpg")

        async def close(self):
            pass

    pm.api = API_M()
    images.clear()
    sent.clear()
    await pm._check_all(push=True)
    check("关闭后不发送头图", images == [], images)
    check("文字推送仍正常", len(sent) == 1)

    pn = make_plugin(push_with_image=True)
    pn.storage.add_game(401, "Game401")
    pn.storage.add_binding("umo:test")
    pn._init_state(401, make_price(401, 10000, 0, image="http://img/401.jpg"))
    pn._update_state_and_decide(401, make_price(401, 10000, 0, image="http://img/401.jpg"))

    class API_N(FakeSteamAPI):
        async def fetch_app_price(self, appid):
            return make_price(appid, 3000, 70, image="http://img/401.jpg")

        async def close(self):
            pass

    pn.api = API_N()
    images.clear()
    await pn._check_all(push=True)
    check("开启时发送头图", images == ["http://img/401.jpg"], images)

    # ==================================================================
    print()
    print("=" * 60)
    print("【兼容性】v0.3.0 数据升级到 v0.3.1")
    print("-" * 60)
    old = {
        "games": {"100": {"name": "OldGame", "added_at": 1, "source": "manual"}},
        "price_state": {"100": {"last_seen_final": 5000, "currency": "CNY"}},
        "bindings": ["umo:a"],
        "wishlist_sources": {"76561198000000003": {"label": "旧来源", "added_at": 1}},
        "dismissed": ["200"],
    }
    data_dir = Path(tempfile.mkdtemp())
    (data_dir / "data.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    restored = Storage(data_dir)
    check("旧数据加载正常", restored.games == old["games"])
    check("dismissed 保留", restored.dismissed == ["200"])
    check("dismissed_names 缺省为空 dict", restored.dismissed_names == {})
    check("无名字时回退为 AppID 标签", restored.dismissed_label(200) == "AppID 200")
    restored.dismiss(300, "Named")
    restored.save()
    check("重新加载保留 dismissed_names", Storage(data_dir).dismissed_names == {"300": "Named"})

    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())