/**
 * Builds the TVA card's model: the bench from task-verification-agent-example, a black four-port
 * manifold with three ports already capped and the fourth open, the blue block-off cap beside it,
 * its O-ring and a wrench. The cycle plays the SOP the agent checks (sop/oring.txt) as three moves,
 * each part going on top of the last:
 *
 *   1. The O-ring is set over the open port's nipple, down onto its seat.
 *   2. The blue cap is threaded onto the nipple over it by hand until snug.
 *   3. A wrench tightens the cap to the specified torque.
 *
 * (The SOP seats the O-ring in the cap first; seating it on the port instead keeps the cap the same
 * way up throughout, which reads far more clearly at this size.)
 * The tightening is scripted pose by pose: an open-end wrench pulls the cap round a sixth of a turn,
 * slides off the flats, swings back, slides on again and pulls once more. The cap and the wrench
 * play the same schedule, so the jaws never leave the flats while they turn.
 *
 *   node scripts/card-model/tva-cap.mjs --out <svg>                  writes the SVG (the console keeps it as
 *                                                                src/lib/components/agents/models/tva-cap.svg)
 *   node scripts/card-model/tva-cap.mjs --out <svg> --preview <html>  also a standalone page; ?t=<s> pauses it there
 *
 * World units are centimetres, roughly: z up, the manifold's ports in a row along x, the viewer
 * toward +x and +y.
 */
import process from 'node:process';
import {
	key,
	keyed as keyedAt,
	outArg,
	previewArg,
	turning as turningAt,
	writeModel,
	ZERO
} from './common.mjs';
import * as k from './kit/iso-kit.mjs';
import * as G from './kit/lathe.mjs';

const svgAt = outArg(process.argv.slice(2));
const previewAt = previewArg(process.argv.slice(2));

const WIDTH = 560;
const HEIGHT = 340;

// --- layout ------------------------------------------------------------------------------
const BENCH = { x: -13.5, y: -5, w: 26, d: 17.5, r: 0.6 };
const BENCH_T = 1.5;
const BLOCK = { x: -11, y: -3, w: 22, d: 6, r: 0.3 };
const BLOCK_H = 5;
const PORTS = [-7.5, -2.5, 2.5, 7.5];
const OPEN = PORTS[3];
const NIPPLE = { r: 1.1, h: 1.8 };
// The cap: a hex, its corners on this radius, threaded over the nipple.
const HEX_R = 1.9;
const CAP_H = 2.4;
const SEAT_Z = BLOCK_H + 0.4;
// The O-ring, sized to sit snug round the nipple's foot, on the seat.
const ORING = { R: NIPPLE.r + 0.24, r: 0.22 };
const RING_Z = SEAT_Z + ORING.r;
// Where the cap and the O-ring wait on the bench, and how high each is lifted to be carried.
const WAIT = [-1, 7.5];
const RING_WAIT = [4.5, 8.5];
const LIFT = 10;
const RING_LIFT = 9.5;
const THREAD_UP = 1.6;
const DEG = Math.PI / 180;

// --- the wrench ---------------------------------------------------------------------------
// A 19 mm open-end wrench: the jaws' heading when it engages (the handle runs 15° off
// it, as a real wrench's head is offset) and how far it slides back to clear the hex. Engaged, the
// handle points at the viewer, so the pull sweeps it across the front of the bench.
const WRENCH_AT = 60 * DEG;
const WRENCH_Z = SEAT_Z + 0.6;
const WRENCH_T = 0.6;
const FLAT = HEX_R * Math.cos(Math.PI / 6);
const JAW = FLAT + 0.05;
const HEAD_R = FLAT + 1.35;
const OFFSET = 15 * DEG;
// The handle: its width, and its length from the head's centre to the centre of its round end.
const WRENCH = { width: 1.6, length: 10 };
const CLEAR = HEAD_R + HEX_R + 0.4;
const PULL = 60 * DEG;
const STEP = 5 * DEG;
// Off the cap, the wrench is drawn more coarsely: it swings back in these steps and slides in quarters.
const SWING_STEP = 10 * DEG;
const SLIDE_STEP = CLEAR / 4;

// --- the cycle, in seconds ----------------------------------------------------------------
const PERIOD = 13;
const FPS = 20;
// One tightening: pull, slide off, swing back, slide on, pull again.
const MOVES = [
	{ s: 0.8, pull: [0, 1] },
	{ s: 0.35, slide: [0, 1], at: 1 },
	{ s: 0.6, swing: [1, 0] },
	{ s: 0.35, slide: [1, 0], at: 0 },
	{ s: 0.8, pull: [0, 1] }
];
const TIGHTEN_S = MOVES.reduce((sum, m) => sum + m.s, 0);
const T = {
	ringLift: [0.5, 1.0],
	ringCarry: [1.0, 2.0],
	ringDown: [2.0, 2.7],
	lift: [3.1, 3.7],
	carry: [3.7, 4.8],
	thread: [5.0, 6.6],
	wrenchIn: [6.9, 7.6],
	tighten: [7.7, 7.7 + TIGHTEN_S],
	wrenchOut: [7.9 + TIGHTEN_S, 8.5 + TIGHTEN_S],
	reset: [12.0, 12.6]
};

// --- camera -------------------------------------------------------------------------------
// Fit every cap's crown, the cap in the air, and the wrench's whole reach (engaged and slid back,
// at both ends of its pull), so nothing is cut off at the frame's edge.
const reach = (angle, slide) => {
	const d = [Math.cos(angle), Math.sin(angle)];
	const h = [Math.cos(angle + OFFSET), Math.sin(angle + OFFSET)];
	return [
		[
			OPEN + d[0] * slide + h[0] * (WRENCH.length + WRENCH.width),
			d[1] * slide + h[1] * (WRENCH.length + WRENCH.width),
			WRENCH_Z + WRENCH_T
		],
		[OPEN + d[0] * (slide - HEAD_R), d[1] * (slide - HEAD_R), WRENCH_Z]
	];
};
const P = k.fitProjection(
	[
		...k.boxCorners(BENCH, -BENCH_T, 0),
		...PORTS.map((x) => [x, 0, SEAT_Z + CAP_H + THREAD_UP]),
		[WAIT[0], WAIT[1], LIFT + CAP_H + 1],
		[RING_WAIT[0], RING_WAIT[1], RING_LIFT + 1],
		[OPEN, 0, RING_LIFT + 1],
		...[0, -PULL].flatMap((turn) => [
			...reach(WRENCH_AT + turn, 0),
			...reach(WRENCH_AT + turn, CLEAR)
		])
	],
	WIDTH,
	HEIGHT,
	{ pad: 10, azimuth: 36 }
);
const at = (p) => k.iso(p, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const origin = at([0, 0, 0]);
const screenOf = ([x, y, z]) => {
	const p = at([x, y, z]);
	return [+(p[0] - origin[0]).toFixed(2), +(p[1] - origin[1]).toFixed(2)];
};
const diff = (a, b) => {
	const [p, q] = [screenOf(a), screenOf(b)];
	return [+(p[0] - q[0]).toFixed(2), +(p[1] - q[1]).toFixed(2)];
};
const alongZ = (x, y, z) => G.frameOf([x, y, z], [0, 0, 1], [1, 0, 0], [0, 1, 0]);
// A keyed group starts drawn at its pose for t = 0, so the still (before the first frame, or under
// reduced motion) shows the opening of the cycle rather than every part at once.
const keyed = (keys, svg) => keyedAt(PERIOD, keys, svg);
const ease = (u) => u * u * (3 - 2 * u);

// --- the bench and the manifold -----------------------------------------------------------------
function bench() {
	return S(k.slabOf(BENCH, -BENCH_T, BENCH_T, P, 4), { tone: 'hi' });
}
function manifold() {
	const out = [S(k.slabOf(BLOCK, 0, BLOCK_H, P, 2), { tone: 'hi' })];
	// Anodised black: a darker band down the face toward us, and the mounting bolts on top.
	const face = [
		[BLOCK.x + 0.4, BLOCK.y + BLOCK.d, 0.4],
		[BLOCK.x + BLOCK.w - 0.4, BLOCK.y + BLOCK.d, 0.4],
		[BLOCK.x + BLOCK.w - 0.4, BLOCK.y + BLOCK.d, BLOCK_H - 0.4],
		[BLOCK.x + 0.4, BLOCK.y + BLOCK.d, BLOCK_H - 0.4]
	];
	out.push(`<path class="iso-shade" data-shade="1" d="${k.pathOf(face.map(at), true)}"/>`);
	const bolts = [BLOCK.x + 1.2, BLOCK.x + BLOCK.w - 1.2].flatMap((x) =>
		[BLOCK.y + 1, BLOCK.y + BLOCK.d - 1].map((y) => k.ring(x, y, 0.4, BLOCK_H, P, 14))
	);
	out.push(L(bolts.join(''), { tone: 'mid' }));
	return out.join('');
}
// Every port's seat: the spot-faced boss a cap screws down onto, the same under all four, and a
// little wider than the hex so it shows evenly round each cap.
const SEAT_R = HEX_R + 0.3;
function seat(x) {
	return S(G.disc(-0.1, SEAT_Z - BLOCK_H, SEAT_R, alongZ(x, 0, BLOCK_H), P), { tone: 'mid' });
}
// A port's threaded nipple, standing out of its seat.
function nipple(x) {
	const F = alongZ(x, 0, BLOCK_H);
	const out = [S(G.disc(0.4, NIPPLE.h + 0.4, NIPPLE.r, F, P), { tone: 'hi' })];
	const threads = [];
	for (let s = 0.7; s < NIPPLE.h + 0.2; s += 0.3) threads.push(G.arcOf(F, s, NIPPLE.r, P));
	out.push(L(threads.join(''), { tone: 'lo' }));
	// The bore, open at the top.
	out.push(
		`<path class="iso-shade" data-shade="0" d="${k.ring(x, 0, NIPPLE.r - 0.35, NIPPLE.h + 0.4 + BLOCK_H, P, 24)}"/>`
	);
	return out.join('');
}

// --- the cap ---------------------------------------------------------------------------------------
const hexAt = (angle) =>
	Array.from({ length: 6 }, (_, i) => [
		HEX_R * Math.cos(angle + (i * Math.PI) / 3),
		HEX_R * Math.sin(angle + (i * Math.PI) / 3)
	]);
// Screwed on: the hex, and its closed crown on top. The crown is round, so a cap that turns draws
// its hex at each angle and the crown once.
const capHex = (x, y, z, angle) =>
	S(G.prismOf(alongZ(x, y, z), hexAt(angle), 0, CAP_H - 0.5, P), { tone: 'hi' });
const capCrown = (x, y, z) =>
	S(G.disc(CAP_H - 0.5, CAP_H, HEX_R * 0.8, alongZ(x, y, z), P), { tone: 'hi' });
const capOn = (x, y, z, angle) => capHex(x, y, z, angle) + capCrown(x, y, z);
// The O-ring, lying flat round the nipple: a torus. The nipple stands through it, so it is drawn
// twice, clipped at its centre line on screen: the half beyond the nipple before it, the near half
// after. The clips sit inside the ring's moving group, so they travel with it.
function oring() {
	const n = 16;
	const meridian = Array.from({ length: n + 1 }, (_, i) => {
		const a = (i / n) * 2 * Math.PI;
		return [ORING.r * Math.sin(a), ORING.R + ORING.r * Math.cos(a)];
	});
	return S(G.lathe(meridian, alongZ(OPEN, 0, RING_Z), P, { steps: 48 }), { tone: 'hi' });
}
const RING_MID = +at([OPEN, 0, RING_Z])[1].toFixed(2);
const RING_CLIPS = `<defs><clipPath id="tva-oring-back"><rect x="-${WIDTH}" y="-${HEIGHT}" width="${3 * WIDTH}" height="${HEIGHT + RING_MID}"/></clipPath><clipPath id="tva-oring-front"><rect x="-${WIDTH}" y="${RING_MID}" width="${3 * WIDTH}" height="${2 * HEIGHT}"/></clipPath></defs>`;
const ringHalf = (half) => `<g clip-path="url(#tva-oring-${half})">${oring()}</g>`;

// --- the wrench ------------------------------------------------------------------------------------
// A plain open-end wrench, drawn as one piece: a single outline (the round head with its U-shaped
// mouth, and a straight handle with a round end) extruded to the wrench's thickness. The outline is
// worked out once in the jaws' own frame (`a` from the mouth toward the handle, `b` across it) and
// placed for each pose.
function wrenchProfile() {
	const arc = (cx, cy, r, from, to, steps) =>
		Array.from({ length: steps + 1 }, (_, i) => {
			const phi = from + ((to - from) * i) / steps;
			return [cx + r * Math.cos(phi), cy + r * Math.sin(phi)];
		});
	const h = [Math.cos(OFFSET), Math.sin(OFFSET)];
	const m = [-h[1], h[0]];
	const handle = (s, w) => [s * h[0] + w * m[0], s * h[1] + w * m[1]];
	const angleOf = ([x, y]) => Math.atan2(y, x);
	const w = WRENCH.width / 2;
	// Where the handle's sides leave the head's round.
	const neck = Math.sqrt(HEAD_R ** 2 - w ** 2);
	const end = handle(WRENCH.length, 0);
	const tip = Math.asin(JAW / HEAD_R);
	// The mouth: straight jaws on the flats, closing in a round throat clear of the hex's corner.
	const throat = HEX_R + 0.1 - JAW;
	const outer = [
		// From the lower jaw's tip, round the back of the head to the handle...
		...arc(0, 0, HEAD_R, -Math.PI + tip, angleOf(handle(neck, -w)), 8),
		// ...along the handle and round its end...
		...arc(end[0], end[1], w, OFFSET - Math.PI / 2, OFFSET + Math.PI / 2, 6),
		// ...back to the head and round to the upper jaw's tip...
		...arc(0, 0, HEAD_R, angleOf(handle(neck, w)), Math.PI - tip, 8),
		// ...then in along the upper jaw, round the throat and out along the lower jaw.
		[throat, JAW],
		...arc(throat, 0, JAW, Math.PI / 2, -Math.PI / 2, 8).slice(1, -1),
		[throat, -JAW]
	];
	return { outer };
}
const PROFILE = wrenchProfile();

// Signed area: positive when a ring runs anticlockwise (seen from above).
const areaOf = (ring) =>
	ring.reduce((sum, [x0, y0], i) => {
		const [x1, y1] = ring[(i + 1) % ring.length];
		return sum + x0 * y1 - x1 * y0;
	}, 0) / 2;
const wound = (ring, ccw) => (areaOf(ring) > 0 === ccw ? ring : [...ring].reverse());

// Keeps the part of a ring on one side of the line through the origin across `dir` (where
// dot(p, dir) * side >= 0). Each point carries whether the edge after it lies along that cut, so the
// cut is neither walled nor outlined: the two halves meet without a seam.
function halfOf(ring, dir, side) {
	const f = ([x, y]) => (x * dir[0] + y * dir[1]) * side;
	const out = [];
	ring.forEach((p, i) => {
		const q = ring[(i + 1) % ring.length];
		const [fp, fq] = [f(p), f(q)];
		if (fp >= 0) out.push({ p, cut: false });
		if (fp >= 0 !== fq >= 0) {
			const u = fp / (fp - fq);
			const x = [p[0] + (q[0] - p[0]) * u, p[1] + (q[1] - p[1]) * u];
			// Leaving the kept side, the edge from here to where the ring comes back runs along the cut.
			out.push({ p: x, cut: fp >= 0 });
		}
	});
	return out;
}

// A flat plate: the rings (an outer one anticlockwise, holes clockwise) extruded from z0 to z1 at
// (x0, y0), drawn as the kit draws a solid: an opaque body, the walls facing the viewer shaded by
// the light, the top over them, and the edges. Neighbouring walls are drawn as one strip and
// neighbouring edges as one line, which keeps a pose small.
function plateSvg(rings, x0, y0, z0, z1) {
	const V = G.viewOf(P);
	const Lt = G.lightOf(P);
	const lift = ([x, y], z) => at([x0 + x, y0 + y, z]);
	const body = [];
	const shades = [[], [], [], []];
	const lines = [];
	rings.forEach((ring, ri) => {
		const n = ring.length;
		const edges = ring.map(({ p, cut }, i) => {
			const q = ring[(i + 1) % n].p;
			const len = Math.hypot(q[0] - p[0], q[1] - p[1]) || 1;
			const normal = [(q[1] - p[1]) / len, -(q[0] - p[0]) / len];
			const seen = !cut && normal[0] * V[0] + normal[1] * V[1] > 1e-6;
			const band = Math.min(3, G.bandOf(normal[0] * Lt[0] + normal[1] * Lt[1]));
			return { p, q, cut, seen, band };
		});
		// Runs of consecutive edges that `keep` holds for, round the ring.
		const runs = (keep) => {
			const start = edges.findIndex((e) => !keep(e));
			if (start < 0) return [edges];
			const out = [];
			let run = [];
			for (let j = 1; j <= n; j++) {
				const e = edges[(start + j) % n];
				if (keep(e)) run.push(e);
				else if (run.length) out.push(run.splice(0));
			}
			if (run.length) out.push(run);
			return out;
		};
		const along = (run, z) => [run[0].p, ...run.map((e) => e.q)].map((pt) => lift(pt, z));
		const strip = (run) => k.pathOf([...along(run, z0), ...along(run, z1).reverse()], true);
		for (const run of runs((e) => !e.cut)) lines.push(k.pathOf(along(run, z1)));
		for (const run of runs((e) => e.seen)) {
			body.push(strip(run));
			lines.push(k.pathOf(along(run, z0)));
			let from = 0;
			run.forEach((e, j) => {
				if (j === run.length - 1 || run[j + 1].band !== e.band) {
					shades[e.band].push(strip(run.slice(from, j + 1)));
					from = j + 1;
				}
			});
		}
		// Upright edges where a wall comes into view or goes out of it, and, on the outer ring,
		// where it turns a real corner (not a facet of a curve).
		edges.forEach((e, i) => {
			const prev = edges[(i - 1 + n) % n];
			if (e.cut || prev.cut || !(e.seen || prev.seen)) return;
			const a = [prev.q[0] - prev.p[0], prev.q[1] - prev.p[1]];
			const b = [e.q[0] - e.p[0], e.q[1] - e.p[1]];
			const turn = (a[0] * b[0] + a[1] * b[1]) / (Math.hypot(...a) * Math.hypot(...b) || 1);
			const corner = ri === 0 && turn < Math.cos(0.6);
			if (corner || e.seen !== prev.seen) lines.push(k.pathOf([lift(e.p, z0), lift(e.p, z1)]));
		});
	});
	const top = rings
		.map((ring) =>
			k.pathOf(
				ring.map(({ p }) => lift(p, z1)),
				true
			)
		)
		.join('');
	return [
		body.length ? `<path class="iso-fill" d="${body.join('')}"/>` : '',
		...shades.map((list, band) =>
			list.length ? `<path class="iso-shade" data-shade="${band}" d="${list.join('')}"/>` : ''
		),
		`<path class="iso-fill" fill-rule="evenodd" d="${top}"/>`,
		`<path class="iso-top" fill-rule="evenodd" d="${top}"/>`,
		L(lines.join(''), { tone: 'hi' })
	].join('');
}

// The wrench at a pose: `angle` is the jaws' heading (from the mouth toward the handle) and `slide`
// how far it has slid back off the hex. It is one solid, split at the vertical plane through the
// cap's axis that faces the viewer: the half beyond it (`side` -1) is drawn before the cap, so the far
// jaw goes behind the hex, and the near half after.
function wrench(angle, slide, side) {
	const d = [Math.cos(angle), Math.sin(angle)];
	const n = [-d[1], d[0]];
	const place = ([a, b]) => [(a + slide) * d[0] + b * n[0], (a + slide) * d[1] + b * n[1]];
	const V = G.viewOf(P);
	const dir = [V[0], V[1]];
	const ring = halfOf(wound(PROFILE.outer.map(place), true), dir, side);
	return ring.length > 2 ? plateSvg([ring], OPEN, 0, WRENCH_Z, WRENCH_Z + WRENCH_T) : '';
}

// --- the moves ---------------------------------------------------------------------------------------
// Both parts are drawn where they end up and moved there from the bench. At the reset each fades
// out where it is and back in at its place on the bench.
const back = (home) => [
	key(T.reset[0], ZERO, 1),
	key(T.reset[0] + 0.3, ZERO, 0),
	key(T.reset[0] + 0.35, home, 0),
	key(T.reset[1], home, 1),
	key(PERIOD, home, 1)
];
// The O-ring is lifted off the bench, carried over the open port and lowered round the nipple.
const ringHome = diff([RING_WAIT[0], RING_WAIT[1], ORING.r], [OPEN, 0, RING_Z]);
const ringKeys = [
	key(0, ringHome, 1),
	key(T.ringLift[0], ringHome, 1),
	key(T.ringLift[1], diff([RING_WAIT[0], RING_WAIT[1], RING_LIFT], [OPEN, 0, RING_Z]), 1),
	key(T.ringCarry[1], diff([OPEN, 0, RING_LIFT], [OPEN, 0, RING_Z]), 1),
	key(T.ringDown[1], ZERO, 1),
	...back(ringHome)
];
// The cap, crown up throughout: lifted off the bench, carried over the port, and threaded down onto it.
const capHome = diff([WAIT[0], WAIT[1], 0], [OPEN, 0, SEAT_Z]);
const over = screenOf([0, 0, THREAD_UP]);
const capOnKeys = [
	key(0, capHome, 1),
	key(T.lift[0], capHome, 1),
	key(T.lift[1], diff([WAIT[0], WAIT[1], LIFT], [OPEN, 0, SEAT_Z]), 1),
	key(T.carry[1], over, 1),
	key(T.thread[0], over, 1),
	key(T.thread[1], ZERO, 1),
	...back(capHome)
];
// The cap's shadow while it is in the air: on the bench under it, then on the block's top over the port.
const shadowTo = diff([OPEN, 0, BLOCK_H], [WAIT[0], WAIT[1], 0]);
const shadowKeys = [
	key(0, ZERO, 0),
	key(T.lift[0], ZERO, 0),
	key(T.lift[1], ZERO, 1),
	key(T.carry[0], ZERO, 1),
	key(T.carry[1], shadowTo, 1),
	key(T.thread[0], shadowTo, 1),
	key(T.thread[0] + 0.4, shadowTo, 0),
	key(PERIOD, ZERO, 0)
];
function shadow() {
	return `<path class="iso-shade" data-shade="1" d="${k.ring(WAIT[0], WAIT[1], HEX_R + 0.2, 0, P, 32)}"/>`;
}

// The schedule, step by step at FPS. Turning clockwise seen from above (to tighten) is a falling
// angle. Threading by hand spins the cap a full turn on the way down; tightening follows MOVES.
// Angles are snapped to STEP so the cap's and the wrench's drawings can be shared between steps.
const snap = (a) => Math.round(a / STEP) * STEP;
// `seconds` of motion as eased progress, 0 to 1, one value per step.
const steps = (seconds) => {
	const n = Math.max(2, Math.round(seconds * FPS));
	return Array.from({ length: n }, (_, i) => ease(i / (n - 1)));
};
const threadPoses = steps(T.thread[1] - T.thread[0]).map((u) => ({
	cap: snap(WRENCH_AT + 2 * Math.PI * (1 - u))
}));
const tightenPoses = MOVES.flatMap((move) =>
	steps(move.s).map((u) => {
		const lerp = ([a, b]) => a + (b - a) * u;
		if (move.pull) {
			const angle = snap(WRENCH_AT - PULL * lerp(move.pull));
			return { cap: angle, wrench: angle, slide: 0 };
		}
		if (move.slide) {
			const slide = Math.round((CLEAR * lerp(move.slide)) / SLIDE_STEP) * SLIDE_STEP;
			return { cap: WRENCH_AT, wrench: snap(WRENCH_AT - PULL * move.at), slide };
		}
		const swing = Math.round((PULL * lerp(move.swing)) / SWING_STEP) * SWING_STEP;
		return { cap: WRENCH_AT, wrench: WRENCH_AT - swing, slide: CLEAR };
	})
);

// Each distinct drawing once; `order` plays them back. The hex repeats every 60°, so the cap only
// needs its angle within a sixth of a turn.
function sequence(poses, keyOf, draw) {
	const index = new Map();
	const frames = [];
	const order = poses.map((pose) => {
		const id = keyOf(pose);
		if (!index.has(id)) {
			index.set(id, frames.length);
			frames.push(draw(pose));
		}
		return index.get(id);
	});
	return { frames, order };
}
const sixth = (a) =>
	Math.round(((((a - WRENCH_AT) % PULL) + PULL) % PULL) / STEP) % Math.round(PULL / STEP);
const capSeq = sequence(
	[...threadPoses, ...tightenPoses],
	(pose) => sixth(pose.cap),
	(pose) => capHex(OPEN, 0, SEAT_Z, WRENCH_AT + sixth(pose.cap) * STEP)
);
const capOrder = [
	capSeq.order.slice(0, threadPoses.length),
	capSeq.order.slice(threadPoses.length)
];
const wrenchSeq = (side) =>
	sequence(
		tightenPoses,
		(pose) => `${Math.round(pose.wrench / STEP)}:${pose.slide.toFixed(2)}`,
		(pose) => wrench(pose.wrench, pose.slide, side)
	);

// The wrench slides on along its jaws before the first pull, and off them after the last.
const awayFrom = (angle) => screenOf([Math.cos(angle) * 7, Math.sin(angle) * 7, 0]);
const wrenchKeys = [
	key(0, awayFrom(WRENCH_AT), 0),
	key(T.wrenchIn[0], awayFrom(WRENCH_AT), 0),
	key(T.wrenchIn[1], ZERO, 1),
	key(T.wrenchOut[0], ZERO, 1),
	key(T.wrenchOut[1], awayFrom(WRENCH_AT - PULL), 0),
	key(PERIOD, awayFrom(WRENCH_AT), 0)
];
const wrenchPart = (side) => {
	const { frames, order } = wrenchSeq(side);
	return keyed(wrenchKeys, turningAt(PERIOD, [T.tighten], frames, FPS, [order]));
};

const items = [
	RING_CLIPS,
	bench(),
	manifold(),
	keyed(shadowKeys, shadow()),
	...PORTS.map(seat),
	...PORTS.slice(0, 3).map((x) => capOn(x, 0, SEAT_Z, WRENCH_AT)),
	keyed(ringKeys, ringHalf('back')),
	nipple(OPEN),
	keyed(ringKeys, ringHalf('front')),
	wrenchPart(-1),
	keyed(
		capOnKeys,
		turningAt(PERIOD, [T.thread, T.tighten], capSeq.frames, FPS, capOrder) +
			capCrown(OPEN, 0, SEAT_Z)
	),
	wrenchPart(1)
];

const LABEL =
	'A black four-port manifold on a bench, three ports capped and the fourth open: an O-ring is set over the open port, the blue cap is threaded on over it by hand, then an open-end wrench tightens it in two pulls.';
writeModel({
	out: svgAt,
	width: WIDTH,
	height: HEIGHT,
	label: LABEL,
	body: items.join(''),
	fig: 'TVA',
	title: 'Manifold cap',
	previewAt
});
