# PI001 Pi 上的宿主集成：主会话、RPC worker 与护栏

- 状态：设计草案（用户要求“允许使用 pi 作为主 agent，pi 可以用 pi --rpc”；2026-11-25 用户对 §9 的决定：“claude一样的选择，然后不需要权限模式”）
- 日期：2026-11-25
- 实现：[DN001 总体架构](../DN001-architecture.md) · [DN006 插件层](../DN006-plugin-layer.md) · [DN008 校验原则](../DN008-guardrails.md) · [DN009 分发与安装](../DN009-distribution-and-install.md) · 评审器：[PI004](PI004-reviewer.md)

## 1. 范围

本文是 DN001 / DN006 / DN008 / DN009 在 [Pi](https://pi.dev)（`@earendil-works/pi-coding-agent` 命令行，下称 pi）上的实现：

- **主会话**是用户在数据目录中打开的交互式 pi（TUI），加载 TaskSteward 的 pi 扩展；
- **worker** 是 tsw 以 `pi --mode rpc` 启动的长驻子进程，一个 attempt 一个，由 tsw 的会话服务进程持有；
- 宿主集成 `pi`、会话适配器 `pi-tui`（主会话）与 `pi-rpc`（worker）、评审器 `pi-cli`（PI004）都是新增的插件实现，Claude Code 的实现不变，仍是默认。

先支持 Linux 与 WSL（Unix 套接字、POSIX 进程组）；Windows 上的 pi 留到之后。

## 2. 与 Claude Code 的差别

| 项 | Claude Code（CC001、CC006） | pi（本文） |
| --- | --- | --- |
| 主会话 | 桌面 app 中的会话 | 终端中的交互式 `pi`，目录为数据目录，输入 `/skill:orchestrate` 开始 |
| worker 的创建 | 主会话调用 app 的 `start_session`，只能在用户 turn 中（`dispatch_window`） | 主会话调用扩展的 `tsw_start_session`，tsw 启动 `pi --mode rpc`；任何 turn 都可以，不需要预启动 |
| worker 的界面 | 侧栏中的会话，用户可直接对话 | 没有自己的界面：用户在终端用 `tsw pi chat <任务>` 与它直接对话（§5.4） |
| tsw 工具 | 插件的 MCP server | 扩展用 `pi.registerMcpServer` 注册同一个 `tsw mcp`，工具名 `mcp__tsw__<工具>`（§4） |
| 会话间消息 | `send_message` | 扩展的 `tsw_send_message`（§5.3） |
| 护栏 | hooks（`PreToolUse` 等） | 扩展的 `tool_call` / `input` / `agent_before_settle` 事件转成同样的 `tsw guard` 调用（§6） |
| 会话身份 | `CLAUDE_CODE_HOST_SESSION_ID` | `TSW_SESSION_ID`，缺省为 pi 的 `PI_SESSION_ID` 加前缀 `pi:`（§4.1） |
| 宿主自己的保护 | 权限模式与 auto 模式分类器 | 不使用权限模式（§6.3） |
| 唤醒 | 跨会话消息（push） | 主会话扩展轮询邮箱，有消息时开始一轮（§5.3） |
| 定时 | `CronCreate` | 没有：例行会话不可用；提问超时由主会话扩展的轮询发现（§5.4、§7） |

## 3. 组成

| 部分 | 位置 | 职责 |
| --- | --- | --- |
| pi 包与扩展 | 产品检出的 `pi/`（`package.json` 的 `pi.extensions` 指向 `pi/extension/index.ts`） | 会话开始时问 tsw 本会话的角色；为 TaskSteward 会话注册 `tsw` MCP server、加上 pi 说明、注册 `tsw_start_session` 与 `tsw_send_message`；把工具调用、用户消息与一轮的结束交给 `tsw pi guard`；主会话中轮询邮箱与到期的提问 |
| 主会话说明 | 数据目录的 `CLAUDE.md`（pi 把它当作上下文文件读取）与 `.pi/skills/`（`tsw host render` 从 `.claude/skills/` 复制） | 与 Claude Code 同一份说明与 skill；扩展再把 `tsw/host/pi_notes.md`（Claude Code 的写法在 pi 上对应什么）加到 TaskSteward 会话的系统提示 |
| 会话服务 `tsw pi serve` | tsw 运行时，只用标准库 | 每个数据目录一个，按需启动、与主会话进程分离；持有 worker 的 RPC 子进程，读其事件流 |
| `tsw pi` 命令 | `tsw/host/pi_cli.py` | 扩展用：`whoami`、`start`、`send`、`stop`、`list`、`poll`、`guard`；用户用：`chat` |

扩展是 TypeScript（pi 用 jiti 直接加载，不需要编译），只是薄的转发层：所有规则仍在 tsw 的 Python 运行时中，tsw 运行时仍只依赖标准库。扩展经 `TSW_PYTHON`（缺省 `python3`）运行检出中的 `plugin/bin/tsw_launch.py`；同一进程中只加载一次（已安装的包与 `-e` 同时指向它时）。

## 4. 主会话

1. 用户在终端 `cd <数据目录> && pi`，信任该目录一次（pi 才读取项目的 `.pi/skills/`）。扩展在 `session_start` 运行 `tsw pi whoami`（带 `TSW_SESSION_ID` 与当前目录），得到角色、`tsw mcp` 的启动方式与 pi 说明。
2. TaskSteward 会话（主会话、worker、待绑定的会话）注册 `tsw` MCP server（`exposure: direct`，环境带 `TSW_HOME` 与 `TSW_SESSION_ID`），并运行 `tsw pi guard SessionStart`：主会话照 DN001 §6.4 自动领取调度租约，结果作为扩展消息显示。
3. 用户输入 `/skill:orchestrate` 开始主会话（对应 Claude Code 的 `/orchestrate`）。
4. 与 TaskSteward 无关的会话（角色 `none`）不注册 tsw 工具，扩展只做护栏。

### 4.1 会话身份

`tsw.host.identity` 依次取 `TSW_SESSION_ID`、Claude Code 的 `CLAUDE_CODE_HOST_SESSION_ID`、pi 给 bash 工具的 `PI_SESSION_ID`（加前缀 `pi:`）。扩展调用 tsw 时都设 `TSW_SESSION_ID`；主会话中模型运行的 `tsw` 命令经 `PI_SESSION_ID` 得到同一个 ID；worker 的整个进程带 `TSW_SESSION_ID=pi:<会话名>`。账本中 pi 会话的 `session_ref` 因此不会与 Claude Code 的混淆。只增不删，Claude Code 的行为不变。pi 给它启动的每个进程设 `PI_CODING_AGENT`，tsw 把它与 `TSW_SESSION_ID`、`PI_SESSION_ID` 都当作“在会话中”（用户本人的命令在会话中拒绝，DN006 §8.10）。

### 4.2 pi 工具名

DN008 的 block list 规则按工具名与参数判断。`tsw pi guard` 把 pi 的工具映射为 guard 已认识的名称，guard 本身不改：

| pi 工具 | 转为 | 判断依据 |
| --- | --- | --- |
| `write` | `Write` | `path` → `file_path` |
| `edit` | `Edit` | `path` → `file_path` |
| `bash` | `Bash` | `command` |
| `read`、`grep`、`find`、`ls` | `Read`、`Grep`、`Glob`、`LS` | 路径（只用于 Dashboard 令牌目录的规则 CC-B12） |
| 其他（`mcp__tsw__*`、其他扩展的工具） | 不检查 | 由 tsw 的状态机守卫，或与 Claude Code 上未匹配的工具相同（block list） |

## 5. worker

### 5.1 创建与投递

主会话照常调用 `attempt_start`（DN002 T07），再以它返回的 `session_name`、会话目录（worktree 或执行目录）与 `prompt` 调用 `tsw_start_session`，拿到 `session_ref` 后 `attempt_reconcile`。`tsw pi start`：

- 调用者须是主会话；`session_name` 须属于一个处于 `starting` 的 attempt（先落账再创建，与 Claude Code 上 `start_session` 的规则相同）；会话目录须在该 attempt 的执行目录之中；
- 请会话服务以 `pi --mode rpc --session-dir <数据目录>/cache/pi-sessions/sessions --session-id <会话名> --name <标题> -e <产品检出>/pi [--model <provider/model>] --thinking <roles.worker.effort>` 在会话目录启动 worker（`roles.worker.model` 为 `default` 时用 pi 自己的默认模型），环境带 `TSW_HOME`、`TSW_SESSION_ID=pi:<会话名>`、`TSW_PYTHON`、`TSW_REPO`；
- 会话状态（目录、请求、是否运行）先写入 `cache/pi-sessions/<会话名>.json` 再启动进程；进程起来后发送 RPC `prompt`（`attempt_start` 返回的 worker 说明加任务书路径）；
- 之后的消息经 `prompt` 投递，worker 正在运行时为 `streamingBehavior: "followUp"`（`tsw pi chat` 为 `steer`）；
- 停止的 worker（或上一个会话服务启动的）收到消息时以同一会话 ID 与目录恢复（`reuse_session`）。

### 5.2 会话服务

worker 不作为主会话 pi 进程的子进程：用户退出或重开主会话时，正在工作的 worker 不被结束（用户决定，§9）。`tsw pi serve`：

- 每个数据目录一个（`locks/pi-sessions.lock`，第二个立即退出）；监听 Unix 套接字，放在只有本用户能进入的目录（`$XDG_RUNTIME_DIR`，没有时 `<临时目录>/tsw-<uid>`，套接字路径长度有限）。`/tmp` 是共用的：客户端发送任何内容之前核对该目录是本用户的真实目录、其他用户不能进入，套接字属于本用户，Linux 上另核对对端进程的用户（`SO_PEERCRED`）；不满足时不连接；
- 客户端连接不上时以独立进程组启动它（stderr 写入 `cache/pi-sessions/serve.log`）；没有运行中的 worker 达 `TSW_PI_IDLE_SECONDS`（缺省 600 秒）后退出；
- 每个 worker 一个 `pi --mode rpc` 子进程（独立进程组），持续读取它的 stdout，只在 LF 处分帧；记录是否在运行一轮（`agent_start` / `agent_settled`）与退出码。pi 退出时结束它的整个进程组（留下的子进程不会占着输出继续运行）；写命令与等待回应各有时限，不再读取命令的 pi 被结束；停止时先在时限内 abort，然后无论如何结束进程组；退出时不再接受新的请求；
- 只在内存中保留每个 worker 最近完成的回复（供 `tsw pi chat` 显示），不把模型输出写到任何地方；reportable 事件都经 tsw 工具落账，与 Claude Code 相同；
- 子进程意外退出后，主会话按 DN002 的会话丢失处理，或给它发消息使它恢复。

### 5.3 会话间消息与唤醒主会话

`tsw_send_message`（`tsw pi send`）给服务器持有的 worker 投递；给其他会话（主会话的交互式 pi）写入它的邮箱 `cache/pi-sessions/mailbox/`。内容都带 `<cross-session-message from="<发送者>">` 包装，guard 与 Claude Code 上一样认作会话间消息，不是用户的话（DN008 §3 误认来源）。

worker 落账 reportable 事件后照常给持租主会话发消息（DN001 §6.3）。主会话扩展在空闲时每 `TSW_PI_POLL_SECONDS`（缺省 5 秒）运行一次 `tsw pi poll`，取走邮箱中的消息，以 `pi.sendMessage(..., {triggerTurn: true, deliverAs: "followUp"})` 注入并开始一轮。能力声明 `wake: poll`。

### 5.4 用户与 worker 直接对话、worker 提问

与 Claude Code 相同（用户决定，§9）：用户可以直接与 worker 对话，worker 先在自己的会话中问用户，超时无人回答才转交主会话（DN006 §8.1a、§8.3）。

- **直接对话**：`tsw pi chat <任务 ID 或会话名>` 在用户的终端中连接会话服务：先显示 worker 最近的回复，然后逐行读取用户输入、投递，并显示 worker 完成的回复。它是用户本人在终端运行的命令，会话中运行时拒绝。
- **用户原话**：经 `tsw pi chat` 投递的消息不加包装，worker 扩展的 `input` 事件把它交给 `tsw guard UserPromptSubmit`，按用户的话记录（G-USER，DN002 §5.1a）；主会话发给 worker 的消息与任务书带包装，不是用户的话。
- **提问**：worker 照常 `ask_user`，在回复中写出问题，并提醒用户用 `tsw pi chat` 回答（pi 说明）。
- **超时转交**：Claude Code 上由例行会话发现超时的问题（DN006 §8.3）。pi 上 `tsw pi poll` 对持租主会话一并返回到期未答的问题（与例行表同一条规则 `handoff_due`），扩展开始一轮，请主会话 `sync` 转交并按 handle-input 处理（每个问题只提醒一次）；主会话不在运行时，问题留在 worker 中，下一次主会话运行时处理。

### 5.5 能力声明

| 适配器 | `execution` | `interactive` | `dispatch_window` / `prestart` | `wake` | `user_direct_chat` | `timer` | `reuse_session` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `pi-tui`（主会话） | `delegated` | 是 | 否 / 否 | `poll` | 是 | 否 | 是 |
| `pi-rpc`（worker） | `delegated` | 是 | 否 / 否 | `poll` | 是（`tsw pi chat`） | 否 | 是 |

配置校验（DN003 §4）：`plugins.host_integration` 为 `pi` 时 `roles.orchestrator.session` 须为 `pi-tui`、`roles.worker.session` 须为 `pi-rpc`，`routine.enabled` 须为 false，不能用 `teams-chat` 渠道（它依赖例行会话）；pi 会话与评审器的模型写作 `provider/model`，主会话与 worker 也可以写 `default`（pi 自己的默认模型）。

## 6. 护栏

### 6.1 事件对应

| Claude Code hook | pi 扩展事件 | 做法 |
| --- | --- | --- |
| `SessionStart` | `session_start` | `tsw pi guard SessionStart`，上下文作为扩展消息显示 |
| `PreToolUse` | `tool_call`（`write`、`edit`、`bash`，以及只读的 `read`、`grep`、`find`、`ls`） | 按 §4.2 转换后经 guard；拒绝时返回 `{block: true, reason}`，reason 是 guard 的说明（含规则编号与正确做法）。只读工具也经过，Dashboard 令牌目录（CC-B12）对它们同样拒绝 |
| `UserPromptSubmit` | `input`（交互输入与 RPC 输入；扩展注入的不算） | 经 guard，记录用户原话供 G-USER 引用（DN002 §5.1a）；上下文以 steer 加入这一轮 |
| `Stop` | `agent_before_settle` | 经 guard（角色由 guard 当时解析：worker 启动时待绑定，之后才绑定）；需要提醒时加一条扩展消息并继续一轮（每轮最多一次） |

会话的 shell 中不能运行 `tsw pi guard`（与 `tsw guard` 相同：它会记录用户原话，只由扩展调用）。`tsw pi guard` 按 Claude Code hooks 的字段组装 payload（`session_id`、`cwd`、`tool_name`、`tool_input`、`prompt`），guard 读取时本来就忽略未知字段（compatibility.md）；pi 没有会话记录的 `transcript_path`，guard 读不到来源时照旧按它看到的消息判断。guard 内部失败时放行并在 stderr 报告，与 hooks 相同。

### 6.2 用户原话

G-USER 依赖“这一轮来自用户本人”。pi 中，主会话的交互输入、`tsw pi chat` 投递给 worker 的输入是用户的话；扩展注入的消息（唤醒、提醒）与带 `<cross-session-message>` 包装的会话间消息不是。

### 6.3 不使用权限模式

用户决定 pi 上不需要权限模式：pi 没有权限模式，也没有 auto 模式分类器，tsw 不为它另造一层。`roles.*.permission_mode` 在 pi 上不生效（配置校验给出警告，不报错），主会话与 worker 的 bash 与文件写入只受 §6.1 的 block list 约束。DN008 §5.3 交给宿主的一般风险（删除文件、危险命令）在 pi 上由用户自己承担，这是用户接受的取舍。

## 7. 不支持的部分（第一版）

- 例行会话（DN006 §8.9）：pi 没有会话内定时任务。超时转交由主会话扩展的轮询代替（§5.4）；`teams-chat` 渠道依赖例行会话，不可用。
- workdeck 门户：它投递到 Claude 会话收件箱，不认识 pi 会话；安装时这一步跳过。
- 会话标题与归档（DN002 §5.8）：没有侧栏，只记账。
- Dashboard 常驻服务：Linux 上没有计划任务，第一版只支持在终端运行 `tsw dashboard`。
- Windows 上的 pi。

## 8. 安装（Linux / WSL）

终端入口 `setup.sh`（DN009 §12）探测 Python 3.10+ 与 git，运行 `tsw setup install --interactive`。本机有 pi 时才提供宿主问题的 `pi` 选项（只有一个可选时不问）；找不到 `claude` 而找到 pi 时默认 pi。

| 步骤 | Claude Code（Linux） | pi |
| --- | --- | --- |
| 垫片与 PATH | 启动脚本在状态目录 `bin/tsw`，`~/.local/bin/tsw` 是指向它的符号链接（已有别的 `tsw` 时拒绝，不替换）；`~/.local/bin` 不在 PATH 时 `path` 为手工步骤，给出 `export` 行，不自动改 | 同左 |
| 插件 | `claude plugin marketplace add` | `pi install <产品检出>/pi`（本地路径的 pi 包，原地加载，没有副本）；已声明时跳过；卸载时 `pi remove` 移除安装加入的 |
| 用户设置 | `~/.claude/settings.json` 的 `env` 与 auto 模式规则 | 跳过：扩展从检出运行 tsw，数据目录由 `.local/tsw-home` 定位 |
| 配置 | 不变 | 主会话 `pi-tui`、worker `pi-rpc`（模型 `default`）、例行会话关闭 |
| render | `CLAUDE.md`、`.claude/skills/` | 另把 skill 复制到 `.pi/skills/`；没有插件副本要更新 |
| workdeck | 有 `workdeck` 时可选 | 跳过 |
| verify | 插件来源与副本 | `pi_package`：pi 的用户设置声明了检出中的 pi 包 |
| 安装记录 | `$XDG_STATE_HOME/task-steward/`（缺省 `~/.local/state/task-steward/`） | 同左 |

安装后的下一步：打开新终端，`cd <数据目录> && pi`，信任目录，输入 `/skill:orchestrate`；与 worker 对话用另一个终端中的 `tsw pi chat <任务 ID>`。

## 9. 用户的决定（2026-11-25）

用户原话：“claude一样的选择，然后不需要权限模式”。

1. worker 的进程归属：与 Claude Code 一样，worker 不随主会话结束，由独立的会话服务持有（§5.2）。
2. worker 提问：与 Claude Code 一样，用户可直接与 worker 对话，worker 先在自己的会话中问，超时才转交主会话（§5.4）。
3. 权限：不使用权限模式（§6.3）。
4. 范围：第一版 Linux / WSL，不做例行会话、workdeck 与 Dashboard 常驻服务（§7）；超时转交由主会话扩展的轮询代替例行会话（§5.4）。

## 10. 验证

- unit：会话身份的顺序；pi 工具到 guard payload 的转换与 guard 答复的转换；会话间消息的包装；宿主 pi 的配置与校验规则；`provider/model`；宿主的默认与可选项。
- component：会话服务以 pi 的替身（`tests/fixtures/fake_pi.py`，本仓库之外的平台，模拟证据）创建、投递、恢复、停止；邮箱；`tsw pi start` 只为已落账的 attempt 在其执行目录中创建；轮询；pi 包的安装与移除；以 pi 为宿主的完整 `tsw setup install` / `uninstall`。
- surface：`tsw pi send` 到 `poll`、`whoami`、`guard` 拒绝写入账本、`chat` 在会话中拒绝、`start` 经独立的 `tsw pi serve` 启动替身 worker 并在停止后退出。
- 本机：扩展在真实 pi 的 RPC 模式中加载，`session_start` 把数据目录中的会话认作主会话、领取租约并显示上下文（不调用模型）。
- live（`pi-e2e`，尚未实现）：真实 pi 主会话派发一个 RPC worker，worker 自评审、提问、`tsw pi chat` 回答、提交，主会话验收与取消。在此之前，真实 pi 上的完整流程为未验证。
