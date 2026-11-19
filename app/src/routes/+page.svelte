<script lang="ts">
	import Menubar from '@archetypeai/ds-ui-svelte-labs/primitives/menubar';
	import VideoPlayer from '@archetypeai/ds-ui-svelte-labs/primitives/video-player';
	import * as Card from '@archetypeai/ds-ui-svelte-console/primitives/card';
	import * as Alert from '@archetypeai/ds-ui-svelte-console/primitives/alert';
	import * as Collapsible from '@archetypeai/ds-ui-svelte-console/primitives/collapsible';
	import * as DropZone from '@archetypeai/ds-ui-svelte-console/primitives/dropzone';
	import { Button } from '@archetypeai/ds-ui-svelte-console/primitives/button';
	import { Badge } from '@archetypeai/ds-ui-svelte-console/primitives/badge';
	import { Input } from '@archetypeai/ds-ui-svelte-console/primitives/input';
	import { Label } from '@archetypeai/ds-ui-svelte-console/primitives/label';
	import { Textarea } from '@archetypeai/ds-ui-svelte-console/primitives/textarea';
	import { Spinner } from '@archetypeai/ds-ui-svelte-console/primitives/spinner';
	import { EmptyState } from '@archetypeai/ds-ui-svelte-console/primitives/empty-state';
	import { Progress } from '@archetypeai/ds-ui-svelte-console/primitives/progress';
	import * as Tabs from '@archetypeai/ds-ui-svelte-console/primitives/tabs';
	import { toast } from 'svelte-sonner';
	import UploadIcon from '@lucide/svelte/icons/upload';
	import FileTextIcon from '@lucide/svelte/icons/file-text';
	import { cn } from '#lib/utils.js';
	import * as tva from '#lib/api/tva.js';
	import { parseSop, stepText } from '#lib/sop.js';
	import { EXAMPLES } from '#lib/examples.js';
	import ChunkGrid from '#lib/components/ui/custom/chunk-grid.svelte';

	// --- API key and endpoint: entered at runtime, held in $state, never read from .env ---
	let apiKey = $state('');
	let endpoint = $state('');
	let config = $state<tva.Config | null>(null);
	let savingKey = $state(false);
	const endpointChanged = $derived(
		!!endpoint.trim() && endpoint.trim().replace(/\/+$/, '') !== config?.endpoint
	);

	async function saveKey(e: SubmitEvent) {
		e.preventDefault();
		savingKey = true;
		try {
			config = await tva.setConfig(apiKey.trim(), endpoint.trim());
			apiKey = '';
			endpoint = config.endpoint;
			toast.success('Connected', { description: config.endpoint });
			refreshAgent();
		} catch (err) {
			toast.error('Not connected', { description: (err as Error).message });
		} finally {
			savingKey = false;
		}
	}

	// --- agent ---
	let agent = $state<tva.Agent | null>(null);
	let values = $state<tva.AgentValues>({ max_frames: 64, size: 224, max_new_tokens: 5760 });
	let detailsOpen = $state(false);
	const status = $derived(agent?.status ?? 'stopped');
	const running = $derived(status === 'running');

	async function refreshAgent() {
		try {
			agent = await tva.getAgent();
			if (agent.status === 'starting') detailsOpen = true;
		} catch {
			/* server restarting; next poll retries */
		}
	}

	async function start() {
		try {
			agent = await tva.startAgent(values);
			detailsOpen = true;
		} catch (err) {
			toast.error('Could not start the agent', { description: (err as Error).message });
		}
	}

	async function stop() {
		if (!confirm('Stop the agent? Starting it again takes another 10–12 minutes.')) return;
		await tva.stopAgent().catch(() => {});
		refreshAgent();
	}

	$effect(() => {
		tva.getConfig().then((c) => {
			config = c;
			endpoint = c.endpoint;
		});
		refreshAgent();
		const id = setInterval(refreshAgent, 3000);
		return () => clearInterval(id);
	});

	const fmtDur = (s: number) => {
		s = Math.max(0, Math.round(s));
		const m = Math.floor(s / 60);
		return m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
	};

	const statusClass: Record<string, string> = {
		running: 'text-atai-good',
		starting: 'text-atai-neutral',
		stopping: 'text-atai-warning',
		error: 'text-atai-critical',
		stopped: 'text-muted-foreground'
	};

	// --- video ---
	let videoUrl = $state('');
	let upload = $state<tva.Upload | null>(null);
	let uploading = $state(false);
	let time = $state(0);
	let paused = $state(true);
	let duration = $state(0);

	async function pickVideo(files: File[]) {
		const file = files[0];
		if (!file) return;
		// Earlier results keep their own blob URL, so it is not revoked here.
		videoUrl = URL.createObjectURL(file);
		time = 0;
		paused = true;
		duration = 0;
		upload = null;
		uploading = true;
		try {
			upload = await tva.uploadVideo(file);
		} catch (err) {
			toast.error('Upload failed', { description: (err as Error).message });
		} finally {
			uploading = false;
		}
	}

	let videoInput: HTMLInputElement;

	// --- SOP ---
	let sopText = $state(load('tva.sop'));
	const steps = $derived(parseSop(sopText));
	let sopInput: HTMLInputElement;

	function load(key: string) {
		try {
			return localStorage.getItem(key) ?? '';
		} catch {
			return '';
		}
	}
	$effect(() => {
		try {
			localStorage.setItem('tva.sop', sopText);
		} catch {
			/* storage unavailable */
		}
	});

	async function loadSop(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		if (file) sopText = await file.text();
		input.value = '';
	}

	// --- verify ---
	let verifying = $state(false);
	let history = $state<tva.Verification[]>([]);
	let current = $state<tva.Verification | null>(null);

	const blocker = $derived(
		!config?.key_set
			? 'Enter an API key in the top bar.'
			: !running
				? status === 'starting'
					? 'Waiting for the agent to finish loading.'
					: 'Start the agent first.'
				: uploading
					? 'Uploading the video.'
					: !upload
						? 'Choose a video.'
						: !steps.length
							? 'Add at least one SOP step.'
							: null
	);

	// Whole video: one query. Long video: one query per chunk, merged per step.
	let mode = $state<'whole' | 'chunked'>('whole');
	let chunkSeconds = $state(20);
	let overlap = $state(0);
	// One query per step and chunk (the default): no list for the model to replay from
	// step 1 or to renumber, at the cost of steps × chunks queries. On the gel example
	// it was the only setting that got the last steps right.
	let perStep = $state(true);
	// Measured on production, 2026-11-18: a one-line SOP gets a much shorter answer.
	const secondsPerQuery = $derived(perStep ? 2 : 5);
	let job = $state<tva.Job | null>(null);
	const chunkCount = $derived(tva.chunkCount(duration, chunkSeconds, overlap));
	const queryCount = $derived(chunkCount * (perStep ? steps.length : 1));
	const chunkError = $derived(
		chunkSeconds < 4 || chunkSeconds > 120
			? 'Chunk length must be 4–120 s.'
			: overlap < 0 || overlap >= chunkSeconds
				? 'Overlap must be at least 0 and shorter than the chunk.'
				: null
	);
	// The 2 MiB request cap fits only a minute or two of video; suggest chunking past that.
	const looksLong = $derived(duration > 90);

	let verifyStartedAt = $state(0);
	let verifyNow = $state(0);

	async function runVerify() {
		if (!upload) return;
		verifying = true;
		verifyStartedAt = verifyNow = Date.now();
		const forVideo = videoUrl;
		try {
			const options: tva.VerifyOptions =
				mode === 'chunked'
					? { mode, chunk_seconds: chunkSeconds, overlap, per_step: perStep }
					: { mode: 'whole' };
			job = await tva.startVerify(upload, steps, options);
			while (job.status === 'running') {
				await new Promise((r) => setTimeout(r, 1000));
				job = await tva.getJob(job.id);
				verifyNow = Date.now();
			}
			if (job.status === 'done' && job.result) {
				const r = { ...job.result, videoUrl: forVideo };
				history = [r, ...history];
				refreshRuns();
				current = r;
				if (r.failed_chunks?.length)
					toast.warning(`${r.failed_chunks.length} chunk(s) got no answer`, {
						description: 'They were left out of the merge.'
					});
			} else if (job.status === 'error') {
				toast.error('Verification failed', { description: job.error ?? '' });
			}
		} catch (err) {
			toast.error('Verification failed', { description: (err as Error).message });
		} finally {
			verifying = false;
			job = null;
			refreshAgent();
		}
	}

	async function cancelVerify() {
		if (job) await tva.cancelJob(job.id).catch(() => {});
	}

	// Results saved by the server, so a reload or another tab can reopen them.
	let savedRuns = $state<tva.SavedRun[]>([]);
	const refreshRuns = () =>
		tva
			.listRuns()
			.then((r) => (savedRuns = r))
			.catch(() => {});

	async function openRun(name: string) {
		try {
			current = await tva.getRun(name);
			const url = new URL(location.href);
			url.searchParams.set('run', name);
			window.history.replaceState(null, '', url);
		} catch (err) {
			toast.error('Could not open the run', { description: (err as Error).message });
		}
	}

	$effect(() => {
		refreshRuns();
		const name = new URL(location.href).searchParams.get('run');
		if (name) openRun(name);
	});

	function setRule(rule: string) {
		if (!current?.merged) return;
		current.rule = rule as tva.MergeRule;
		current.steps = current.merged[rule as tva.MergeRule];
	}

	function show(r: tva.Verification) {
		current = r;
		if (r.videoUrl && r.videoUrl !== videoUrl) {
			videoUrl = r.videoUrl;
			time = 0;
			paused = true;
		}
	}

	const verdictClass: Record<string, string> = {
		PASSED: 'text-atai-good',
		FAILED: 'text-atai-critical',
		MISSING: 'text-atai-warning',
		'NOT REPORTED': 'text-muted-foreground'
	};

	const markers = $derived(
		(current && (!current.videoUrl || current.videoUrl === videoUrl) ? current.steps : [])
			.filter((s) => s.timestamp_start != null && s.timestamp_end != null)
			.map((s) => ({
				start: s.timestamp_start!,
				end: s.timestamp_end!,
				label: `Step ${s.step} · ${s.status}`
			}))
	);

	const summary = $derived.by(() => {
		if (!current) return '';
		const counts: Record<string, number> = {};
		current.steps.forEach((s) => (counts[s.status] = (counts[s.status] ?? 0) + 1));
		return Object.entries(counts)
			.map(([k, v]) => `${v} ${k.toLowerCase()}`)
			.join(' · ');
	});

	function seek(t: number) {
		time = t;
		paused = false;
	}
</script>

<div class="flex min-h-screen flex-col bg-background text-foreground">
	<Menubar>
		<form class="flex items-center gap-2" onsubmit={saveKey}>
			<Input
				size="sm"
				class="w-96 font-mono"
				autocomplete="off"
				spellcheck="false"
				aria-label="API endpoint"
				title="API endpoint. Keys only work on the deployment that issued them."
				placeholder={config?.default_endpoint ?? 'API endpoint'}
				bind:value={endpoint}
				disabled={status !== 'stopped' && status !== 'error'}
			/>
			<Input
				type="password"
				size="sm"
				class="w-56"
				autocomplete="off"
				aria-label="Archetype AI API key"
				placeholder={config?.key_set
					? `Key ${config.key_hint}${config.key_source && config.key_source !== 'app' ? ` (${config.key_source})` : ''}`
					: 'Paste your API key'}
				title={config?.key_source && config.key_source !== 'app'
					? `Using the key from ${config.key_source}. Paste a key to override it for this session.`
					: 'Paste your API key, or set ATAI_API_KEY in app/.env or .env'}
				bind:value={apiKey}
				disabled={status !== 'stopped' && status !== 'error'}
			/>
			<Button
				type="submit"
				variant="outline"
				size="sm"
				loading={savingKey}
				disabled={!apiKey.trim() && !endpointChanged}
			>
				Set key
			</Button>
		</form>
	</Menubar>

	<main class="mx-auto flex w-full max-w-6xl flex-col gap-4 p-lg">
		<!-- Agent -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Agent</Card.Title>
				<Card.Description>
					Task Verification in serving: the model loads once (about 10–12 minutes), then each video
					takes a few seconds.
				</Card.Description>
				<Card.Action>
					<Badge variant="status" class={statusClass[status]}>{status}</Badge>
				</Card.Action>
			</Card.Header>
			<Card.Content class="flex flex-col gap-3">
				<div class="flex flex-wrap items-center gap-3">
					{#if status === 'stopped' || status === 'error'}
						<Button onclick={start} disabled={!config?.key_set}>Start agent</Button>
					{:else}
						<Button variant="outline" onclick={stop} disabled={status === 'stopping'}>
							{status === 'starting' ? 'Cancel' : 'Stop agent'}
						</Button>
					{/if}
					<p class="text-sm text-muted-foreground">
						{#if !config?.key_set}
							Enter an API key in the top bar, or set <span class="font-mono">ATAI_API_KEY</span> in
							<span class="font-mono">app/.env</span> and restart the server.
						{:else if agent && status === 'starting' && agent.started_at}
							{#if agent.phase === 'loading' || agent.session}
								Session open; waiting for it to report running.
							{:else if agent.phase === 'placing' || agent.events.some( (e) => / deployment dpl_/.test(e.message) )}
								Deploying: the platform places the deployment on a GPU, then downloads and loads the
								model weights. About 10–12 minutes, with no progress reported; the activity log
								shows its status once a minute.
							{:else}
								Preparing the project, blueprint copy and package.
							{/if}
							<span class="font-mono">{fmtDur(agent.now - agent.started_at)}</span> elapsed.
						{:else if agent && running && agent.last_query_at}
							Ready. Idle <span class="font-mono">{fmtDur(agent.now - agent.last_query_at)}</span>;
							the platform closes the session after 30 idle minutes. Stop it when you're done, since
							it holds a GPU.
						{:else if status === 'stopping'}
							Stopping the session and deployment.
						{:else}
							Endpoint <span class="font-mono">{config.endpoint}</span>
							{#if config.key_source && config.key_source !== 'app'}
								· key from <span class="font-mono">{config.key_source}</span>
							{/if}
						{/if}
					</p>
				</div>

				{#if agent?.error}
					<Alert.Root variant="danger">
						<Alert.Title>The agent stopped</Alert.Title>
						<Alert.Description>{agent.error}</Alert.Description>
					</Alert.Root>
				{/if}

				<Collapsible.Root bind:open={detailsOpen}>
					<Collapsible.Trigger chevron="start">
						<Label variant="sectionHeader">Settings and activity</Label>
					</Collapsible.Trigger>
					<Collapsible.Content class="flex flex-col gap-3 pt-3">
						<div class="grid max-w-xl grid-cols-3 gap-3">
							<div class="flex flex-col gap-1">
								<Label for="v-frames">max_frames (1–64)</Label>
								<Input
									id="v-frames"
									type="number"
									min="1"
									max="64"
									bind:value={values.max_frames}
									disabled={status !== 'stopped' && status !== 'error'}
								/>
							</div>
							<div class="flex flex-col gap-1">
								<Label for="v-size">size (224–2048)</Label>
								<Input
									id="v-size"
									type="number"
									min="224"
									max="2048"
									bind:value={values.size}
									disabled={status !== 'stopped' && status !== 'error'}
								/>
							</div>
							<div class="flex flex-col gap-1">
								<Label for="v-tokens">max_new_tokens</Label>
								<Input
									id="v-tokens"
									type="number"
									min="256"
									bind:value={values.max_new_tokens}
									disabled={status !== 'stopped' && status !== 'error'}
								/>
							</div>
						</div>
						<p class="text-sm text-muted-foreground">
							Set before starting; each setting gets its own deployment. 64 / 224 / 5760 is the
							setting this repo measured.
							{#if agent?.setting}<span class="font-mono">
									{agent.setting} · {agent.session}
								</span>{/if}
						</p>
						<div class="max-h-48 overflow-auto rounded-interactive bg-muted p-3 font-mono text-xs">
							{#if agent?.events.length}
								{#each agent.events as ev, i (i)}
									<div
										class={cn(
											'break-words whitespace-pre-wrap',
											ev.level === 'error' && 'text-atai-critical',
											ev.level === 'warn' && 'text-atai-warning'
										)}
									>
										{new Date(ev.t * 1000).toLocaleTimeString()}
										{ev.message}
									</div>
								{/each}
							{:else}
								<span class="text-muted-foreground">No activity yet.</span>
							{/if}
						</div>
					</Collapsible.Content>
				</Collapsible.Root>
			</Card.Content>
		</Card.Root>

		<div class="grid gap-4 md:grid-cols-2">
			<!-- Video -->
			<Card.Root class="min-w-0">
				<Card.Header>
					<Card.Title>Video</Card.Title>
					{#if videoUrl}
						<Card.Action>
							<Button variant="outline" size="sm" onclick={() => videoInput.click()}>Change</Button>
						</Card.Action>
					{/if}
				</Card.Header>
				<Card.Content class="flex flex-col gap-2">
					<input
						bind:this={videoInput}
						type="file"
						accept="video/*,.mp4,.mov,.m4v,.webm,.mkv,.avi"
						class="hidden"
						onchange={(e) => pickVideo(Array.from(e.currentTarget.files ?? []))}
					/>
					{#if videoUrl}
						<VideoPlayer
							src={videoUrl}
							muted
							bind:time
							bind:paused
							bind:duration
							{markers}
							controlsVisibility={markers.length ? 'always' : 'auto'}
						/>
						<p class="text-sm text-muted-foreground">
							{#if uploading}<Spinner class="size-4" /> Uploading
							{/if}{upload?.filename ?? ''}
						</p>
					{:else}
						<DropZone.Root
							accept="video/*,.mp4,.mov,.m4v,.webm,.mkv,.avi"
							ariaLabel="Upload a video"
							onfilesSelected={pickVideo}
						>
							{#snippet idle()}
								<div class="flex flex-col items-center gap-3 p-6">
									<UploadIcon class="size-6 stroke-icon-default" aria-hidden="true" />
									<DropZone.CaptionStack>
										<DropZone.Title>Drop a video here</DropZone.Title>
										<DropZone.Action>or click to choose</DropZone.Action>
										<DropZone.Hint>
											mp4, mov, m4v, webm or mkv. Re-encoded to fit the 2 MiB serving limit; the
											model sees 64 frames at 224 px.
										</DropZone.Hint>
									</DropZone.CaptionStack>
								</div>
							{/snippet}
						</DropZone.Root>
					{/if}
				</Card.Content>
			</Card.Root>

			<!-- SOP -->
			<Card.Root class="min-w-0">
				<Card.Header>
					<Card.Title>SOP</Card.Title>
					<Card.Action>
						<Button variant="outline" size="sm" onclick={() => sopInput.click()}>
							<FileTextIcon aria-hidden="true" /> Load file
						</Button>
					</Card.Action>
				</Card.Header>
				<Card.Content class="flex flex-col gap-3">
					<input
						bind:this={sopInput}
						type="file"
						accept=".txt,.md,.markdown,text/plain,text/markdown"
						class="hidden"
						onchange={loadSop}
					/>
					<Textarea
						aria-label="SOP"
						class="min-h-40 font-mono"
						bind:value={sopText}
						placeholder="Type or paste the SOP here: one step per line, or a Markdown list."
					/>
					{#if !sopText.trim()}
						<div class="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
							Or start from an example:
							{#each EXAMPLES as ex (ex.label)}
								<Button variant="link" size="inline" onclick={() => (sopText = ex.sop)}>
									{ex.label}
								</Button>
							{/each}
						</div>
					{/if}
					<p class="text-sm text-muted-foreground">
						{#if steps.length}
							{steps.length} step{steps.length > 1 ? 's' : ''}, each with its own verdict. Sent as
							"Step N: …" lines.
						{:else}
							One step per line, or load a .txt or .md file. With a Markdown list, only its items
							become steps.
						{/if}
					</p>
					{#if steps.length}
						<ol class="flex flex-col gap-2 text-sm">
							{#each steps as s, i (i)}
								<li class="flex items-start gap-2">
									<Badge variant="step" size="sm" aria-hidden="true">{i + 1}</Badge>
									<span>{stepText(s)}</span>
								</li>
							{/each}
						</ol>
					{/if}
				</Card.Content>
			</Card.Root>
		</div>

		<!-- Verify -->
		<Card.Root>
			<Card.Content class="flex flex-col gap-4">
				<div class="flex flex-wrap items-end gap-4">
					<div class="flex flex-col gap-1">
						<Label>Mode</Label>
						<Tabs.Root bind:value={mode}>
							<Tabs.List variant="segmented">
								<Tabs.Trigger value="whole" disabled={verifying}>Whole video</Tabs.Trigger>
								<Tabs.Trigger value="chunked" disabled={verifying}>Long video</Tabs.Trigger>
							</Tabs.List>
						</Tabs.Root>
					</div>
					{#if mode === 'chunked'}
						<div class="flex w-32 flex-col gap-1">
							<Label for="chunk-seconds">Chunk length (s)</Label>
							<Input
								id="chunk-seconds"
								type="number"
								min="4"
								max="120"
								bind:value={chunkSeconds}
								disabled={verifying}
							/>
						</div>
						<div class="flex w-32 flex-col gap-1">
							<Label for="chunk-overlap">Overlap (s)</Label>
							<Input
								id="chunk-overlap"
								type="number"
								min="0"
								bind:value={overlap}
								disabled={verifying}
							/>
						</div>
						<div class="flex flex-col gap-1">
							<Label>Each query asks about</Label>
							<Tabs.Root
								value={perStep ? 'one' : 'all'}
								onValueChange={(v) => (perStep = v === 'one')}
							>
								<Tabs.List variant="segmented">
									<Tabs.Trigger value="one" disabled={verifying}>One step</Tabs.Trigger>
									<Tabs.Trigger value="all" disabled={verifying}>All steps</Tabs.Trigger>
								</Tabs.List>
							</Tabs.Root>
						</div>
					{/if}
				</div>

				<p class="text-sm text-muted-foreground">
					{#if mode === 'whole'}
						One query over the whole video: the model sees 64 frames spread across it, and the video
						must fit the 2 MiB request after re-encoding (about a minute or two).
						{#if looksLong}
							This video is <span class="font-mono">{Math.round(duration)}s</span>; consider Long
							video.
						{/if}
					{:else if chunkError}
						<span class="text-atai-critical">{chunkError}</span>
					{:else}
						{#if perStep}
							Each chunk is asked about one step at a time, with a one-line SOP, so the model can't
							replay the list from step 1 or file an answer under the wrong step.
						{:else}
							<span class="text-atai-warning">
								Each chunk is checked against the whole SOP at once. Faster, but on the gel example
								the model treated every chunk as the whole procedure and never reached the last two
								steps.
							</span>
						{/if}
						64 frames per chunk; the verdicts are merged per step, with the merge rule chosen on the results.
						{#if chunkCount}
							About <span class="font-mono">{chunkCount}</span> chunk{chunkCount > 1 ? 's' : ''},
							<span class="font-mono">{queryCount}</span> queries, roughly
							<span class="font-mono">{fmtDur(queryCount * secondsPerQuery)}</span>.
						{/if}
					{/if}
				</p>

				<div class="flex flex-wrap items-center gap-3">
					<Button
						onclick={runVerify}
						loading={verifying}
						disabled={!!blocker || verifying || (mode === 'chunked' && !!chunkError)}
					>
						Verify
					</Button>
					{#if verifying && job && job.total > 1}
						<Button variant="outline" onclick={cancelVerify}>Cancel</Button>
					{/if}
					<p class="text-sm text-muted-foreground">
						{#if verifying}
							{job?.phase ?? 'Starting'}…
						{:else}
							{blocker ??
								`Checks ${steps.length} step${steps.length > 1 ? 's' : ''} against ${upload?.filename}.`}
						{/if}
					</p>
				</div>
				{#if verifying}
					<div class="flex flex-col gap-2">
						<!-- null sweeps: the total isn't known until the video is cut -->
						<Progress
							value={job && job.total > 1 ? (100 * job.done) / job.total : null}
							size="sm"
							aria-label="Verification progress"
						/>
						<p class="text-sm text-muted-foreground">
							{#if job && job.total > 1}
								<span class="font-mono">{job.done} / {job.total}</span>
								{mode === 'chunked' && perStep ? 'queries' : 'chunks'} answered ·
								<span class="font-mono">{fmtDur((verifyNow - verifyStartedAt) / 1000)}</span>
								elapsed
							{:else if mode === 'chunked'}
								Preparing and cutting the video; about a minute for a 9-minute video ·
								<span class="font-mono">{fmtDur((verifyNow - verifyStartedAt) / 1000)}</span>
								elapsed
							{:else}
								Re-encoding to fit the request, then one query ·
								<span class="font-mono">{fmtDur((verifyNow - verifyStartedAt) / 1000)}</span>
								elapsed
							{/if}
						</p>
					</div>
				{/if}
			</Card.Content>
		</Card.Root>

		<!-- Verdicts -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Verdicts</Card.Title>
				{#if current}
					<Card.Description>
						<span class="font-mono">{current.video}</span> · {summary} · answered in
						<span class="font-mono">{current.seconds}s</span>
					</Card.Description>
				{/if}
			</Card.Header>
			<Card.Content class="flex flex-col gap-4">
				{#if history.length > 1}
					<div class="flex flex-wrap gap-2">
						{#each history as h (h.id + h.created_at)}
							<Button variant={h === current ? 'cta' : 'outline'} size="sm" onclick={() => show(h)}>
								{h.video} · {new Date(h.created_at * 1000).toLocaleTimeString()}
							</Button>
						{/each}
					</div>
				{/if}

				{#if savedRuns.length}
					<Collapsible.Root>
						<Collapsible.Trigger chevron="start">
							<Label variant="sectionHeader">Saved runs ({savedRuns.length})</Label>
						</Collapsible.Trigger>
						<Collapsible.Content>
							<ul class="flex flex-col gap-1 pt-2 text-sm">
								{#each savedRuns as run (run.name)}
									<li class="flex items-center gap-2">
										<Button variant="link" size="inline" onclick={() => openRun(run.name)}>
											{run.video}
										</Button>
										<span class="text-muted-foreground">
											{run.mode === 'chunked'
												? `long video${run.per_step ? ', one step per query' : ''}`
												: 'whole video'} · {run.steps} steps ·
											<span class="font-mono">
												{new Date(run.created_at * 1000).toLocaleString()}
											</span>
										</span>
									</li>
								{/each}
							</ul>
							<p class="pt-2 text-xs text-muted-foreground">
								A saved run opens without its video: choose the same video above to see its markers
								and play from a step.
							</p>
						</Collapsible.Content>
					</Collapsible.Root>
				{/if}

				{#if current?.merged && current.rule}
					<div class="flex flex-col gap-2">
						<Label>Merge rule</Label>
						<Tabs.Root value={current.rule} onValueChange={setRule}>
							<Tabs.List variant="segmented">
								{#each tva.MERGE_RULES as rule (rule)}
									<Tabs.Trigger value={rule}>{rule}</Tabs.Trigger>
								{/each}
							</Tabs.List>
						</Tabs.Root>
						<p class="text-sm text-muted-foreground">
							{tva.RULE_HELP[current.rule]} Only chunks that passed or failed a step count; a step no
							chunk saw is missing.
							<span class="font-mono">{current.chunks?.length}</span> chunks of
							<span class="font-mono">{current.chunk_seconds}s</span>{#if current.overlap}, <span
									class="font-mono"
								>
									{current.overlap}s
								</span> overlap{/if}{#if current.failed_chunks?.length},
								<span class="text-atai-warning">
									{current.failed_chunks.length} without an answer, left out
								</span>{/if}.
						</p>
					</div>
				{/if}

				{#if current?.evidence}
					<div class="flex flex-col gap-2">
						<Label>What each chunk said</Label>
						<ChunkGrid steps={current.steps} evidence={current.evidence} onSeek={seek} />
					</div>
				{/if}

				{#if current}
					<ol class="flex flex-col divide-y divide-border">
						{#each current.steps as s (s.step)}
							<li class="flex flex-col gap-1 py-3 first:pt-0">
								<div class="flex items-start gap-2">
									<Badge variant="step" size="sm" aria-hidden="true">{s.step}</Badge>
									<span class="flex-1 text-sm font-medium">{stepText(s.text)}</span>
									{#if s.timestamp_start != null}
										<Button
											variant="link"
											size="inline"
											class="font-mono"
											aria-label={`Play step ${s.step} from ${s.timestamp_start.toFixed(1)} seconds`}
											onclick={() => seek(s.timestamp_start!)}
										>
											{s.timestamp_start.toFixed(1)}–{s.timestamp_end?.toFixed(1)}s
										</Button>
									{/if}
									<Badge variant="status" size="sm" class={verdictClass[s.status]}>
										{s.status}
									</Badge>
								</div>
								{#if current.evidence && s.answered_chunks != null}
									<p class="pl-6 text-xs text-muted-foreground">
										{#if s.status === 'PASSED' || s.status === 'FAILED'}
											{s.status === 'PASSED' ? 'Passed' : 'Failed'} in
											<span class="font-mono">{s.agreeing_chunks} of {current.chunks?.length}</span>
											chunks. The time is their longest unbroken run{#if s.run_chunks}{' '}(<span
													class="font-mono"
												>
													{s.run_chunks.length}
												</span> chunks){/if}{#if s.span_all?.[1] != null}; together they span
												<span class="font-mono">
													{s.span_all[0]?.toFixed(0)}–{s.span_all[1].toFixed(0)}s
												</span>{/if}.
											{#if (s.agreeing_chunks ?? 0) > (current.chunks?.length ?? 0) / 2}
												<span class="text-atai-warning">
													Passing in most chunks means the verdict isn't tied to when the step
													happens: with all steps per query the model recites the SOP, and with one
													step it passes wherever the step's objects are in view.
												</span>
											{/if}
										{:else}
											No chunk passed or failed this step (<span class="font-mono">
												{s.answered_chunks} of {current.chunks?.length}
											</span>
											reported it).
										{/if}
									</p>
								{/if}
								<!-- merge_chunks' "no chunk saw this step" repeats the summary above -->
								{#if s.reason && !(current.evidence && s.reason.startsWith('no chunk saw'))}
									<p class="pl-6 text-sm text-muted-foreground">{s.reason}</p>
								{/if}
								{#if current.evidence?.[s.step]}
									<Collapsible.Root class="pl-6">
										<Collapsible.Trigger chevron="start">
											<span class="text-xs text-muted-foreground">
												All {current.evidence[s.step].length} chunk answers
											</span>
										</Collapsible.Trigger>
										<Collapsible.Content>
											<ul class="flex flex-col gap-1 pt-2 text-xs">
												{#each current.evidence[s.step] as e (e.chunk)}
													<li class="flex items-start gap-2">
														<Button
															variant="link"
															size="inline"
															class="shrink-0 font-mono"
															aria-label={`Play chunk ${e.chunk + 1}`}
															onclick={() => seek(e.timestamp_start ?? e.offset)}
														>
															{e.offset.toFixed(0)}–{(e.offset + e.duration).toFixed(0)}s
														</Button>
														<Badge
															variant="status"
															size="sm"
															class={verdictClass[e.status] ?? 'text-muted-foreground'}
														>
															{e.status}
														</Badge>
														<span class="text-muted-foreground">{e.reason ?? ''}</span>
													</li>
												{/each}
											</ul>
										</Collapsible.Content>
									</Collapsible.Root>
								{/if}
							</li>
						{/each}
					</ol>
					<Alert.Root variant="warning">
						<Alert.Title>Use verdicts for triage, not sign-off</Alert.Title>
						<Alert.Description>
							TVA checks whether objects appear in the video, so it can pass a step whose parts are
							visible but were never used, and the reason can describe things that didn't happen.
							Have a person review each verdict.
						</Alert.Description>
					</Alert.Root>
				{:else}
					<EmptyState>
						No verdicts yet. Start the agent, choose a video and an SOP, then verify.
					</EmptyState>
				{/if}
			</Card.Content>
		</Card.Root>
	</main>
</div>
