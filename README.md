# Task Verification Agent — End-to-End Example

A realistic, end-to-end walkthrough of running the **Task Verification Agent (TVA)**
on the Archetype AI Platform (Dev): feed it a procedure video and an SOP, get back
a per-step PASSED / FAILED / MISSING verdict with timestamps and a reason, then
score it against ground truth.

The scenario: a bench recording of a **three-step o-ring cap assembly**, in twelve
takes where the operator deliberately skips steps. A developer wants to know
whether TVA catches the skipped ones — not whether it can describe a video that
went fine.

**It half does, and the half it fails is the dangerous one.** Across all 12 clips
it caught the omitted wrench **3 of 3 times** and the omitted o-ring **0 of 6** —
passing work that was not done, with confident invented detail about fitting a part
that was never fitted. [Full results below.](#results--12-clips-and-one-finding-that-matters-more-than-the-accuracy)

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
report and score it. **~13 minutes** (754-814 s observed), of which ~7 is model
loading — plus queue time if someone else has the single dev GPU.

To check every clip instead of one — same bundle, same SOP, one run per video:

```sh
python3 sweep.py --list             # what is on disk, with labels. No API calls.
python3 sweep.py --all --dry-run    # see the plan, no API calls
python3 sweep.py --all              # one bundle, all 12 clips, sequential (~2.6 h)
python3 evaluate.py eval_out/*.jsonl
```

A sweep is hours long on a shared GPU, so two commands matter as much as the sweep
itself. **The runs live on the platform, not in your client** — see
[If the client dies, the runs do not](#if-the-client-dies-the-runs-do-not):

```sh
python3 sweep.py --collect          # harvest finished runs after a disconnect
python3 sweep.py --cancel-all       # stop what you are not waiting for
```

Every script here is **stdlib-only**: no virtualenv, no `requirements.txt`, no
install step. Three of the four commands above touch no API at all, and scoring
never does:

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
- **`len(results)` is not the step count — rows can be duplicated.** One run
  returned **nine rows for a three-step SOP**: the same three verdicts, emitted
  three times, byte-identical in blocks. Collapse by `step` before scoring, and do
  not assume one row per step. It was the only run at `max_new_tokens: 5120` while
  the other eleven used 8192, so a smaller reserved budget is a suspect — but that
  is one observation, not a demonstrated cause. `evaluate.py` collapses duplicates
  and says so, and warns loudly if two rows for one step ever *disagree*.

### JSON or JSONL? Both — and it matters when you merge

Three things disagree about this, so it is worth stating plainly:

| | |
|---|---|
| the sink format | `jsonl/per-request` |
| the filename the platform returns | `…__output_<clip>.`**`json`** |
| the bytes on disk | **one** JSON object, **no trailing newline** |

**JSONL is the format contract, not a description of these files.**
`jsonl/per-request` means *one JSON record per request* — and a run here processes
one video, so there is exactly one record, byte-identical to plain JSON. The `l`
only materialises when a run has several source inputs, where you get one line per
input. This repo names its copies `.jsonl` after the sink format; the platform's
`.json` is why an earlier version of `--collect` saved files as
`<clip>.json.jsonl`.

**The extension is not derived from the sink format, and it changed mid-session.**
The same pinned blueprint, with `format: jsonl/per-request` throughout, emitted
both:

| run | UTC | emitted |
|---|---|---|
| `agt_7xa5n8yy…` | 21:46 | `…_1_pass_2_pass_3_pass_A.`**`jsonl`** |
| `agt_2jcbx7ka…` | 23:37 | `…_1_pass_2_pass_3_pass_A.`**`json`** |

Nothing caller-side differed. And the declared format does not predict the
extension in either agent: `mga` declares `json/per-request` and emitted `.jsonl`
at 19:57, while `tva` declares `jsonl/per-request` and emitted `.jsonl` then
`.json`. So **the two agents do not use different formats by design** — MGA's
`.jsonl` artifact simply predates a platform-side change that TVA's `.json`
follows.

Two consequences: **do not key anything off the output extension** (match on
`__output_` and strip whatever suffix is there — `--collect` does), and treat the
`format` value as a contract about record structure rather than about filenames.

The consequence is a real trap. **Because there is no trailing newline,
concatenating outputs corrupts them:**

```
cat out_A.jsonl out_B.jsonl > merged.jsonl   # ONE line, two objects, parses as neither
# json.decoder.JSONDecodeError: Extra data: line 1 column 726
```

Read each file separately — `evaluate.py` and `--show` take a list of paths and
read line by line, so neither trap reaches them — or add the newline when merging.

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

### A second SOP, for testing whether wording can fix the blind spot

[`sop/oring-strict.txt`](sop/oring-strict.txt) is the same three steps with an
explicit evidence requirement added to each line:

> Step 1: Install the rubber O-ring into the blue block-off cap. **Report PASSED
> only if the O-ring is visibly a separate loose part in the hands and is then
> seated into the cap's groove on camera. If the cap is picked up and used without
> that fitting action being visible, report MISSING.**

It exists because the baseline SOP produced `PASSED` on **all four** clips where the
O-ring was never fitted, inventing detail like *"stretching the O-ring"*. If the
model can be told not to assume, that is the cheapest possible mitigation.

Two things deliberately absent, both from MGA's experience: **no output-format
instruction** — the results parser owns the template, and a prompt specifying its
own format makes it return zero steps — and **no numeric torque value**, since
nothing in a handheld video can evidence a number.

#### Result: it returned NOTHING, and not for the reason you would guess

Run on `1_fail_2_pass_3_pass_A` — the clip that produced a false `PASSED` on the
baseline SOP — the strict SOP produced **zero rows**:

| | the two context-exhausted runs | the strict-SOP run |
|---|---|---|
| `WARN … reasoning block` | present | **absent** |
| ERROR row | none | none |
| rows emitted | 0 | 0 |
| generation | 1174 s / 1055 s | 998 s |

**No warning at all.** So this is a different failure from the context ceiling: the
model generated for over nine minutes and the parser matched none of it.

The likely cause is that the added clauses instruct the model about **status
values** — *"Report PASSED only if…"*, *"report MISSING"* — which is output-shaping,
not judgement-shaping, however it reads. MGA's example established that a prompt
saying what to *cover* parses cleanly while one saying how to lay out the answer
makes `ManualGenerationResultsParserNode` return zero steps. **TVA has no `prompt`
value, but the SOP is a place to make exactly the same mistake.** Confirming it
needs a run with `parser_compute_stats: true`, which attaches template-conformance
stats — that is what the value is for.

Two conclusions, and the second is the useful one:

- **Do not put output or status instructions in an SOP.** Describe the step. The
  parser owns the verdict vocabulary, and competing with it costs a full run and
  returns nothing — strictly worse than the false pass it was meant to fix, because
  a false pass is at least visible.
- **The prompt is not the lever for the blind spot.** The obvious wording fix
  cannot even be evaluated, so `size` — a ~10 mm o-ring at 224×224 — is where to
  look next. `size` is exposed and honoured, and `null` keeps source resolution.

Had it instead returned `MISSING`, that would *still* not have settled anything:
step 1's confusion matrix is `TN=0`, so TVA has never been observed correctly
rejecting that step, and "it started looking" would have been indistinguishable
from "it became pessimistic and now fails the six clips where the O-ring *was*
fitted" without a `1_pass` control. Trading an invisible false-pass problem for an
invisible false-fail one is not an improvement.

**Keep SOP files free of comments.** `load_sop()` does strip `#` lines, but a file
that needs stripping no longer uploads as itself — it uploads as a generated
normalized copy, reintroducing exactly the drift between the file you edit and the
bytes the model sees that this design avoids. Put rationale in the README, not in
the SOP.

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

**This is worth exploiting, not just knowing.** Because nothing that varies per
clip lives in the bundle, `sweep.py` creates **one bundle and reuses it for every
run**:

```
SOP uploaded as oring.txt
bundle bnd_2rr5jj… (reused for all 12 runs)

[1/12] 1_fail_2_pass_3_fail_A  (1=FAIL/2=pass/3=FAIL)
```

An earlier version created a bundle per clip — twelve identical bundles differing
in nothing but their id. If you find yourself creating a bundle per input, that is
a sign you have put something in `values` that belongs in a connector.

## Stage 3 — Configure

```sh
python3 run_tva_example.py --dry-run     # print the exact payloads, call nothing
```

Only two values are worth setting, and both are honoured:

| Value | This example sends | Honoured? | Notes |
|---|---|---|---|
| `max_frames` | **64** | ✅ | Blueprint default is 16 — on a 30 s clip that is one frame every 1.9 s. 64 is also the reader/preprocessor batch size, so frames land in one batch. |
| `max_new_tokens` | **5120** | ✅ | **Reserved up front, so `prompt + this <= 8192`.** At `max_frames: 64` the ceiling is 5,824 — 8192 and 8191 both fail, at different stages. At 2048 the clips that matter return nothing. See below. |
| `size` | not sent (224) | ✅ | Every frame is resized to 224×224, so 1080p clips are no better than 480p — only slower to upload. |
| `prompt` | **does not exist** | — | Not a defect to work around, just absent. The SOP replaces it. |

The preflight reads the blueprint document rather than hardcoding key names, so a
value that is not wired is caught before spending GPU:

```
  max_frames       64       -> honoured   (blueprint default: 16)
  max_new_tokens   5120     -> honoured   (blueprint default: 2048)
```

### `max_new_tokens` is the one value that decides whether you get an answer

f1-0 emits a `<think>` reasoning block before its verdicts, and **that block shares
the output budget.** If the budget runs out inside it, the parser has nothing to
parse and the run returns an empty result:

```
23:02:32  WARN     parser.running   TaskVerificationResultsParserNode: generation ended inside
                                    the model's reasoning block (no `</think>`), so it never
                                    produced an answer; dropping 0 rehearsed row(s).
                                    Raise `max_new_tokens` if the reasoning was truncated.
23:02:32  INFO     completed        Agent execution terminated successfully
23:02:35  SUCCESS  job.completed    Job completed in 814s
```

Note what that run reported: **`job.completed`, no ERROR row, and
`{"id":"1_pass_2_pass_3_fail_A","results":[]}`** — 44 bytes. A caller checking the
job verdict concludes success.

**The budget needed depends on the answer, and the hard cases need more.** At
`max_new_tokens: 2048`:

| clip | outcome at 2048 |
|---|---|
| `1_pass_2_pass_3_pass_A` — everything done correctly | **3 of 3 verdicts**, 678 bytes |
| `1_pass_2_pass_3_fail_A` — step 3 skipped | **empty**, budget spent inside `<think>` |

So the clean clip succeeds and **the clip you actually care about silently returns
nothing.** For a verification agent that is the worst possible failure shape: it
fails only on the inputs that contain a defect. Any accuracy figure gathered
without checking for this is measuring the easy half of the set.

This repo therefore sends **8192**, and treats the overflow as its own outcome
rather than as success — `poll_logs()` returns `empty-reasoning-overflow`,
`run_tva_example.py` exits non-zero, and `--show` on an empty file names the cause
and the fix.

### Set values explicitly — but know that pinning has a cost too

The blueprint's own `max_new_tokens` default changed from **2048 to 8192 during
this session**. Both halves of the usual advice failed here:

- **Relying on the default** means your runs change behaviour when someone
  republishes the blueprint, with nothing in your code or output to say so.
- **Pinning a value** froze this example at 2048 — the old default — which is
  exactly the value that produces empty results. The upstream bump to 8192 was
  almost certainly the fix for this overflow, and pinning opted out of it.

The lesson is not "pin" or "don't pin". It is that **neither protects you, and the
only thing that caught this was reading the log.** Hence the preflight printing
every value next to the blueprint's current default, so a divergence is visible
before the run rather than inferred afterwards.

## Stage 4 — Run

```sh
python3 run_tva_example.py                    # one clip, the default

# several clips, run one at a time. Stems are SPACE-separated, from --list.
python3 sweep.py --clips 1_pass_2_pass_3_fail_A 1_fail_2_pass_3_pass_A

python3 sweep.py --all --dry-run              # the plan, no API calls
python3 sweep.py --all                        # all twelve, ~2.6 hours

# --all reuses ONE bundle and skips clips already in eval_out/, so an
# interrupted sweep resumes rather than restarting. Ctrl-C cancels the
# in-flight run instead of orphaning it on the shared GPU.
```

### If the client dies, the runs do not

The runs live on the **platform**, not in the client. A closed laptop, a dropped
network, a Ctrl-C or an exhausted retry budget abandons results that are already
computed — and run outputs do not expire. So there is a recovery path, and no
reason to ever re-run a sweep because nobody was listening:

```sh
python3 sweep.py --collect                    # harvest every finished run
python3 sweep.py --collect --bundle bnd_5a33…  # just one sweep's runs
```

It skips clips already on disk and ignores runs still in flight, so it is safe to
run repeatedly while a sweep is still going. It also asks each run for its results
regardless of the instance `status`, because that field is not a reliable
terminality signal.

`python3 sweep.py --cancel-all` is the other half: an abandoned run holds the
shared GPU, so cancel what you are not waiting for.

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

### Results — 12 clips, and one finding that matters more than the accuracy

All twelve clips, one SOP, `max_frames: 64`, `max_new_tokens: 8192`. Committed
under [`reference/sweep/`](reference/sweep), so this is re-scorable offline:

```sh
python3 evaluate.py reference/sweep/*.jsonl
```

```
clip                                    step 1            step 2            step 3
1_fail_2_pass_3_fail_A              FAIL>none   !!    PASS>none   !!    FAIL>none   !!
1_fail_2_pass_3_pass_A              FAIL>PASSED !!    PASS>PASSED OK    PASS>PASSED OK
1_fail_2_pass_3_pass_B              FAIL>PASSED !!    PASS>PASSED OK    PASS>PASSED OK
1_fail_2_pass_3_pass_C              FAIL>none   !!    PASS>none   !!    PASS>none   !!
1_fail_2_pass_3_pass_E              FAIL>PASSED !!    PASS>PASSED OK    PASS>PASSED OK
1_fail_2_pass_3_pass_F              FAIL>PASSED !!    PASS>PASSED OK    PASS>PASSED OK
1_pass_2_pass_3_fail_A              PASS>PASSED OK    PASS>PASSED OK    FAIL>MISSING OK
1_pass_2_pass_3_fail_B              PASS>PASSED OK    PASS>PASSED OK    FAIL>MISSING OK
1_pass_2_pass_3_fail_C              PASS>PASSED OK    PASS>PASSED OK    FAIL>MISSING OK
1_pass_2_pass_3_pass_A              PASS>PASSED OK    PASS>PASSED OK    PASS>PASSED OK
1_pass_2_pass_3_pass_B              PASS>PASSED OK    PASS>PASSED OK    PASS>PASSED OK
1_pass_2_pass_3_pass_C              PASS>PASSED OK    PASS>PASSED OK    PASS>PASSED OK

overall step accuracy: 26/36 (72.2%)
step 1: 6/12 (50%)   step 2: 10/12 (83%)   step 3: 10/12 (83%)
step 1 confusion: TP=6 TN=0 FP=4 FN=0 — all four errors are FALSE PASSES
status distribution: PASSED=26, FAILED=0, MISSING=3
```

**Ignore the 72.7%.** It averages a step with no negative examples (step 2) against
two that behave completely differently, and the difference between them is the
whole result:

| omitted step | what is missing on screen | detections |
|---|---|---|
| **step 3 — the wrench** | a large steel tool that never enters frame | **3 of 3** ✅ |
| **step 1 — the o-ring** | a ~10 mm black ring, absent from a hand movement | **0 of 6** ❌ |

**Every single clip where the o-ring was skipped went undetected** — four as false
PASSES and two as empty results. Not one was caught. (The sixth took four attempts
to run at all: a missing video object, a missing SOP object, then the token ceiling
twice. None of those were properties of the clip.)

#### It does not hedge on the o-ring — it confabulates the action

The three false passes describe, in detail, an installation that never happened:

```
1_fail_2_pass_3_pass_A   PASSED [2.0-5.0s]   "…picking up the blue cap and O-ring, then
                                              placing the O-ring into the groove of the blue cap."
1_fail_2_pass_3_pass_B   PASSED [1.0-6.0s]   "…picking up the blue cap and O-ring, STRETCHING the
                                              O-ring, and placing it onto the cap groove."
1_fail_2_pass_3_pass_E   PASSED [3.0-14.0s]  "…picking up the blue cap and installing the rubber
                                              O-ring onto it."
1_fail_2_pass_3_pass_F   PASSED [2.6-4.4s]   "…picking up the blue cap and installing the O-ring
                                              onto it."
```

"Stretching the O-ring" is a specific, plausible, entirely invented observation.
**A reason field that reads as careful evidence is not evidence that the model
looked.** Compare the wrench, where it correctly reasons from *absence*:

```
1_pass_2_pass_3_fail_A   MISSING  "The video ends immediately after the cap is threaded on by
                                   hand; no wrench is shown and no tightening action takes place."
```

That asymmetry is the deployable conclusion: **TVA is reliable for "was this tool
used" and unreliable for "was this small part fitted"** — and it fails in the
dangerous direction, passing work that was not done. On this evidence, do not
deploy it as an unattended gate on small-part steps. `max_frames: 64` on a 21 s
clip samples every 0.33 s, so this is not a sampling artifact; every frame is
resized to 224×224, which is the more likely culprit.

**`FAILED` was never emitted once** — 24 PASSED, 3 MISSING, 0 FAILED. That matches
what the clips contain (omissions, not botched attempts), so the status choice is
correct; it also means nothing here exercises `FAILED`.

#### The real ceiling is `max_seq_len: 8192`, and it is not exposed

Two clips returned `results: []`, both `1_fail` clips. The log on the hardest one —
both steps 1 and 3 skipped — is the most revealing line in this whole example:

```
WARN  TaskVerificationResultsParserNode: generation ended inside the model's reasoning
      block (no `</think>`), so it never produced an answer; DROPPING 37 REHEARSED ROW(S).
```

**The model produced 37 candidate verdict rows inside its reasoning and every one
was discarded**, because the closing `</think>` never arrived within the budget.
The answer existed; the harness threw it away. Note also 1174 s of runtime for a
21-second clip — the reasoning is where the time goes.

**And "raise `max_new_tokens`" does not fix it**, which is what the platform's own
warning suggests and what an earlier version of this README said. Two separate
checks bound it, and they fire at different times:

```
# at graph instantiation, ~40 s in (after whisper loads)
ERROR  invalid node config: max_new_tokens (8192) must be below max_seq_len (8192)

# at fusion time, ~7 MINUTES in (after newton-fusion loads)
ERROR  preparing Newton request: estimated prompt 2368 tokens + 8191 reserved for
       generation exceeds max_seq_len=8192; use fewer frames or raise max_seq_len /
       lower max_new_tokens
```

So **`max_new_tokens` is RESERVED UP FRONT**, not a cap on what may be produced:
the real rule is `prompt + max_new_tokens <= max_seq_len`, with `max_seq_len` at
**8192** and not exposed as a bundle value. The prompt is measured, so the ceiling
is computable:

| `max_frames` | prompt (tokens) | max usable `max_new_tokens` |
|---|---|---|
| 16 | ~1,192 | **7,000** |
| 32 | ~1,584 | 6,608 |
| **64** (used here) | **2,368** (platform's own figure) | **5,824** |
| 128 | ~3,936 | 4,256 |

The two clips that returned nothing needed more reasoning than ~5,800 tokens, and
**no caller-side setting provides that.** The frames/budget trade-off is not a free
lever either: dropping to 16 frames buys 1,176 more generation tokens but samples a
21-second clip every 1.3 s instead of every 0.33 s.

`tva_api.check_token_budget()` computes this client-side and refuses in about a
second, because the second check above costs the full cold start:

```
** prompt (~2368 tokens at max_frames=64, size=224) + max_new_tokens (8191)
   exceeds max_seq_len=8192.
   At max_frames=64, max_new_tokens must be <= 5824. Lower it, or use fewer frames.
refusing to start a run that cannot fit in the context window.
```

This is also a 32× divergence from the design doc, which motivates its
preprocessing work with F1-0's **256K** context window (MGA's example measured a
video at "0.15% of F1-0's 256K window"). Whatever this deployment serves,
`max_seq_len` is **8192**.

⚠️ **The rules tightened mid-session.** Nine clips ran at `max_new_tokens: 8192`
between 23:37 and 02:47 and produced output; at 03:58 the same value was rejected
at graph instantiation, and 8191 then failed at fusion time. Same pinned
blueprint — the node schema and the request-preparation check changed underneath.
The scripts default to **5120**, comfortably inside the 5,824 ceiling.

Combined with the false passes above: **the clips that exhaust the context are the
same clips that contain the most defects.** Difficulty, reasoning length and defect
count all move together, so the failures cluster precisely where a verification
agent is supposed to earn its keep — and the fix is not available to the caller.

⚠️ **`max_new_tokens: 8192` was accepted during this sweep and is rejected now.**
Nine clips ran at 8192 between 23:37 and 02:47; a run at 03:58 died at graph
instantiation on the `must be below max_seq_len` check, on the same pinned
blueprint. The node schema changed underneath, not the blueprint. The scripts
default to **8191**.

#### Neither lever works, because they all draw on the same 8192 tokens

Two mitigations were tried on a clip that had produced a false `PASSED`. **Both
returned zero rows**, and neither carried the reasoning-block WARN — the parser
simply matched nothing, silently, after ~1000 s of GPU each:

| attempt | changed | result |
|---|---|---|
| stricter SOP | added per-step evidence requirements | **0 rows**, no WARN |
| higher resolution | `size` 224 → 448, frames 64 → 32 | **0 rows**, no WARN |

The arithmetic explains why this was close to unavoidable:

| run | size | frames | asked | prompt | generation actually available |
|---|---|---|---|---|---|
| the 9 that produced verdicts | 224 | 64 | 8192 | 2,368 | **5,824** |
| stricter SOP | 224 | 64 | 5120 | 2,462 | 5,120 |
| higher resolution | 448 | 32 | 4096 | 3,936 | 4,096 |

**The nine successful runs asked for more than the window and got everything left
over — 5,824 tokens.** Every mitigation reduces that number: a longer SOP adds
prompt tokens, and raising `size` adds them quadratically. So the two things you
would naturally try to fix the blind spot both **starve the reasoning that produces
the verdicts at all.**

Note this is not deterministic — one clip did return verdicts at 5,120 — so budget
alone does not predict failure; clip difficulty interacts with it. But the
direction is clear, and it means **`size` cannot currently be evaluated as a fix**:
at `size: 448` the ceiling leaves 4,192 generation tokens, well under what the
working runs used.

**Do not guess a budget.** An earlier version of these scripts defaulted to a
"safe-looking" 5120, which gave the model *less* room than the runs that worked, for
no reason. `tva_api.max_usable_new_tokens()` now computes it from the actual frames
and size — 5,760 at the defaults — and the runners print what they chose.

The real fix is not caller-side. **Raise `max_seq_len`.** At 8192, spatial detail,
temporal resolution and reasoning length are in a three-way fight over one budget,
and a ~10 mm o-ring loses.

#### One infrastructure failure, reported as a completed job

`1_fail_2_pass_3_pass_F` never ran:

```
ERROR  skipping video "1_fail_2_pass_3_pass_F.mp4": downloading video "s3://…" failed
       …
       job.completed  Job completed in 440s
```

`job.completed`, an ERROR row, and no results — so a caller reading the job verdict
sees success. Worth distinguishing carefully when harvesting: "no results" means
either *not finished yet* or *finished and produced nothing*, and conflating them
tells you to keep waiting for a run that is already over. `--collect` reads the log
and says which:

```
agt_6yb49ws6…  FAILED     no results, and it will not produce any:
    skipping video "1_fail_2_pass_3_pass_F.mp4": downloading video "s3://…" failed
```

The failure looks transient — a download, not a bad input — so a retry is the right
response. It is a `1_fail` clip, which is why the o-ring finding rests on 5 usable
clips rather than 6.

#### What this example does and does not establish

Establishes, on 11 scored clips of one task:

- the mechanics end to end, including the runtime SOP path
- **an asymmetric, reproducible blind spot** — 0/6 on the small part, 3/3 on the
  tool, across four independent takes of the same omission
- that the `reason` field can be fluent and wrong, so it cannot be used as a
  confidence signal
- that the reasoning budget fails on the hardest inputs, and drops verdicts it
  had already formed

Does **not** establish: accuracy on other tasks, on longer videos, on
attempted-but-botched steps (`FAILED` never fired), or with a different SOP
wording. One task, one operator, one camera angle. **Treat 0/6 as a red flag worth
reproducing, not as a measured error rate.**

## Reproducing the empty-output failure

The failure most worth recognising is the one that looks like success:
`job.completed`, no ERROR row, and no verdicts. `run_tva_example.py` reproduces it
and reports each symptom as CONFIRMED or NOT REPRODUCED.

**Offline — instant, no API key, no GPU:**

```sh
python3 run_tva_example.py --repro-empty --offline
```

**Live — ~13 minutes:**

```sh
python3 run_tva_example.py --repro-empty
```

```
  clip            1_fail_2_pass_3_fail_A.mp4
                  steps 1 AND 3 skipped — no o-ring, no wrench
  max_new_tokens  5760   <-- the MAXIMUM the context window allows

  READ THIS: the budget is already as high as it can go. There is no caller-side
  setting that fixes this run — a larger budget is rejected, and raising `size` or
  `max_frames` makes it worse by taking tokens from the same window.
```

**The budget is deliberately at its ceiling.** An earlier version of this mode
pinned `max_new_tokens: 2048` to force the failure. That reproduced it, but taught
the wrong lesson — that raising the budget is the fix. **It is not.** The two clips
that returned empty in the sweep were already at the ceiling: they asked for 8192,
and the engine gave them everything left after the prompt. This mode reproduces
*that*, and its log is the most revealing line in the example:

```
WARN  … generation ended inside the model's reasoning block (no `</think>`),
      so it never produced an answer; DROPPING 37 REHEARSED ROW(S).
```

**Thirty-seven candidate verdicts existed and were discarded.** The answer was
formed; the window ran out before it could be closed.

The clip is what makes it fail, not the setting: this one has **both** step 1 and
step 3 skipped, and the model reasons far longer about absences than about a clean
pass. An all-correct clip fits easily and will not reproduce it. That asymmetry is
the deployment risk stated as a reproduction — **the runs that fail are the ones
containing defects.**

The avoidable variant is still available for contrast, and says so when you use it:

```sh
python3 run_tva_example.py --repro-empty --max-new-tokens 2048   # NOTE: avoidable version
```

If the default does not reproduce, that is expected variance rather than a broken
script — reasoning length is not perfectly predictable. Lower `--max-new-tokens`
and it will, but note that you have then reproduced the fixable failure rather than
the structural one.

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
- **An upload can report success and leave no object.** Twice in one session
  `/v0.5/files` returned `is_valid: true` with a `file_id`, and the object was
  absent — once a video, once the SOP — surfacing only inside the run ~13 minutes
  later as `S3 object not found`, with `job.completed` on the job. `is_valid` is
  not sufficient; **read the file back** (`GET /v0.5/files/download/{file_id}`)
  before spending a run, which is what `tva_api.upload_file` now does.
- **`file_id` IS the basename, so re-uploading replaces the object.** A run holds a
  reference to the object as it was at resolution time, so re-uploading the same
  name while a run is in flight is a plausible way to lose its inputs. Unique
  per-run names avoid it entirely, at the cost of readable filenames.
- **The bundle endpoint is plural.** `POST /agents/bundle` → 404; use
  `/agents/bundles`. The RED and OSM example repos still use the singular form.
- **Starting a run returns 202, not 201.**
- **Poll `/logs`, not `/events`.** `/events` carries only "run started" and
  "dispatched to JOS".
- **The instance `status` field is not a terminality signal** — judge from the log
  stream. (MGA saw `running` persist 20+ minutes after a pod died with exit=1.)
- **`job.admitted`'s queue figure is not the real wait, and the real wait can be
  invisible.** One run reported `Job scheduled after 32s in queue` and then did
  not start a pod for **11+ minutes**, with no job of ours holding the GPU and the
  instance `status` reading `running` throughout. `GET /agents/instances` is
  scoped to your org, so a job from another org can block you with nothing in your
  view to explain it. Judge progress from `job.started` and expect blind spots.
- **Values are accepted whether or not they do anything.** A key absent from the
  blueprint's `values`, or present but never referenced as `${values.<key>}`, is
  stored on the bundle and silently ignored.
- **A run can report `job.completed` and return nothing.** `max_new_tokens: 2048`
  on a clip with a skipped step produced `{"id":…,"results":[]}` — 44 bytes, no
  ERROR row, only a WARN. **Count the verdicts; neither the status nor the exit
  code distinguishes it from success.** Committed as
  [`reference/tva-output-1_pass_2_pass_3_fail_A-mnt2048-EMPTY.jsonl`](reference/tva-output-1_pass_2_pass_3_fail_A-mnt2048-EMPTY.jsonl).
- **The reasoning block shares the output budget**, so the tokens a run needs
  depend on how hard the judgement is — and the clips containing a defect need
  more than the clean ones.
- **A run can log an ERROR and still report `job.completed`.** One clip failed to
  download from S3 and never ran; the job reported completed after 440 s with an
  ERROR row and no results. Judge from the ERROR row, not the verdict.
- **Runs are not admitted, or executed, in submission order.** Twelve runs
  submitted together sat **~60 minutes with none admitted**, then completed out of
  order over ~90 minutes. Admission is capacity-gated, not a formality, so
  submitting a batch up front does not reserve an order — only that the work
  eventually happens.
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

## Recommendations

0. **Investigate the small-part blind spot before anyone deploys this as a gate.**
   0 of 6 detections on an omitted o-ring, with fluent invented descriptions of it
   being fitted, against 3 of 3 on an omitted wrench. `size: 224` is the obvious
   suspect — a ~10 mm ring is a handful of pixels — so exposing `size`, or a
   region-of-interest crop, is the first thing worth testing. This is a
   correctness issue, not a configuration one.
1. **Fix the sink format on the canonical `tva` blueprint** — `json/per-request`
   has no registered connector, so the blueprint that ships cannot emit output.
   Highest priority: it makes the agent unusable via its own key.
2. **Validate the graph before loading models, not after.** A blueprint that
   cannot instantiate currently costs the full ~7-minute cold start; connector and
   node-config validation up front turns 996 s into about 1 s.
3. **Do not discard rehearsed rows on an unterminated reasoning block.** One run
   dropped **37 candidate verdict rows** because `</think>` never arrived inside
   the budget. The answer had been formed; emitting those rows with a
   `truncated: true` marker would turn a total loss into a usable, flagged result.
4. **Treat an empty `results` list as a failure, not a completed job.** The parser
   already emits a WARN naming the cause; promoting it to ERROR, or surfacing a
   `rows_emitted: 0` field, would stop a silent empty result reading as success.
   This matters more for TVA than for MGA because the overflow correlates with the
   inputs that contain defects.
5. **Raise `max_seq_len`, or expose it.** At 8192 total, with ~1,600 spent on a
   64-frame prompt, a hard clip cannot fit its reasoning and there is nothing the
   caller can do — 8191 is the maximum accepted `max_new_tokens` and fewer frames
   buys little. The design doc describes a 256K context; this deployment serves
   8192. Failing that, reserve tokens for the answer so truncated reasoning still
   yields verdicts.
6. **Expose `min_temporal_similarity_threshold`.** The design doc's longest section
   is the temporal compression algorithm, with a documented 70% compression and
   25% performance gain, and none of it is reachable from a bundle.
7. **Document that `step` is 0-based** in the output schema. Human-authored SOPs
   are 1-based, and the mismatch fails quietly.
8. **Emit progress events during processing.** Between `input_started` and the
   terminal event there is nothing across 5–6 minutes, so neither a failure nor
   the time spent can be attributed.
9. **Reconsider loading whisper when the video has no audio track.** It costs ~40 s
   of every run here and contributes nothing.

## Repo layout

| Path | What it is |
|---|---|
| `run_tva_example.py` | **Start here.** Single-clip end-to-end run: upload video + SOP → bundle → run → watch `/logs` → download → print → score. `--repro-empty` reproduces the empty-output failure and reports each symptom as CONFIRMED / NOT REPRODUCED. `--dry-run`, `--show` and `--repro-empty --offline` need no key. |
| `sop/oring.txt` | The SOP, one step per line. Edit this to change the procedure — no Python involved. |
| `sweep.py` | Batch path over many clips, reusing one bundle. Sequential by default so a misconfiguration costs one run, not twelve; `--submit-all` instead claims every queue slot up front. `--collect` harvests finished runs after a disconnect and `--cancel-all` frees the GPU. `--list` and `--dry-run` are offline. |
| `evaluate.py` | Offline scorer: per-step accuracy, confusion, FAILED-vs-MISSING split, interval sanity. No API key. |
| `tva_api.py` | Shared stdlib client (streaming upload, retries, `/logs` polling, wired-value preflight). |
| `reference/` | Committed run outputs and a blueprint snapshot, so every number here is checkable offline with no API key. Includes the 44-byte empty result from the `max_new_tokens: 2048` overflow, kept as evidence because it is the failure worth recognising. |
| `docs/` | The TVA design doc this example implements (gitignored — internal). |

## Not yet built

- **An audio-bearing clip.** The whisper half of the pipeline is loaded on every
  run and exercised by none of these.
- **A `size` sweep, now the most valuable experiment here** — promoted because the
  SOP route has been tried and failed. The blind spot is most likely a resolution
  problem: at `size: 224` a ~10 mm ring is a few pixels. `size` is exposed and
  honoured, and `null` keeps the source resolution, so this is one bundle value
  away from being tested.
- **A `parser_compute_stats: true` run on the strict SOP**, to confirm that the
  zero-row result is template non-conformance rather than something else. One run,
  and it is what the value exists for.
- **A clip with a step attempted and done WRONG.** `FAILED` never fired across 12
  clips because every failure here is an omission, so that third status is
  completely unexercised.
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
