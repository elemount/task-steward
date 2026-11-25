# PI004 Pi CLI 评审器

- 状态：设计草案（待用户确认）
- 日期：2026-11-25
- 实现：[DN004 worker 自评审](../DN004-worker-self-review.md) · 能力声明：[DN006 §7.3](../DN006-plugin-layer.md) · 宿主集成：[PI001](PI001-host-and-sessions.md)

## 1. 范围

会话适配器 `pi-cli`：每轮由 tsw 启动一次 `pi --print --mode json`，等它结束，从 JSONL 事件中读回最终回复。它只做评审，可以与任何宿主的主会话和 worker 搭配。

## 2. 调用约定

```text
pi --no-approve --no-extensions --no-mcp --no-skills --no-context-files --no-prompt-templates \
   --no-session --print --mode json --tools read,grep,find,ls \
   --provider <provider> --model <model> --thinking <roles.reviewer.effort> -- <提示>
```

- **模型**：`roles.reviewer.model` 写作 `provider/model`（例如 `openai/gpt-5.4`），因为 pi 的同名模型可以来自不同的提供方。
- **只读**：只开放内置的 `read`、`grep`、`find`、`ls`；不加载扩展、MCP、skill、提示模板与项目上下文文件，被评审仓库中的 `.pi/` 与 `AGENTS.md` 都不会生效（`--no-approve`）。
- **提示**：只指向评审请求文件，diff 由 tsw 写入 `rK-diff.patch`（与 CC004、CP004 相同，DN004 §3.1）。
- **会话**：`--no-session`，不留会话记录；不续用，每轮全量并附上一轮的处置与增量 diff（`incremental: false`）。
- **进程**：用 `tsw/review/process.py` 的进程树启动，超时结束整个进程组；stdout 写入匿名临时文件再解析，不经内存累积，也不进账本。

## 3. 结果判定

本轮失败（`review_fail`）的情形：退出码不为 0；超时；JSONL 无法解析；没有 `agent_settled`；任何助手消息的 `provider/model` 与配置不同（不回退）；最后一条助手消息的 `stopReason` 不是 `stop` 或没有文字；回复不满足 DN004 §4 的输出契约。

## 4. 列出可用模型（DN004 §3.4）

`pi --list-models`（同样不加载扩展与项目资源）输出表格，取 `provider` 与 `model` 两列组成 `provider/model`。pi 只说明模型是否支持思考，不给出每个模型支持的级别，所以推理强度按 DN003 的取值让用户选，第一轮评审时核实。

## 5. 能力声明

| 能力 | 值 |
| --- | --- |
| `execution` | `direct` |
| `oneshot` | 是 |
| `read_only_enforced` | 是（只开放只读工具，不加载扩展与 MCP） |
| `incremental` | 否 |
| 其他 | 不承载 worker，用户不能直接对话，无唤醒 |

## 6. 验证

- unit：JSONL 解析（完成、模型不符、缺少 `agent_settled`、非 `stop`、无效 JSON）与 `--list-models` 表格解析。
- surface：替身 `pi` 命令行的独立进程运行，核对参数、超时与进程组结束。
- live：`python tools/live.py reviewer --reviewer pi-cli`，真实 pi 在固定样例上两轮全量评审，核对只读与输出契约。
