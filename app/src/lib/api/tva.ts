// Client for server/server.py, the local API over TVA in serving.

export type AgentStatus = 'stopped' | 'starting' | 'running' | 'stopping' | 'error';

export type AgentEvent = { t: number; level: string; message: string };

export type Agent = {
	status: AgentStatus;
	error: string | null;
	phase?: 'preparing' | 'placing' | 'loading' | null; // while starting
	session: string | null;
	deployment: string | null;
	setting: string | null;
	values: AgentValues;
	started_at: number | null;
	running_at: number | null;
	last_query_at: number | null;
	events: AgentEvent[];
	now: number;
};

export type AgentValues = { max_frames: number; size: number; max_new_tokens: number };

export type Config = {
	endpoint: string;
	default_endpoint: string;
	key_set: boolean;
	key_hint: string;
	key_source: 'app/.env' | '.env' | 'app' | null; // where the key in use came from
};

export type Upload = { upload_id: string; filename: string };

export type Verdict = 'PASSED' | 'FAILED' | 'MISSING' | 'NOT REPORTED';

export type StepResult = {
	step: number; // 1-based
	text: string;
	status: Verdict;
	timestamp_start?: number;
	timestamp_end?: number;
	reason?: string;
	chunks?: string; // long video: the per-chunk tally, e.g. "MISSINGx5 PASSEDx1"
	agreeing_chunks?: number; // long video: chunks that gave this verdict
	answered_chunks?: number; // long video: chunks that reported this step at all
	span_all?: [number | null, number | null]; // long video: first to last agreeing chunk
	decided_by_chunk?: number; // long video: the first chunk of run_chunks
	run_chunks?: number[]; // long video: the longest unbroken run of agreeing chunks, which sets the time
};

/** What one chunk said about one step, in whole-video time. */
export type Evidence = {
	chunk: number;
	offset: number;
	duration: number;
	status: Verdict | 'ERROR';
	reason?: string;
	timestamp_start?: number | null;
	timestamp_end?: number | null;
};

export const MERGE_RULES = ['any', 'strict', 'majority', 'longest'] as const;
export type MergeRule = (typeof MERGE_RULES)[number];

export const RULE_HELP: Record<MergeRule, string> = {
	any: 'Passed if any chunk passed the step, else failed if any failed it.',
	strict: 'Failed if any chunk failed the step, else passed if any passed it.',
	majority: 'The more common of passed and failed across chunks; a tie is failed.',
	longest: 'The verdict from the chunk with the longest interval for the step.'
};

export type ChunkAnswer = { offset: number; duration: number; error: string | null };

export type Verification = {
	id: string;
	mode: 'whole' | 'chunked';
	video: string;
	seconds: number;
	steps: StepResult[];
	setting: string;
	created_at: number;
	// long video only
	chunk_seconds?: number;
	overlap?: number;
	duration?: number;
	chunks?: ChunkAnswer[];
	evidence?: Record<number, Evidence[]>; // keyed by 1-based step
	per_step?: boolean;
	queries?: number;
	failed_chunks?: number[];
	merged?: Record<MergeRule, StepResult[]>;
	rule?: MergeRule;
	videoUrl?: string;
};

export type Job = {
	id: string;
	status: 'running' | 'done' | 'error' | 'cancelled';
	error: string | null;
	result: Verification | null;
	phase: string;
	done: number;
	total: number;
};

export type VerifyOptions =
	| { mode: 'whole' }
	| { mode: 'chunked'; chunk_seconds: number; overlap: number; per_step: boolean };

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
	const res = await fetch(path, init);
	const body = await res.json().catch(() => ({}));
	if (!res.ok) throw new Error(body.error || body.message || `HTTP ${res.status}`);
	return body as T;
}

const json = (body: unknown): RequestInit => ({
	method: 'POST',
	headers: { 'Content-Type': 'application/json' },
	body: JSON.stringify(body)
});

export const getConfig = () => call<Config>('/api/config');

/** Set the key and/or endpoint; an empty value keeps the current one. */
export const setConfig = (apiKey: string, endpoint: string) =>
	call<Config & { message: string }>('/api/config', json({ api_key: apiKey, endpoint }));

export const getAgent = () => call<Agent>('/api/agent');

export const startAgent = (values: AgentValues) => call<Agent>('/api/agent/start', json(values));

export const stopAgent = () => call<{ ok: boolean }>('/api/agent/stop', { method: 'POST' });

export const uploadVideo = (file: File) =>
	call<Upload>('/api/upload', {
		method: 'POST',
		headers: { 'X-Filename': file.name },
		body: file
	});

export const startVerify = (upload: Upload, steps: string[], options: VerifyOptions) =>
	call<Job>(
		'/api/verify',
		json({
			upload_id: upload.upload_id,
			filename: upload.filename,
			sop: steps.join('\n'),
			...options
		})
	);

export const getJob = (id: string) => call<Job>(`/api/jobs/${id}`);

export const cancelJob = (id: string) => call<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' });

/** Chunks a video of `duration` seconds is cut into; the server's cut can differ by one. */
export function chunkCount(duration: number, seconds: number, overlap: number): number {
	if (!duration || seconds <= overlap) return 0;
	return Math.max(1, Math.ceil((duration - overlap) / (seconds - overlap)));
}

export type SavedRun = {
	name: string;
	video: string;
	mode: 'whole' | 'chunked';
	per_step: boolean;
	created_at: number;
	steps: number;
};

/** Results saved in app/runs/, newest first. */
export const listRuns = () => call<SavedRun[]>('/api/runs');

export const getRun = (name: string) => call<Verification>(`/api/runs/${encodeURIComponent(name)}`);
