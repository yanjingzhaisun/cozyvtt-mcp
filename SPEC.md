# cozyvtt-mcp — SPEC v2（v0.2.0，2026-09-17）

CozyVTT 的 Python FastMCP stdio 桥，固定一个战役，让 AI DM/KP 通过 API 读写游戏状态。双基线：CozyVTT **v1.2.2 / v1.4.0**。本规格描述桥的行为；上游 v1.4.0 文档与源码矛盾时以源码为准。适配依据为 R1 报告《cozyvtt-v1.4.0-adaptation.md》§3—§8、§11，以及 R2 HUMAN DECISIONS。

## 0. 项目约定与兼容范围

- 版本：0.2.0；工具数：20 → **37**（新增 17）；不提供冗余 `saved_roll_get`。
- Python ≥3.11，FastMCP stdio，requests + python-socketio；uv 管理依赖。
- 单一 `COZYVTT_CAMPAIGN_ID`；所有战役专用工具固定此战役。Documents 的资产 scope 可显式带 campaign_id；CAMPAIGN 缺省时用当前战役。
- 只负责 API 协议与状态传递；CoC 成功等级、SAN 损失、治疗量等规则由调用方计算。角色自身 gameSystem 决定卡面 schema，不用战役系统重写角色数据。
- 上游权限为最终裁决：DM、PLAYER、SPECTATOR 与平台 ADMIN/owner 是不同概念；ownerId 不能证明 DM 权限。

| CozyVTT 基线 | 支持范围 | 验证边界 |
|---|---|---|
| v1.2.2 | 保留原有 20 工具能力；聊天只读最新页，除非实际响应提供 nextCursor；新 REST 功能按精确错误降级 | 旧快照与既有离线回归 |
| v1.4.0 | 37 工具；Documents / Saved Rolls / DM 移交 / Hit Dice / Session 历史；游标分页 | 源码契约 + 离线 HTTP/WS mock；本次未连接实例 |

不能从任意 404、空列表、WS 超时推断版本。`campaign_status.features` 的新 API 能力默认 `unknown`；Hit Dice 在非 DND_5E 下为 false，在 DND_5E 下仍为 unknown（系统允许不等于服务端事件存在）。

## 1. 认证与 REST 客户端

- `POST /api/auth/login`，JSON email/password/rememberMe=true；会话 Cookie 存 requests.Session，Session 请求与登录线程安全串行化。
- 懒初始化；登录后启动 10 分钟一次 `GET /api/auth/ping` 保活。初始化失败可在 180 秒冷却后重试。
- 每个 REST 请求收到 401 最多重新登录一次并重试；跨调用登录冷却至少 3 分钟，包括失败，Retry-After 可延长。不得循环重登。
- 429 指数退避 1/2/4 秒，最多三次；网络失败与其他错误不自动重放写操作。
- REST 401 使 Ctx 角色/系统缓存失效，并取消已有 WS 的认证状态、触发重连；重连取得新 Cookie。上游 session 吊销不保证踢下已有 WS，也没有专用吊销 401 code。
- `ApiError` 保留 status/message/上游 JSON（含 validationErrors、PASSWORD_CHANGE_REQUIRED 等 code）；错误结果可附 `data:{status,upstream}`。
- JSON GET/POST/PUT/PATCH/DELETE；multipart POST 使用 requests `data=` + `files=` 自动生成 boundary。每次 401/429 重试将文件流恢复到初始位置，避免重发空文件。
- `get_raw` 根据 Content-Type 返回 text 或 bytes 并保留 ETag；PDF 不走 JSON 或 `_raw`。text 无 charset 时按 UTF-8，304 返回 `{not_modified:true}`。
- 凭证环境变量仍为 `COZYVTT_URL / COZYVTT_EMAIL / COZYVTT_PASSWORD / COZYVTT_CAMPAIGN_ID`；未新增配置或修改 MCP 注册。

## 2. WebSocket 与缓存

- Socket.IO 握手带 URL 过滤后的 session Cookie；首次 WS 工具调用懒连接，等待 namespace connect 与服务端 connected，再发 `authenticate {campaignId}`，等对应 campaignId 的 authenticated。
- 单个后台管理线程重连；旧连接回调以 generation 隔离。认证后发送 `initiative.request_state {}`。
- 内存环形缓冲容量 500；记录 `{seq,ts,event,payload,generation}`；原始 payload 不裁剪（character.updated 不保证有 character）。
- 监听：chat.message、dice.rolled、dice.rolled.secret、map.changed、token.moved、initiative.state、session.started/paused/ended/resumed、character.hp.updated，以及新增的 **character.updated、campaign.dm.transferred、roster.updated、dice.historyCleared**。
- 服务端 error → `system.error`，保留原始 detail。Unauthorized、no longer a member、not a member 取消 authenticated、清缓存；普通掷骰等业务错误不取消认证。
- `campaign.dm.transferred` 清除 Ctx 角色与系统缓存；更新监听器已知的本人 DM/PLAYER 角色。REST 移交成功也主动清缓存，避免依赖 best-effort 广播。
- `Ctx.get_system()` 懒缓存；`read_campaign()` 更新角色与系统；token_move 写前重新读取角色。缓存读取不持锁执行网络 I/O，避免与 WS 回调互锁。
- WS 写操作只能返回 `{sent:true,confirmed:false,status:'pending',since,...}`。不能将邻近广播当作此次操作的关联 ACK；错误与结果去 events_poll 读取，不自动重放。
- events_poll 返回 events/next_seq/latest_seq/high_water_seq/oldest_seq/has_more/gap/cursor_reset/connected/authenticated/last_error；增加 role/stale/campaign_cache_stale。next_seq 才是下次 since；高水位不是读取游标。断线时仍能读取缓冲错误。

## 3. 限流与并发

- REST 默认全局 300/min/IP；仅处理 429，不主动耗额度探测。
- 骰子 30/min/user：dice_roll 最小间隔 2.1 秒，串行排队。
- Documents 上传/直接创建另共享 30/min/user；文件上传限额由实例决定，默认 50 MiB。桥不把默认值当成不可修改的硬上限。
- Saved Rolls 每用户每战役 50 条；创建在本桥内串行化，表达式合法性和额度由服务器判断。跨客户端仍可能竞态，桥不承诺数据库硬约束。
- character_update 读—合并—写在本进程内串行；上游无 If-Match / 原子 patch，与浏览器并发保存仍可能覆盖。

## 4. 工具清单（共 37 个）

函数体统一返回 `{ok:bool,data?:any,error?:string}`；参数 schema 校验错误由 FastMCP 处理。下表路径 `{id}` 表示当前 campaign_id。

| 工具 | 通道 | 端点/事件 | 参数与结果摘要 |
|---|---|---|---|
| `campaign_status` | REST | GET /api/campaigns/{id} + /health（备用 /api/auth/ping） | 无参数；health/campaign/current_map/me/features/feature_evidence/role/owner |
| `chat_send` | WS | chat.message | content 非空≤2000，type=DM/PLAYER；pending |
| `chat_read` | REST | GET /api/campaigns/{id}/messages | limit:int=20[1..100]、cursor:str/null；messages+pagination 原样 |
| `events_poll` | 缓冲 | 上述订阅事件 | since:int=0≥0、limit:int=100[1..500]；分页/角色/失效状态 |
| `dice_roll` | WS | dice.roll | expression、is_secret=false、purpose=''、character_name=null → secret/purpose/characterName；pending |
| `map_list` | REST | GET /api/campaigns/{id}/maps | 无参数；地图列表 |
| `map_switch` | REST+WS | PUT /api/campaigns/{id}/maps/{mapId}/set-current → map.change | map_id；分别报告 persisted 与 broadcast_pending |
| `token_add` | REST | POST /api/campaigns/{id}/maps/{mapId}/tokens | map_id/name/image_url/x/y、character_id=''、width/height:int1..10=1、layer='token'、visible=true、controlled_by=null |
| `token_move` | REST | PUT /api/campaigns/{id}/maps/{mapId}/tokens/{tokenId} | map_id/token_id/x/y → position；拒绝 SPECTATOR/未知角色；落库且广播未确认 |
| `token_hp` | WS | character.hp.update | character_id/delta；pending |
| `initiative_state` | WS读+缓冲 | initiative.request_state → initiative.state | refresh:bool=true；只把新事件作为刷新结果，超时 state=null/stale=true |
| `initiative_manage` | WS | initiative.add/remove/roll/set/reorder/start/next/end | action、token_id/map_id/value/ordered_token_ids/expression/character_name；roll 有系统门 |
| `character_list` | REST | GET /api/campaigns/{id}/characters | roster 原样 |
| `character_get` | REST | GET /api/characters/{characterId} | character_id；完整卡面，不裁剪未知字段 |
| `character_update` | REST | GET + PUT /api/characters/{characterId} | character_id、data 顶层补丁；卡面 data 递归合并、数组整体替换 |
| `character_create` | REST | POST /api/characters；GET roster；必要时 POST /api/characters/{characterId}/assign | name 1..200、data对象/null、token_image_url=''；不重复创建 |
| `character_validate` | REST | GET /api/characters/{characterId}/validate | character_id；上游结果 + validation_reliable:false/validation_note |
| `creature_search` | REST | GET /api/campaigns/{id}/creatures | search/source/cr/limit/offset；srd 仅 DND_5E |
| `token_place_creature` | REST | GET /api/campaigns/{id}/creatures/{creatureId} → POST /api/campaigns/{id}/maps/{mapId}/tokens | creature_id/map_id/x/y；使用模板名/图 |
| `session_manage` | REST | POST /api/campaigns/{id}/sessions；PUT /api/campaigns/{id}/sessions/{sessionId}/pause 或 /end | action=start/pause/end、notes:null/string≤2000、save_state=true；后两项仅 end |
| `document_upload` | REST multipart | POST /api/assets/upload | file_path、scope=USER、campaign_id/name/description/tags可选；type固定DOCUMENT |
| `document_create` | REST | POST /api/assets/documents | name、format:txt/md、content、scope=USER、campaign_id/description可选 |
| `document_list` | REST | GET /api/assets?type=DOCUMENT | scope/campaign_id/search可选，page=1≥1，limit=50[1..100] |
| `campaign_document_list` | REST | GET /api/campaigns/{id}/documents | 无参数；共享 link 与原生 CAMPAIGN 文档 |
| `document_read` | REST raw+本地文件 | GET /api/assets/documents/{documentId} | document_id、etag可选；文本或 downloads/ 文件路径，304 不带内容 |
| `document_update` | REST | PUT /api/assets/documents/{documentId}/content | document_id/content；整体替换文本，非PDF |
| `document_share` | REST | POST /api/campaigns/{id}/documents | document_id → assetId；仅 DM，可分享资产权限由服务器核验 |
| `document_unshare` | REST | DELETE /api/campaigns/{id}/documents/{documentId} | document_id；仅撤销该 link |
| `document_delete` | REST | DELETE /api/assets/{documentId} | document_id；删除资产和所有分享 links |
| `saved_roll_list` | REST | GET /api/campaigns/{id}/macros | 无参数；本人宏全字段 |
| `saved_roll_create` | REST | POST /api/campaigns/{id}/macros | name:trim1..60、expression:trim1..200 |
| `saved_roll_update` | REST | PUT /api/campaigns/{id}/macros/{macroId} | macro_id、name/expression至少一个 |
| `saved_roll_delete` | REST | DELETE /api/campaigns/{id}/macros/{macroId} | macro_id；仅本人本战役 |
| `campaign_transfer_dm` | REST+缓冲 | PUT /api/campaigns/{id}/dm；监听 campaign.dm.transferred | user_id → userId；DM/owner/admin 可调，目标必须已是成员 |
| `character_hitdice_spend` | WS | character.hitdice.spend | character_id/index:int≥0；仅 DND_5E，pending，不掷骰/治疗 |
| `session_list` | REST | GET /api/campaigns/{id}/sessions | 无参数；最多50场、降序，含活动场次 |
| `session_notes_update` | REST | PUT /api/campaigns/{id}/sessions/{sessionId}/notes | session_id/notes≤2000；DM，空串清空 |

### 4.1 系统门控与能力状态

- `creature_search source=srd`：DND_5E。未指定 source 时，非5e/flexible 强制 custom。
- `initiative_manage action=roll`：DND_5E / PATHFINDER_2E / SHADOWRUN_6E。CoC7e 为 DEX 排序，使用 add/set/start。
- `character_hitdice_spend`：仅 `require_system(DND_5E)`，flexible 不通过；无可靠 WS 能力探测，不另加版本拒绝，不伪造扣数成功。
- 其他工具不按规则系统裁剪；服务器校验成员、角色与资源权限。
- `campaign_status.features` 保留 srd_creature_library/homebrew_creature_library/initiative_roll，新增 documents/saved_rolls/dm_transfer/hitdice_spend。前三项新 API 为 unknown；Hit Dice 结合系统门报告 false/unknown；不能凭 unknown 宣称支持。
- `campaign_status.role` 从 userRole 或本人 membership 获取；owner 为 `{id,is_me}`，缺证据为 null。绝不把 owner 视为 DM。

### 4.2 chat_read 破坏性迁移

旧调用 `chat_read(limit=20,offset=20)` 不再接受。先 `chat_read(limit=20)`；若响应有 `pagination.nextCursor` 且非 null，下次 `chat_read(limit=20,cursor=<原值>)`。messages/pagination 均原样透传，不构造 offset、before、total 或自制游标；骰子不占消息页。

旧实例没有 nextCursor 时只允许最新页。后续传 cursor 返回“此实例不支持可靠的历史游标分页；仅可读取最新一页。”。初次就传 cursor 时必须检查实际响应，缺 nextCursor 也报错，绝不把重复的最新页当成历史页。未知游标/limit 错误不回退重试。

### 4.3 Documents

- scope 参数使用 USER/CAMPAIGN/GLOBAL，不能传 personal。USER 为个人文档；管理员 USER 列表自动加 uploadedBy=本人ID；GLOBAL 登录可读；CAMPAIGN 创建要求该战役 DM。
- 上传本地 PDF/txt/md，type=DOCUMENT；tags 拼为逗号分隔字符串。服务器负责魔数与 UTF-8/control 字符校验、实际大小限制与 scope 权限。
- 直接创建 name trim1..200、description trim≤1000；content 可空、UTF-8≤900KiB，禁 NUL/C0/DEL（TAB/LF/FF/CR 除外）。修改正文同限制。JSON body 还受服务器1MiB限制。
- `MAX_DOCUMENT_SIZE_MB` 是上游环境变量，默认50；管理员设置页只展示，改变需重启。桥没有固定的50MiB上传硬限制。
- 战役共享私人文档通过 campaign_document_list 查到，用 document_read 读正文；不先读 `/api/assets/{id}` 元数据来判断权限，因为该端点可能拒绝已共享的 USER 文件。
- txt/md 返回 `{mime_type,etag,content}`（上游 md 实际常为 text/plain）。PDF 返回 `{mime_type:'application/pdf',etag,file_path,file_size}`，原始 bytes 原子落盘到项目 `downloads/<document_id>.pdf`；其他二进制 `.bin`。拒绝文件名穿越与 downloads 符号链接，downloads 不纳入 Git。
- etag 原样传 If-None-Match；304 → `{not_modified:true}`，由调用方复用已有结果，不自动再下载。已下载内容由本地使用者管理；上游删文档不自动清除本地副本。
- 编辑仅上传者/admin，不能编辑PDF。share/unshare 为当前战役 DM；shared=false 表示原生文档，无link可撤销，unshare 不删除资产，也不取消 GLOBAL/原生CAMPAIGN/其他分享赋予的读取权。
- 新建/编辑/上传原样返回 `{asset}` 等上游 JSON，列表原样返回 assets/pagination 或 documents；删除资产可能按scope允许上传者/DM/admin，由服务端裁决。

### 4.4 Saved Rolls、DM 移交、Hit Dice

- Saved Rolls 即 DiceMacro，严格 per-user/per-campaign，任意成员管理自己的宏；list 返回完整 Macro。不能传 userId 冒充别人，无 `saved_roll_get` 或虚构 execute 端点。保存表达式先由服务器真实解析，桥不自行骰出结果；使用宏时把 expression 交给 dice_roll。
- DM 移交与 Owner 收回是同一个 PUT dm，收回时 user_id=本人。旧 DM 降为 PLAYER，新 DM 必须是已有成员，ownerId 不变。REST成功后清缓存，不发送两个 role 更新来模拟移交。
- `character_hitdice_spend {characterId,index}` 只花一次 remaining；成功一般广播 character.updated，失败经 error → system.error。仅发后返回：`操作已发送，结果尚未确认；请读取事件或状态，勿重复执行。`。
- Custom Roll 复用 `dice_roll(expression,is_secret,purpose,character_name)`，映射 characterName；没有新增事件。名字是显示归属，不是授权凭据。
- Hit Dice 花费、掷骰与 token_hp 治疗是三个独立步骤，没有事务或 ACK。旧实例可能静默忽略新WS事件；不能从超时判定支持/失败或自动重发。

### 4.5 角色、地图与场次修改

- character_get 原样保留未知 data 字段；character_update 顶层只允许 name/data/tokenImageUrl；卡面 data 必须对象，先GET递归合并再PUT，数组整体替换、null显式保留。服务器按角色自己的系统校验；400 validationErrors 完整保留，不自动删新字段再试。
- CoC conditions 为对象；spellsAndMythos.cthulhuMythos 与 skills.cthulhuMythos.currentValue 独立，不能擅自同步；appearance/notes/spells 等照原值传递。Keeper notes 对战役成员可读，不是私密字段。
- DND data.hitDice 数组保留旧 total 字段及新 die/maximum/remaining/class；桥不重写池结构，不用 PUT 模拟 spend。
- character_validate 固定附 `validation_reliable:false` 和原因：v1.4 路由丢弃 validation.success=false，可能误报 isValid=true，旧版可靠性未知。仍保留上游字段；仅owner可调，fixed gameSystem 才可验证。
- 建卡 name1..200，可提供完整 data；创建后GET roster，已分配则省略 assign；未确认时用旧 assign。失败返回 created/assigned/character，提示勿重复创建。
- token_add width/height 为整数1..10，controlled_by → controlledBy。token_move 写前要求已知 DM/PLAYER，SPECTATOR/未知拒绝；服务端再检查 DM 或 controlledBy=本人。REST token 更新不保证广播。
- map_switch 先REST保存，再WS map.change；结果 persisted=true/broadcast_pending=true 仍不代表广播成功。WS发送失败返回 ok=false，附 persisted=true/broadcast_pending=false/confirmed=false，不能回滚或重放REST。
- initiative_state(refresh=true) 发 request_state 后最多等2秒的新事件；超时状态未知，不冒充未开战；refresh=false 可读旧缓存并标 stale。
- session_manage end 传 saveState/notes；pause/end 先取 activeSession.id。notes 最多2000且全桌可读；end空串不清空已有值，session_notes_update trim后空串才清空。列表可能含未结束会话；改摘要不广播、不重复结束会话。

## 5. 降级纪律与错误

| 情况 | 桥返回 error / 行为 |
|---|---|
| HTTP404 且上游 message 精确为 `The requested resource does not exist` | **E-old**：`当前 CozyVTT 实例未提供此功能；请升级到支持该功能的版本后重试。` |
| 其他 HTTP404（Document/Macro/Session/Asset/Campaign not found 等） | **E-resource**：`资源不存在或当前账号无权访问（HTTP 404）：<上游 message>` |
| HTTP400/401/403/413/429/500等 | 保留 `HTTP <status>: <message>`，data.status/upstream 保留详细校验/code |
| 旧上传路由存在但不认 DOCUMENT，返回400 | 透传类型不支持错误，不改scope、不再上传、不误报路由缺失 |
| 旧聊天响应没有 nextCursor，调用历史游标 | 返回专用 E-cursor，允许无cursor最新页 |
| WS 写发送成功但无业务ACK | pending/confirmed=false；Hit Dice 固定 E-pending 文案，错误自然进入events_poll |
| 已创建角色但分配失败／已保存地图但广播失败 | ok=false + 可恢复的 data；明确已发生的副作用，禁止盲目重复写 |

不因一个业务404永久禁用功能；不以空列表推断功能缺失；不把PDF转 `_raw` 字符串；不把文档改为公开上传作为降级；不把WS超时当作“不扣数”；不在权限错误后尝试修改membership来绕过。

## 6. 日志、测试与交付

- 日志写项目 logs/；不记录上传正文、PDF bytes、Cookie、密码或完整认证请求；工具错误保留业务错误以便定位。日志/下载文件不提交。
- 主验收 `.venv/bin/python -m pytest`：responses mock HTTP、FakeSIO mock WS，测试夹具禁止真实 TCP，stdio 子进程同样禁网。
- 覆盖旧契约、37工具注册、chat cursor/offset拒绝、multipart重试/raw/304、Documents404两类、Saved Rolls、DM移交、HitDice门控/pending、新WS事件/失去成员资格、场次notes、角色不可靠验证、token角色/尺寸、map分步结果与initiative新状态。
- 本轮不运行线上冒烟、不改config.yaml/MCP注册、不访问实例。现有 scripts/smoke.py 与 smoke_write.py 仅供另行授权后使用，不属于此次离线验收。
- 本地一条任务 commit；不push、不打tag、不发release。源码、README、SPEC、版本元数据与测试一起交付。

## 7. 非目标

账号/邀请管理、规则计算、多战役并发、地图上传、个人 Personal Notes、独立 Saved Roll get、线上部署与上游代码修复均不在 v0.2.0 范围。Documents 文件上传属于本次新增范围。
