# Task Verification Agent — End-to-End Example

A realistic, end-to-end walkthrough of running the **Task Verification Agent (TVA)**
on the Archetype AI Platform (Dev): feed it a procedure video and an SOP, get back
a per-step PASSED / FAILED / MISSING verdict with timestamps and a reason, then
score it against ground truth.

The scenario: a bench recording of a **three-step o-ring cap assembly**, in twelve
takes where the operator deliberately skips steps. A developer wants to know
whether TVA catches the skipped ones — not whether it can describe a video that
went fine.

TVA V1 is zero-shot. The design doc states there are no build-time optimization
steps, and the platform agrees: the `tva` blueprint pins its own models
(`newton-fusion:1.0` and `whisper:large-v3`), so like
[MGA](https://github.com/archetypeai/manual-generation-agent-example) — and unlike
[RED](https://github.com/archetypeai/rare-event-detection-agent-example) and
[OSM](https://github.com/archetypeai/operational-state-monitoring-agent-example) —
there is **no classifier to fit and no `artifacts` map to pass.**

```
video ──► sample frames ──► preprocess ─┐
                                        ├─► newton-fusion f1-0 ─► parse ─► per-step verdicts
SOP (.txt) ──► PrepareSOPNode ──────────┤
         whisper ASR ───────────────────┘
```

**The one thing that makes TVA different from MGA: the SOP arrives at runtime, per
run.** Read the two blueprints' source connectors side by side — this is the whole
distinction, and it is two lines of YAML:

```yaml
# mga                                    # tva
source:                                  source:
  key: MultiModalSource                    key: MultiModalSource
  config:                                  config: {}      # accepts a .txt input
    default_text: ${values.prompt}
    text_extensions: []   # accepts NO text input
```

MGA takes **no text input at all** (`text_extensions: []`) and gets its instruction
from a bundle *value*, so the instruction is fixed for the life of a bundle. TVA
accepts a `.txt` and routes it `source.text -> prepare.in` through a dedicated
`PrepareSOPNode` — so every run carries its own procedure and **one bundle serves
every SOP.** That single edge turns "describe this video" into "check this video
against my procedure", and most of this README is about the consequences.

## ⚠️ The canonical `tva` blueprint cannot currently produce output

Read this before running anything, because it costs 7 minutes to discover.

`tva` was republished on **2026-08-11 at 22:01:18** — mid-session, while runs were
in flight — with its sink format changed from `jsonl/per-request` to
`json/per-request`. **No connector is registered for that format**, so every run
against the canonical, active blueprint dies at graph instantiation:

```
22:19:05  ERROR  error           instantiating graph: no connector registered for
                                 format="json/per-request" kind="s3"
22:19:07  ERROR  pod.terminated  Container agent terminated: Error (exit=1)
```

The two blueprints are **byte-identical apart from that one string**:

| | canonical `tva` = `blp_7xfkarv…` | superseded `blp_6dp7cye…` |
|---|---|---|
| sink format | `json/per-request` | **`jsonl/per-request`** |
| `max_new_tokens` default | 8192 | 2048 |
| nodes, graph, models | identical | identical |
| result | **exit 1, no output** | **works** |

Three things make this expensive rather than merely broken:

- **The graph is validated AFTER both models are downloaded and loaded.** The run
  spends 39 s on whisper and 5min8s + 1min39s on newton-fusion, *then* fails.
  Observed cost: **996 s for zero output.**
- **Nothing upstream hints at it.** The bundle returns `status: ready`. The failure
  appears only in `/logs`, and `/results` is simply empty.
- **A sweep repeats it once per clip.** Four clips would be ~40 GPU-minutes of
  identical failure on a shared single-GPU dev box.

So this repo **pins the superseded blueprint by id** and preflights the sink
client-side, turning a 7-minute failure into an instant one:

```
  ** sink format 'json/per-request' has NO REGISTERED CONNECTOR on dev. This run
     will load both models (~7 min) and then die at graph instantiation.
  refusing to spend ~7 min of GPU on a run that cannot produce output.
```

`sweep.py` refuses outright; `run_tva_example.py` aborts unless you pass `--force`.
**To flip back when the sink is fixed**, set `BLUEPRINT_DEFAULT = BLUEPRINT_ACTIVE`
in `run_tva_example.py` — the preflight reads the blueprint document rather than a
hardcoded list, so it stops warning on its own and doubles as a regression test.

**The general lesson, and it contradicts the advice this repo started from:** MGA's
example concluded *reference blueprints by key, not by id*, because a pinned id
stopped resolving. Here the key resolves fine and produces nothing, while the
superseded id works. **Neither is safe on its own** — what is safe is reading the
blueprint document before spending GPU.

## Quick start — one command

Nothing to build and nothing to fit — **no `atai_core`, no GPU, no AWS**. Python 3
and a dev API key:

```sh
cp .env.example .env           # add ATAI_API_KEY and ATAI_API_ENDPOINT
python3 run_tva_example.py
```

Upload video + SOP → create a bundle → run → stream `/logs` → download → print the
report and score it. **~13 minutes**, of which ~7 is model loading.

Every script here is **stdlib-only**: no virtualenv, no `requirements.txt`, no
install step. Scoring needs no API key at all:

```sh
python3 evaluate.py reference/*.jsonl
```

## What comes back

```json
{"id":"1_pass_2_pass_3_pass_A","results":[
  {"step":0,"status":"PASSED","timestamp_start":2.0,"timestamp_end":9.0,
   "frame_start":60.0,"frame_end":270.0,
   "reason":"The video clearly shows the person picking up the blue cap and O-ring, then installing the O-ring onto the cap."}, …]}
```

Five properties, none of them documented in the blueprint:

- **`step` is 0-BASED.** A 3-step SOP returns steps 0, 1, 2. Humans write SOPs
  1-based, so a scorer that joins them naively is off by one — and it fails
  *quietly*. This example hit it for real: a scorer assuming 1-based read a
  perfect 3-of-3 run as

  ```
  step 1: expected PASS  got PASSED        [OK]
  step 2: expected PASS  got PASSED        [OK]
  step 3: expected PASS  got NOT REPORTED  [MISS]   <-- the verdict existed, under step 2
  ```

  Two steps still line up, so it looks like a model that missed the last step
  rather than a join bug. Verified on a real run, asserted in
  `run_tva_example.STEP_BASE`, and auto-detected in `evaluate.py`.
- **Timestamps in seconds come free.** The blueprint exposes only
  `parser_output_frame_indices`; `timestamp_start`/`timestamp_end` are emitted
  anyway, so no frames→seconds conversion is needed.
- **Frame indices are SOURCE frames, not sampled frames.** `frame = timestamp ×
  fps` held on every row (810 = 27.0 × 30). Sampled space would have capped at 63
  at `max_frames: 64`.
- **A `reason` string per step, and it is the most useful field.** It cites what
  was on screen — and it volunteered *"the blue cap"* without the SOP mentioning
  colour, which is direct evidence the vision path is doing the work rather than
  paraphrasing the SOP back.
- **Unlike MGA, the output does NOT tile the timeline.** MGA segments
  contiguously, so every second must be labelled; TVA emits one row per SOP step
  and the rows can leave gaps. **TVA is verification, not segmentation.**

## The end-to-end example, stage by stage

```
video + SOP ─► 1 select ─► 2 write the SOP ─► 3 configure ─► 4 run ─► 5 evaluate
              (labels)     (the ONLY lever)   (2 values)    (platform)  (offline)
                                ▲
                    there is no `prompt` on this
                    blueprint — the SOP is the prompt
```

Five stages, and the middle one is where the work is. RED and OSM spend most of
theirs producing a fitted classifier; TVA is zero-shot, so what replaces that is
**SOP authoring**. There is no `prompt` value to tune, no format instruction to
get wrong, and exactly two generation values worth setting.

| Stage | Script | Needs |
|---|---|---|
| 1 Select clips | `sweep.py --list` | nothing |
| 2 Write the SOP | `sop/oring.txt` | nothing |
| 3 Configure | `run_tva_example.py --dry-run` | nothing |
| 4 Run | `run_tva_example.py` / `sweep.py` | dev API key |
| 5 Evaluate | `evaluate.py` | nothing |

## Prerequisites

Notably short: **no `atai_core` checkout, no GPU, no AWS, no `ffmpeg`, no
`yt-dlp`.** TVA is zero-shot, the blueprint pins its own models, and the clips are
local files rather than YouTube ids — so unlike the MGA example there is no
download step that can silently half-succeed.

- `.env` in the repo root:

  ```
  ATAI_API_KEY=<dev API key>
  ATAI_API_ENDPOINT=https://api.dev.u1.archetypeai.app
  ```

- **Python 3.** Stdlib only.
- **Nothing else.** The clips are committed in `data/clips/`, so there is no fetch
  step and no media tooling to install.

⚠️ **The `/agents` API is dev-only.** A prod endpoint
(`api.u1.archetypeai.app`) returns **404 for every `/agents` path**, which reads
like an unregistered blueprint rather than the wrong host. A prod key on the dev
host returns 401. Both mistakes cost time here; `tva_api.py` now warns when the
endpoint is not dev.

## Stage 1 — Select clips, and check what the labels actually mean

Twelve takes of the same three-step assembly, 21–43 s each, 640×480 or 1920×1080,
30 fps. **Ground truth is encoded in the filename** —
`1_pass_2_pass_3_fail_A.mp4` means step 1 correct, step 2 correct, step 3 not; the
trailing letter is a take id. That keeps the labels visible in `ls` and impossible
to desynchronise from the media.

```sh
python3 sweep.py --list
```

Two properties of this set matter more than its size, and neither is visible from
the filenames alone:

**The failures are OMISSIONS, not mistakes.** Confirmed by stepping through the
frames: in `1_fail` the cap goes straight to the manifold with no o-ring fitted at
all, and in `3_fail` **the wrench never enters frame** — the clip ends hand-tight.
Nothing is attempted and botched. That matters because TVA has two ways to say
"not done":

```
FAILED   the step was attempted and done wrong
MISSING  the step never happened
```

For this set **`MISSING` is the status that matches the video** and `FAILED` is the
one worth a second look — the opposite of what the label word "fail" suggests.
A binary pass/fail label cannot express that distinction, so `evaluate.py` scores
the verdict (PASSED vs not) and reports the FAILED/MISSING split separately rather
than hiding it inside one accuracy number.

**Step 2 is `pass` in all twelve clips**, so read its column as a consistency
check rather than as a detection result — "always say PASSED" also scores 100% on
it. Steps 1 and 3 are where the discrimination is, and both have failures.
`sweep.py --list` prints this note itself so nobody quotes a step-2 number as
accuracy.

**These clips have no audio track at all** — verified with `ffprobe`: one video
stream, no audio stream. The blueprint runs `audio_reader → whisper →
prompt.audio` regardless. It does **not** fail: whisper loads (36 s download,
3.3 s load, every run) and contributes nothing. So ~40 seconds of every run is
spent loading a model that cannot help, and **the audio half of the fusion is
untested by this example.** MGA's run C proved the audio path carries real signal
on a narrated video; nothing here exercises it.

**The clips are committed**, in `data/clips/` — 80 MB, largest file 11 MB, no LFS.
They were recorded internally for this example, so there is no third-party licence
to respect, and committing them means **the example is self-contained**: no
`yt-dlp`, no `ffmpeg`, and no download step that can half-succeed. That is a real
difference from the MGA example, where a missing `ffmpeg` let `yt-dlp` exit 0
having merged nothing and the failure surfaced several steps later.

## Stage 2 — Write the SOP (the only lever you have)

The SOP is a **text file, one step per line**, and it lives at
[`sop/oring.txt`](sop/oring.txt):

```
Step 1: Install the rubber O-ring into the blue block-off cap, ensuring it is properly seated in the groove.
Step 2: Thread the blue cap onto the black manifold by hand until snug, without cross-threading.
Step 3: Using a wrench, tighten the blue cap to the specified torque.
```

`PrepareSOPNode` "accumulates the SOP step lines into a single prepared-instruction
string", so **the line breaks are the step boundaries and nothing else is.** A
wrapped line silently becomes two steps.

**There is no `prompt` value on this blueprint.** The system message lives inside
`TaskVerificationPromptGeneratorNode` and is not exposed. That is a genuine
simplification rather than a gap: MGA's `prompt` is exposed and *fragile* — a
prompt that specifies its own output format makes its parser return zero steps.
TVA gives you no way to make that mistake. The flip side is that **the SOP wording
is your entire influence over the model.** Three choices in the wording above:

| choice | why |
|---|---|
| **Explicit `Step N:` prefixes** | Redundant to the parser — `step` is positional and 0-based whatever the text says. They are for the model, to tie each verdict to a numbered step rather than to line order. |
| **Named colours** ("blue cap", "black manifold") | The bench holds a blue anodized cap and a black manifold with other blue and black fittings beside them, so "the cap" is genuinely ambiguous at 224×224. Insurance, not a fix: the model already said *"the blue cap"* unprompted. |
| **Behavioural, not numeric** | The SOP says "tighten to the specified torque", not "to 12 Nm". Nothing in a 30-second handheld video can evidence a number, so asking for one invites a guess. |

`sop/oring.txt` is uploaded **as itself** — same bytes, same name (`oring.txt`),
no generated copy — so the file you edit, the file on the platform and the text the
model sees cannot drift apart. Provenance for past runs comes from git, where the
SOP is committed, rather than from the platform's file list.

### Where the SOP lives on the wire — and where it does not

**Not in the bundle.** The bundle carries only `values`:

```json
{"id":"bnd_3j8z…","values":{"max_frames":64,"max_new_tokens":2048},"status":"ready"}
```

The SOP rides with the **run**, as a source connector input:

```json
{"source":[{"type":"file","id":"1_pass_2_pass_3_pass_A.mp4","format":"mp4"},
           {"type":"file","id":"sop_1_pass_2_pass_3_pass_A.txt","format":"txt"}]}
```

So one bundle is reusable across every SOP, and **`MultiModalSource` routes the two
inputs by their `format` field, not by position** — order does not matter. Looking
for the SOP in the bundle and not finding it is the expected first confusion here.

## Stage 3 — Configure

```sh
python3 run_tva_example.py --dry-run     # print the exact payloads, call nothing
```

Only two values are worth setting, and both are honoured:

| Value | This example sends | Honoured? | Notes |
|---|---|---|---|
| `max_frames` | **64** | ✅ | Blueprint default is **16** — on a 30 s clip that is one frame every 1.9 s. 64 is also the reader/preprocessor batch size, so frames land in one batch. |
| `max_new_tokens` | **2048** | ✅ | Three steps with reasons fit easily. |
| `size` | not sent (224) | ✅ | Every frame is resized to 224×224, so 1080p clips are no better than 480p — only slower to upload. |
| `prompt` | **does not exist** | — | Not a defect to work around, just absent. The SOP replaces it. |

The preflight reads the blueprint document rather than hardcoding key names, so a
value that is not wired is caught **before** spending GPU:

```
  max_frames       64       -> honoured   (blueprint default: 16)
  max_new_tokens   2048     -> honoured   (blueprint default: 8192)
```

**Set these explicitly even when the defaults look fine.** The blueprint's own
`max_new_tokens` default changed from 2048 to 8192 *during this session* (see
below). Runs that send the value explicitly were unaffected; runs relying on the
default silently changed behaviour.

## Stage 4 — Run

```sh
python3 run_tva_example.py                              # one clip
python3 sweep.py --clips 1_pass_2_pass_3_fail_A …       # several, sequential
```

Timings from real runs, and they are dominated by cold start:

| phase | time |
|---|---|
| whisper download + load | 36.2 s + 3.3 s |
| **newton-fusion download + load** | **5min 0s + 1min 39s** |
| graph execution, 31 s clip | ~5 min 20 s |
| **total** | **763 s (12.7 min)** |

**Nothing is cached between runs.** Two back-to-back runs on the same GPU each
downloaded newton-fusion from scratch — 5 minutes, twice. **~7 of every 13 minutes
is model loading**, so the marginal cost of one more clip is far lower than the
first, and batching with `sweep.py` is worth it.

**Generation is deterministic.** The same clip and SOP run twice — 25 minutes
apart, under **two different API keys in two different orgs** — produced
byte-identical output: 678 bytes, md5 `7eb028ac7d39938b261be17eaaf3e2bb`. So a
matching digest means you reproduced a run exactly, and a differing one means
something real changed (the clip, `max_frames`, the SOP, or the blueprint). The
committed file in `reference/` is that digest.

**Dev serializes these jobs.** Run 2 was submitted while run 1 held the GPU and
waited **7 minutes** for it. Note the trap:

```
21:51:57  job.admitted   Job scheduled after 12s in queue     <-- misleading
21:59:00  job.started    Job started — first pod is now running
```

`job.admitted` reports a 12-second queue and then the pod does not start for seven
minutes. **Judge the real start from `job.started`, not from `job.admitted`.**

## Stage 5 — Evaluate

```sh
python3 evaluate.py reference/*.jsonl
python3 evaluate.py eval_out/*.jsonl --verbose    # with the reason for every step
```

Offline and stdlib-only — no API key, no network — so re-scoring is free and
repeatable, which matters because a run costs 13 minutes of a shared GPU once but
you will read its numbers many times.

It scores the **verdict** (PASSED vs not-PASSED), because that is what a binary
`pass`/`fail` label can support. It deliberately **does not score timestamps**:
unlike MGA, where the output is a temporal segmentation and IoU is the natural
metric, TVA's intervals are supporting evidence for a verdict and there are no
per-step ground-truth spans in these filenames. Intervals are printed and sanity
checked (end-before-start, zero-length), never scored. **Read the absence of an
IoU column as "unmeasured", not as "localises perfectly".**

<!-- RESULTS -->

## Platform behavior worth knowing

- **The `/agents` API is dev-only.** Prod 404s on every `/agents` path; a prod key
  on dev returns 401. Two different symptoms for the same class of mistake.
- **A blueprint can be republished under you, mid-session, and the new version can
  be broken.** See the warning at the top: `tva` changed id and sink format at
  22:01:18 while runs were in flight, and the new canonical version cannot emit
  output. **Read the blueprint document before every run** — by key or by id, the
  identifier alone tells you nothing about whether the graph will instantiate.
- **The graph is validated after the models are loaded, not before.** A blueprint
  that cannot instantiate still costs the full ~7-minute cold start. Validating
  connectors and node configs first would turn 996 s into about 1 s.
- **`connectors.source[].id` wants the file BASENAME.** `/v0.5/files` returns both
  a `file_id` (the basename) and a `file_uid` (`fil_…`); the connector wants the
  former. The uid fails at source resolution, after the pod has started.
- **Uploads enforce the DECLARED MIME type**, not the bytes. `text/plain` for the
  SOP and `video/mp4` for the clip. A `.mp4` declared as `text/csv` is rejected.
- **The bundle endpoint is plural.** `POST /agents/bundle` → 404; use
  `/agents/bundles`. The RED and OSM example repos still use the singular form.
- **Starting a run returns 202, not 201.**
- **Poll `/logs`, not `/events`.** `/events` carries only "run started" and
  "dispatched to JOS".
- **The instance `status` field is not a terminality signal** — judge from the log
  stream. (MGA saw `running` persist 20+ minutes after a pod died with exit=1.)
- **`job.admitted`'s queue figure is not the real wait** — see Stage 4.
- **Values are accepted whether or not they do anything.** A key absent from the
  blueprint's `values`, or present but never referenced as `${values.<key>}`, is
  stored on the bundle and silently ignored.
- **No progress events during processing.** Between `input_started` and the
  terminal event there is nothing — no frame count, no transcript length, no
  per-node timing.
- **whisper loads even with no audio track**, costing ~40 s per run and
  contributing nothing.

## Where the deployment diverges from the design doc

Observations for the author, not defects.

- **`min_temporal_similarity_threshold` is not exposed.** The doc lists it as a
  Newton Fusion Model parameter and devotes its longest section to the temporal
  compression algorithm it controls — four stages, MAD-based adaptive
  segmentation, a documented *"70% compression with a 25% performance
  improvement"*. The blueprint wires only `model` and `max_new_tokens` into the
  fusion node, so **the headline algorithmic contribution of TVA V1 is not
  reachable from a bundle and cannot be evaluated through this API.**
- **Audio is in V1.** The doc says F1-0 adds audio in a later version; the
  blueprint runs `whisper:large-v3` into `prompt.audio` today.
- **The parser's `step` field is 0-based and undocumented.** The doc specifies
  `step: <int>` without saying where it starts.
- **`MISSING` rows are specified to carry no timestamps** and the doc's own format
  agrees (`step N: MISSING, reason: <reason>` has no timestamp fields) — worth
  stating in the output schema, since consumers must handle nulls.
- **The model is fp8** — `f1_0_35b_a3b_fp8_base_040625b4d0750b`, confirming the
  35B/3B-active MoE description and adding quantization the doc does not mention.

## Repo layout

| Path | What it is |
|---|---|
| `run_tva_example.py` | **Start here.** Single-clip end-to-end run: upload video + SOP → bundle → run → watch `/logs` → download → print → score. `--dry-run` and `--show` need no key. |
| `sop/oring.txt` | The SOP, one step per line. Edit this to change the procedure — no Python involved. |
| `sweep.py` | Batch path: runs several clips sequentially, keeps going on failure, cancels the in-flight run if interrupted. `--list` is offline. |
| `evaluate.py` | Offline scorer: per-step accuracy, confusion, FAILED-vs-MISSING split, interval sanity. No API key. |
| `tva_api.py` | Shared stdlib client (streaming upload, retries, `/logs` polling, wired-value preflight). |
| `reference/` | Committed run outputs and a blueprint snapshot, so the numbers are checkable offline. |
| `docs/` | The TVA design doc this example implements (gitignored — internal). |

## Not yet built

- **An audio-bearing clip.** The whisper half of the pipeline is loaded on every
  run and exercised by none of these.
- **A `max_frames` sweep.** 64 vs the blueprint's 16 is the obvious first
  comparison on 21–43 s clips, and `sweep.py --max-frames 16` runs it.
- **Timestamp scoring.** Would need per-step ground-truth intervals, which the
  filename labels cannot carry. The reasons are more informative per GPU-minute.
- **Longer videos.** Every clip here is under 45 s. MGA fails outright above ~5
  minutes; where TVA's ceiling sits is untested, and the design doc says
  performance is known to degrade past 10 minutes.
- **A negative-control SOP.** Feeding a procedure the video does not show at all
  would test whether TVA can say MISSING for everything, rather than pattern-match
  the SOP onto whatever is on screen. Cheap and worth doing.
