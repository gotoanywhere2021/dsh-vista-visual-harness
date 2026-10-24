/**
 * Offline smoke test for the VISTA host plugin: imports the real plugin module,
 * drives its tool definitions with a fake tool/attachment context, and runs the
 * real Python engine against a static fixture image (no GUI interaction).
 *
 *   node tests/smoke.mjs        (from the repository root)
 */

import { mkdtemp, rm, access } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { apply } from "../index.js";

const IMAGE = fileURLToPath(new URL("./fixtures/target-pattern.png", import.meta.url));

const problems = [];
function check(condition, message) {
	if (!condition) problems.push(message);
}

const registrations = [];
let attachmentCounter = 0;
const attachments = {
	async saveImage({ data, mediaType, name }) {
		attachmentCounter += 1;
		return { attachmentId: `att-test-${attachmentCounter}`, mediaType, bytes: data.length, width: 1, height: 1, name };
	}
};

const scope = {
	get(name) {
		return name === "attachments" ? attachments : undefined;
	},
	tools: {
		register(definition) {
			registrations.push(definition);
			return () => {};
		}
	}
};

const ctx = {
	inject(services, callback) {
		check(Array.isArray(services) && services.includes("attachments"), "apply() must inject the attachments service");
		callback(scope);
	}
};

const runsRoot = await mkdtemp(join(tmpdir(), "vista-plugin-"));
const dispose = apply(ctx, { runsRoot });

// ---- schema subset check (mirrors dsh-tools' supported keyword whitelist) ----
const ALLOWED = new Set(["type", "oneOf", "properties", "required", "additionalProperties", "items", "enum", "const", "description", "title"]);
function scanSchema(node, path) {
	if (node === null || typeof node !== "object") return;
	if (Array.isArray(node)) {
		node.forEach((child, index) => scanSchema(child, `${path}[${index}]`));
		return;
	}
	for (const key of Object.keys(node)) {
		if (!ALLOWED.has(key)) problems.push(`unsupported schema keyword ${path}.${key}`);
	}
	if (node.properties) for (const [key, value] of Object.entries(node.properties)) scanSchema(value, `${path}.properties.${key}`);
	if (node.items) scanSchema(node.items, `${path}.items`);
	if (node.oneOf) node.oneOf.forEach((value, index) => scanSchema(value, `${path}.oneOf[${index}]`));
}

check(registrations.length === 8, `expected the plugin to register its tools, got ${registrations.length}`);
const byName = new Map(registrations.map((definition) => [definition.name, definition]));
for (const [toolName, definition] of byName) {
	check(typeof definition.description === "string" && definition.description.length > 20, `${toolName}: missing description`);
	check(typeof definition.execute === "function", `${toolName}: missing execute`);
	check(definition.output && typeof definition.output.render === "function", `${toolName}: missing output.render`);
	scanSchema(definition.parameters, `${toolName}.parameters`);
	scanSchema(definition.output.schema, `${toolName}.output.schema`);
}

async function callTool(toolName, args) {
	const definition = byName.get(toolName);
	check(definition !== undefined, `missing tool ${toolName}`);
	if (!definition) return undefined;
	const value = await definition.execute(args, {});
	const content = definition.output.render(args, value);
	check(Array.isArray(content) && content.length > 0, `${toolName}: render returned no content`);
	return { value, content };
}

try {
	// 1. static start
	const start = await callTool("vista_start", { source: "image", image: IMAGE });
	await access(start.value.path);
	const imageBlock = start.content.find((block) => block.type === "image");
	check(imageBlock !== undefined, "vista_start: render must return an image block");
	check(typeof start.value.run === "string", "vista_start: value.run must identify the run");

	// 2. exact pixel read from the archived frame
	const pixels = await callTool("vista_read_pixels", {
		run: start.value.run,
		region: "0,0,288,288",
		rows: 8,
		cols: 8,
		question: "smoke"
	});
	const parsed = JSON.parse(pixels.value.text);
	check(Array.isArray(parsed.palette) && parsed.palette.length === 4, `read_pixels: expected 4 palette colours, got ${parsed.palette?.length}`);
	check(parsed.grid?.length === 8 && parsed.grid[0]?.length === 8, "read_pixels: expected an 8x8 grid");

	// 3. re-attention: crop + enlarge an old turn, as a montage
	const inspect = await callTool("vista_inspect", {
		run: start.value.run,
		question: "smoke",
		montage: true,
		views: [
			{ label: "full", turn: 0, region: [0, 0, 288, 288], zoom: 1 },
			{ label: "corner", turn: 0, region: [0, 0, 36, 36], zoom: 6 }
		]
	});
	await access(inspect.value.path);
	check(inspect.content.some((block) => block.type === "image"), "vista_inspect: render must return an image block");

	// 4. notes + history + status
	await callTool("vista_notes", { run: start.value.run, action: "write", target: "guide", content: "# guide\nsmoke" });
	const guide = await callTool("vista_notes", { run: start.value.run, action: "read", target: "guide" });
	check(JSON.parse(guide.value.text).content.includes("smoke"), "vista_notes: guide round-trip failed");
	const history = await callTool("vista_history", { run: start.value.run, limit: 2 });
	check(JSON.parse(history.value.text).view === "events", "vista_history: unexpected view");
	const status = await callTool("vista_status", { run: start.value.run });
	check(JSON.parse(status.value.text).frames >= 1, "vista_status: expected archived frames");

	// 5. static runs must refuse to act
	let refused = false;
	try {
		await callTool("vista_play", { run: start.value.run, action: "click", x: 1, y: 1 });
	} catch (error) {
		refused = /static/i.test(String(error?.message ?? error));
	}
	check(refused, "vista_play: static runs must refuse actions with a clear error");

	// 6. bad arguments must fail loudly
	let rejected = false;
	try {
		await callTool("vista_play", { run: start.value.run, action: "click" });
	} catch {
		rejected = true;
	}
	check(rejected, "vista_play: missing x/y must be rejected");
} finally {
	dispose();
	await rm(runsRoot, { recursive: true, force: true });
}

if (problems.length > 0) {
	console.error(`SMOKE FAIL (${problems.length})`);
	for (const problem of problems) console.error(` - ${problem}`);
	process.exit(1);
}
console.log(`SMOKE OK: ${registrations.length} tools, ${attachmentCounter} images attached, engine round-trips passed`);

