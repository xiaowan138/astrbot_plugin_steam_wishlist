# astrbot_plugin_steam_wishlist

> ⚠️ **开发阶段提示**:本插件目前处于开发阶段,可能存在尚未发现的 bug。如遇问题欢迎提交 Issue,遇到价格推送异常时可通过 `/sw check` 手动验证。建议先在小范围会话中试用稳定后再大规模使用。

Steam 愿望单价格监控插件。监控单个游戏或整个 Steam 公开愿望单的价格,当折扣达到设定阈值(或触及插件观测到的历史最低价)时,自动推送提醒到绑定的会话;基础价下调(厂商降价)与大型促销同样有提醒。

## 功能特性

- **按名称搜索添加**: `/sw search <游戏名>` 搜索,按序号直接添加,无需查找 AppID
- **单游戏监控**: 支持商店链接(含国区 steamchina)/AppID/搜索序号三种添加方式
- **愿望单一键导入 + 同步**: 导入整个公开愿望单;`/sw sync` 随时同步愿望单的新增/移除,手动移除的游戏不会被同步加回
- **折扣阈值推送**: 折扣百分比达到阈值才提醒,避免小额折扣打扰
- **史低检测**: 持续记录观测期内最低价,触及史低时特别标记
- **基础价下调提醒**: 厂商非促销降价(砍基础价)也会推送,捡漏好时机
- **聚合推送防刷屏**: 单轮检查超过 3 个游戏同时达标时,合并为一条汇总消息
- **防重复推送**: 同一次降价只提醒一次,回到原价后再次降价会重新提醒
- **下架自动清理**: 游戏连续多次查询不存在时自动移除监控,不留死数据
- **多会话推送**: 支持多个群/私聊同时绑定,统一推送
- **完全免费**: 仅使用 Steam 公开接口,无需任何 API Key

## 安装

### 方式一: WebUI 安装

在 AstrBot WebUI 插件市场中搜索 `steam_wishlist` 安装。

### 方式二: 手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xiaowan138/astrbot_plugin_steam_wishlist
```

重启 AstrBot 生效。

## 快速上手

```
/sw bind                                    # 在当前会话绑定推送(群或私聊均可)
/sw search portal 2                        # 按名称搜索
/sw add 1                                   # 按搜索结果序号添加
/sw import https://steamcommunity.com/profiles/你的SteamID/wishlist   # 导入整个愿望单
```

绑定并添加监控后,插件会按配置的间隔自动检查价格并推送折扣提醒;之后愿望单有变动可随时 `/sw sync` 同步。

## 指令列表

| 指令 | 说明 |
| --- | --- |
| `/sw help` | 查看帮助 |
| `/sw bind` | 绑定当前会话接收推送 |
| `/sw unbind` | 解绑当前会话 |
| `/sw search <游戏名>` | 按名称搜索游戏(支持多词) |
| `/sw add <序号/链接/AppID>` | 添加单游戏监控(序号来自 5 分钟内的最近一次搜索) |
| `/sw import <愿望单链接或SteamID>` | 导入整个公开愿望单 |
| `/sw sync` | 同步已导入愿望单的新增/移除 |
| `/sw remove <链接或AppID>` | 移除监控(愿望单来源的游戏同步时不会再加回) |
| `/sw list` | 查看监控列表与当前价格 |
| `/sw check` | 立即全量检查并推送符合条件的折扣 |
| `/sw status` | 查看运行状态(含下次检查倒计时) |

### 如何找到我的愿望单链接?

1. 打开自己的 Steam 愿望单页面,浏览器地址形如
   `https://steamcommunity.com/profiles/76561198xxxxxxxxxx/wishlist`,直接复制即可;
2. `https://store.steampowered.com/wishlist/profiles/76561198xxxxxxxxxx/` 与
   自定义 URL(`wishlist/id/yourname/`)形式同样支持,有无末尾斜杠均可;
3. 也可以直接粘贴 17 位 SteamID 数字或自定义 URL 名。

注意:愿望单需为公开(Steam 默认公开),私密愿望单无法拉取。

## 配置项(WebUI 插件配置页)

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_auto_check` | `true` | 启用后台自动价格检查 |
| `check_interval_hours` | `6` | 自动检查间隔(小时),不建议小于 2 |
| `discount_threshold` | `30` | 折扣推送阈值(%),0 表示任何折扣都推送 |
| `region` | `cn` | Steam 商店区域代码,影响货币与价格 |
| `language` | `schinese` | 返回的游戏名称语言 |
| `request_delay_seconds` | `1.5` | 批量查询时相邻请求间隔(秒),防风控 |
| `notify_lowest_only` | `false` | 仅在史低或低于史低价时推送 |
| `push_targets` | `[]` | 额外推送目标(通常用 `/sw bind` 即可,无需手填) |

## 推送逻辑说明

- 添加游戏或导入愿望单时建立价格基线,**当前正在进行的折扣不会立即推送**(避免导入即刷屏),从下次检查开始跟踪变化;
- 之后价格下降且折扣达到阈值时推送;回到原价后,同一价格再次打折会再次提醒;
- 基础价下调(厂商降价,非促销)不受折扣阈值限制,直接提醒;
- 单轮检查推送数 ≤ 3 时逐条发送(带游戏头图);超过 3 条合并为一条纯文本汇总,大型促销(夏促/冬促)不会刷屏;
- "历史最低价"指插件运行期间观测到的最低价,非全网史低数据;
- 免费游戏(无 `price_overview`)仅跟踪名称变化,不参与折扣推送;
- 插件启动约 90 秒后先做一轮快速检查,避免 AstrBot 重启后长时间收不到推送。

## 数据存储

所有数据(监控列表、价格状态、推送绑定、愿望单来源)保存在 AstrBot 数据目录
`data/plugin_data/astrbot_plugin_steam_wishlist/data.json`,插件升级/重装不会丢失数据。

## 注意事项

- 请遵守 Steam 接口频率限制:愿望单较大(如 100+ 游戏)时一轮检查耗时约
  `游戏数 × request_delay_seconds`,属正常现象;
- 若日志出现 `429` 限流,请调大 `request_delay_seconds` 或检查间隔;
- 不同平台的主动消息能力不同,QQ 官方机器人等平台可能不支持主动推送,推荐 aiocqhttp 等适配器;
- 本插件与 Valve Corporation 无关,Steam 商标归 Valve 所有。

## 许可证

MIT License
