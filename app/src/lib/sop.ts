// Turn a pasted or loaded SOP (.txt or .md) into TVA's format: one step per line,
// numbered "Step N: …". The line breaks are the step boundaries, so a wrapped line
// must be joined here or it becomes two steps.

const LIST_ITEM = /^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$/;
const width = (s: string) => s.replace(/\t/g, '    ').length;

function stripMarkup(text: string): string[] {
	const out: string[] = [];
	let fence = false;
	let front = false;
	text
		.replace(/\r/g, '')
		.split('\n')
		.forEach((line, i) => {
			if (i === 0 && line.trim() === '---') return void (front = true);
			if (front) return void (line.trim() === '---' && (front = false));
			if (/^\s*```/.test(line)) return void (fence = !fence);
			if (!fence) out.push(line.replace(/<!--.*?-->/g, ''));
		});
	return out;
}

/** A Markdown list wins over prose: only its top-level items become steps. */
export function parseSop(text: string): string[] {
	const lines = stripMarkup(text);
	const items = lines.map((l) => l.match(LIST_ITEM)).filter((m) => m !== null);
	let steps: string[] = [];
	if (items.length) {
		const indent = Math.min(...items.map((m) => width(m[1])));
		let open = false;
		for (const line of lines) {
			const m = line.match(LIST_ITEM);
			if (m) {
				open = width(m[1]) === indent;
				if (open) steps.push(m[2]);
			} else if (!line.trim()) {
				open = false;
			} else if (open && !/^\s*#/.test(line)) {
				steps[steps.length - 1] += ' ' + line.trim();
			}
		}
	} else {
		steps = lines.filter(
			(l) => l.trim() && !/^\s*#/.test(l) && !/^\s*([-*_]\s*){3,}$/.test(l) && !/^\s*>/.test(l)
		);
	}
	return steps
		.map((s) =>
			s
				.replace(/^\[[ xX]\]\s*/, '')
				.replace(/\*\*|__|`/g, '')
				.replace(/^\s*step\s*\d+\s*[:.)-]\s*/i, '')
				.replace(/\s+/g, ' ')
				.trim()
		)
		.filter(Boolean)
		.map((s, i) => `Step ${i + 1}: ${s}`);
}

export const stepText = (step: string) => step.replace(/^Step \d+: /, '');
