"""插件数据持久化。

所有数据存放在 AstrBot 数据目录下(而非插件自身目录),保证插件升级/重装时数据不丢失。
采用单文件 JSON + 原子写入(先写临时文件再替换),避免进程中断导致数据损坏。

数据结构:
- games:            {appid(str): {"name": str, "added_at": int, "source": "manual"|"steam:<steamid>"}}
- price_state:      {appid(str): {...价格快照、推送状态、价格历史...}}
- bindings:         [unified_msg_origin, ...]
- wishlist_sources: {steamid64: {"label": str, "added_at": int}}  通过 /sw import 记录,供 /sw sync 使用
- dismissed:        [appid(str), ...]  用户手动移除的来自愿望单的游戏,sync 时不再自动加回
"""

import json
import os
import tempfile
import time
from pathlib import Path

MANUAL_SOURCE = "manual"
# 每个游戏最多保留的价格历史点数(用于 /sw history 走势)
PRICE_HISTORY_LIMIT = 60


def steam_source(steamid: str) -> str:
    return f"steam:{steamid}"


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.file_path = data_dir / "data.json"
        self.games: dict[str, dict] = {}
        self.price_state: dict[str, dict] = {}
        self.bindings: list[str] = []
        self.wishlist_sources: dict[str, dict] = {}
        self.dismissed: list[str] = []
        self.load()

    def load(self):
        if not self.file_path.exists():
            return
        try:
            with open(self.file_path, encoding="utf-8") as f:
                data = json.load(f)
            self.games = data.get("games", {})
            self.price_state = data.get("price_state", {})
            self.bindings = data.get("bindings", [])
            self.dismissed = data.get("dismissed", [])
            self.wishlist_sources = self._normalize_sources(data.get("wishlist_sources", []))
        except (json.JSONDecodeError, OSError):
            # 数据文件损坏时保留空态,不中断插件加载
            self.games, self.price_state, self.bindings = {}, {}, []
            self.wishlist_sources, self.dismissed = {}, []

    @staticmethod
    def _normalize_sources(raw) -> dict[str, dict]:
        """兼容 v0.2.0 及更早的 list 格式,统一升级为 {steamid: {label, added_at}}。"""
        if isinstance(raw, dict):
            return raw
        normalized: dict[str, dict] = {}
        for item in raw or []:
            if isinstance(item, str) and item:
                normalized[item] = {"label": "", "added_at": int(time.time())}
        return normalized

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "games": self.games,
            "price_state": self.price_state,
            "bindings": self.bindings,
            "wishlist_sources": self.wishlist_sources,
            "dismissed": self.dismissed,
        }
        fd, tmp_path = tempfile.mkstemp(dir=str(self.data_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except OSError:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # ---- 监控列表 ----

    def add_game(self, appid: int, name: str, source: str = MANUAL_SOURCE) -> bool:
        """添加监控游戏。已存在时更新名称并返回 False。"""
        key = str(appid)
        if key in self.games:
            self.games[key]["name"] = name
            return False
        self.games[key] = {
            "name": name,
            "added_at": int(time.time()),
            "source": source,
        }
        return True

    def remove_game(self, appid: int) -> dict | None:
        """移除监控游戏,返回原游戏记录;不存在时返回 None。"""
        key = str(appid)
        game = self.games.pop(key, None)
        self.price_state.pop(key, None)
        return game

    def get_state(self, appid: int) -> dict:
        return self.price_state.get(str(appid), {})

    def set_state(self, appid: int, state: dict):
        self.price_state[str(appid)] = state

    # ---- 愿望单来源与移除记录 ----

    def add_wishlist_source(self, steamid: str, label: str = "") -> bool:
        if steamid in self.wishlist_sources:
            if label and not self.wishlist_sources[steamid].get("label"):
                self.wishlist_sources[steamid]["label"] = label
            return False
        self.wishlist_sources[steamid] = {"label": label, "added_at": int(time.time())}
        return True

    def remove_wishlist_source(self, steamid: str) -> bool:
        return self.wishlist_sources.pop(steamid, None) is not None

    def append_history(self, appid: int, final_cents: int, discount_percent: int, ts: int):
        """记录一个价格观测点,供 /sw history 展示走势。

        与上一次完全相同的价格不重复记录,避免长时间运行后历史被同值填满。
        金额只存分值,展示时按区域货币统一渲染。
        """
        state = self.price_state.setdefault(str(appid), {})
        history = state.setdefault("history", [])
        if history and history[-1].get("p") == final_cents:
            return
        history.append({"t": ts, "p": final_cents, "d": discount_percent})
        if len(history) > PRICE_HISTORY_LIMIT:
            del history[: len(history) - PRICE_HISTORY_LIMIT]

    def dismiss(self, appid: int):
        """记录用户主动移除的愿望单游戏,/sw sync 时不再自动加回。"""
        key = str(appid)
        if key not in self.dismissed:
            self.dismissed.append(key)

    def undismiss(self, appid: int):
        """用户重新手动添加时,解除移除记录。"""
        key = str(appid)
        if key in self.dismissed:
            self.dismissed.remove(key)

    # ---- 推送绑定 ----

    def add_binding(self, umo: str) -> bool:
        if umo in self.bindings:
            return False
        self.bindings.append(umo)
        return True

    def remove_binding(self, umo: str) -> bool:
        if umo in self.bindings:
            self.bindings.remove(umo)
            return True
        return False
