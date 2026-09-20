# cozyvtt-mcp

[![cozyvtt-mcp MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/yanjingzhaisun/cozyvtt-mcp/badges/card.svg)](https://glama.ai/mcp/servers/yanjingzhaisun/cozyvtt-mcp)

[English](README.md) | **中文**

[CozyVTT](https://github.com/CheekyChinchilla/CozyVTT)（自托管、开源的虚拟跑团桌）的 MCP（Model Context Protocol）桥。让 AI agent 以 **DM/KP** 身份加入战役：聊天叙事、服务器公证骰、移动 token、切地图、管先攻、结算角色卡。

为 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 打造并实测，但兼容任意 MCP 客户端（stdio 传输）。

## 升级说明

README 讲的是怎么装、怎么用当前版本；变更历史在 [CHANGELOG.md](CHANGELOG.md)。接客户端前有两处迁移点要知道：

- **0.3.0 改了三个工具名**：`campaign_status` → `campaign_get`、`initiative_state` →
  `initiative_read`、`token_hp` → `token_hp_update`。没有注册别名，请同步更新客户端里勾选的工具。
  [新旧对照](CHANGELOG.md#breaking)。
- **`chat_read` 改用 `limit`/`cursor` 分页**（`offset` 参数在 0.2.0 已移除）。

默认加载全部工具；可选预设见[工具集](#tool-sets工具集)。

## Tool sets（工具集）

**默认加载全部工具（`all`）。** 可选的注册期筛选会让未选工具**完全不出现在
`tools/list` 中**；其余工具的行为不受影响。多个预设用逗号分隔并取并集，重复名称允许，
包含 `all` 时加载全部工具。命令行参数优先于 `COZYVTT_MCP_TOOLSETS` 环境变量。
空值或未知名称会以非零状态退出，并列出有效名称和工具数量。
`--list-toolsets` 打印各预设的工具和默认项，然后成功退出，无需连接 CozyVTT。

| Preset | Tools | Selection |
|---|---:|---|
| `play` | 20 | `campaign_get`, `chat_read`, `chat_send`, `creature_search`, `dice_roll`, `events_poll`, `initiative_manage`, `initiative_read`, `map_create`, `map_delete`, `map_list`, `map_switch`, `session_list`, `session_manage`, `session_notes_update`, `token_add`, `token_delete`, `token_hp_update`, `token_move`, `token_place_creature` |
| `docs` | 9 | 全部 `document_*` 工具，加 `campaign_document_list` |
| `roster` | 7 | 全部 `character_*` 工具 |
| `macros` | 4 | 全部 `saved_roll_*` 工具 |
| `admin` | 1 | `campaign_transfer_dm` |
| `all`（默认） | 41 | 全部工具 |

```sh
.venv/bin/python server.py --toolsets play,macros
COZYVTT_MCP_TOOLSETS=docs,roster .venv/bin/python server.py
.venv/bin/python server.py --list-toolsets
```

MCP 客户端可在服务的 `args` 中追加 `"--toolsets", "play"`，或在 `env` 中设置
`COZYVTT_MCP_TOOLSETS`。

以下数据由 `.venv/bin/python scripts/measure_tool_budget.py` 离线 stdio 实测。
字节数为各工具定义的 UTF-8 JSON 大小之和，不含 JSON-RPC 外层和列表分隔符；
token 数按 `bytes // 4` 估算，并非 tokenizer 实测。

| Preset | JSON bytes | Estimated tokens | Savings vs all |
|---|---:|---:|---:|
| `play` | 30,925 | 7,731 | 48% |
| `docs` | 13,418 | 3,354 | 77% |
| `roster` | 9,152 | 2,288 | 85% |
| `macros` | 4,664 | 1,166 | 92% |
| `admin` | 1,199 | 299 | 98% |
| `all` | 59,358 | 14,839 | 0% |

工具面里还包含 `map_create`、`map_delete`、`token_delete` 和 `character_delete`。
地图创建使用已有图片资源；删除当前地图前须先用 `map_switch` 切换。
`token_delete` 接受地图 token ID；`token_hp_update` 接受角色 ID 并调整角色卡 HP。
`character_delete` 永久删除自己拥有的角色卡。需要角色或文档工具时可追加
`roster` 或 `docs`。

## 兼容性

| cozyvtt-mcp | CozyVTT | 说明 |
|---|---|---|
| **0.4.0** | **v1.2.2 / v1.4.0** | 双基线。工具集预设，加 `map_create`/`map_delete`/`token_delete`/`character_delete`。离线契约 228/228；`Dockerfile` 已在真 `python:3.12-slim` rootfs 内逐层校验并重放，空环境下可启动并列出 41 个工具。本版未做真实战役验证，也未构建 OCI 镜像（构建宿主没有 Docker daemon）。 |
| **0.3.0** | **v1.2.2 / v1.4.0** | 双基线。参数描述、MCP annotations、容器镜像。离线 176/176；镜像已构建并空环境启动（37 工具）。未做真实战役验证。 |
| **0.2.0** | **v1.2.2 / v1.4.0** | 双基线：保留原有工具；新 REST 路由在旧实例上明确降级报错。离线 176/176；v1.4.0 真实实例冒烟（读写＋Documents/Saved Rolls 往返）2026-09-17 通过。 |
| 0.1.1 | v1.2.2 | 上一版，20 个工具 |

兼容表描述的是已支持的契约，不是推断的服务器版本。新功能在确认可用前一律报告 `unknown`；空列表或业务 404 不能作为路由不存在的证据。完整 41 工具契约见 [SPEC v2](SPEC.md)。

## 功能

41 个工具，工具体结果统一返回 `{ok, data?, error?}`；参数校验由 FastMCP 处理：

- **场次/战役**：`campaign_get`（role 与 owner 分开报告；新能力键可为 `unknown`）、`session_manage`、`session_list`、`session_notes_update`、`campaign_transfer_dm`（owner 收回也走它）、`map_list`、`map_switch`
- **叙事**：`chat_send`（DM / PLAYER）、`chat_read`
- **骰**：`dice_roll`（服务器公证出骰；`is_secret=true` 对应线上字段 `secret`，在 v1.4.0 上只投递给掷骰者与 DM）、`events_poll`（近期缓冲事件，不是持久历史；DICE_ROLL 事件不出现在聊天历史里）
- **Token/地图**：`token_add`（整数尺寸 1..10）、`token_move`、`token_hp_update`、`token_place_creature`、`token_delete`、`map_create`、`map_delete`、`creature_search`（SRD＋自定义怪库）
- **战斗**：`initiative_manage`（add / remove / **roll** / **set** / **reorder** / start / next / end）、`initiative_read`（注意：CoC7e 先攻按 DEX 排序不骰——这是上游规则行为，`roll` 已相应门控）
- **角色**：`character_list`、`character_get`、`character_create`、`character_delete`、`character_validate`、`character_update`（规则数值由 agent 计算，桥只负责写值）
- **文档**：`document_upload`、`document_create`、`document_list`、`campaign_document_list`、`document_read`、`document_update`、`document_share`、`document_unshare`、`document_delete`
- **Saved Rolls**：`saved_roll_list`、`saved_roll_create`、`saved_roll_update`、`saved_roll_delete`（按用户×战役私有；每用户每战役 50 条，表达式服务器校验）。`saved_roll_list` 返回完整宏，没有 `saved_roll_get`。
- **Hit Dice**：`character_hitdice_spend`（仅 DND_5E；只扣一次，不骰骰子不加血）

## 系统门控

桥保持规则系统无关，但少数能力只在特定系统下有意义，按战役 `gameSystem` 枚举门控（懒取并缓存；枚举：`DND_5E` / `PATHFINDER_2E` / `SHADOWRUN_6E` / `CALL_OF_CTHULHU_7E`）：

| 能力 | 放行系统 | 原因 |
|---|---|---|
| `creature_search source=srd` | `DND_5E` | SRD 怪库由 Open5e 播种——那是 5e 数据源 |
| `initiative_manage action=roll` | `DND_5E`、`PATHFINDER_2E`、`SHADOWRUN_6E` | 服务器按系统推导先攻骰式；CoC7e 根本不骰（DEX 排序） |
| `character_hitdice_spend` | `DND_5E` | 仅本地系统门控。WS 无可靠能力探测手段；发送后永远是 pending 口径。 |

被门控的调用返回明确的 `{ok: false, error}` 说明放行系统，而不是发出一个服务器会忽略或误解的事件。未设 `gameSystem` 的（flexible）战役 fail-closed。未指定 `source` 时，非 5e 战役只搜 `custom`；5e 战役可搜两种来源。`campaign_get().features` 报告当前战役可用的门控能力。

## 架构

```
MCP client (stdio)
  └─ server.py (FastMCP, lazy init, synchronous first-call self-check)
      ├─ auth.py        — rememberMe login, 10-min keepalive, 3-min re-login spacing, 429 backoff
      ├─ client.py      — REST wrapper: one 401→re-login→retry, 429 exponential backoff (1/2/4s, ≤3)
      ├─ ws_listener.py — socket.io listener, 500-event ring buffer, one reconnect worker
      └─ tools/         — 41 MCP tools (read/write, documents, campaign additions)
```

设计要点：

- **Dice discipline**: rolls are generated and persisted by the server. Public results go to the table; reviewed v1.4.0 sends secret results to the roller and DMs. The bridge provides recent buffered events, not a durable roll-history query.
- **规则在桥外**：技能检定、SAN 损失、伤害——由 agent/GM 计算，桥只做公证骰与写值。桥对规则系统无关。
- `token_move` 走 REST PUT；服务器检查 DM/controlledBy 权限，桥额外拒绝旁观者。REST 落库不代表有 `map.changed` 广播。`map_switch` 先 REST 落库再显式发 WS `map.change`；WS 失败不影响已成功的 REST 结果，也绝不重放。

## 结果与更新契约

- `dice_roll`、`chat_send`、`token_hp_update`、`initiative_manage`、`character_hitdice_spend` 返回 `sent: true`、`confirmed: false`、`status: "pending"`。业务广播与 `system.error` 用 `events_poll` 读；不要盲目重放写操作。两条基线都没有关联 ACK。骰子可带 `purpose` 与 `character_name`（线上 `characterName`）用于 Custom Roll 展示。Hit Dice 的花费、骰骰、回血是三个独立操作，不是事务；旧服务器可能静默忽略花费事件。
- `events_poll` 最早未读优先。保存 `next_seq` 作为下次 `since`；`latest_seq` 是其兼容别名。`high_water_seq` 是缓冲高水位，不是分页游标。注意检查 `gap`、`cursor_reset`、`has_more`、`connected`、`authenticated`。
- `character_update(character_id, data={"data": {"hp": {"current": 5}}})` 先递归合并卡面字段再 PUT。未指定的字段保留；数组/标量整体替换，`null` 为显式置空。顶层字段为 `name`、`data`、`tokenImageUrl`。本桥进程内串行化更新；浏览器并发保存仍需上游乐观锁。
- `character_create` 先建卡、查 roster、确认未入列才 assign。assign 失败时错误信息带已建角色 ID：请在 UI 里 assign 该卡，别再建一张。CoC 的 conditions/Mythos/spells/appearance/notes 与 DND 的新旧生命骰字段在合并中都能存活；Keeper notes 对战役成员不保密。
- `session_manage` 走 REST。pause/end 解析 `campaign.activeSession.id`；start 新建场次。end 接受 `notes`（≤2000 字，全战役可读）与 `save_state=true`。空 notes 不会清掉旧摘要；要清空用 `session_notes_update(session_id, notes="")`。`session_list` 的最近 50 条里可能含进行中场次。
- Documents 的 scope 为 `USER`（个人）、`CAMPAIGN`、`GLOBAL`。`document_list` 过滤资产库；`campaign_document_list` 能发现被分享的私人文档。直接创建/编辑 txt/md 限 900 KiB UTF-8；文件上传走实例限额（默认 50 MiB）。PDF 内容不可编辑。unshare 只删一条 link，不能收回原生/global 来源的权限；`shared:false` 表示战役原生文档。删除会移除资产及其全部 links。
- WS 重连使用最新的、按 URL 过滤的 Cookie，并在战役认证后主动请求当前先攻状态。远程部署请用 HTTPS。
- `chat_read` 用 `limit` + `cursor` 分页：先读最新一页，再把返回的 `pagination.nextCursor` 原样作 `cursor` 传入；`nextCursor` 为 `null` 即到底。无游标元数据的旧实例只服务最新一页，并如实返回「此实例不支持可靠的历史游标分页；仅可读取最新一页。」而不会重复同一页。
- 文档原文读取保留 MIME 与 ETag。文本返回 `{mime_type,etag,content}`；PDF 写入项目 `downloads/<document_id>.pdf` 并返回 `{mime_type,etag,file_path,file_size}`。传 `etag` 走 `If-None-Match`；304 返回 `{not_modified:true}` 供调用方复用已有内容。downloads 已 gitignore，上游删除不会清掉本地副本。
- REST `401` 会使现有 WS 认证与战役缓存失效；失去战役成员资格会取消 WS 认证（而不是反复重连）。`campaign_transfer_dm` 会清角色/系统缓存；`character.updated`、`campaign.dm.transferred`、`roster.updated`、`dice.historyCleared` 会被缓冲供 `events_poll` 读取。
- `character_validate` 始终附 `validation_reliable:false`：上游 v1.4.0 会丢弃校验失败并误报 `isValid:true`；旧服务器的可靠性未知。
- `initiative_read(refresh=true)` 主动拉取最新状态，超时报告未知；REST 路径下 token 坐标是确定的。
- 路由缺失型 404（上游原文恰好是 `The requested resource does not exist`）返回「当前 CozyVTT 实例未提供此功能；请升级到支持该功能的版本后重试。」；其他 404 返回「资源不存在或当前账号无权访问（HTTP 404）：<上游 message>」。错误 `data` 保留状态码与上游细节。向旧上传路由传 DOCUMENT 类型可能 400——原样返回该错误，不会换类型/scope 重试。

## 环境要求

- Python ≥ 3.11
- 一个 CozyVTT v1.2.2 或 v1.4.0 实例，以及你的账号在目标战役中拥有各工具所需权限
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

## 安装

```bash
git clone https://github.com/yanjingzhaisun/cozyvtt-mcp.git
cd cozyvtt-mcp
uv sync   # 或: python -m venv .venv && .venv/bin/pip install fastmcp requests "python-socketio[client]" websocket-client
```

### Docker (stdio)

```bash
docker build -t cozyvtt-mcp:0.4.0 .
docker run --rm -i --env-file /path/to/cozyvtt.env cozyvtt-mcp:0.4.0
```

Use `-i` to keep stdin open; MCP uses stdin/stdout, without a network port or TTY.
The image installs only compatible, hash-checked runtime wheels from `uv.lock`,
including fastmcp, requests, python-socketio[client], and websocket-client. No editable
package install or dependency re-resolution is performed. Credentials are supplied
at runtime. Without any COZYVTT variables, `initialize` and `tools/list` still work;
only business tool calls initialize authentication. Mount upload files inside the
container and pass those container paths to `document_upload`. Mount `/app/downloads`
if binary downloads must survive container removal. No Docker build was run locally.

## 配置

环境变量（仓库不含任何密钥）：

| 变量 | 示例 | 说明 |
|---|---|---|
| `COZYVTT_URL` | `http://localhost:8899` | 实例地址 |
| `COZYVTT_EMAIL` | `dm@example.local` | DM 账号 |
| `COZYVTT_PASSWORD` | — | DM 密码 |
| `COZYVTT_CAMPAIGN_ID` | `uuid` | 目标战役 |

### Hermes Agent（`config.yaml`）

```yaml
mcp_servers:
  cozyvtt:
    command: /path/to/cozyvtt-mcp/.venv/bin/python
    args: [/path/to/cozyvtt-mcp/server.py]
    env:
      COZYVTT_URL: "http://localhost:8899"
      COZYVTT_EMAIL: "dm@example.local"
      COZYVTT_PASSWORD: "<secret>"
      COZYVTT_CAMPAIGN_ID: "<campaign-uuid>"
```

注册后需重启 Hermes（MCP server 不热加载）。

### 通用 MCP 客户端

任意支持 stdio 的客户端：command 填 venv 的 python，args 填 `server.py`，env 同上。

## 测试

```bash
.venv/bin/python -m pytest
```

对真实实例的只读冒烟：

```bash
COZYVTT_SMOKE=1 COZYVTT_URL=... COZYVTT_EMAIL=... COZYVTT_PASSWORD=... \
  COZYVTT_CAMPAIGN_ID=... .venv/bin/python scripts/smoke.py
```

（`scripts/smoke_write.py` 会写聊天、一颗公骰和一颗暗骰——只在一次性测试战役里手动跑。它校验发送者与唯一 purpose；要证明玩家收不到暗骰，还需一个独立的玩家连接。）

离线测试屏蔽 TCP，包含真实 FastMCP 内存与 stdio 检查，不需要战役凭证。对 v1.4.0 真实实例的集成验证已于 2026-09-17 完成（读写冒烟＋Documents、Saved Rolls 往返）。本地验证 venv 为 Python 3.12.13；Python 3.13 尚未验证。

## 排障

- **日志**：`logs/cozyvtt-mcp.log`（auth 事件、WS 状态、工具调用；绝不含密码）
- **Repeated 401**: reviewed v1.4.0 limits failed credential attempts to 5 per 15 minutes/IP. Successful credential requests do not count there; older accounting is unverified. The bridge enforces a re-login interval of at least 3 minutes, including failed attempts; Retry-After may extend it.
- **`events_poll` 为空**：可能只是没有新事件。检查 `connected`、`authenticated`、`last_error`、`connection_error`；单一 WS 工作线程会重连断开的连接。初始化失败有 180 秒冷却，过后可不重启重试
- **CoC7e 先攻不骰骰**：上游行为——CoC7e 先攻按 DEX 排序，本来就不产生骰子

## 许可证

MIT（见 [LICENSE](LICENSE)）。CozyVTT 本体为 AGPLv3——本项目是独立的 API 客户端，不含 CozyVTT 代码。

## 链接

- CozyVTT 上游：https://github.com/CheekyChinchilla/CozyVTT
- AI 集成讨论：https://github.com/CheekyChinchilla/CozyVTT/issues/32
- 版本历史与 breaking 对照：[CHANGELOG.md](CHANGELOG.md)
- Releases：https://github.com/yanjingzhaisun/cozyvtt-mcp/releases
- 工具定义准确度审计（措辞修正与边界）：[ForAI/TDQS_Quality_Report.md](ForAI/TDQS_Quality_Report.md)

## 生态

- [dnd5e-rules](https://github.com/yanjingzhaisun/dnd5e-rules)——确定性 D&D 5e 规则结算（纯函数，SRD 5.1 数据 CC-BY-4.0）。与本桥配套的规则层：服务器出骰面，桥负责传输，这个库算数，agent 负责叙事。
