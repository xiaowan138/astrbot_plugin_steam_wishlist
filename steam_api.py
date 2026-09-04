"""Steam 商店 API 封装。

仅使用无需 API Key 的公开接口:
- appdetails:       查询单个游戏的详情与价格(含折扣)
- IWishlistService:  官方愿望单接口(替代已被下线的 wishlistdata)
- steamcommunity 个人主页 HTML: 将自定义URL名(vanity)解析为 64 位 SteamID
"""

import asyncio
import re
import time
from dataclasses import dataclass

import aiohttp

STORE_API = "https://store.steampowered.com/api/appdetails"
SEARCH_API = "https://store.steampowered.com/api/storesearch/"
WISHLIST_API = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
COMMUNITY_ID_URL = "https://steamcommunity.com/id/{vanity}/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

CURRENCY_SYMBOLS = {
    "CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥", "GBP": "£",
    "HKD": "HK$", "TWD": "NT$", "RUB": "₽", "KRW": "₩", "SGD": "S$",
}

APP_URL_PATTERN = re.compile(
    r"(?:store\.steampowered\.com|store\.steamchina\.com)/app/(\d+)", re.IGNORECASE
)
# 两种愿望单链接形态: store 站的 /wishlist/profiles/xxx 与社区站的 /profiles/xxx/wishlist
WISHLIST_ID_PATTERNS = [
    re.compile(r"wishlist/profiles/(\d{17})", re.IGNORECASE),
    re.compile(r"profiles/(\d{17})/wishlist", re.IGNORECASE),
]
WISHLIST_VANITY_PATTERNS = [
    re.compile(r"wishlist/id/([A-Za-z0-9_-]+)/?", re.IGNORECASE),
    re.compile(r"id/([A-Za-z0-9_-]+)/wishlist", re.IGNORECASE),
]
STEAMID_IN_HTML_PATTERN = re.compile(r"steamid[\"']?\s*[:=]\s*[\"']?(\d{17})", re.IGNORECASE)


@dataclass
class GamePrice:
    """一个游戏的当前价格快照。金额单位为「分」,与 Steam API 保持一致。"""

    appid: int
    name: str
    header_image: str
    currency: str
    initial_cents: int | None
    final_cents: int | None
    discount_percent: int
    initial_formatted: str
    final_formatted: str

    @property
    def has_price(self) -> bool:
        """免费游戏、未发售或无价格信息的条目没有 price_overview。"""
        return self.final_cents is not None


def extract_appid(text: str) -> int | None:
    """从任意文本中提取 Steam 商店链接里的 AppID,也接受纯数字。"""
    text = text.strip()
    matched = APP_URL_PATTERN.search(text)
    if matched:
        return int(matched.group(1))
    if text.isdigit():
        return int(text)
    return None


def extract_wishlist_steamid(text: str) -> str | None:
    """从愿望单链接中提取 64 位 SteamID 或自定义URL名。"""
    for pattern in WISHLIST_ID_PATTERNS:
        matched = pattern.search(text)
        if matched:
            return matched.group(1)
    for pattern in WISHLIST_VANITY_PATTERNS:
        matched = pattern.search(text)
        if matched:
            return f"vanity:{matched.group(1)}"
    text = text.strip()
    if re.fullmatch(r"\d{17}", text):
        return text
    if re.fullmatch(r"[A-Za-z0-9_-]{2,32}", text):
        return f"vanity:{text}"
    return None


class SteamAPIError(Exception):
    """Steam 接口请求失败或返回异常数据。"""


class AppNotFoundError(SteamAPIError):
    """AppID 已下架或不存在(appdetails 明确返回 success=false)。"""


class SteamAPI:
    """带节流与共享会话的 Steam 公开接口客户端。"""

    def __init__(self, region: str = "cn", language: str = "schinese", delay: float = 1.5):
        self.region = region
        self.language = language
        self.delay = max(0.2, delay)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last_request_ts = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20), headers=DEFAULT_HEADERS
                )
            return self._session

    async def close(self):
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _throttled_get(self, url: str, params: dict | None = None) -> dict | str:
        """节流 GET 请求,返回解析后的 JSON(或原始文本)。"""
        session = await self._get_session()
        async with self._lock:
            elapsed = time.monotonic() - self._last_request_ts
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self._last_request_ts = time.monotonic()
        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                raise SteamAPIError("请求过于频繁,已被 Steam 临时限流,请稍后再试或调大请求间隔")
            if resp.status != 200:
                raise SteamAPIError(f"Steam 接口返回 HTTP {resp.status}")
            try:
                return await resp.json()
            except aiohttp.ContentTypeError:
                return await resp.text()

    async def fetch_app_price(self, appid: int) -> GamePrice:
        """查询一个游戏的当前价格快照。"""
        data = await self._throttled_get(
            STORE_API, {"appids": appid, "cc": self.region, "l": self.language}
        )
        if not isinstance(data, dict):
            raise SteamAPIError(f"AppID {appid} 返回了非预期数据")
        entry = data.get(str(appid)) or {}
        if not entry.get("success") or "data" not in entry:
            raise AppNotFoundError(f"AppID {appid} 查询失败(可能已下架或参数错误)")
        detail = entry["data"]
        overview = detail.get("price_overview")
        if overview:
            return GamePrice(
                appid=appid,
                name=detail.get("name", str(appid)),
                header_image=detail.get("header_image", ""),
                currency=overview.get("currency", ""),
                initial_cents=overview.get("initial"),
                final_cents=overview.get("final"),
                discount_percent=overview.get("discount_percent", 0),
                initial_formatted=overview.get("initial_formatted", ""),
                final_formatted=overview.get("final_formatted", ""),
            )
        # 免费游戏/未上架售卖: 无 price_overview
        return GamePrice(
            appid=appid,
            name=detail.get("name", str(appid)),
            header_image=detail.get("header_image", ""),
            currency="",
            initial_cents=None,
            final_cents=None,
            discount_percent=0,
            initial_formatted="",
            final_formatted="免费" if detail.get("is_free") else "暂无售价",
        )

    async def fetch_wishlist(self, steamid64: str) -> dict[str, str]:
        """拉取公开愿望单,返回 {appid: 占位名}。

        官方接口仅返回 appid/priority/date_added,游戏名称由 appdetails 获取,
        这里以 appid 作为占位名,由上层在首次价格检查时回填真实名称。
        """
        data = await self._throttled_get(WISHLIST_API, {"steamid": steamid64})
        if not isinstance(data, dict):
            raise SteamAPIError("愿望单接口返回了非预期数据")
        items = data.get("response", {}).get("items", [])
        if items is None:
            items = []
        result: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            appid = str(item.get("appid", ""))
            if appid.isdigit():
                result[appid] = appid
        return result

    async def search_games(self, term: str, limit: int = 10) -> list[dict]:
        """按名称搜索游戏,返回 [{appid, name, price_display}],供 /sw search 使用。"""
        data = await self._throttled_get(
            SEARCH_API, {"term": term, "cc": self.region, "l": self.language}
        )
        if not isinstance(data, dict):
            raise SteamAPIError("搜索接口返回了非预期数据")
        results: list[dict] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict) or item.get("type") != "app":
                continue
            price = item.get("price") or {}
            final = price.get("final")
            display = ""
            if final is not None:
                symbol = CURRENCY_SYMBOLS.get(price.get("currency", ""), price.get("currency", ""))
                display = f"{symbol}{final / 100:g}"
            results.append(
                {
                    "appid": item.get("id"),
                    "name": item.get("name", ""),
                    "price_display": display,
                }
            )
            if len(results) >= limit:
                break
        return results

    async def resolve_vanity(self, vanity: str) -> str:
        """将自定义URL名解析为 64 位 SteamID(解析 Steam 社区个人主页 HTML)。"""
        session = await self._get_session()
        url = COMMUNITY_ID_URL.format(vanity=vanity)
        async with session.get(url) as resp:
            if resp.status == 404:
                raise SteamAPIError(f"找不到自定义URL「{vanity}」对应的用户")
            if resp.status != 200:
                raise SteamAPIError(f"访问 Steam 社区失败(HTTP {resp.status})")
            html = await resp.text()
        matched = STEAMID_IN_HTML_PATTERN.search(html)
        if not matched:
            raise SteamAPIError("无法从该用户主页解析出 SteamID,请改用数字ID或完整愿望单链接")
        return matched.group(1)
