"""插件数据持久化。

所有数据存放在 AstrBot 数据目录下(而非插件自身目录),保证插件升级/重装时数据不丢失。
采用单文件 JSON + 原子写入(先写临时文件再替换),避免进程中断导致数据损坏。

数据结构:
- games:            {appid(str): {"name": str, "added_at": int, "source": "manual"|"steam:<steamid>"}}
- price_state:      {appid(str): {...价格快照与推送状态...}}
- bindings:         [unified_msg_origin, ...]
- wishlist_sources: [steamid64, ...]  通过 /sw import 记录,供 /sw sync 使用
- dismissed:        [appid(str), ...]  用户手动移除的来自愿望单的游戏,sync 时不再自动加回
"""

import json
import os
import tempfile
import time
from pathlib import Path

MANUAL_SOURCE = "manual"


def steam_source(steamid: str) -> str:
    return f"steam:{steamid}"


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.file_path = data_dir / "data.json"
        self.games: dict[str, dict] = {}
        self.price_state: dict[str, dict] = {}
        self.bindings: list[str] = []
        self.wishlist_sources: list[str] = []
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
            self.wishlist_sources = data.get("wishlist_sources", [])
            self.dismissed = data.get("dismissed", [])
        except (json.JSONDecodeError, OSError):
            # 数据文件损坏时保留空态,不中断插件加载
            self.games, self.price_state, self.bindings = {}, {}, []
            self.wishlist_sources, self.dismissed = [], []

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

    def add_wishlist_source(self, steamid: str) -> bool:
        if steamid in self.wishlist_sources:
            return False
        self.wishlist_sources.append(steamid)
        return True

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
