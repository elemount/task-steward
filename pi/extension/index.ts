/**
 * TaskSteward for pi (docs/design/pi/PI001-host-and-sessions.md).
 *
 * A thin layer: every rule is in tsw (Python). At session start the extension asks `tsw pi whoami`
 * which role this session has. A TaskSteward session gets the tsw MCP server and the host notes,
 * and the tools that create and message sessions. Every session's tool calls, prompts and turn
 * ends go through `tsw pi guard`. The main session polls its mailbox and the due handoffs and
 * starts a turn when there is something to handle.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const LAUNCHER = join(ROOT, "plugin", "bin", "tsw_launch.py");
const PYTHON = process.env.TSW_PYTHON || "python3";
const POLL_MS = Number(process.env.TSW_PI_POLL_SECONDS || "5") * 1000;
const ROLES = new Set(["orchestrator", "worker", "routine", "revoked"]);
const LOADED = Symbol.for("task-steward.pi.loaded");

type Json = Record<string, any>;

interface Run {
	code: number;
	stdout: string;
	stderr: string;
}

function tsw(args: string[], env: Json, cwd: string, input = ""): Promise<Run> {
	return new Promise((done) => {
		const child = spawn(PYTHON, ["-X", "utf8", LAUNCHER, ...args], {
			cwd,
			env: { ...process.env, ...env },
			stdio: ["pipe", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (chunk) => (stdout += chunk));
		child.stderr.on("data", (chunk) => (stderr += chunk));
		child.on("error", (error) => done({ code: 127, stdout, stderr: String(error) }));
		child.on("close", (code) => done({ code: code ?? 1, stdout, stderr }));
		child.stdin.end(input);
	});
}

function parse(run: Run): Json {
	try {
		const lines = run.stdout.trim().split("\n");
		return JSON.parse(lines[lines.length - 1] || "{}");
	} catch {
		return {};
	}
}

export default function taskSteward(pi: ExtensionAPI) {
	// Loaded once per process even when both the installed package and `-e` name it.
	const marker = globalThis as Json;
	if (marker[LOADED]) return;
	marker[LOADED] = true;

	let session = "";
	let role = "none";
	let notes = "";
	let cwd = process.cwd();
	let timer: ReturnType<typeof setInterval> | undefined;
	const reported = new Set<string>();
	const env = () => ({ TSW_SESSION_ID: session });
	const guard = async (event: string, payload: Json) =>
		parse(await tsw(["pi", "guard", event, "--json"], env(), cwd, JSON.stringify({ cwd, ...payload })));

	pi.on("session_start", async (_event, ctx) => {
		cwd = ctx.cwd;
		session = process.env.TSW_SESSION_ID || `pi:${ctx.sessionManager.getSessionId()}`;
		const who = parse(await tsw(["pi", "whoami", "--json"], env(), cwd));
		role = String(who.role || "none");
		notes = String(who.notes || "");
		if (!ROLES.has(role) && !who.pending) return;
		if (who.mcp) {
			pi.registerMcpServer("tsw", {
				command: who.mcp.command,
				args: who.mcp.args,
				env: { ...who.mcp.env, TSW_SESSION_ID: session },
				cwd,
				exposure: "direct",
				description: "TaskSteward: all state changes of tasks go through these tools",
			});
		}
		const started = await guard("SessionStart", {});
		if (started.context) {
			pi.sendMessage({ customType: "task-steward", content: started.context, display: true });
		}
		if (role === "orchestrator" && !timer) {
			timer = setInterval(() => void poll(ctx), POLL_MS);
		}
	});

	pi.on("session_shutdown", async () => {
		if (timer) clearInterval(timer);
		timer = undefined;
	});

	async function poll(ctx: { isIdle(): boolean }) {
		if (!ctx.isIdle()) return;
		const found = parse(await tsw(["pi", "poll", "--json"], env(), cwd));
		const messages: string[] = found.messages || [];
		const due = ((found.handoff_due || []) as string[]).filter((id) => !reported.has(id));
		due.forEach((id) => reported.add(id));
		if (!messages.length && !due.length) return;
		const parts = [...messages];
		if (due.length) {
			parts.push(
				`[TaskSteward] worker 会话中的问题 ${due.join("、")} 已超时无人回答：调用 sync 把它们转交给主会话，再按 handle-input skill 处理。`,
			);
		}
		pi.sendMessage(
			{ customType: "task-steward", content: parts.join("\n\n"), display: true },
			{ triggerTurn: true, deliverAs: "followUp" },
		);
	}

	pi.on("before_agent_start", async (event) => {
		if (!notes) return;
		return { systemPrompt: `${event.systemPrompt}\n\n${notes}` };
	});

	pi.on("input", async (event) => {
		if (event.source === "extension" || !session) return;
		const out = await guard("UserPromptSubmit", { prompt: event.text });
		if (out.context) {
			pi.sendMessage({ customType: "task-steward", content: out.context, display: true }, { deliverAs: "steer" });
		}
	});

	pi.on("tool_call", async (event) => {
		if (!["write", "edit", "bash"].includes(event.toolName)) return;
		const out = await guard("PreToolUse", { tool: event.toolName, input: event.input });
		if (out.block) return { block: true, reason: String(out.reason || "TaskSteward 护栏拒绝") };
	});

	let reminded = false;
	pi.on("agent_before_settle", async (event) => {
		if (reminded || !ROLES.has(role)) {
			reminded = false;
			return;
		}
		const out = await guard("Stop", {});
		if (!out.followUp) return;
		reminded = true;
		return {
			entries: [
				...event.entries,
				{ type: "custom_message", customType: "task-steward", content: out.followUp, display: true },
			],
			continue: true,
		};
	});

	// The host tools of a main session (Claude Code's start_session and send_message).
	pi.registerTool({
		name: "tsw_start_session",
		label: "TaskSteward: start a worker session",
		description:
			"按 attempt_start 的返回创建 pi worker 会话（Claude Code 上的 start_session）：session_name、cwd 与 prompt 原样传入；返回 session_ref，随后用 attempt_reconcile 记录",
		parameters: Type.Object({
			session_name: Type.String(),
			cwd: Type.String(),
			prompt: Type.String(),
			title: Type.Optional(Type.String()),
		}),
		async execute(_id, params) {
			const args = ["pi", "start", "--name", params.session_name, "--cwd", params.cwd, "--prompt", params.prompt];
			if (params.title) args.push("--title", params.title);
			const run = await tsw([...args, "--json"], env(), cwd);
			return { content: [{ type: "text", text: run.stdout || run.stderr }], details: {} };
		},
	});
	pi.registerTool({
		name: "tsw_send_message",
		label: "TaskSteward: message a session",
		description:
			"给另一个 TaskSteward 会话发消息（Claude Code 上的 send_message）：worker 用 pi:<会话名>，主会话用 whoami 的 orchestrator_session。对方收到的是会话间消息，不是用户的话",
		parameters: Type.Object({ session_id: Type.String(), message: Type.String() }),
		async execute(_id, params) {
			const run = await tsw(["pi", "send", "--to", params.session_id, "--message", params.message, "--json"], env(), cwd);
			return { content: [{ type: "text", text: run.stdout || run.stderr }], details: {} };
		},
	});
}
