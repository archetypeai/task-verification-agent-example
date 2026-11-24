/**
 * What every demo-card model build shares: the SMIL that runs each model inside its SVG,
 * the map from the kit's classes to the console's token utilities, and writing the SVG (plus an
 * optional standalone preview page, where `?t=<s>` freezes the cycle).
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as k from './kit/iso-kit.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));

export const attr = (value) => JSON.stringify(value).replace(/"/g, '&quot;');

/** A keyframe: `[t, dx, dy]`, or `[t, dx, dy, opacity]` for a part that fades. */
export const key = (t, off, o) => (o === undefined ? [t, ...off] : [t, ...off, o]);
export const ZERO = [0, 0];

// The motion is SMIL inside the SVG, so the page needs no script to run it: every animation loops
// over its model's cycle of `period` seconds from the SVG's time 0. DemoModel only pauses the SVG
// (offscreen, prefers-reduced-motion), and a paused SVG at time 0 shows each part at its first key.

const num = (n) => String(+n.toFixed(3));
const fraction = (t, period) => String(+(t / period).toFixed(6));
// Smoothstep (3u² − 2u³) is exactly the cubic Bézier with control points at 1/3 and 2/3.
const SMOOTH = '0.3333 0 0.6667 1';
const loop = (period) => `dur="${period}s" repeatCount="indefinite"`;

/**
 * `keys` spread over the whole cycle: held from 0 up to the first key and from the last key to the
 * end, with no two keys at the same time. Two keys at one time are a jump, which SMIL needs as a
 * very short step, so the later one moves a millisecond on (or the earlier one back, at the end).
 */
function spread(period, keys) {
	const ks = keys.map((k) => [...k]);
	if (ks[0][0] > 0) ks.unshift([0, ...ks[0].slice(1)]);
	if (ks[ks.length - 1][0] < period) ks.push([period, ...ks[ks.length - 1].slice(1)]);
	const STEP = 0.001;
	for (let i = 1; i < ks.length; i++) if (ks[i][0] <= ks[i - 1][0]) ks[i][0] = ks[i - 1][0] + STEP;
	ks[ks.length - 1][0] = Math.min(ks[ks.length - 1][0], period);
	for (let i = ks.length - 2; i >= 0; i--)
		if (ks[i][0] >= ks[i + 1][0]) ks[i][0] = ks[i + 1][0] - STEP;
	return ks;
}

/**
 * The SMIL for `keys` (`[t, dx, dy]`, or `[t, dx, dy, opacity]`): a translate, and a fade where the
 * keys carry opacity, each eased between keys. Goes inside the element it moves.
 */
export function keyAnimation(period, keys) {
	const ks = spread(period, keys);
	const timing = `keyTimes="${ks.map(([t]) => fraction(t, period)).join(';')}" calcMode="spline" keySplines="${Array(
		ks.length - 1
	)
		.fill(SMOOTH)
		.join(';')}" ${loop(period)}`;
	let out = '';
	if (ks.some(([, dx, dy]) => dx || dy))
		out += `<animateTransform attributeName="transform" type="translate" values="${ks
			.map(([, dx, dy]) => `${num(dx)} ${num(dy)}`)
			.join(';')}" ${timing}/>`;
	if (ks[0].length > 3)
		out += `<animate attributeName="opacity" values="${ks.map((k) => num(k[3])).join(';')}" ${timing}/>`;
	return out;
}

/**
 * A group that follows `keys` round the cycle. It is written at its first key's offset and opacity,
 * so it is right at time 0 and wherever the SVG is paused there: a part that waits hidden stays hidden.
 */
export const keyed = (period, keys, svg) => {
	const [, dx, dy, o] = keys[0];
	const pose = `${dx || dy ? ` transform="translate(${num(dx)} ${num(dy)})"` : ''}${o === undefined ? '' : ` opacity="${num(o)}"`}`;
	return `<g${pose}>${keyAnimation(period, keys)}${svg}</g>`;
};

/** Which frame a turning part shows at `at` seconds into its cycle, or -1 to leave it as it is. */
function turnFrame({ spans, fps, order }, at, count) {
	const live = spans.findIndex(([a, b]) => at >= a && at <= b);
	if (!order) return live < 0 ? -1 : Math.floor(at * fps + 1e-9) % count;
	if (live >= 0) {
		const seq = order[live];
		return seq[Math.min(seq.length - 1, Math.floor((at - spans[live][0]) * fps + 1e-9))];
	}
	let past = -1;
	spans.forEach(([, b], i) => b < at && (past = i));
	return past < 0 ? order[0][0] : order[past][order[past].length - 1];
}

/**
 * Which frame shows when, over one cycle: `[[t, frame], …]` from t = 0, one entry per change. A
 * part outside its spans holds the frame it last showed, carried over from the cycle before.
 */
function turnRuns(period, part, count) {
	const { spans, fps, order } = part;
	const cuts = new Set([0, period]);
	for (let i = 1; i < period * fps; i++) cuts.add(i / fps);
	for (const [a, b] of spans) {
		cuts.add(a);
		cuts.add(b);
		if (order) for (let t = a; t < b; t += 1 / fps) cuts.add(t);
	}
	const times = [...cuts].filter((t) => t >= 0 && t <= period).sort((x, y) => x - y);
	const states = times
		.slice(0, -1)
		.map((t, i) => [t, turnFrame(part, (t + times[i + 1]) / 2, count)]);
	let held = [...states].reverse().find(([, n]) => n >= 0)?.[1] ?? (order ? order[0][0] : 0);
	const runs = [];
	for (const [t, n] of states) {
		if (n >= 0) held = n;
		if (!runs.length || runs[runs.length - 1][1] !== held) runs.push([t, held]);
	}
	return runs;
}

/**
 * A part drawn at a few phases (`frames`), stepped through at `fps` while a span is on. With
 * `order` (one list of frame indexes per span) it plays that sequence once per span instead of
 * looping, holding the span's last frame after it and the first span's first frame before them all,
 * so a motion can be scripted step by step and reuse a drawing wherever the same pose comes back.
 * Each frame switches on and off with a discrete `display` animation.
 */
export const turning = (period, spans, frames, fps = 14, order) => {
	const runs = turnRuns(period, { spans, fps, order }, frames.length);
	return `<g>${frames
		.map((frame, i) => {
			const shows = runs.map(([, n]) => n === i);
			const anim = shows.some((s) => s !== shows[0])
				? `<animate attributeName="display" values="${shows.map((s) => (s ? 'inline' : 'none')).join(';')}" keyTimes="${runs
						.map(([t]) => fraction(t, period))
						.join(';')}" calcMode="discrete" ${loop(period)}/>`
				: '';
			return `<g${shows[0] ? '' : ' display="none"'}>${anim}${frame}</g>`;
		})
		.join('')}</g>`;
};

// The kit's classes in the console's token utilities (the same map as the AD rig's export).
const CLASS = {
	fill: 'fill-card',
	top: 'fill-card dark:fill-foreground/15',
	shade: [
		'fill-foreground/15 dark:fill-background/60',
		'fill-foreground/10 dark:fill-background/20',
		'fill-foreground/5 dark:fill-foreground/5',
		'fill-transparent dark:fill-foreground/10'
	],
	line: {
		hi: 'stroke-foreground/90',
		mid: 'stroke-foreground/50',
		lo: 'stroke-foreground/30',
		faint: 'stroke-foreground/15'
	},
	dots: {
		hi: 'fill-foreground/80',
		mid: 'fill-foreground/40',
		lo: 'fill-foreground/20',
		faint: 'fill-foreground/10'
	},
	tube: 'fill-card',
	shine: 'fill-card dark:fill-foreground/15',
	tubeShade: 'fill-foreground/15 dark:fill-background/60'
};
function tokens(markup, lines = CLASS.line) {
	return markup
		.replace(/<path class="iso-halo"[^>]*\/>/g, '')
		.replace(/class="iso-fill"/g, `class="${CLASS.fill}"`)
		.replace(/class="iso-top"/g, `class="${CLASS.top}"`)
		.replace(/class="iso-shade" data-shade="(\d)"/g, (_, n) => `class="${CLASS.shade[+n]}"`)
		.replace(
			/class="iso-line[^"]*" data-tone="(\w+)"( data-free="[^"]*")?/g,
			(_, t) => `class="fill-none ${lines[t]}"`
		)
		.replace(/ data-dashed=""/g, ' stroke-dasharray="3 3"')
		.replace(/ data-dotted=""/g, ' stroke-dasharray="0.01 3.5"')
		.replace(/class="iso-dots" data-tone="(\w+)"/g, (_, t) => `class="${CLASS.dots[t]}"`)
		.replace(/class="tb-body"/g, `class="${CLASS.tube}"`)
		.replace(/class="tb-shine"/g, `class="${CLASS.shine}"`)
		.replace(/class="tb-shade"/g, `class="${CLASS.tubeShade}"`)
		.replace(/ class="(iso-solid|tb)"( data-seamless="")?/g, '');
}

// The preview runs the SVG's own SMIL; `?t=<s>` pauses it at that point of the cycle.
const PLAY = `
const fixed = new URLSearchParams(location.search).get('t')
const svg = document.querySelector('svg')
svg.setCurrentTime(fixed != null ? +fixed : 0)
if (fixed != null) svg.pauseAnimations()`;

/**
 * Writes the SVG to `out` from the kit markup in `body`, and with `previewAt` a standalone page
 * too. A model drawn lighter than the rest passes its own `lines` (the line
 * tones, as in `CLASS.line`) and `strokeWidth`.
 */
export function writeModel({
	out,
	width,
	height,
	label,
	body,
	fig,
	title,
	previewAt,
	lines,
	strokeWidth = 1.1
}) {
	const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="${label}">${tokens(body, lines)}</svg>`;
	writeFileSync(out, svg + '\n');
	console.log('wrote', out, `${(svg.length / 1024).toFixed(0)} KB`);
	if (!previewAt) return;
	const css = readFileSync(join(HERE, 'kit/iso.css'), 'utf8');
	const page = `<!doctype html><meta charset="utf-8"><style>${css} body{margin:0;padding:24px;background:var(--anatomy-page)} .iso-plate{max-width:640px;margin:auto}</style><body class="iso" data-theme="light">${k.plateHtml({ fig, title, hint: '', readout: '', keys: [], caption: '', body: k.figureSvg({ width, height, label, body }) })}<script>${PLAY}</script></body>`;
	writeFileSync(previewAt, page);
	console.log('wrote', previewAt);
}

/** The path after `flag` on the command line, or null when the flag isn't there. */
function pathArg(argv, flag) {
	if (!argv.includes(flag)) return null;
	const path = argv[argv.indexOf(flag) + 1];
	if (!path?.match(/^[^-]/)) throw new Error(`${flag} needs a path`);
	return path;
}
/** `--out <path>`: where the SVG goes (the console's `src/lib/components/agents/models/<name>.svg`). */
export const outArg = (argv) => {
	const path = pathArg(argv, '--out');
	if (!path) throw new Error('usage: node <script> --out <svg path> [--preview <html path>]');
	return path;
};
/** `--preview <path>` from the command line, if given. */
export const previewArg = (argv) => pathArg(argv, '--preview');
