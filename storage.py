"""插件数据持久化。

所有数据存放在 AstrBot 数据目录下(而非插件自身目录),保证插件升级/重装时数据不丢失。
采用单文件 JSON + 原子写入(先写临时文件再替换),避免进程中断导致数据损坏。
"""

import json
import os
import tempfile
import time
from pathlib import Path


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.file_path = data_dir / "data.json"
        # games: {appid(str): {"name": str, "added_at": int}}
        # price_state: {appid(str): {...价格快照与推送状态...}}
        # bindings: [unified_msg_origin, ...]
        self.games: dict[str, dict] = {}
        self.price_state: dict[str, dict] = {}
        self.bindings: list[str] = []
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
        except (json.JSONDecodeError, OSError):
            # 数据文件损坏时保留空态,不中断插件加载
            self.games, self.price_state, self.bindings = {}, {}, []

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "games": self.games,
            "price_state": self.price_state,
            "bindings": self.bindings,
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

    def add_game(self, appid: int, name: str) -> bool:
        """添加监控游戏。已存在时更新名称并返回 False。"""
        key = str(appid)
        if key in self.games:
            self.games[key]["name"] = name
            return False
        self.games[key] = {"name": name, "added_at": int(time.time())}
        return True

    def remove_game(self, appid: int) -> str | None:
        """移除监控游戏,返回游戏名;不存在时返回 None。"""
        key = str(appid)
        game = self.games.pop(key, None)
        self.price_state.pop(key, None)
        return game["name"] if game else None

    def get_state(self, appid: int) -> dict:
        return self.price_state.get(str(appid), {})

    def set_state(self, appid: int, state: dict):
        self.price_state[str(appid)] = state

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
