# Task Verification Agent — End-to-End Example

A realistic, end-to-end walkthrough of running the **Task Verification Agent (TVA)**
on the Archetype AI Platform (prod): feed it a procedure video and an SOP, get back
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

> **Where the numbers come from.** Every result, timing and limit in this README was
> measured on the **dev** platform in August 2026, against the `tva` blueprint as it
> stood then. The example now targets **prod** (`https://platform.u1.archetypeai.app/api`),
> whose `tva` blueprint was republished on 2026-10-15 with a different transcriber and
> new value ranges — see [Running on prod](#running-on-prod). The 12-clip baseline was
> re-run there on 2026-10-26: [the o-ring blind spot holds, and a new failure appeared](#results-on-prod--12-clips-2026-10-26).
> **Agent serving is available only on dev, as of 2026-10-28** (not on prod or staging).
> It loads the agent once and answers each clip in ~3 s instead of a ~8-minute run:
> [Stage 6](#stage-6--serve-load-once-one-query-per-clip-dev).

> **Agent Lifecycle 2.0:** the scripts run on projects, packages, deployments and
> sessions; bundles and instances are being removed from the platform. A run's
> `max_frames`, `size` and `max_new_tokens` are now set on a copy of `tva` in the
> project `TVA o-ring`, one copy per setting (`tva-f64-s224-t5760`), and each clip
> is a session on that copy's deployment. The results below were measured with
> bundles. What changed, and how to move your own scripts: [MIGRATION.md](MIGRATION.md).

TVA V1 is zero-shot. The design doc states there are no build-time optimization
steps, and the platform agrees: the `tva` blueprint pins its own models
(`newton-fusion-1-0` and `whisper-base` on prod; `newton-fusion:1.0` and
`whisper:large-v3` on the August dev blueprint), so like
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
from a blueprint *value*, so the instruction is fixed for the life of the agent.
TVA accepts a `.txt` and routes it `source.text -> prepare.in` through a dedicated
`PrepareSOPNode` — so every run carries its own procedure and **one agent serves
every SOP.** That single edge turns "describe this video" into "check this video
against my procedure", and most of this README is about the consequences.

## Quick start — one command

Nothing to build and nothing to fit — **no `atai_core`, no GPU, no AWS**. Python 3
and a prod API key:

```sh
cp .env.example .env           # add ATAI_API_KEY; the endpoint is already set
python3 run_tva_example.py
```

```sh
# .env
ATAI_API_KEY=<API key>
ATAI_API_ENDPOINT=https://platform.u1.archetypeai.app/api
```

Upload video + SOP → find or make the agent for this setting (a copy of `tva` with
the values set, its package and a batch deployment, once per setting) → open a
session → poll it → download → print the report and score it. Budget **20-30 minutes**, not the ~13 of job time: a run is
754-1614 s once it starts (of which ~7 min is model loading, on every run, never
cached) and **the queue is not included in that.** On dev, where these figures were
measured, one GPU was shared across orgs; observed waits ranged from 12 s to 796 s,
and one 41-minute wall clock for a 17-minute job (measured with bundles). If nothing
has happened for ten minutes, it is queued, not hung: the session stays `pending`
until it is dispatched.

To check every clip instead of one — same agent, same SOP, one session per video:

```sh
python3 sweep.py --list                    # what is on disk, with labels. No API calls.
python3 sweep.py --all --dry-run           # see the plan, no API calls
python3 sweep.py --all --submit-all        # queue all 12 up front, then collect
python3 evaluate.py eval_out/tva-output-*.json
```

**`--submit-all` holds your place in the queue; it did not run the clips in
parallel on dev**, which had one GPU and executed jobs one at a time, so the wall
clock stayed hours either way — the paired 12-clip sweep took ~2.2 h this way against
a ~2.6 h sequential estimate. **Prod ran two jobs at once** on 2026-10-26, and the
12-clip sweep took 51 min from first pod to last result. What it prevents is losing the GPU *between* your
clips: submitting one at a time leaves the slot unclaimed after each run, and a job
from another org can take it. An 11-minute gap between `job.admitted` and a pod
starting is the clearest case of that we saw.

The trade-off is blast radius. Sequential submission costs one run if you got a value
wrong; `--submit-all` costs all twelve. Run `--dry-run` first, and check the SOP and
`values` it prints.

**`--all` skips any clip already in `eval_out/`, so in a fresh clone of this repo it
runs nothing.** The twelve outputs are committed, which is deliberate — the results
are re-scorable offline and nobody has to spend 2.6 h of shared GPU to read this
README. `--dry-run` says which it would run:

```
already done, would be SKIPPED (12):
would run (0), sequentially, ~13 min each -> ~0 min (~0.0 h):
```

That is the resume behaviour working, not a failure: an interrupted sweep continues
where it stopped instead of restarting. Two ways to actually run:

```sh
python3 sweep.py --all --submit-all --fresh          # re-run, REPLACING eval_out/
python3 sweep.py --all --submit-all --out-dir eval_rerun   # or keep both side by side
python3 evaluate.py eval_rerun/tva-output-*.json
```

`--out-dir` is the one to reach for when comparing configurations — it is how the
paired sweep was kept separate from the original. `--fresh` refuses to write into
`reference/`, which holds the committed outputs every number in this README is scored
from.

A sweep is hours long on a shared GPU, so two commands matter as much as the sweep
itself. **The runs live on the platform, not in your client** — see
[If the client dies, the runs do not](#if-the-client-dies-the-runs-do-not):

```sh
python3 sweep.py --collect          # harvest finished runs after a disconnect
python3 sweep.py --cancel-all       # stop what you are not waiting for
python3 cleanup.py --yes            # delete the sessions, deployments and packages
```

`state/lifecycle.json` (gitignored) records the project, each setting's copy,
package and deployments, and every session the moment it is opened: that is what
`--collect`, `--cancel-all` and `cleanup.py` walk. It belongs to one deployment of the
platform and one account; to run against another, move `state/` aside.

Every script here is **stdlib-only**: no virtualenv, no `requirements.txt`, no
install step. Three of the four commands above touch no API at all, and scoring
never does:

```sh
python3 evaluate.py reference/*.json
```

## Running on prod

Checked against prod on 2026-10-26. Two things changed; the client handles both.

**The endpoint layout.** The `platform.*` endpoints end in `/api`, and the files API
lost its version prefix:

| | legacy `api.*` hosts | prod (`https://platform.u1.archetypeai.app/api`) |
|---|---|---|
| agents | `<endpoint>/agents/…` | `<endpoint>/agents/…` |
| files | `<endpoint>/v0.5/files/…` | `<endpoint>/files/…` — `/v0.5` is a 404 |
| a run output's `ref` | resolves under `/v0.5` | resolves under `<endpoint>` |

`tva_api.files_root()` picks the right prefix from the endpoint (an endpoint ending in
`/api` is the new layout), so the legacy hosts still work. The `/agents` API is no
longer dev-only.

**The `tva` blueprint** (`blp_3aswaf72w58etvp7v4kc4xrmxw`, 2026-10-15) differs from the
August dev snapshot in [`reference/blueprint-tva-snapshot.json`](reference/blueprint-tva-snapshot.json):

| | August dev | prod, 2026-10-15 |
|---|---|---|
| models | `newton-fusion:1.0`, `whisper:large-v3` | `newton-fusion-1-0`, **`whisper-base`** (see below) |
| graph nodes | `fusion`, `whisper` | `generator`, `transcriber` |
| `max_frames` | default 16 | default **64**, must be `1..=64` |
| `max_new_tokens` | default 16384 (observed) | default **8192** |
| `size` | unchecked | must be `224..=2048` |
| `min_temporal_similarity_threshold` | not wired | **wired**, default `0.0` (off) |
| `artifacts` map | pinned S3 checkpoints | absent |
| sink | `json/per-request` | `json/per-request` |

The blueprint now documents its own video-to-SOP pairing rules, which match what this
example measured: a `<stem>.txt` pairs with every video whose name *contains* `<stem>`,
the longest matching stem wins, and a single `.txt` applies to every video.

**`newton-fusion:1.0` and `newton-fusion-1-0` are one model under two names.** The
second is the first made a valid Kubernetes name: atai_core's `mount_name()` lowercases an
artifact key and folds everything else to `-`, and its test asserts exactly this mapping.
It is the modelpack `modelpacks/models/newton-fusion/1.0.0/35b-a3b-fp8-base` (Newton-Fusion
35B-A3B, fp8), the only version there, renamed when every model became a DVC-pinned
modelpack in early October. Whether its weights are byte-identical to what
the August blueprint loaded cannot be checked: that checkpoint was pinned nowhere we can
compare. So the step-3 regression below is tied to the **republish** — transcriber, node
graph and model packaging changed together — not proven to come from the model. It is
also not the model behind `/query`'s Newton C 2.6 (`Newton::c2_6_8b_fp8_…`), a separate 8B
checkpoint.

The preflight reads the blueprint document, so the node renames need no code change,
and every value this example sends is still wired.

### Results on prod — 12 clips, 2026-10-26

Same clips, same SOP (`sop/oring-numbered.txt`), same values (`max_frames: 64`,
`max_new_tokens: 5760`) as the August baseline. Committed in [`eval_prod/`](eval_prod):

```sh
python3 evaluate.py eval_prod/tva-output-*.json
```

```
clip                         step 1            step 2            step 3
1_fail_2_pass_3_fail_A    FAIL>PASSED  !!    PASS>PASSED OK    FAIL>MISSING OK
1_fail_2_pass_3_pass_A    FAIL>MISSING OK    PASS>PASSED OK    PASS>PASSED  OK
1_fail_2_pass_3_pass_B    FAIL>PASSED  !!    PASS>PASSED OK    PASS>PASSED  OK
1_fail_2_pass_3_pass_C    FAIL>PASSED  !!    PASS>PASSED OK    PASS>PASSED  OK
1_fail_2_pass_3_pass_E    FAIL>PASSED  !!    PASS>PASSED OK    PASS>MISSING !!
1_fail_2_pass_3_pass_F    FAIL>PASSED  !!    PASS>PASSED OK    PASS>FAILED  !!
1_pass_2_pass_3_fail_A    PASS>PASSED  OK    PASS>PASSED OK    FAIL>MISSING OK
1_pass_2_pass_3_fail_B    PASS>PASSED  OK    PASS>PASSED OK    FAIL>MISSING OK
1_pass_2_pass_3_fail_C    PASS>PASSED  OK    PASS>PASSED OK    FAIL>MISSING OK
1_pass_2_pass_3_pass_A    PASS>PASSED  OK    PASS>PASSED OK    PASS>FAILED  !!
1_pass_2_pass_3_pass_B    PASS>PASSED  OK    PASS>PASSED OK    PASS>PASSED  OK
1_pass_2_pass_3_pass_C    PASS>PASSED  OK    PASS>PASSED OK    PASS>PASSED  OK

overall step accuracy: 28/36 (77.8%)     dev, August: 26/36 (72.2%)
step 1: 5 errors, all FALSE PASSES — credits work that was not done
step 3: 3 errors, all FALSE FAILS  — rejects work that was done
status distribution: PASSED=28, FAILED=2, MISSING=6
```

| | prod | dev, August |
|---|---|---|
| clips that returned no verdicts | **0 of 12** | 2 of 12 |
| omitted o-ring caught | **1 of 6** | 0 of 6 |
| omitted wrench caught | 4 of 4 | 3 of 3 answered |
| wrench used, but rejected | **3 of 9** | 0 |

**The o-ring blind spot holds.** Five of six omitted o-rings pass, most with the same
invented detail as on dev (*"placing an O-ring into its groove"*). One run caught it
for the first time on a single-omission clip: *"The video does not show the rubber
O-ring being installed into the blue block-off cap."*

**New on prod: correct wrench work is rejected.** Step 3 was reliable on dev. On prod
it rejected the wrench step on three clips where the frames show the wrench clamped on
the cap and turning it — committed under
[`reference/prod-2026-10-26/`](reference/prod-2026-10-26). The reasons describe the
end of the clip, where the wrench lies on the mat, not what happened:

- `pass_A` — *"picks up the wrench but does not use it … then place the manifold into
  a plastic bag."* There is no bag in the clip.
- `pass_E` — *"The video ends after the cap is hand-tightened."* It runs 20 s past the
  wrench step.
- `pass_F` — *"The wrench is present on the mat but is never used."*

So errors now run **both ways**: step 1 passes work that was not done, step 3 fails work
that was. One of the three is a clean take with every step done, which is the case a QA
gate cannot afford — it rejects a good assembly.

**Still deterministic.** `pass_A` from the sweep and from a separate single run 23
minutes earlier are byte-identical apart from the input id, so these verdicts are
reproducible rather than noise.

**Not visible from the API:** which GPU served these runs. `/logs` carries no node or
device field.

## What comes back

```json
{"id":"1_pass_2_pass_3_pass_A","results":[
  {"step":0,"status":"PASSED","timestamp_start":2.0,"timestamp_end":9.0,
   "frame_start":60.0,"frame_end":270.0,
   "reason":"The video clearly shows the person picking up the blue cap and O-ring, then installing the O-ring onto the cap."}, …]}
```

Four properties will catch you out, none of them documented in the blueprint:

- **`step` is 0-BASED.** A 3-step SOP returns 0, 1, 2. A scorer that assumes 1-based
  reads a *perfect* run as 2-of-3 — which looks like a model that missed the last
  step, not a join bug. `evaluate.py` auto-detects it.
- **`frame_*` are SOURCE frames, not sampled ones** — `frame = timestamp × fps`
  (810 = 27.0 × 30), so they exceed `max_frames`. Seconds come free either way.
- **`len(results)` is not the step count.** The model repeats itself and the parser
  keeps every repetition: 21 rows for a 3-step SOP, once. Collapse by `step` and
  prefer the informative row — a placeholder (`reason: "..."`, no timestamps) can
  arrive first. This is also the likely cause of the empty results; see
  [Lesson 4](#4-bigger-budgets-do-not-buy-answers).
- **Do not trust `reason` as evidence.** It reads like a report of what was on
  screen, but a fabricated observation is word-for-word as confident as a real one.
  [Why, with the frames to prove it.](#rewriting-the-sop-does-not-fix-it-and-the-reason-field-is-not-evidence)

## Stage 1 — Select clips, and check what the labels actually mean

Twelve takes of the same three-step assembly, 21–43 s each, 30 fps — ten at 640×480
and two at 1920×1080. **Ground truth is encoded in the filename** —
`1_pass_2_pass_3_fail_A.mp4` means step 1 correct, step 2 correct, step 3 not; the
trailing letter is a take id. That keeps the labels visible in `ls` and impossible
to desynchronise from the media.

The resolution spread matters less than it looks: **every frame is resized to
`size × size` (224 by default) before the model sees it**, so a 1080p source is
downscaled 8.6× and ends up with no more detail than a 640×480 one. Worth
remembering when judging whether a small part is visible — the answer is about 224,
not about the source.

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

For this set **`MISSING` is the status that matches the video**, and the naming is the
opposite of what the label word "fail" suggests. That prediction held completely:
across 12 clips, 8 SOP variants, 4 blueprint versions and every budget tried,
**`FAILED` has never once been emitted.** Every non-PASSED verdict has been `MISSING`.

A binary pass/fail label cannot express that distinction, so `evaluate.py` scores the
verdict (PASSED vs not) and reports the split separately rather than hiding it inside
one accuracy number.

**Step 2 is `pass` in all twelve clips**, so read its column as a consistency
check rather than as a detection result — "always say PASSED" also scores 100% on
it. Steps 1 and 3 are where the discrimination is, and both have failures.
`sweep.py --list` prints this note itself so nobody quotes a step-2 number as
accuracy.

**These clips have no audio track at all** — verified with `ffprobe`: one video
stream, no audio stream. The blueprint runs `audio_reader → whisper →
prompt.audio` regardless. It does **not** fail: whisper loads (`large-v3` on dev:
35–36 s download, 3.3–4.3 s load, on every run) and contributes nothing. So ~40
seconds of every run is spent loading a model that cannot help, and **the audio half of the fusion is
untested by this example.** MGA's run C proved the audio path carries real signal
on a narrated video; nothing here exercises it.

**The clips are committed**, in `data/clips/` — 80 MB, largest file 11 MB, no LFS.
They were recorded internally for this example, so there is no third-party licence
to respect, and committing them means **the example is self-contained**: no
`yt-dlp`, no `ffmpeg`, and no download step that can half-succeed. That is a real
difference from the MGA example, where a missing `ffmpeg` let `yt-dlp` exit 0
having merged nothing and the failure surfaced several steps later.

## Stage 2 — Write the SOP (the only lever you have)

The SOP is a **text file, one step per line**. The baseline is
[`sop/oring-numbered.txt`](sop/oring-numbered.txt) — the default for every script
here, and what produced every result in this README:

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

[`sop/oring.txt`](sop/oring.txt) holds the same three steps **without** the `Step N:`
prefixes — the bare format the design doc shows. **The prefixed one is the baseline
because the format decides whether a defect-bearing clip answers at all.** A controlled
2×2, `max_new_tokens` pinned to 5752 so the format was the only variable, each cell
reproduced byte-identically:

| | `oring-numbered.txt` | `oring.txt` (bare) |
|---|---|---|
| all steps performed | 3 rows · 3/3 ✅ | 3 rows · 3/3 ✅ |
| steps 1 and 3 skipped | **21 rows · 3/3** ✅ | **0 rows · no verdicts** ❌ |

The prefixed format *duplicates* the `[index]` the pipeline already adds, and is
therefore the one the doc implies you should not use — and it is the one that answers.
Note that the easy clip cannot see this: both score 3/3 there, so **a smoke test on a
clean recording would have concluded the formats are equivalent.** Outputs for all four
cells are in [`reference/grid-5752/`](reference/grid-5752):

```sh
python3 evaluate.py reference/grid-5752/*.json
```

This is about *whether* you get output, not about what the verdict says — no format
changed a verdict (see [Lesson 3](#3-the-sop-is-not-the-lever-and-the-prompt-is-not-the-gap)).

The uploaded text is exactly the step lines, with blanks and `#` comments stripped, so
the file you edit and the text the model sees cannot drift apart. Provenance comes
from git, where every SOP is committed — not from the platform's file list, which only
holds whatever was uploaded last.

### Do not put status instructions in an SOP

[`sop/oring-strict.txt`](sop/oring-strict.txt) adds an evidence requirement to each
line — *"Report PASSED only if the O-ring is visibly a separate loose part in the
hands… otherwise report MISSING"* — and returned **zero rows**, in a way that is not
the context ceiling:

| | the context-exhausted runs | the strict-SOP run |
|---|---|---|
| `WARN … reasoning block` | present | **absent** |
| rows emitted | 0 | 0 |

**No warning at all**: the model generated for over nine minutes and the parser
matched none of it. The likely cause is that those clauses instruct the model about
**status values**, which is output-shaping however it reads — and MGA established the
same failure, where a prompt specifying its own layout makes the parser return zero
steps. TVA has no `prompt` value, but the SOP is a place to make the identical
mistake.

So describe the step and leave the verdict vocabulary to the parser, which already
has those instructions in its system message ([below](#what-the-system-message-already-demands)).

Two honest caveats. This is **one run on one clip**, and a later variant using the
same `report PASSED / Otherwise report MISSING` phrasing *did* produce a parser
warning with three drafted rows — a different failure — so "status words break the
parser" is plausible, not established. And it does not matter much either way:
**rewriting the SOP does not fix the blind spot at all.** Eight variants across five
mechanisms were tried, including a faithful port of a prompt that scored 98% on these
clips elsewhere, and not one verdict changed —
[the null result](#rewriting-the-sop-does-not-fix-it-and-the-reason-field-is-not-evidence).

**Comments in SOP files are free.** `load_sop()` strips `#` lines and blanks, and only
the surviving step lines are uploaded — verified against the platform copy. Each
variant in [`sop/`](sop) carries its own rationale block for that reason.

### The SOP has an expected format, and it is in the design doc

The doc is explicit that this matters:

> *"the specific details and format of the system and instruction are important and
> **should not be arbitrarily set or modified**"*

and its golden reference is two lines:

```python
steps = [f"[{index}] {sop_step}" for index, sop_step in enumerate(video_sop_steps)]
instruction = f"Steps: {steps}"
```

**The pipeline adds its own `[index]`, and `enumerate` makes it 0-based.** The
system message then instructs the model in the same scheme:

> *"where N is the step's number written as a plain integer **starting at 0** (the
> first step is `step 0:`, the second is `step 1:`…)"*

The doc also shows a worked **example** of the assembled instruction, and it settles
what a step string should contain — bare text, with the indices supplied:

```
Instruction
Steps:

[0] Unbox the Sactional side and seat pieces.
[1] Attach the hardware and feet to the seat base.
[2] Connect the new Sactional pieces to each other and the existing couch using clamps.
[3] Place the seat and back cushions onto the assembled frame.
```

So `Steps:` and `[0] [1] [2]` are what `PrepareSOPNode` **builds**; they do not belong
in your file. [`sop/oring.txt`](sop/oring.txt) is therefore three bare lines, and the
pipeline turns them into exactly the shape above.

**An earlier version of this README claimed the doc contained no example SOP and
treated the plain format as an inference.** That was wrong — the example is in a
figure. The format is documented, and the default now follows it.
[`sop/oring-numbered.txt`](sop/oring-numbered.txt) preserves the `Step N:` variant
because **every committed result was produced with it**, before the example was
found. Its prefixes double up on the automatic index (`[0] Step 1: …`) with
disagreeing numbers; whether that cost anything is still untested.

### Do not key anything off the output extension

Every output in this repo is saved as `.json`, which is what the payload is: **one
JSON object, with no trailing newline.** But the platform's own extension is not
derived from the declared sink format and has changed mid-session — the same pinned
blueprint, `format: jsonl/per-request` throughout, emitted `.jsonl` at 21:46 and
`.json` at 23:37 with nothing differing caller-side.

So match on `__output_` and strip whatever suffix is there, as `--collect` does, and
read `format` as a contract about record structure rather than filenames. The
apparent "TVA uses JSON, MGA uses JSONL" split was an artefact of comparing runs from
either side of a platform-side change; both emit `.json` now.

**The missing trailing newline is a real trap when merging:**

```sh
cat out_A.json out_B.json > merged.json   # ONE line, two objects, parses as neither
```

### What the system message already demands

Reading it changes how to interpret two findings above — they are the model
**disobeying explicit instructions**, not merely behaving oddly:

| the system message says | what was observed |
|---|---|
| *"Mark a step PASSED only if it is **clearly shown** being carried out"* | four false PASSES on an o-ring never fitted |
| *"one line each, and **output nothing else**"* | 21 rows for a 3-step SOP |
| *"mark it MISSING if it is not shown at all"* | correct when it fires — 3/3 on the wrench |

It also confirms two things this example had inferred from behaviour: `MISSING`
carries no timestamps by design, and *"It is OK if there are temporal gaps between
the steps"* — so TVA genuinely does not tile the timeline, unlike MGA.

And it explains why the stricter SOP returned zero rows. That SOP added *"Report
PASSED only if…"* clauses — **competing with instructions the system message
already gives**, in a format the doc warns not to modify. The lesson is sharper
than "do not specify output format": the system message has already covered
status semantics, so an SOP repeating them can only conflict.

### Pairing a video with its SOP: matching stems

A run's inputs arrive as **one flat list of file ids**. Nothing in the payload says
"this text belongs to that video", so the pipeline works it out from the names. The
blueprint documents four cases, and the matching is **substring, not equality** — a
`<stem>.txt` pairs with any video whose name *contains* `<stem>`:

| you pass | what happens |
|---|---|
| **1 video + 1 SOP** | that procedure applies to the video **whether or not the names match** |
| 1 SOP + N videos | the same procedure applies to every video, stems irrelevant |
| N SOPs + N videos | each video takes the SOP whose stem it contains; **longest match wins**, with a warning |
| a video no SOP matches | falls back to `default_text`, and is **skipped** if that is unset |

Two things follow, and the first explains a result further down this page:

- **For the 1:1 runs everything here does, the filenames cannot affect pairing.** A
  single `.txt` applies to the video regardless of name — which is why a paired and an
  unpaired run of the same clip reproduced each other's `rehearsed` counts exactly.
  Stem matching only decides anything when a run carries **several** SOPs.
- **Substring matching is looser than it looks.** `file-1.txt` pairs with
  `cam-file-1-run.mp4`, and if two SOP stems both match a video the longer one wins.
  Short or overlapping stems can therefore mis-pair in a multi-input run. The naming
  below is immune by accident and by design: every id is long, fully qualified and
  unique, so no stem is a substring of another.

**The default naming satisfies that and two other requirements at once:**

```
1_pass_2_pass_3_pass_A-oring-numbered-20260813T154342Z-2f81.mp4
1_pass_2_pass_3_pass_A-oring-numbered-20260813T154342Z-2f81.txt
```

| part | why |
|---|---|
| the clip stem | recognisable in an org-wide file list |
| the SOP stem | an id says WHICH procedure a run was checked against |
| `<UTC timestamp>` | sorts and greps; tells you which upload was yours and when |
| `<4 hex>` | the part that actually guarantees uniqueness — **two people starting in the same second is exactly the case being fixed** |

The last two matter because **this repo is meant to be used by several people at
once.** An org shares one flat file namespace and `file_id` IS the basename, so
without them two colleagues running the same clip write to the same object — and a run
pins its inputs at *input-resolution* time, so the second upload destroys the first's
queued run. `api.run_suffix()` generates the tail **once per run** (once per *sweep*
for `sweep.py`, so a whole batch greps as a unit) and both halves of the pair share it,
because a per-file suffix would break the very pairing it protects.

Two consequences worth knowing:

- **The idempotent-upload skip no longer fires** on the default path, because the name
  is new every time. That is fine — uniqueness makes the clobber impossible by
  construction rather than defending against it — but a 12-clip sweep now re-uploads
  **80 MB** of video. At ~0.8 s per clip against a ~1000 s run, the time cost is noise.
- **Uploads accumulate.** There is no file-cleanup endpoint in this API, so every
  sweep leaves its objects behind. (`cleanup.py` deletes sessions, deployments and
  packages, not files.)

`--pair-sop` keeps the clip-stem-only form for reproducing the platform team's literal
recommendation, plus the same suffix. [`sop/paired/`](sop/paired) holds one copy per
clip for it, with a `regenerate.py --check` that fails if a copy drifts.

**Measured before the mechanism was known, and it agrees with it.** The same clip was
run paired and unpaired at three configurations, and each pair reproduced the other's
distinctive `rehearsed` count exactly — which is what the one-SOP rule above predicts:

| config | unpaired | paired with matching stem |
|---|---|---|
| 64 frames / 5752 | rehearsed **21** | rehearsed **21** |
| 32 frames / 6536 | rehearsed **0** | rehearsed **0** |
| 16 frames / 6928 | rehearsed **3** | rehearsed **3** |

Reproducing 21, 0 and 3 respectively is not coincidence — those are fingerprints of
the generation. **The SOP filename does not reach the model**, so every result
committed here, all produced with a single shared SOP whose name did not match the
clip, stands unaffected.

For **multi-input** runs the convention is not optional — it is the only thing that
can associate a video with its procedure.

**A second reason to prefer it:** a per-clip SOP name makes the clobber bug
impossible. Two concurrent runs sharing one `oring.txt` can overwrite each other's
input object, which destroyed three runs during this work; `X.txt` and `Y.txt` cannot
collide.

### Where the SOP lives on the wire — and where it does not

**Not in the agent.** The values live on the project's copy of `tva`, set when it is
copied, one copy per setting:

```json
POST /agents/blueprints/tva/copy
{"project_id":"prj_…","blueprint_key":"tva-f64-s224-t5760",
 "overrides":{"values":{"max_frames":64,"max_new_tokens":5760,"size":224}}}
```

The package and the batch deployment built from it carry no values at all. The SOP
rides with the **session**, as a source connector input:

```json
POST /agents/sessions
{"deployment_id":"dpl_…",
 "connectors":{"source":[{"type":"file","id":"1_pass_2_pass_3_pass_A.mp4","format":"mp4"},
                         {"type":"file","id":"sop_1_pass_2_pass_3_pass_A.txt","format":"txt"}]}}
```

So one deployment serves every SOP, and **`MultiModalSource` routes the two inputs
by their `format` field, not by position** — order does not matter. Looking for the
SOP on the blueprint and not finding it is the expected first confusion here.

**This is worth exploiting, not just knowing.** Because nothing that varies per clip
lives in the agent, every script reuses **one copy, package and deployment per
setting** — across runs, sweeps and scripts — and opens a session per clip:

```
agent tva-f64-s224-t5760: copy blp_…, package pkg_…, deployment dpl_… (reused for all 12 runs)

[1/12] 1_fail_2_pass_3_fail_A  (1=FAIL/2=pass/3=FAIL)
  session_id=ses_…
```

With bundles, an earlier version created a bundle per clip — twelve identical
bundles differing in nothing but their id. If you find yourself making a copy or a
deployment per input, that is a sign you have put something in `values` that belongs
in a connector.

## Stage 3 — Configure

```sh
python3 run_tva_example.py --dry-run     # print the exact payloads, call nothing
```

These are the values that matter here, and all are honoured. Defaults and ranges
are the current prod blueprint's; the "highest tried" column is from dev.

| Value | This example sends | Highest tried | Notes |
|---|---|---|---|
| `max_frames` | **64** | 64 | Prod default is 64 and the blueprint rejects anything outside `1..=64`. The August dev default was 16 — one frame every 1.9 s on a 30 s clip. 64 is also the reader/preprocessor batch size, so frames land in one batch. Also run at 32 and 16. |
| `max_new_tokens` | **5760** | **16384** | Shares the context window with the prompt. Prod defaults to 8192. On dev, 16384 — that blueprint's default — ran fine, which puts the served `max_seq_len` at **≥ 18,754**. It returned no more answers than 5760 did, so **the ceiling is deliberately not the default**: see [Lesson 4](#4-bigger-budgets-do-not-buy-answers). |
| `size` | not sent (224) | **448** | Must be `224..=2048` on prod. Every frame is resized to `size × size`, so a 1080p clip carries no more detail than a 480p one. Tried once at 448 (with frames cut to 32 to fit) and it returned **no verdicts** — visual tokens grow quadratically, starving the generation. |
| `min_temporal_similarity_threshold` | not sent (0.0) | — | **New on prod.** Collapses runs of near-identical frames; `0.0` disables it. Untested here. |
| `prompt` | **does not exist** | — | Not a defect to work around, just absent. The SOP replaces it. |

These values go on the project's copy of `tva` (`overrides.values`), not on a
package or a session, which take none: **each setting is its own copy**, keyed
`tva-f<frames>-s<size>-t<budget>`, made the first time it runs and reused after. A
copy with no overrides would run at the blueprint's own defaults, which today include
`max_new_tokens` 8192 — the budget that looped inside `<think>` and returned
`results: []` on a hard clip here ([Lesson 4](#4-bigger-budgets-do-not-buy-answers)).
A copy refuses a key the blueprint does not declare with a `400`, but accepts one that
is declared and never referenced.

The preflight reads the document of the blueprint the copy is made from rather than
hardcoding key names, so an unwired value is caught before spending GPU, and every
value is printed next to the blueprint's current default — which has changed under
this example more than once.
From a dev run in August:

```
max_new_tokens not set -> using 5760 (the ceiling here is 16314; a bigger budget is
more room to LOOP, not to reason — see api.DEFAULT_MAX_NEW_TOKENS).

  max_frames       64       -> honoured   (blueprint default: 16)
  max_new_tokens   5760     -> honoured   (blueprint default: 16384)
  size             224      -> honoured   (blueprint default: 224)
```

## Stage 4 — Run

```sh
python3 run_tva_example.py                    # one clip, the default
python3 sweep.py --clips 1_pass_2_pass_3_fail_A 1_fail_2_pass_3_pass_A
python3 sweep.py --all --submit-all           # all twelve — see Quick start
```

Ctrl-C cancels the in-flight session rather than orphaning it on the shared GPU.

**Anything long, run it with `--background`.** `run_tva_example.py`, `sweep.py`,
`iterate_sop.py` and `sweep_serve.py` all take it: the command relaunches itself under
`nohup`, so closing the terminal does not kill it, with `caffeinate -i -w <pid>` keeping
the Mac awake until it ends, and logs to `logs/<script>.log`:

```sh
python3 sweep_serve.py --all --chunk 4 --background
# running in the background (pid 28680, caffeinate keeps the Mac awake)
#   follow:  tail -f logs/sweep_serve.log
#   stop:    kill 28680      # cleans up like Ctrl-C
```

`kill <pid>` is delivered as Ctrl-C, so a sweep still cancels its session, or its
serving session and deployment, on the way out. `--background other.log` picks the log file.

### If the client dies, the runs do not

The runs live on the **platform**, and outputs do not expire — so a closed laptop or a
Ctrl-C never costs a sweep:

```sh
python3 sweep.py --collect                     # harvest every finished run
python3 sweep.py --cancel-all                  # an abandoned run holds the GPU
```

`--collect` is safe to repeat mid-sweep: it walks the sessions recorded in
`state/lifecycle.json` (each is recorded the moment it is opened), skips what is on
disk, ignores sessions still in flight, and reports failed or cancelled ones as never
going to produce output. `--setting tva-f64-s224-t5760` restricts it to one setting.

### Cold start dominates, and nothing is cached

Measured on dev with `whisper:large-v3`. On prod (2026-10-26), `whisper-base` loads in
~4 s, newton-fusion still downloads from scratch on every run (~4m50s) and loads in
1m48s–5m42s (usually ~1m50s), and a job takes 488–729 s. Nothing is cached between runs there either.

| phase | time |
|---|---|
| whisper download + load | 35–36 s + 3.3–4.3 s |
| **newton-fusion download + load** | **~5 min + ~1.5 min** |
| generation, 31 s clip | ~5 min |
| **total** | **763–1614 s** |

Two back-to-back runs on the same GPU each downloaded newton-fusion from scratch. So
**~7 of every 13 minutes is model loading**, and the marginal cost of one more clip in
a sweep is far lower than the first. **Agent serving pays it once per agent instead of
once per clip** — [Stage 6](#stage-6--serve-load-once-one-query-per-clip-dev), dev only as of 2026-10-28.

**Dev serialized jobs; prod ran two at once.** In the prod sweep each new pod started
within 2 s of one of the two running jobs finishing. On dev, `job.admitted` was
misleading about when yours starts:

```
21:51:57  job.admitted   Job scheduled after 12s in queue     <-- not the real start
21:59:00  job.started    Job started — first pod is now running
```

Seven minutes between them, because another run held the GPU. Judge the real start
from `job.started`.

### Generation is deterministic — for short outputs

The same clip and SOP reproduced **byte-identically** across two orgs' API keys, three
budgets, a 10-hour gap, two blueprint versions, and an A/A pair 3 seconds apart. So a
matching md5 means an exact reproduction, and a differing one means something real
changed — which rules out drift and randomness as explanations elsewhere in this
document.

⚠️ All of those outputs were ~800 bytes. At 3,675 bytes — right at the `</think>`
boundary — two blueprint versions **disagreed**: one emitted 21 rows, the other
rehearsed the same 21 and dropped them. Digests are unproven near the budget.

## Stage 5 — Evaluate

```sh
python3 evaluate.py reference/*.json
python3 evaluate.py eval_out/tva-output-*.json --verbose    # with the reason for every step
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
python3 evaluate.py reference/sweep/*.json
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
status distribution: PASSED=27, FAILED=0, MISSING=3
                     (27 + 3 = 30 = the 10 clips that answered x 3 steps)
```

**Ignore the 72.2%.** It averages a step with no negative examples (step 2) against
two that behave completely differently, and the difference between them is the
whole result:

| omission | detections |
|---|---|
| **the wrench** — a large steel tool that never enters frame | **3 of 3** ✅ |
| **the o-ring, when it is the ONLY thing missing** | **0 of 4** ❌ |
| **the o-ring, when the wrench is ALSO missing** | **2 of 2** ✅ — but only at `numbered @~5755`; other configs return nothing |

**The o-ring CAN be detected — reproducibly — but only under one configuration.**
A run on the double-omission clip returned the correct verdicts with a precise
reason (*"The installation of the O-ring into the blue cap is not shown in the
video."*), and a repeat at a near-identical budget produced **byte-identical
output**. Two of two.

A second sweep of all twelve clips, with **stem-matched SOPs** (`--pair-sop`, the
pairing the platform team recommends) and the same SOP text, reproduced the finding
exactly — 22/33 overall, and **step 1 correct on 6 of 6 `1_pass` clips and 0 of 6
`1_fail` clips.** Committed in [`eval_paired/`](eval_paired):

```sh
python3 evaluate.py eval_paired/tva-output-*.json
```

Pairing changed only *how* the failures present — two clips that false-passed
unpaired returned nothing instead — so the step-1 error count fell from 4 to 2 purely
because wrong answers became no answers. Not an improvement.

### Rewriting the SOP does not fix it, and the reason field is not evidence

Seven SOPs were tried on the two hardest clips, chosen to fail for different reasons.
All are committed under [`sop/`](sop), all results under [`eval_iter/`](eval_iter):

```sh
python3 iterate_sop.py --compare
```

**Five declarative variants gave byte-for-byte the same wrong answer.** Same
blueprint, same 64 frames, same 8192 budget — only the text differed:

| SOP | mechanism | step 1 on `..._3_pass_E` |
|---|---|---|
| `oring-numbered` | plain action description | PASSED ❌ |
| `oring-focused` | attention direction + action verbs | PASSED ❌ |
| `oring-discriminative` | scene-anchored state change | PASSED ❌ |
| `oring-maximal` | **the 98%-accuracy Direct Query prompt, ported** | PASSED ❌ |
| `oring-hybrid` | action + state change together | PASSED ❌ |

Two more dropped the declarative mood — one a question, one a conditional, on the
theory that asserting the action gives the model something to echo. **Neither produced
a verdict at all**, at 8192 or 16384.

`oring-maximal` is the one that moves the problem. It is a faithful port of the prompt
that reportedly scored **98% on these same clips** via Direct Query — scene framing,
component inventory, attention direction, permissive verbs — and it fails identically
to the bare three-line SOP. So the gap between 98% and 0-of-6 is **not prompt content**.
Whatever causes it is structural, and out of a SOP author's reach.

#### The SOP's own diction lands in the "observation"

Each `reason` is a compression of its own SOP's step-1 clause, and the differences
track the SOPs exactly:

| the SOP writes | the reason writes |
|---|---|
| the **small black rubber** O-ring (`focused`) | "the O-ring" |
| the **black** O-ring (`maximal`, the only SOP phrased this way) | "the **black** O-ring" |
| "attaches, places, or aligns" (`hybrid`) | "**attaches** it to" — the first verb offered |
| "**leaves the cluster** of spare O-rings" (`discriminative`) | "picking up a black O-ring **from the cluster**" |

Change one adjective in the SOP and that adjective appears in the model's account of
what it saw. An unplanned controlled experiment, and a tidier one than anything here was
designed to be: the only varied input is diction, and the diction lands in the field
that is supposed to describe the video.

> **`reason` is a restatement of what was asked, in the asker's vocabulary — not a
> report of what was seen.** It cannot be used as evidence or as a confidence signal.

The frames settle it. For the interval TVA itself reported for step 1 (2.7–5.5 s), the
cluster of spare O-rings sits in **pixel-identical positions** while the worker goes
straight from picking up the cap to threading it —
[`reference/oring-never-fitted-1_fail_2_pass_3_pass_E/`](reference/oring-never-fitted-1_fail_2_pass_3_pass_E).
The described action does not occur, and the model supplied a confident timestamp for it.

#### What it actually checks is OBJECT PRESENCE, not performance

One rule accounts for every result. It was found by writing a SOP whose step 3 asks for
*"thread sealant from a tube"* — a prop that exists in no clip — and running it on a
clip where all three real steps are performed:

```
step 0  PASSED   The worker picks up the O-ring and places it onto the blue cap.
step 1  PASSED   The worker screws the blue cap into the manifold port by hand.
step 2  MISSING  No thread sealant tube is shown or used in the video.
```

It refused the step whose object was absent, with a correct reason. So it *does* compare
the description against the frames:

| step | is the object in the scene? | performed? | verdict |
|---|---|---|---|
| the wrench, on `*_3_fail` clips | **absent from every frame** | no | `MISSING` ✅ 3 of 3 |
| the sealant tube | **absent — no such prop exists** | no | `MISSING` ✅ |
| the o-ring, on `1_fail_*` clips | **PRESENT**, in the spares cluster | no | `PASSED` ❌ 0 of 6 |

**The question it answers reliably is "is this thing in the video at all?" — not "did
the worker do this with it?"** When the object is present, the action is assumed. The
O-rings are laid out on the mat in every clip, so that check passes every time. Across
both sweeps and every configuration, **TVA has never once reported the o-ring as not
fitted.**

That also explains the variant that should have worked: `oring-discriminative` asked for
a ring *leaving the cluster* — but the cluster is right there, satisfying a presence
check with the very object the step was written to discriminate against.

The empty results fit the same story. All four non-answers in the paired sweep were
`1_fail` clips (`C(6,4)/C(12,4)` = 15/495 ≈ **3%** by chance), and the failure mode is
the budget consumed inside `<think>`. The clips that give the model a contradiction are
the clips where it reasons until the budget is gone — **emptiness and the false pass are
two outcomes of one conflict.**

> **TVA detects missing tools and missing parts. It does not detect a missing assembly
> step when the part is sitting on the table.**

For incoming-QA work that is close to backwards: the parts are always laid out, so the
omissions a reviewer most needs caught are exactly the ones in the blind spot, while the
ones it catches — a tool absent from the whole recording — are the easiest for a human
to notice unaided.

## Stage 6 — Serve: load once, one query per clip (dev)

```sh
python3 sweep_serve.py --clips 1_pass_2_pass_3_pass_A --repeat 3 --out-dir eval_probe
python3 sweep_serve.py --all --background      # every clip on one loaded agent; log logs/sweep_serve.log
python3 sweep_serve.py --all --keep            # leave it loaded…
python3 sweep_serve.py --all --session ses_… --fresh   # …and reuse it: no load at all
python3 evaluate.py eval_serving/tva-output-*.json
```

Stages 4 and 5 run in **batch**: upload, run, poll, download, and every run downloads
and loads the models again. **Agent serving** deploys the same setting's package as
`serving` and opens one session on it: the agent loads once and then answers
`POST /agents/sessions/{id}/query` for each clip in a single round trip, with no
upload, queue or polling. Twelve clips cost one load and twelve answers instead of
twelve loads. At the end `sweep_serve.py` cancels the session and stops and deletes
the serving deployment, which is what holds the GPU worker; `--keep` leaves both
running for `--session`, and `cleanup.py` removes them later.

The measurements below were made with bundles, on a serving *instance* of the `tva`
bundle; the query and its answer are unchanged. Rerun on 2026-11-06 with a serving
deployment, all 12 answers came back byte-identical.

> **Agent serving is available on dev only, as of 2026-10-28.** Not on prod or
> staging yet: batch (Stages 4 and 5) is what prod offers.

It came with GPU-backed agents, and its limits as measured then:

- **One GPU agent at a time on dev.** A second one is refused while the first runs, so
  `sweep_serve.py` releases its agent on exit, Ctrl-C included, unless `--keep`. An
  agent with no query for 20 minutes was released; a serving session now closes
  after 30 idle minutes, the deployment's default.
- **The video goes inline.** A served agent takes no data-service file ids yet:
  `{"model": "tv", "query": "<SOP>", "file_ids": ["base64:<mp4>"]}`, the request
  shape of atai_core's `tv` modelpack. The SOP travels in `query`, so one agent still
  serves every SOP.
- **The whole request is capped at 2 MiB**, about 1.5 MB of video. Every clip here is
  over it (5–11 MB), so `sweep_serve.py` re-encodes each one into `data/clips-serving/`
  (gitignored) at 640×480, CRF 28, no audio: 0.6–1.4 MB, 0.74–1.75 MiB as a request.
  TVA samples 64 frames at 224 px anyway, but the bytes differ from batch's.
- **Each query has a 58 s budget.** The first query to a fresh agent used to 504; since
  then the agent warms its models up before it reports `running`.
  `sweep_serve.py` still retries 5xx until the load deadline.
- **An instance's `/logs` showed the load as it happened** (from 2026-10-28): *"Still
  loading newton-fusion-1-0 model after 4min0s"* once a minute, then the warm-up and
  whisper's load. A session has no `/logs`, only lifecycle `/events`, so the load is
  again a wait for `status: running`; `sweep_serve.py` prints the events.
- **A query can hang, and a hung query does not keep the agent alive.** In the first
  `--chunk 4` sweep (2026-10-26), one query never returned. The agent saw nothing for 20
  minutes and was released (`completed`, exactly 20 min after the last answer), and every
  later query got `409 … only a running agent answers queries`. So `sweep_serve.py` gives
  a query 90 s (the platform's budget is 58 s), retries it at most 3 times, stops the
  sweep on a 409, and resumes on the next run from the chunks still unanswered. That
  agent also took 1,385 s to load, against 926–929 s for the two before it.
- **A bad input used to end the agent; now it fails only its query.** On the resumed
  sweep (2026-10-26), a 1.1 s tail chunk (35 frames, below the blueprint's 64-frame
  `max_frames`) got `409 … the agent's graph failed: video preprocessing failed`, and the
  agent was gone. A 40-frame tail had earlier been answered, but in 24.6 s against
  ~2.5 s. Both were then fixed: a clip with an odd frame count is padded, and a clip the
  agent cannot read gets a `400` naming the problem while the agent stays loaded
  (checked with MGA's agent on dev, 2026-10-28: a 35-frame clip answered, an
  audio-only mp4 and random bytes got 400s, and the next query answered).
  `sweep_serve.py` still folds any tail chunk under 64 frames into the chunk before it,
  so every chunk sent has 89 or more. Batch never meets this: every whole clip is over
  600 frames.

**The answer is shaped differently.** The served sink answers in `atai-records`: the
step rows are `response.data` themselves, numbered from 0, with no `{"id", "results"}`
around them. `sweep_serve.py` wraps them so `evaluate.py` scores serving and batch
alike.

### What it measured — `pass_A`, dev, 2026-10-26

One clip, three queries per agent, on two agents 20 minutes apart, against the batch
run of the same clip on dev the same day ([`eval_dev/serving/`](eval_dev/serving), log in
[`eval_dev/serving-probe.log`](eval_dev/serving-probe.log)):

| | batch, dev | serving, dev |
|---|---|---|
| model load | ~7 min, **on every clip** | **929 s and 926 s (15.5 min), once per agent** — from `run` to `status: running` |
| per clip | ~80 s of generation in a 502 s job | first query **8.1 s** and **3.7 s** (no 504 either time), then **3.0–3.2 s**; ~2.7 s of it on the server |
| 12 clips, estimated | ~1.7 h | ~17 min |
| repeated query | byte-identical | step rows identical across all six, on both agents |
| verdicts | PASSED / PASSED / FAILED | **the same**, false FAILED on step 3 included |

- **The verdicts match; the details do not, byte for byte.** Step 1 starts at 0.0 s
  served against 3.9 s in batch, and the reasons are worded differently (both invent a
  plastic bag on step 3). The served clip was re-encoded to fit the cap, so this is
  the input changing rather than serving disagreeing with batch.
- **The load is longer than batch's**, in line with the 11–12 minutes the platform team
  quotes, and there is no preloading yet. Serving pays off from the second clip.

### All 12 clips, whole, on one agent — dev, 2026-10-26

```sh
python3 sweep_serve.py --all --background
python3 evaluate.py eval_serving/tva-output-*.json
```

**694 s for all twelve: 657 s of model load, then twelve answers of 2.7–3.3 s.** Batch
takes ~1.7 h for the same set on dev. The score matches batch, the errors do not:

| | overall | step 1 (O-ring) | step 3 (wrench) |
|---|---|---|---|
| batch, prod (original clips) | 28/36 | 7/12 | 9/12, 3 false FAILs |
| serving, whole clip (re-encoded) | 28/36 | 6/12 | 10/12, 2 false FAILs |

Three of twelve clips changed verdict between them:

```
clip                     truth   batch  serving
1_fail_2_pass_3_pass_A   FPP     MPP    PPP     <- the one O-ring catch, lost
1_fail_2_pass_3_pass_E   FPP     PPM    PPP     <- a false FAIL fixed
1_fail_2_pass_3_pass_F   FPP     PPF    PPM     <- still a false FAIL, now MISSING
```

So **serving the re-encoded clip is not a drop-in for batch on the original**: same
accuracy, different mistakes. Whether the difference is the re-encode or serving itself
is still open — `pass_A`, the one clip compared both ways, matched batch exactly — and a
batch run on a re-encoded clip settles it. Batch's one O-ring catch is fragile either
way: re-encoding the clip lost it, and so did chunking the original bytes (below).

### Chunking: shorter videos, merged per step (exploratory)

```sh
python3 sweep_serve.py --all --chunk 4 --dry-run    # cut, print what would be sent, call nothing
python3 sweep_serve.py --all --chunk 4              # the clips' own bytes, no re-encode
python3 sweep_serve.py --all --chunk 10 --overlap 4
python3 merge_chunks.py eval_serving_c4             # merge per rule, score each, offline
```

An option on top of the whole-clip sweep, not a replacement. With `--chunk`, each clip
goes as several shorter videos on the same loaded agent, and every chunk's raw answer is
saved to `<out-dir>/chunks/<clip>.json`. Why try it:

- **No re-encode.** Every clip here has a keyframe every ~2 s and runs at ~253 KB/s, so a
  chunk of 4 s or less fits the cap as the clip's own bytes, cut with `-c copy` on
  keyframes. Longer chunks are re-encoded at CRF 23, lighter than a whole clip's 28.
  A chunk starts on the keyframe at or before its slot and always runs to the slot's end,
  so chunks never leave a gap; where keyframes are irregular (`pass_E`, `pass_F`) that
  makes a few chunks up to 6 s. A tail under 64 frames is folded into the chunk before
  (see the limits above). `--chunk 4 --dry-run` on all twelve: 83 chunks, every one the
  original bytes, 3.0–6.0 s and 89–177 frames, at most 1.55 MB.
- **No length limit.** A 5-minute recording becomes 30–75 queries of ~3 s on one agent.
- **Denser frames.** 64 frames over 4 s is ~16 fps, against ~2 fps over a whole clip.

What makes it hard: a served agent keeps nothing between queries, so each chunk is
verified against the **whole SOP**. Most steps come back `MISSING` in most chunks, a
step can span chunks, and the clip's verdict depends on how the chunks are merged.
`merge_chunks.py` applies four rules to the saved answers, moves chunk times to clip
times, and scores each rule with `evaluate.py`:

| rule | a step is | the risk it carries here |
|---|---|---|
| `any` | `PASSED` if any chunk passed it | more false passes: the O-ring is on the table in every chunk |
| `strict` | `FAILED` if any chunk failed it | more false FAILs: a 4 s slice of a step can look incomplete |
| `majority` | the more common of `PASSED`/`FAILED`; a tie is `FAILED` | |
| `longest` | the verdict with the longest interval | |

A step no chunk saw is `MISSING`. Each merged row says how the chunks voted
(`"chunks": "MISSINGx6 PASSEDx2"`). Merging is offline, so one GPU session can compare
every rule and chunk size.

#### Results — 4 s chunks, 12 clips, dev, 2026-10-26

```sh
python3 merge_chunks.py eval_serving_c4
```

83 chunks, all the clips' own bytes, ~2.5 s a query: the answering itself took under 3
minutes on one agent (it took three agents to get there — the hang and the short-chunk
crash above). Against the prod batch baseline on the full clips:

| | overall | step 1 (O-ring) | step 3 (wrench) | errors |
|---|---|---|---|---|
| batch, prod (whole clips) | 28/36 | 7/12 | 9/12 | 5 false passes, 3 false FAILs |
| serving, whole clips (re-encoded) | 28/36 | 6/12 | 10/12 | 6 false passes, 2 false FAILs |
| **chunks, `any`** | **30/36** | **6/12** | **12/12** | 6 false passes |
| chunks, `longest` | 30/36 | 6/12 | 12/12 | 6 false passes |
| chunks, `majority` | 28/36 | 6/12 | 10/12 | 6 false passes, 2 false FAILs |
| chunks, `strict` | 26/36 | 6/12 | 8/12 | 6 false passes, 4 false FAILs |

```
clip                     truth   batch  any  majority  strict
1_fail_2_pass_3_fail_A   FPF     PPM    PPM  PPM       PPM
1_fail_2_pass_3_pass_A   FPP     MPP    PPP  PPF       PPF     <- batch's only O-ring catch, lost
1_fail_2_pass_3_pass_E   FPP     PPM    PPP  PPP       PPF     <- false FAIL fixed
1_fail_2_pass_3_pass_F   FPP     PPF    PPP  PPP       PPP     <- false FAIL fixed
1_pass_2_pass_3_fail_A   PPF     PPM    PPM  PPM       PPM     (wrench omissions: MISSING under
1_pass_2_pass_3_fail_B   PPF     PPM    PPM  PPM       PPM      every rule — no chunk ever
1_pass_2_pass_3_fail_C   PPF     PPM    PPM  PPM       PPM      passed a wrench that was not used)
1_pass_2_pass_3_pass_A   PPP     PPF    PPP  PPF       PPF     <- false FAIL fixed
```
(P = PASSED, F = FAILED, M = MISSING; the three clips every source got right are left out.)

- **Chunking fixes the step-3 false FAILs.** Under `any`, all three wrench uses that the
  whole clip rejected pass, and all three wrench omissions still come back `MISSING`:
  step 3 is 12/12. That fits the earlier reading of the false FAILs — on a whole clip the
  model judged the end state; a chunk that contains the tightening shows it.
- **It makes the O-ring blind spot complete.** 0 of 6 omitted O-rings caught, against
  1 of 6 in batch: the one catch (`1_fail_2_pass_3_pass_A`) is lost, as predicted —
  every chunk shows the O-ring pile, so each is another chance to pass it. The overall
  gain is all step 3; on the step that matters most for QA, chunking is worse.
- **Rules that punish a single FAILED chunk bring the false FAILs back.** A 4 s slice of
  the tightening can look unfinished, so `strict` and `majority` reject work `any` accepts.
- **One chunk invented a step.** `1_fail_2_pass_3_pass_F`'s first chunk returned a 4th
  row (`step: 3`, `MISSING`, "the video ends before the person uses a wrench…") for a
  3-step SOP. The merge carries it as an extra `MISSING` step; `evaluate.py` scores only
  the SOP's three.

#### 10 s chunks, 4 s overlap — dev, 2026-10-27

```sh
python3 sweep_serve.py --all --chunk 10 --overlap 4 --background
python3 merge_chunks.py eval_serving_c10o4
```

51 chunks, re-encoded at CRF 23 (a 10 s chunk is ~2.5 MB raw, over the cap), answered in
about 2 minutes after a 739 s load.

| | overall | step 1 (O-ring) | step 3 (wrench) |
|---|---|---|---|
| 10 s chunks, `any` / `majority` / `longest` | **30/36** | 6/12 | **12/12** |
| 10 s chunks, `strict` | 28/36 | 6/12 | 10/12 |

- **The same verdicts as 4 s chunks under `any`, clip for clip** — and now `majority`
  agrees. With 10 s windows a step falls inside one chunk rather than across two or three,
  so fewer chunks see a fragment of the tightening and call it unfinished. Only `strict`
  still rejects good work.
- **The O-ring blind spot does not move: 0 of 6**, at 4 s and at 10 s, on original bytes
  and re-encoded.
- **Two more malformed answers.** One `pass_A` chunk returned two rows, leaving out step 3,
  and a `fail_B` chunk returned a fourth step again. The merge treats a missing row as
  "this chunk did not see the step".

**Across everything so far**, on the same 12 clips:

| | overall | O-ring caught | wrench steps right |
|---|---|---|---|
| batch, prod, whole original clips | 28/36 | 1 of 6 | 9 of 12 |
| serving, whole re-encoded clips | 28/36 | 0 of 6 | 10 of 12 |
| serving, 4 s chunks, `any` | 30/36 | 0 of 6 | 12 of 12 |
| serving, 10 s chunks, `any` or `majority` | 30/36 | 0 of 6 | 12 of 12 |

Chunking fixes the wrench false FAILs and does nothing for the O-ring, which is the failure
that matters for QA. Twelve clips: a direction, not a measurement.

### How long all twelve took

| approach | where | wall clock | how it was spent |
|---|---|---|---|
| batch | dev, August (shared GPU) | ~2.2 h | 12 jobs in turn, 763–1,614 s each, plus queueing |
| batch | prod, 2026-10-26 | 51 min | 12 jobs of 488–729 s, two at a time, each loading the model |
| batch | dev, new GPU workers | ~1.7 h (estimate) | 12 × ~502 s in turn, from one measured run |
| **serving, whole clips** | dev | **11.6 min** | 657 s load once, then 12 answers in 37 s |
| **serving, 4 s chunks** (83) | dev | **14.0 min** | 689 s load, then 59 answers in 148 s (the clean, resumed run) |
| **serving, 10 s chunks** (51) | dev | **14.5 min** | 739 s load, then 51 answers in 128 s |
| serving, whole clips, rerun 2026-10-28 | dev | 12.9 min | 731 s load, then 12 answers in 39 s |
| serving, 4 s chunks, rerun 2026-10-28 (83) | dev | 15.6 min | 706 s load, then all 83 answers in 229 s, one agent, no retries |
| serving, 10 s chunks, rerun 2026-10-28 (51) | dev | 15.5 min | 789 s load, then 51 answers in 139 s |

Once loaded, a video costs ~3 s whole, ~11 s as 10 s chunks and ~20 s as 4 s chunks,
against a fresh 8–27 minutes in batch, so the gap widens with every video added. Two
costs the table hides: the load ranged from 657 s to 1,385 s across five agents, and the
4 s chunks took three agents to finish — the hang and the short-chunk crash added ~105
minutes, ~2 h in all. On 2026-10-28, after the platform fixes, all three sweeps ran
start to finish on one agent each and returned byte-identical answers to 10-26's, so the
scores above stand. A bad clip now fails only its own query (400) instead of the agent.

## Lessons learned (as of 2026-08-12, measured on dev)

What 40-odd runs across 12 clips, 8 SOP variants, 4 blueprint versions and 5 budgets
actually settled — and what they did not.

### 1. It verifies object presence, not performance

The blind spot is asymmetric and reproducible: **0 of 6** on an omitted o-ring, **3 of
3** on an omitted wrench, **1 of 1** on a prop that exists in no clip. The difference is
not size or salience — it is whether the object is *on screen at all*. When it is, the
action is assumed. Since a workstation has its parts laid out by definition, the
omissions a QA reviewer most needs caught are the ones in the blind spot.

### 2. `reason` is a restatement of the SOP, not a report of the video

Change one adjective in a step and the adjective appears in the model's "observation."
The frames for one false pass show the parts never moving during the interval TVA
itself cited. **A confident reason is not evidence that the model looked**, so it cannot
serve as an audit trail — which is the feature that makes TVA look trustworthy.

### 3. The SOP is not the lever, and the prompt is not the gap

Seven SOPs — plain, attention-directed, scene-anchored, interrogative, conditional, and
a faithful port of a prompt that scored 98% on these clips via Direct Query — produced
either the identical wrong verdict or no verdict at all. **Whatever separates 98% from
0-of-6 is structural**, and unreachable from a SOP.

### 4. Bigger budgets do not buy answers

The window was never the constraint: the served `max_seq_len` is **≥ 18,754** and the
blueprint defaults to 16,384. Yet raising 5760 → 8192 turned a correct answer into no
answer, and 8192 → 16384 changed nothing at all. The model **expands its reasoning to
fill whatever room it is given** — one run rehearsed the verdict block ~12 times inside
`<think>` and lost all of it.

So the platform's own advice — *"Raise `max_new_tokens` if the reasoning was
truncated"* — pointed the wrong way in every case measured here. Read the
`dropping N rehearsed row(s)` count instead:

| N | what happened | what to do |
|---|---|---|
| `dropping 37` | verdict block emitted ~12× — repetition | **lower** the budget |
| `dropping 3` | drafted once, cut at `</think>` | budget is not the lever |
| `dropping 0` | never converged | budget is not the lever |

`run_tva_example.py --repro-empty` reproduces it and reports each symptom as CONFIRMED
or NOT REPRODUCED, offline or live:

```sh
python3 run_tva_example.py --repro-empty --offline   # instant, no API key, no GPU
python3 run_tva_example.py --repro-empty             # live, ~20 min
```

### 5. The failures cluster on the inputs that matter

Empty results are not random. All four non-answers in the paired sweep were `1_fail`
clips — `C(6,4)/C(12,4)` ≈ 3% by chance — because a clip containing a contradiction is
one the model reasons about until the budget is gone. **Emptiness and the false pass are
two faces of one conflict**, and both land only on defect-bearing inputs. Any accuracy
figure gathered without checking for empty results measures the easy half of the set.

### 6. Nothing about a broken run looks broken

`status: ready`, HTTP `202`, `running` → `completed`, and `results: []`. No error field.
A 3-step SOP can return 21 rows and the pipeline will not notice, though it built the
instruction from the step count. **With bundles, `/logs` was the only place the truth
appeared; a session has no `/logs`, so count the verdicts.**

### 7. A preflight built on a measured constant goes stale in the worst direction

Two guardrails here refused valid work. `check_sink()` encoded a denylist of blueprint
versions from evidence about one of them, and blocked the canonical blueprint for
eighteen hours after it had been fixed — found only by a deliberate `--force` run.
`check_token_budget()` hardcoded `max_seq_len: 8192` and rejected a 6,928-token
configuration as impossible; it was valid, and the real window turned out to be more
than twice that.

Both failed by **sounding certain while blocking good work**, which is worse than
letting a bad run through — a bad run costs 18 minutes and teaches you something. Both
are now floors and warnings rather than refusals, and both say the served limit is
unknown. A third instance was self-inflicted: a *computed* default derived from that
constant silently moved when the constant was corrected. Fixed values do not drift.

### What this establishes, and what it does not

**Establishes**, on 11 scored clips of one task: the mechanics end to end including the
runtime SOP path; an asymmetric reproducible blind spot; that `reason` cannot serve as
evidence; and that the reasoning budget discards verdicts it has already formed.

**Does not establish**: accuracy on other tasks, longer videos, attempted-but-botched
steps (`FAILED` never fired once), or a different camera angle. One task, one operator,
one rig. **Treat 0/6 as a red flag worth reproducing, not as a measured error rate.**

**Not settled** — `size`. It was tried once, at 448 with frames dropped to 32 to fit the
window, and returned **no verdicts at all**, so it is untested as a *fix* rather than
untried. That is the trap: raising `size` costs visual tokens quadratically, so it
starves the generation that produces the verdicts. The frames also argue against
resolution being the cause — the ring is never in the worker's hands, so there is no
small object to resolve — but it has not been ruled out.

## Platform behavior worth knowing

### Things that will cost you a run

- **`file_id` IS the basename, so re-uploading DESTROYS an in-flight run's input.**
  This cost three runs before the mechanism was clear: a run pins the object at
  *input-resolution* time, dev queues runs for up to an hour, and any re-upload of the
  same name in that window is fatal to whatever is already queued. It surfaces minutes
  later as `S3 object not found` — with `job.completed` on the job. All three uploads
  were byte-identical; the content never changed, only the object behind the name.

  **The fix is not to rename** but to **not re-upload what is already there.**
  `tva_api.upload_file()` compares local bytes against
  `GET /files/download/{file_id}` and skips when they match. `iterate_sop.py` and
  the default upload path go further and derive ids from *both* the clip and the SOP,
  so two variants cannot collide at all.
- **`connectors.source[].id` wants the file BASENAME.** The files API returns both a
  `file_id` (the basename) and a `file_uid` (`fil_…`); the connector wants the former.
  The uid fails at source resolution, after the pod has started.
- **Uploads enforce the DECLARED MIME type**, not the bytes. `text/plain` for the SOP,
  `video/mp4` for the clip. A `.mp4` declared as `text/csv` is rejected.
- **A blueprint can be republished under you mid-session, and the new version can be
  broken.** On 2026-08-11 at 22:01 `tva` changed id AND sink format while runs were in
  flight, to a format with no registered connector; every run against canonical died at
  graph instantiation for ~18 hours. **Read the blueprint document before every run** —
  the identifier alone tells you nothing about whether the graph will instantiate.
- **The graph is validated AFTER the models load.** A blueprint that cannot instantiate
  still costs the full ~7-minute cold start. Validating connectors and node configs
  first would turn 996 s into about 1 s.

### Things that look like success and are not

Measured with bundles: three distinct failures all reported `job.completed`, and none
set an error field:

| what happened | how it looks | how to tell |
|---|---|---|
| reasoning overflowed | `results: []`, one WARN | count the verdicts |
| the video never downloaded | `results: []`, an ERROR row | read `/logs` for ERROR |
| the parser matched nothing | `results: []`, **no WARN at all** | absence of both |

- **So `/logs` was the only place the truth appeared.** The bundle returned
  `status: ready`, the run `202`, the instance `status` said `running` then
  `completed`, and `/results` was simply empty. **A session has no `/logs`**: only
  lifecycle `/events` and `/results`. The session's own `status` and `error` say
  whether it failed; an empty run still ends `completed`, so the scripts count the
  verdicts (`tva_api.empty_output()`), and the WARN and "no WARN" rows above can no
  longer be told apart. A 44-byte empty result is committed as
  [`reference/tva-output-1_pass_2_pass_3_fail_A-mnt2048-EMPTY.json`](reference/tva-output-1_pass_2_pass_3_fail_A-mnt2048-EMPTY.json).
- **The instance `status` field was not a terminality signal.** MGA saw `running`
  persist 20+ minutes after a pod died with exit=1. The scripts poll a session's
  `status` instead; whether it has the same flaw is not yet measured.
- **`is_valid: true` is not proof the object landed.** It is returned alongside a
  `file_id` regardless, so read the file back before spending a run.
- **Values are accepted whether or not they do anything.** A bundle stored a key
  absent from the blueprint's `values`, or present but never referenced as
  `${values.<key>}`, and silently ignored it. A copy now refuses the first with a
  `400`, but still accepts the second.

### Queue and timing

- **`job.admitted`'s queue figure was not the real wait, and the real wait can be
  invisible** (measured with bundles). One run reported `Job scheduled after 32s in queue` and then did not
  start a pod for **11+ minutes**, with no job of ours holding the GPU. `GET
  /agents/instances` is scoped to your org, so another org's job can block you with
  nothing in your view to explain it. **Judge progress from `job.started`.**
- **Runs are not admitted, or executed, in submission order.** Twelve submitted together
  sat ~60 minutes with none admitted, then completed out of order over ~90 minutes.
  Submitting a batch up front does not reserve an order.
- **No progress events during processing.** Between `input_started` and the terminal
  event there is nothing — no frame count, no transcript length, no per-node timing.
- **whisper loads even with no audio track**, costing ~40 s of every run and
  contributing nothing.

### API shape

- **The endpoint decides the files prefix.** On `platform.*` endpoints (ending in
  `/api`) files live at `/files`; on the legacy `api.*` hosts they lived at
  `/v0.5/files`. The wrong one is a bare 404 with no body. (On the legacy hosts, a
  prod key on dev returned 401.)
- **A package, a deployment and a batch session take no values.** Set them on the
  blueprint copy (`POST /agents/blueprints/tva/copy` with `overrides.values`). A
  batch session given `values` is refused with a `422`. A serving session accepts
  `values`, but only for a node's session fields (for TVA, the fusion model's
  `max_new_tokens`); `sweep_serve.py` uses the copy's anyway, so serving and batch
  run the same setting.
- **A session has `/events` and `/results`, not `/logs`.** `/events` carries the
  session's lifecycle (opened, dispatched, failed with a reason), not model-load
  timings or the parser's warnings.
- **Bundle-era, for reference:** the bundle endpoint was plural (`POST /agents/bundle`
  → 404), starting a run returned 202, and `/logs` rather than `/events` carried the
  run's progress.

## Where the deployment diverges from the design doc

Observations for the author, not defects.

- **Three of the five documented fusion parameters are unreachable, including the
  one that governs every failure above.** The doc's own "Newton Fusion Model
  Parameters" list specifies:

  | doc parameter | wired in the blueprint? |
  |---|---|
  | `model_version` | ✅ as `model` |
  | `max_tokens` | ✅ as `max_new_tokens` |
  | **`max_model_len`** | ❌ — *"the maximum number of total tokens the model can use in its context window, **which includes input and output tokens**"* |
  | `gpu_memory_utilization` | ❌ |
  | `min_temporal_similarity_threshold` | ❌ on August dev — ✅ wired on prod since 2026-10-15 |

  **`max_model_len` is `max_seq_len`.** The doc specifies it, describes the
  input-and-output sharing exactly as observed, and the blueprint does not expose it.
  The served value is not readable through the API either — it can only be bracketed
  by runs, and it moved twice during this work. Measured at **≥ 18,754**, still well
  under the doc's stated 256K.

  Worth being careful about the causal claim here, because an earlier version of this
  section got it wrong. The doc predicts context overflow —

  > *"using the raw video frames will result in the model context overflowing for
  > even short videos"*

  — and the whole preprocessing design exists to fit inside `max_model_len`. But the
  empty outputs here are **not** that failure. Doubling the budget to the blueprint's
  own 16,384 default changed nothing: the model expands its reasoning to fill whatever
  room it is given (see [Lesson 4](#4-bigger-budgets-do-not-buy-answers)). So
  `max_model_len` being unreachable is a real gap in the API surface, but it is not
  what causes the empty results.

  `min_temporal_similarity_threshold` was the other consequential omission on the
  August blueprint (prod now exposes it, default `0.0`, off). The doc devotes its longest section to the temporal compression it controls — four
  stages, MAD-based adaptive segmentation, a documented *"70% compression with a
  25% performance improvement"* — and **that headline algorithmic contribution
  could not be evaluated through that API at all.** It is also the mechanism that
  would most plausibly relieve the ceiling, by shrinking the visual prompt without
  dropping frames.
- **Audio is in V1.** The doc says F1-0 adds audio in a later version; the
  blueprint runs a transcriber (`whisper-base` on prod) into `prompt.audio` today.
- **The parser's `step` field is 0-based and undocumented.** The doc specifies
  `step: <int>` without saying where it starts.
- **`MISSING` rows are specified to carry no timestamps** and the doc's own format
  agrees (`step N: MISSING, reason: <reason>` has no timestamp fields) — worth
  stating in the output schema, since consumers must handle nulls. Observed
  consistently across every output committed here: **35 of 35 `MISSING` rows have null
  timestamps, and 98 of 100 `PASSED` rows carry an interval.** The two exceptions are
  exactly the predicted artefact — placeholder rows with `reason: "..."` that the model
  emits when it loops — so a `PASSED` row *without* an interval is not a verdict, and
  `evaluate.py` flags those as SUSPECT rather than scoring them.
- **The model is fp8** — the August dev blueprint pinned
  `f1_0_35b_a3b_fp8_base_040625b4d0750b` (prod no longer names the checkpoint),
  confirming the 35B/3B-active MoE description and adding quantization the doc does not mention.

## Recommendations

Ordered by value, and every one of them is backed by a run in this repo.

1. **Investigate the blind spot before anyone deploys this as a gate.** 0 of 6
   detections on an omitted o-ring, with fluent invented descriptions of it being
   fitted, against 3 of 3 on an omitted wrench and 1 of 1 on a prop that appears in no
   clip. The pattern is **object presence, not part size**: when the object is on screen
   the action is assumed, and a workstation always has its parts laid out. `size: 224`
   was the obvious suspect and is probably not the cause — the ring is never in the
   worker's hands, so there is no small object to resolve. It is also barely testable as
   things stand: the one run at 448 had to drop to 32 frames to fit the window and
   returned no verdicts, because visual tokens grow quadratically with `size`. **A
   region-of-interest crop would test the hypothesis without that trade-off.** This is a
   correctness issue, not a configuration one.
2. **Do not discard rehearsed rows on an unterminated reasoning block.** One run dropped
   **37 candidate verdict rows** because `</think>` never arrived inside the budget;
   others dropped 21 and 3. The answer had been formed. Emitting those rows with a
   `truncated: true` marker would turn a total loss into a usable, flagged result — the
   cheapest fix on this list, and it would have recovered four of the empty results here.
3. **Validate the output's arity against the SOP, and expose `repetition_penalty` and
   `stop`.** A 3-step SOP returned 21 rows. `PrepareSOPNode` built the instruction from
   the step count and the parser never compares the two, so a self-evidently wrong
   result passes through as data. The model loops — one run re-emitted the same
   3-verdict block six extra times, another ~12 times inside `<think>` and produced
   nothing — which makes repetition the likely cause of the empty results rather than a
   cosmetic quirk. Both levers were demonstrably wireable: MGA's superseded blueprint
   carried them. No SOP wording substitutes, because the SOP governs what is judged and
   not when generation stops.
4. **Treat an empty `results` list as a failure, not a completed job.** The parser
   already emits a WARN naming the cause; promoting it to ERROR, or surfacing a
   `rows_emitted: 0` field, would stop a silent empty result reading as success. This
   matters more for TVA than for MGA because the failure correlates with the inputs that
   contain defects.
5. **Fix the WARN's advice, which points the wrong way.** It says *"Raise
   `max_new_tokens` if the reasoning was truncated."* Measured here: raising 5760 → 8192
   turned a correct answer into no answer, and 8192 → 16384 changed nothing at all. The
   reasoning is not truncated for want of space, it expands to fill the space. The
   `dropping N` count already distinguishes the cases — say so in the message.
6. **Validate the graph before loading models, not after.** A blueprint that cannot
   instantiate currently costs the full ~7-minute cold start; connector and node-config
   validation up front turns 996 s into about 1 s.
7. **Expose `max_model_len` — the doc already specifies it.** Not a feature request: the
   design doc's fusion-parameter list defines it as the context window "which includes
   input and output tokens", and the blueprint does not wire it. It is also not readable
   through the API, so a caller can only bracket it by runs, and it moved twice during
   this work — measured at ≥ 18,754 against the doc's stated 256K. It is not what causes
   the empty results, but a caller cannot currently reason about its own budget.
8. ~~**Expose `min_temporal_similarity_threshold`.**~~ Done on prod (2026-10-15). The
   next step is to measure it: it is the most plausible way to shrink the visual
   prompt without dropping frames.
9. **Document that `step` is 0-based** in the output schema. Human-authored SOPs are
   1-based, and the mismatch fails quietly — a perfect run reads as 2-of-3.
10. **Emit progress events during processing.** Between `input_started` and the terminal
    event there is nothing across 5–6 minutes, so neither a failure nor the time spent
    can be attributed.
11. **Reconsider loading whisper when the video has no audio track.** It costs ~40 s of
    every run here and contributes nothing.
12. **Let a served agent take data-service file ids, or raise the 2 MiB request cap.**
    Every clip here is 5–11 MB, so serving them means re-encoding (or chunking) first,
    and a re-encoded clip is not the input batch saw: verdicts matched on `pass_A`, but
    timestamps and reasons did not. Batch and serving should be comparable on the same
    bytes.

## Repo layout

| Path | What it is |
|---|---|
| `run_tva_example.py` | **Start here.** One clip end to end: upload video + SOP → the setting's agent → a batch session → poll it → download → print → score. `--dry-run`, `--show` and `--repro-empty --offline` need no API key. |
| `iterate_sop.py` | **SOP iteration.** Derives the platform file ids from both the clip and the SOP (`<clip>-<sop>.mp4` + `.txt`), so variants cannot overwrite each other and an id says which SOP produced it. `--compare` scores every collected variant offline. |
| `sweep.py` | Batch over many clips, one deployment, a session per clip. `--submit-all` claims queue slots up front, `--fresh` re-runs what is already on disk, `--collect` harvests after a disconnect (`--setting` for one setting), `--cancel-all` frees the GPU. `--list` and `--dry-run` are offline. |
| `sweep_serve.py` | **Stage 6.** The sweep over agent serving: one serving deployment and session, one `/query` per clip, each clip re-encoded under the 2 MiB request cap. `--repeat` checks determinism, `--keep`/`--session` reuse a loaded session, `--chunk`/`--overlap` send each clip as shorter videos, `--dry-run` is offline. |
| `cleanup.py` | Deletes the sessions, deployments and packages recorded in `state/`; lists them first, `--yes` to delete. The project and its copies of `tva` stay. |
| `tva_project.py` | The project `TVA o-ring`, a copy of `tva` per setting and its package and deployments, found or made and reused; every session recorded in `state/lifecycle.json` (gitignored). |
| `merge_chunks.py` | Stage 6, chunking: merges `sweep_serve.py --chunk` answers into one verdict per step under four rules and scores each. Offline. |
| `background.py` | `--background` for the long scripts: `nohup`, `caffeinate -i -w`, a log in `logs/` (gitignored), and `kill` handled like Ctrl-C. Adapted from the LARCO quickstart's. |
| `evaluate.py` | Offline scorer: per-step accuracy, confusion, FAILED-vs-MISSING split, interval sanity. No API key. |
| `tva_api.py` | Shared stdlib client — endpoint-aware files prefix (`files_root()`), streaming upload, idempotent re-upload, copies, packages, deployments and sessions, wired-value and token preflight. |
| `sop/oring-numbered.txt` | **The baseline SOP**, and the default for every script. One step per line. |
| `sop/oring.txt` | The same steps without `Step N:` prefixes — the design doc's bare format, kept for the 2×2 comparison. |
| `sop/oring-strict.txt` | Adds status instructions and returns **zero rows** — the worked example of an SOP competing with the parser. |
| `sop/oring-{focused,discriminative,maximal,hybrid}.txt` | The four wording variants behind the null result. Each carries its own rationale as `#` comments, which are stripped before upload. |
| `sop/oring-{question,conditional}.txt` | The two non-assertive variants. Neither produced a verdict at any budget. |
| `sop/oring-sealant.txt` | Step 3 asks for a prop that exists in no clip. The run that established **object presence, not performance**. |
| `sop/paired/` | One SOP per clip for `--pair-sop`'s stem matching. `regenerate.py --check` catches drift from `oring-numbered.txt`. |
| `data/clips/` | The twelve clips, committed — 80 MB, no LFS. Ground truth is in each filename. |
| `reference/` | **Every number in this README is scored from here, offline.** `sweep/` the 12-clip run · `grid-5752/` the SOP-format 2×2 · `strict-sop/` and `size-448/` the two mitigations that returned nothing · `oring-never-fitted-…/` the frames proving a false pass is fabricated · plus the 44-byte empty result and a blueprint snapshot. |
| `eval_paired/` | The stem-paired 12-clip sweep — the second run of the baseline. |
| `eval_prod/` | The 12-clip baseline on prod, 2026-10-26 — the third run, and the first on the new blueprint. |
| `reference/prod-2026-10-26/` | The first prod single run, and frames showing the wrench in use on the three clips where prod said it was not. |
| `eval_iter/` | One result per SOP variant, `<clip>-<sop>_f<frames>_t<budget>.json`. `iterate_sop.py --compare` reproduces the null-result table. |
| `eval_dev/` | `pass_A` on dev's GPU workers, 2026-10-26: the batch run, and under `serving/` the three served answers Stage 6 measures. |
| `eval_probe/` | The second serving probe of `pass_A` (`sweep_serve.py --repeat 3`): same rows as the first, on a fresh agent. |
| `data/clips-serving/` | The clips re-encoded, or cut into chunks, to fit the serving request cap. Gitignored — `sweep_serve.py` regenerates them. |
| `eval_out/` | Where `sweep.py` writes by default. Gitignored — regenerable. |
| `MIGRATION.md` | From bundles and instances to projects, packages, deployments and sessions: what changed here, and how to move your own scripts. |
| `docs/` | The TVA design doc this example implements. **Gitignored — internal.** |

## Not yet built

Roughly in order of what would teach us most per GPU-minute. Agent serving (Stage 6)
makes most of these cheap: on one loaded agent, a clip costs ~3 s rather than a run.

- **Batch on the re-encoded clips.** Whole-clip serving changed 3 of 12 clips' verdicts
  against batch on the originals. Batch on `1_fail_2_pass_3_pass_A`'s re-encode (one run,
  the lost O-ring catch) says whether the re-encode or serving caused it.

- **Why prod rejects wrench work that was done.** Three false FAILs on step 3, each
  with a reason describing the clip's final frames. Trimming the idle tail off
  `1_fail_2_pass_3_pass_E` (43 s, the last ~20 s static) is a one-run test of whether
  the end state is what decides it.
- **`min_temporal_similarity_threshold`, now that prod wires it.** Compression could
  free context for more frames or a larger `size` — the trade-off that sank the 448 run.
- **A clip with the O-ring pile OFF CAMERA.** The sharpest test of the object-presence
  rule, and it needs one new recording rather than a new run: the rule predicts `MISSING`
  on step 1 for the first time. If it still passes, the rule is wrong and the finding
  needs restating.
- **A clip with a step attempted and done WRONG.** `FAILED` never fired once across 12
  clips, because every failure here is an omission — so a third of the status vocabulary
  is completely unexercised, and "attempted but incorrect" is the case a real QA gate
  cares most about.
- **A fully negative-control SOP** — all three steps describing actions the video never
  shows. The single-step version of this is done and produced the central finding
  (`sop/oring-sealant.txt`, step 3 correctly `MISSING`), but whether TVA can reject an
  entire procedure, or starts pattern-matching onto whatever is on screen, is untried.
- **An audio-bearing clip.** whisper loads on every run and is exercised by none of
  these, so half the fusion is untested here.
- **A region-of-interest crop, or `size: 336`.** 448 was tried once and returned nothing
  — frames had to drop to 32 to fit, which starves generation. A crop tests the
  resolution hypothesis without paying quadratically for it.
- **A `parser_compute_stats: true` run on the strict SOP**, to confirm the zero-row
  result is template non-conformance rather than something else. One run, and it is what
  the value exists for.
- **Longer videos.** Every clip here is under 45 s. MGA fails outright above ~5 minutes;
  where TVA's ceiling sits is untested, and the design doc says performance degrades
  past 10 minutes.
- **Timestamp scoring.** Would need per-step ground-truth intervals, which the filename
  labels cannot carry. The reasons are more informative per GPU-minute.
