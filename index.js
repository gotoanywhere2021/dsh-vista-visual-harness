/**
 * VISTA visual harness — native DeepSeek Harness host tools.
 *
 * Implements the harness from "VISTA: A Visual Harness for Reasoning in an
 * Interactive World" (arXiv:2610.02200) as first-class agent tools:
 *
 *   visual observation  -> every turn is archived as a lossless PNG frame;
 *   lossless memory     -> frames are indexed by turn/frame and never rewritten;
 *   model-directed look -> `vista_inspect` re-attends to old frames/regions and
 *                          returns the image itself; `vista_read_pixels` reads
 *                          exact per-cell colours;
 *   one action per turn -> `vista_play` executes exactly one input and returns
 *                          the observation of the resulting state;
 *   notes + handoff     -> `vista_notes` maintains GUIDE.md/WORKING.md and the
 *                          compaction checkpoint.
 *
 * The plugin is a thin, dependency-free facade over the verified Python engine
 * shipped in ./engine (the same engine the vista-visual-harness skill uses).
 * Host code runs in the DSH host process; capture/input stay in Python.
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { existsSync, readdirSync } from "node:fs";
import { readFile, writeFile, rm } from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import { homedir, tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

export const name = "vista-visual-harness";
export const inject = ["tools"];

const execFileAsync = promisify(execFile);
const ENGINE_DIR = join(dirname(fileURLToPath(import.meta.url)), "engine");
const VISTA_PY = join(ENGINE_DIR, "vista.py");

let cachedPython;

function pythonPath(config) {
	if (config?.pythonPath) return config.pythonPath;
	if (process.env.VISTA_PYTHON) return process.env.VISTA_PYTHON;
	if (cachedPython !== undefined) return cachedPython;
	const runtimesRoot = join(homedir(), ".dsh", "dsh-runtimes");
	try {
		for (const entry of readdirSync(runtimesRoot)) {
			const candidate = join(runtimesRoot, entry, "dependencies", "python", "python.exe");
			if (existsSync(candidate)) {
				cachedPython = candidate;
				return candidate;
			}
		}
	} catch {
		/* fall through to PATH */
	}
	cachedPython = process.platform === "win32" ? "python" : "python3";
	return cachedPython;
}

function runsRoot(config) {
	return config?.runsRoot ?? process.env.VISTA_HOME ?? join(homedir(), ".dsh", "vista");
}

function newRunDir(config) {
	const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
	const salt = Math.random().toString(16).slice(2, 8);
	return join(runsRoot(config), "runs", `${stamp}-${salt}`);
}

function lastJson(text) {
	if (typeof text !== "string") return undefined;
	const lines = text.split(/\r?\n/);
	for (let index = lines.length - 1; index >= 0; index -= 1) {
		const line = lines[index].trim();
		if (!line.startsWith("{")) continue;
		try {
			return JSON.parse(line);
		} catch {
			/* keep scanning */
		}
	}
	return undefined;
}

async function runEngine(config, argv, timeoutMs = 120_000) {
	const python = pythonPath(config);
	const env = {
		...process.env,
		VISTA_HOME: runsRoot(config),
		PYTHONIOENCODING: "utf-8",
		PYTHONUTF8: "1"
	};
	let stdout;
	try {
		({ stdout } = await execFileAsync(python, [VISTA_PY, ...argv], {
			cwd: ENGINE_DIR,
			env,
			timeout: timeoutMs,
			windowsHide: true,
			maxBuffer: 16 * 1024 * 1024
		}));
	} catch (error) {
		const parsed = lastJson(String(error?.stdout ?? "") + "\n" + String(error?.stderr ?? ""));
		throw new Error(`vista engine: ${parsed?.error ?? error?.message ?? String(error)}`);
	}
	const payload = lastJson(stdout);
	if (payload === undefined) throw new Error(`vista engine returned no JSON: ${String(stdout).slice(-300)}`);
	if (payload.ok !== true) throw new Error(`vista engine: ${payload.error ?? "unknown error"}`);
	return payload;
}

async function saveImage(attachments, filePath, summary, extra = {}) {
	const value = { path: filePath, summary, ...extra };
	if (attachments === undefined) return value;
	try {
		const data = await readFile(filePath);
		const reference = await attachments.saveImage({ data, mediaType: "image/png", name: basename(filePath) });
		return {
			...value,
			attachmentId: reference.attachmentId,
			mediaType: reference.mediaType,
			bytes: reference.bytes,
			width: reference.width,
			height: reference.height,
			...(reference.name === undefined ? {} : { name: reference.name })
		};
	} catch (error) {
		return { ...value, summary: `${summary}\n(image could not be attached: ${error?.message ?? String(error)}; use the path)` };
	}
}

const IMAGE_OUTPUT_SCHEMA = {
	type: "object",
	additionalProperties: false,
	properties: {
		path: { type: "string" },
		summary: { type: "string" },
		attachmentId: { type: "string" },
		mediaType: { type: "string" },
		bytes: { type: "integer" },
		width: { type: "integer" },
		height: { type: "integer" },
		name: { type: "string" },
		turn: { type: "integer" },
		n_frames: { type: "integer" },
		run: { type: "string" }
	},
	required: ["path", "summary"]
};

const TEXT_OUTPUT_SCHEMA = {
	type: "object",
	additionalProperties: false,
	properties: { text: { type: "string" } },
	required: ["text"]
};

function renderImageOutput(_args, value) {
	const blocks = [{ type: "text", text: `${value.summary}\n<path>${value.path}</path>` }];
	if (typeof value.attachmentId === "string" && value.mediaType !== undefined) {
		blocks.push({
			type: "image",
			attachment: {
				attachmentId: value.attachmentId,
				mediaType: value.mediaType,
				bytes: value.bytes,
				width: value.width,
				height: value.height,
				...(value.name === undefined ? {} : { name: value.name })
			}
		});
	}
	return blocks;
}

function renderTextOutput(_args, value) {
	return [{ type: "text", text: value.text }];
}

const RUN_PARAMETER = { type: "string", description: "Run directory from vista_start. Omit to use the newest run." };

const VIEWS_SCHEMA = {
	type: "array",
	description: "Views to retrieve from the lossless memory, in order. Each view is one image crop.",
	items: {
		type: "object",
		additionalProperties: false,
		properties: {
			label: { type: "string" },
			turn: { type: "integer" },
			frame: { type: "string" },
			region: {
				type: "array",
				items: { type: "integer" },
				description: "x, y, w, h in pixels of the archived original frame."
			},
			zoom: { type: "number" }
		},
		required: ["label"]
	}
};

const REGION_SCHEMA = { type: "string", description: "Region 'x,y,w,h' in archived frame pixels." };

function toolParameters(properties, required) {
	return {
		type: "object",
		additionalProperties: false,
		properties,
		...(required === undefined ? {} : { required })
	};
}

/**
 * Register the ten VISTA tools once the attachment store (needed to return
 * images inline) is available. Returns the disposer for every registration.
 */
export function apply(ctx, config = {}) {
	const disposers = [];
	let registered = false;

	ctx.inject(["attachments"], (scope) => {
		if (registered) return;
		registered = true;
		const attachments = typeof scope.get === "function" ? scope.get("attachments") : undefined;
		for (const definition of buildDefinitions(attachments, config)) {
			const dispose = scope.tools.register(definition);
			if (typeof dispose === "function") disposers.push(dispose);
		}
	});

	return () => {
		for (const dispose of disposers.splice(0)) {
			try {
				dispose();
			} catch {
				/* ignore teardown errors */
			}
		}
	};
}

function buildDefinitions(attachments, config) {
	const definitions = [];
	const add = (definition, render) => {
		const { outputSchema, ...rest } = definition;
		definitions.push({ ...rest, output: { schema: outputSchema, render } });
	};
	const addImage = (definition) => add(definition, renderImageOutput);
	const addText = (definition) => add({ ...definition, outputSchema: TEXT_OUTPUT_SCHEMA }, renderTextOutput);

	addImage({
		name: "vista_start",
		description:
			"Start a VISTA visual-harness run on an interactive world: a window, the whole screen, a screen region, or one static image. Archives the first observation as a lossless PNG and returns the image itself. After starting, call read-only vista_* tools to inspect the visual memory and vista_play to act exactly one step.",
		parameters: toolParameters({
			source: { type: "string", enum: ["window", "screen", "region", "image"], description: "Observation source. Defaults from the other arguments (window/image/screen)." },
			window: { type: "string", description: "Window title substring to observe, e.g. a game window title." },
			region: { type: "string", description: "Screen region 'x,y,w,h' when source=region." },
			image: { type: "string", description: "Absolute path of a static PNG/JPEG to ingest when source=image." },
			observeMax: { type: "integer", description: "Observation long-side cap in pixels (default 1024)." },
			run: { type: "string", description: "Explicit run directory (default: fresh directory under the VISTA home)." }
		}),
		outputSchema: IMAGE_OUTPUT_SCHEMA,
		async execute(args) {
			const source = args.source ?? (args.window !== undefined ? "window" : args.image !== undefined ? "image" : "screen");
			const runDir = args.run ?? newRunDir(config);
			const argv = ["init", "--run-dir", runDir];
			if (source === "window") {
				if (!args.window) throw new Error("vista_start: source=window requires the window title substring");
				argv.push("--window", args.window);
			} else if (source === "region") {
				if (!args.region) throw new Error("vista_start: source=region requires region 'x,y,w,h'");
				argv.push("--region", args.region);
			} else if (source === "image") {
				if (!args.image) throw new Error("vista_start: source=image requires an absolute image path");
				argv.push("--image", args.image);
			} else {
				argv.push("--screen");
			}
			if (args.observeMax !== undefined) argv.push("--observe-max", String(args.observeMax));
			const result = await runEngine(config, argv);
			const summary = [
				`VISTA run started (turn ${result.turn}).`,
				`run: ${result.run_dir}`,
				`frame: ${result.frame_size?.[0] ?? "?"}x${result.frame_size?.[1] ?? "?"}; capture: ${JSON.stringify(result.capture ?? {})}`,
				`Next: inspect/read_pixels for detail, then vista_play for exactly one action.`
			].join("\n");
			return saveImage(attachments, result.observation_path, summary, {
				turn: result.turn,
				run: result.run_dir
			});
		}
	});

	addImage({
		name: "vista_observe",
		description:
			"Archive one new observation turn without acting (lossless PNG). Use when the world changes on its own or before deciding the first action. Returns the image itself.",
		parameters: toolParameters({
			run: RUN_PARAMETER,
			frames: { type: "integer", description: "Frames to archive in this turn (a burst catches animation, default 1)." },
			interval: { type: "number", description: "Seconds between burst frames (default 0.12)." }
		}),
		outputSchema: IMAGE_OUTPUT_SCHEMA,
		async execute(args) {
			const argv = ["capture", "--frames", String(args.frames ?? 1), "--interval", String(args.interval ?? 0.12)];
			if (args.run) argv.push("--run", args.run);
			const result = await runEngine(config, argv);
			return saveImage(attachments, result.observation_path, `Observation archived: turn ${result.turn}, ${result.n_frames} frame(s), ${result.frame_size?.[0]}x${result.frame_size?.[1]}.`, {
				turn: result.turn,
				n_frames: result.n_frames,
				run: args.run
			});
		}
	});

	addImage({
		name: "vista_inspect",
		description:
			"Re-attend to the lossless visual memory: retrieve any archived turn/frame, crop any region, enlarge it, and return the image itself (optionally a labeled montage of several views). This is the harness's explicit attention over interaction history — use it to revisit past evidence or read fine detail instead of guessing.",
		parameters: toolParameters({
			run: RUN_PARAMETER,
			question: { type: "string", description: "What these views should settle." },
			views: VIEWS_SCHEMA,
			montage: { type: "boolean", description: "Stack all views into one labeled image (default true)." },
			region: REGION_SCHEMA,
			turn: { type: "integer", description: "Turn for the single-view form." },
			frame: { type: "string", description: "'final' (default) or a frame number." },
			zoom: { type: "number", description: "Enlargement factor; default auto (~640 px long side)." },
			label: { type: "string", description: "Label for the single-view form." }
		}),
		outputSchema: IMAGE_OUTPUT_SCHEMA,
		async execute(args) {
			const views = Array.isArray(args.views) && args.views.length > 0
				? args.views
				: args.region === undefined
					? undefined
					: [{ label: args.label ?? "view", turn: args.turn, frame: args.frame, region: parseRegion(args.region), zoom: args.zoom }];
			if (views === undefined) throw new Error("vista_inspect: provide views[] or region");
			const tmpFile = join(tmpdir(), `vista-inspect-${Date.now()}-${Math.random().toString(16).slice(2, 8)}.json`);
			await writeFile(tmpFile, JSON.stringify(views), "utf8");
			try {
				const argv = ["inspect", "--views-file", tmpFile];
				if (args.question !== undefined) argv.push("--question", args.question);
				if (args.montage !== false) argv.push("--montage");
				if (args.run) argv.push("--run", args.run);
				const result = await runEngine(config, argv);
				const chosen = result.montage_path ?? result.views?.[0]?.path;
				const metas = (result.views ?? []).map((view) => `${view.label}: turn ${view.turn} frame ${view.frame} region ${JSON.stringify(view.region)} zoom ${view.zoom}x -> ${view.output_size?.join("x")}`);
				const summary = [
					args.question === undefined ? "Views:" : `Question: ${args.question}`,
					...metas,
					result.montage_path === undefined ? "" : `montage: ${result.montage_path}`
				].filter(Boolean).join("\n");
				if (chosen === undefined) throw new Error("vista_inspect: engine returned no image path");
				const size = result.montage_size ?? result.views?.[0]?.output_size;
				return saveImage(attachments, chosen, summary, {
					...(size === undefined ? {} : { width: size[0], height: size[1] }),
					run: args.run
				});
			} finally {
				await rm(tmpFile, { force: true });
			}
		}
	});

	addText({
		name: "vista_read_pixels",
		description:
			"Read exact colours from an archived frame: split a region into rows x cols cells and return a palette, the per-cell palette indices, exact RGB, and cells that straddle two colours. Use it whenever a judgement depends on colour identity or measurement rather than visual impression.",
		parameters: toolParameters({
			run: RUN_PARAMETER,
			question: { type: "string", description: "Why exact colours are needed." },
			region: REGION_SCHEMA,
			rows: { type: "integer", description: "Rows in the cell grid." },
			cols: { type: "integer", description: "Columns in the cell grid." },
			turn: { type: "integer", description: "Archived turn (default: latest)." },
			frame: { type: "string", description: "'final' (default) or a frame number." }
		}),
		async execute(args) {
			const argv = ["read_pixels"];
			if (args.run) argv.push("--run", args.run);
			if (args.turn !== undefined) argv.push("--turn", String(args.turn));
			if (args.frame !== undefined) argv.push("--frame", String(args.frame));
			if (args.region !== undefined) argv.push("--region", args.region);
			if (args.rows !== undefined) argv.push("--rows", String(args.rows));
			if (args.cols !== undefined) argv.push("--cols", String(args.cols));
			if (args.question !== undefined) argv.push("--question", args.question);
			argv.push("--compact");
			const result = await runEngine(config, argv);
			const text = JSON.stringify(result);
			return { text: text.length > 24_000 ? `${text.slice(0, 24_000)}\n…(truncated; narrow the region)` : text };
		}
	});

	addImage({
		name: "vista_play",
		description:
			"Play exactly ONE action in the observed world (click, click in frame coordinates, key chord, typed text, move, scroll, drag), then archive the resulting turn and return the new observation image. Before calling, state what you expect to see; after seeing the image, state what changed.",
		parameters: toolParameters({
			run: RUN_PARAMETER,
			action: { type: "string", enum: ["click", "click_frame", "key", "type", "move", "scroll", "drag"], description: "The single action to execute." },
			x: { type: "integer", description: "Screen x for click/move." },
			y: { type: "integer", description: "Screen y for click/move." },
			key: { type: "string", description: "Key chord for action=key, e.g. enter or ctrl+s." },
			text: { type: "string", description: "Text for action=type." },
			amount: { type: "integer", description: "Wheel clicks for action=scroll (negative = down)." },
			fromX: { type: "integer", description: "Drag start x." },
			fromY: { type: "integer", description: "Drag start y." },
			toX: { type: "integer", description: "Drag end x." },
			toY: { type: "integer", description: "Drag end y." },
			frames: { type: "integer", description: "Frames archived after the action (default 3)." },
			interval: { type: "number", description: "Seconds between archived frames (default 0.12)." },
			settle: { type: "number", description: "Seconds to wait before capturing the result (default 0.18)." }
		}, ["action"]),
		outputSchema: IMAGE_OUTPUT_SCHEMA,
		async execute(args) {
			const argv = ["act"];
			if (args.run) argv.push("--run", args.run);
			switch (args.action) {
				case "click":
					requireNumber(args.x, "x");
					requireNumber(args.y, "y");
					argv.push("--click", `${args.x},${args.y}`);
					break;
				case "click_frame":
					requireNumber(args.x, "x");
					requireNumber(args.y, "y");
					argv.push("--click-frame", `${args.x},${args.y}`);
					break;
				case "key":
					if (!args.key) throw new Error("vista_play: action=key requires key");
					argv.push("--key", args.key);
					break;
				case "type":
					if (args.text === undefined) throw new Error("vista_play: action=type requires text");
					argv.push("--type", args.text);
					break;
				case "move":
					requireNumber(args.x, "x");
					requireNumber(args.y, "y");
					argv.push("--move", `${args.x},${args.y}`);
					break;
				case "scroll":
					requireNumber(args.amount, "amount");
					argv.push("--scroll", String(args.amount));
					break;
				case "drag":
					requireNumber(args.fromX, "fromX");
					requireNumber(args.fromY, "fromY");
					requireNumber(args.toX, "toX");
					requireNumber(args.toY, "toY");
					argv.push("--drag", `${args.fromX},${args.fromY}:${args.toX},${args.toY}`);
					break;
				default:
					throw new Error(`vista_play: unknown action ${String(args.action)}`);
			}
			argv.push("--frames", String(args.frames ?? 3), "--interval", String(args.interval ?? 0.12), "--settle", String(args.settle ?? 0.18));
			const result = await runEngine(config, argv);
			const action = result.action ?? {};
			return saveImage(attachments, result.observation_path, `Played ${action.type ?? args.action} ${JSON.stringify(action.params ?? {})}.\nTurn ${result.turn}; ${result.n_frames} frame(s) archived. State the visible changes, then revise your notes.`, {
				turn: result.turn,
				n_frames: result.n_frames,
				run: args.run
			});
		}
	});

	addText({
		name: "vista_notes",
		description:
			"Maintain the two VISTA notes: GUIDE.md (durable, revisable understanding that survives turns/levels) and WORKING.md (current state, prediction, scratch, handoff). Also saves a compaction checkpoint and prints the fresh-context handoff bundle. Use before a long run risks context compaction.",
		parameters: toolParameters({
			run: RUN_PARAMETER,
			action: { type: "string", enum: ["read", "write", "checkpoint", "compact"], description: "Note operation." },
			target: { type: "string", enum: ["guide", "working"], description: "Which note for read/write/checkpoint." },
			content: { type: "string", description: "Full replacement content for write/checkpoint." }
		}, ["action"]),
		async execute(args) {
			const argv = [];
			const target = args.target ?? "working";
			if (args.action === "read") argv.push(target === "guide" ? "read-guide" : "read-working");
			else if (args.action === "write") {
				if (args.content === undefined) throw new Error("vista_notes: action=write requires content");
				argv.push(target === "guide" ? "write-guide" : "write-working", "--text", args.content);
			} else if (args.action === "checkpoint") {
				if (args.content === undefined) throw new Error("vista_notes: action=checkpoint requires content");
				argv.push("checkpoint", target === "guide" ? "--guide-text" : "--working-text", args.content);
			} else if (args.action === "compact") argv.push("compact");
			else throw new Error(`vista_notes: unknown action ${String(args.action)}`);
			if (args.run) argv.push("--run", args.run);
			const result = await runEngine(config, argv);
			return { text: JSON.stringify(result, undefined, 2) };
		}
	});

	addText({
		name: "vista_history",
		description: "Read the harness records: exact action events, attempt summaries split at resets, or the archived frame index. Use it to recover what was already tried instead of repeating actions.",
		parameters: toolParameters({
			run: RUN_PARAMETER,
			view: { type: "string", enum: ["events", "attempts", "frames"], description: "Record view (default events)." },
			start: { type: "integer", description: "First turn (inclusive)." },
			end: { type: "integer", description: "Last turn (inclusive)." },
			limit: { type: "integer", description: "Maximum records (default 24)." }
		}),
		async execute(args) {
			const argv = ["history", "--view", args.view ?? "events", "--limit", String(args.limit ?? 24)];
			if (args.start !== undefined) argv.push("--start", String(args.start));
			if (args.end !== undefined) argv.push("--end", String(args.end));
			if (args.run) argv.push("--run", args.run);
			const result = await runEngine(config, argv);
			return { text: JSON.stringify(result) };
		}
	});

	addText({
		name: "vista_status",
		description: "Summarize the current VISTA run: capture source, archived turn/frame counts, current turn, last action, and run directory.",
		parameters: toolParameters({ run: RUN_PARAMETER }),
		async execute(args) {
			const argv = ["status"];
			if (args.run) argv.push("--run", args.run);
			const result = await runEngine(config, argv);
			return { text: JSON.stringify(result, undefined, 2) };
		}
	});

	return definitions;
}

function requireNumber(value, field) {
	if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`vista_play: ${field} must be a finite number`);
}

function parseRegion(region) {
	if (typeof region !== "string") throw new Error("region must be 'x,y,w,h'");
	const parts = region.split(",").map((piece) => Number(piece.trim()));
	if (parts.length !== 4 || parts.some((value) => !Number.isFinite(value))) throw new Error(`invalid region ${JSON.stringify(region)}`);
	return parts;
}
