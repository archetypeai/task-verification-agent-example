<script lang="ts">
	// Steps × chunks: what every chunk said about every step. A chunk that passes
	// steps 1–4 while the video shows only step 4 stands out as a column of passes.
	import { Badge } from '@archetypeai/ds-ui-svelte-console/primitives/badge';
	import { cn } from '#lib/utils.js';
	import type { Evidence, StepResult } from '#lib/api/tva.js';

	let {
		class: className,
		steps,
		evidence,
		onSeek
	}: {
		class?: string;
		steps: StepResult[];
		evidence: Record<number, Evidence[]>;
		onSeek: (t: number) => void;
	} = $props();

	const chunkCount = $derived(evidence[1]?.length ?? 0);
	const tickEvery = $derived(Math.max(1, Math.ceil(chunkCount / 8)));

	const cellClass: Record<string, string> = {
		PASSED: 'bg-atai-good',
		FAILED: 'bg-atai-critical',
		MISSING: 'bg-muted',
		'NOT REPORTED': 'border border-dashed border-border',
		ERROR: 'border border-dashed border-atai-warning'
	};

	const fmt = (t: number) => `${Math.round(t)}`;

	function label(step: number, e: Evidence) {
		const span = `${fmt(e.offset)}–${fmt(e.offset + e.duration)} s`;
		return `Step ${step} · chunk ${e.chunk + 1} (${span}) · ${e.status}${e.reason ? `: ${e.reason}` : ''}`;
	}
</script>

<div class={cn('flex flex-col gap-2', className)}>
	<div
		class="grid items-center gap-x-0.5 gap-y-1"
		style:grid-template-columns={`auto repeat(${chunkCount}, minmax(0, 1fr))`}
		role="group"
		aria-label="What each chunk said about each step"
	>
		<span></span>
		{#each { length: chunkCount } as _, i (i)}
			<span class="truncate font-mono text-2xs text-muted-foreground">
				{#if i % tickEvery === 0}{fmt(evidence[1][i].offset)}s{/if}
			</span>
		{/each}

		{#each steps as s (s.step)}
			<Badge variant="step" size="sm" class="mr-2" aria-hidden="true">{s.step}</Badge>
			{#each evidence[s.step] ?? [] as e (e.chunk)}
				<button
					type="button"
					class={cn(
						'h-5 min-w-0 rounded-sm outline-offset-1 focus-visible:outline-2 focus-visible:outline-ring',
						cellClass[e.status] ?? 'bg-muted',
						(s.run_chunks ? s.run_chunks.includes(e.chunk) : s.decided_by_chunk === e.chunk) &&
							'ring-2 ring-foreground ring-inset'
					)}
					title={label(s.step, e)}
					aria-label={label(s.step, e)}
					onclick={() => onSeek(e.timestamp_start ?? e.offset)}
				></button>
			{/each}
		{/each}
	</div>

	<div class="flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
		<span class="flex items-center gap-1.5">
			<span class="size-3 rounded-sm bg-atai-good"></span>Passed
		</span>
		<span class="flex items-center gap-1.5">
			<span class="size-3 rounded-sm bg-atai-critical"></span>Failed
		</span>
		<span class="flex items-center gap-1.5">
			<span class="size-3 rounded-sm bg-muted"></span>Missing
		</span>
		<span class="flex items-center gap-1.5">
			<span class="size-3 rounded-sm border border-dashed border-border"></span>Not reported
		</span>
		<span class="flex items-center gap-1.5">
			<span class="size-3 rounded-sm ring-2 ring-foreground ring-inset"></span>Sets the step's time
		</span>
		<span>Hover a cell for that chunk's reason; click to play from there.</span>
	</div>
</div>
