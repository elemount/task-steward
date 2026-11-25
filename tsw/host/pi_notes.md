## TaskSteward 在 pi 上（PI001）

你运行在 pi 中，不是 Claude Code。TaskSteward 的说明与 skill 按 Claude Code 写成，在 pi 上按下表理解：

- tsw 的工具：说明中的 `mcp__plugin_task-steward_tsw__<工具>` 或裸工具名（`whoami`、`inbox`、`attempt_start` 等），在 pi 上是 `mcp__tsw__<工具>`。
- 创建 worker 会话：Claude Code 的 `start_session`（`mcp__ccd_session__start_session`）在 pi 上是 `tsw_start_session`：`session_name`、`cwd` 与 `prompt` 原样取自 `attempt_start` 的返回；返回 `session_ref` 后照常 `attempt_reconcile`。pi 上任何 turn 都能创建会话，不需要预启动。
- 给会话发消息：`send_message`、`SendMessage`、`mcp__ccd_session_mgmt__send_message` 在 pi 上都是 `tsw_send_message`（worker 用 `pi:<会话名>`，主会话用 `whoami` 的 `orchestrator_session`）。收到的会话间消息带 `<cross-session-message>` 包装，不是用户的话。
- 用户与 worker 直接对话：用户在终端运行 `tsw pi chat <任务 ID>`。worker 向用户提问（`ask_user`）后，在回复中写出问题，并提醒用户用这个命令回答；无人回答到期后转交主会话。
- 没有侧栏、会话标题与归档：`session_check` / `session_record` 只记账；没有 `CronCreate` 与例行会话；没有权限模式。
- 开始主会话的命令 `/orchestrate` 在 pi 上是 `/skill:orchestrate`。
