# Task Verification Agent — End-to-End Example

Run Archetype AI's **Task Verification Agent (TVA)** on recordings of a procedure,
with the procedure itself (an SOP) as a second input, and get back a per-step
**PASSED / FAILED / MISSING** verdict with timestamps and a reason. Then score the
verdicts against ground truth. The example runs on Agent Lifecycle 2.0 (projects,
packages, deployments and sessions), in batch and in serving.

The scenario: a bench recording of a **three-step o-ring cap assembly**, in twelve
takes, some of them with a step deliberately skipped:

```
Step 1: Install the rubber O-ring into the blue block-off cap, ensuring it is properly seated in the groove.
Step 2: Thread the blue cap onto the black manifold by hand until snug, without cross-threading.
Step 3: Using a wrench, tighten the blue cap to the specified torque.
```

TVA is zero-shot: the `tva` blueprint brings its own models, so there is nothing to
fit. You write the SOP, set three values, and run it on a video.

## Verified on production

Run against `https://platform.u1.archetypeai.app/api`, all 12 clips, the same SOP and
values:

| | Batch (`sweep.py`) | Serving (`sweep_serve.py`) |
|---|---|---|
| steps judged correctly | **28 / 36** | **28 / 36** |
| skipped wrench (step 3) caught | **3 of 3** | **3 of 3** |
| skipped o-ring (step 1) caught | **1 of 6** | **0 of 6** |
| correct steps rejected | 3 of 27 | 2 of 27 |
| time | ~10 min per clip (9.7 min on 2026-11-12); 51 min for all 12, two running at once | 710 s for the deployment to start, then ~3 s per clip; 752 s in all |
| outputs | [`reference/batch/`](reference/batch) | [`reference/serving/`](reference/serving) |

The batch outputs are from 2026-10-26; rerun on 2026-11-12 on the new lifecycle, the
same clip returned the same verdicts. The serving outputs are from 2026-11-12. Batch
and serving agree on 34 of the 36 steps.

Every clip, every step. ✗ marks a wrong verdict; where batch and serving differ, the
cell shows `batch / serving`:

| Clip | Step 1 (o-ring) | Step 2 (thread) | Step 3 (wrench) | TVA step 1 | TVA step 2 | TVA step 3 |
|---|---|---|---|---|---|---|
| `1_fail_2_pass_3_fail_A` | **skipped** | done | **skipped** | PASSED ✗ | PASSED | MISSING |
| `1_fail_2_pass_3_pass_A` | **skipped** | done | done | MISSING / PASSED ✗ | PASSED | PASSED |
| `1_fail_2_pass_3_pass_B` | **skipped** | done | done | PASSED ✗ | PASSED | PASSED |
| `1_fail_2_pass_3_pass_C` | **skipped** | done | done | PASSED ✗ | PASSED | PASSED |
| `1_fail_2_pass_3_pass_E` | **skipped** | done | done | PASSED ✗ | PASSED | MISSING ✗ / PASSED |
| `1_fail_2_pass_3_pass_F` | **skipped** | done | done | PASSED ✗ | PASSED | FAILED ✗ / MISSING ✗ |
| `1_pass_2_pass_3_fail_A` | done | done | **skipped** | PASSED | PASSED | MISSING |
| `1_pass_2_pass_3_fail_B` | done | done | **skipped** | PASSED | PASSED | MISSING |
| `1_pass_2_pass_3_fail_C` | done | done | **skipped** | PASSED | PASSED | MISSING |
| `1_pass_2_pass_3_pass_A` | done | done | done | PASSED | PASSED | FAILED ✗ |
| `1_pass_2_pass_3_pass_B` | done | done | done | PASSED | PASSED | PASSED |
| `1_pass_2_pass_3_pass_C` | done | done | done | PASSED | PASSED | PASSED |

Step 2 is done in every clip, so its 12 of 12 says nothing about whether TVA would
catch it skipped.

**What this means in practice:** TVA reliably catches a skipped step that leaves
something visibly absent: the wrench never enters the frame. It mostly **misses a
skipped step whose parts are on screen anyway**: the o-ring sits on the bench in every
take, and TVA passes the step, often with a confident `reason` describing an o-ring
being fitted that never was. So a `PASSED` verdict on a step like that needs a human
check, and the `reason` text is not evidence that the step happened.

Score the committed outputs yourself, offline:

```sh
python3 evaluate.py reference/batch/*.json
python3 evaluate.py reference/serving/*.json
```

## Quick start

You need Python 3.10+ (every script is standard library only) and an API key. The 12
clips are in the repo (`data/clips/`, 80 MB), so there is nothing to download.

```sh
cp .env.example .env           # set ATAI_API_KEY; the endpoint is already set
python3 run_tva_example.py
```

It uploads the default clip and the SOP, makes the agent (a copy of `tva` with the
values set, its package and a batch deployment), opens a session, prints the
session's events while it runs, then downloads the verdicts and prints them next to
the ground truth:

```
step 1: expected PASS  got PASSED   [OK  ]
step 2: expected PASS  got PASSED   [OK  ]
step 3: expected PASS  got FAILED   [MISS]
```

All 12 clips, a session per clip on one deployment:

```sh
python3 sweep.py --list                        # the clips and their labels; no API calls
python3 sweep.py --all --submit-all            # queue all 12, then collect
python3 evaluate.py eval_out/tva-output-*.json
```

`--submit-all` queues every clip before collecting any, so the platform can run
several at once. The trade-off: a wrong value costs all twelve runs, not one, so run
`--dry-run` first.

## How it works

`run_tva_example.py` makes these calls. `--dry-run` prints them without making any.

| Step | Call | Notes |
|---|---|---|
| 1. Upload | `POST /files`, twice | The video (`video/mp4`) and the SOP (`text/plain`), under unique names: `file_id` is the filename, shared across your organization, so reusing a name replaces the file under any run still reading it. |
| 2. Project | `POST /projects` | `TVA o-ring`, found by name after the first run. |
| 3. Copy of `tva` | `POST /agents/blueprints/tva/copy` | With `max_frames`, `size` and `max_new_tokens` as `overrides.values`. One copy per setting, keyed `tva-f64-s224-t5760`. |
| 4. Package | `POST /agents/packages` | Poll until `ready`. |
| 5. Deployment | `POST /agents/deployments` | `kind: batch`; ready at once. |
| 6. Session | `POST /agents/sessions` | One per clip. `connectors.source` names **both** files: `{"type": "file", "id": <video file_id>, "format": "mp4"}` and the same for the SOP with `"format": "txt"`. The platform routes them by `format`. |
| 7. Watch | `GET /agents/sessions/{id}` and `/events` | `pending → running → completed`, or `failed` with an `error`. |
| 8. Results | `GET /agents/sessions/{id}/results` | The verdicts are a file: `ref` is a root path (`/api/files/…/content`), so resolve it against the host. |

Steps 2–5 happen once per setting. `state/lifecycle.json` (gitignored) keeps the ids,
and later runs reuse them. The SOP travels with each session, so one copy serves
every SOP.

### The SOP is your only lever

There is **no `prompt` value** on the `tva` blueprint: the SOP wording is the whole of
your influence on the model.

- **One step per line.** The line breaks are the step boundaries; a wrapped line
  becomes two steps. Blank lines and `#` comments are stripped before upload.
- **`Step N:` prefixes** tie each verdict to a numbered step. On a clip with two
  skipped steps, the same SOP without them returned no verdicts at all.
- **Name what distinguishes the parts** ("the blue cap", "the black manifold"): every
  frame is shrunk to 224×224 before the model sees it.
- **Describe behaviour, not numbers.** Nothing in a short video can show "12 Nm", so
  the SOP says "tighten to the specified torque".

Rewording the SOP did not fix the o-ring blind spot: eight variants, including ones
that name the o-ring's colour and where it comes from, gave the same verdicts on the
hardest clips.

### The three values

| Value | This example sends | Why |
|---|---|---|
| `max_frames` | 64 | Frames sampled evenly across the clip. Prod accepts 1–64. |
| `size` | 224 | Each frame is resized to `size × size`. A larger size uses more of the context per frame; 448 (with 32 frames to fit) returned no verdicts. |
| `max_new_tokens` | 5760 | The model reasons before it answers, out of the same budget, and its failure mode is **repetition**: given more room it repeats itself, and can run out mid-reasoning with nothing to show. On a clip with two skipped steps, 8192 returned no verdicts where 5760 returned correct ones. The blueprint default is 8192. |

A package, a deployment and a batch session take no values, so they go on the copy.
Before uploading, the script checks that `tva` actually uses each of them.

### The output

One JSON document per run:

```json
{"id": "1_pass_2_pass_3_pass_A", "results": [
  {"step": 0, "status": "PASSED", "timestamp_start": 3.9, "timestamp_end": 10.0,
   "frame_start": 117.0, "frame_end": 300.0,
   "reason": "The video clearly shows the person picking up the blue cap and the O-ring, then carefully placing the O-ring into the groove of the blue cap."},
  …]}
```

- `step` is 0-based; the SOP is 1-based. `evaluate.py` handles it.
- `MISSING` rows carry no timestamps: a step that never happened has no interval.
- The model can repeat itself, so `len(results)` is not the step count: collapse by
  `step` (`evaluate.py` does).
- **A `completed` session can hold no verdicts** (`results: []`). The scripts count
  them and exit non-zero when there are none.

## Serving

A batch session loads the model for every clip. A serving deployment loads it once,
then answers a query per clip in seconds:

```sh
python3 sweep_serve.py --all
python3 evaluate.py eval_serving/tva-output-*.json
```

It deploys the same setting's package as `serving`, opens one session, waits for it to
be `running` (about 12 minutes), then sends each clip and the SOP to
`POST /agents/sessions/{id}/query`. At the end it cancels and deletes the session and
stops and deletes the deployment, which holds a GPU worker; Ctrl-C and `kill` do the
same. `--keep` leaves both up for `--session ses_…`.

- **The video goes inline, and a request is capped at 2 MiB**, so each clip is
  re-encoded to fit. The bytes differ from batch's, and a verdict can too.
- A session with no query for 30 minutes is closed.
- `--chunk` and `merge_chunks.py` split a clip into shorter pieces and merge the
  verdicts per step. Exploratory: on these clips it did not help on the o-ring step.

## Ground truth

Each clip's name is its label: `1_pass_2_pass_3_fail_A.mp4` means steps 1 and 2 were
done and step 3 was not; the trailing letter is the take. Every failure in this set
is an **omission** (the o-ring or the wrench is never used), so `MISSING` is the
status that matches the video, and `evaluate.py` scores PASSED against not-PASSED.
The clips have no audio track; the blueprint transcribes audio anyway, and it
contributes nothing here.

## Cleanup

```sh
python3 cleanup.py          # list what would be deleted
python3 cleanup.py --yes    # delete it
```

It deletes the sessions, deployments and packages this example made, in that order,
starting from the packages in `state/lifecycle.json`. The project and its copies of
`tva` stay, so the next run only packages again. `state/` belongs to one platform
endpoint: to run against another, move it aside.

## Repo layout

| File | |
|---|---|
| `run_tva_example.py` | One clip, end to end, with the verdicts scored. |
| `sweep.py` | All 12 clips in batch, a session per clip. |
| `sweep_serve.py` | Serving: one deployment and session, a query per clip. |
| `merge_chunks.py` | Merges chunked serving answers per step. Offline. |
| `evaluate.py` | Scores outputs against the labels in the clip names. Offline. |
| `cleanup.py` | Deletes what the scripts made. |
| `tva_api.py`, `tva_project.py` | The API client and the project, copy, package and deployment bookkeeping. |
| `background.py` | `--background` for the long-running scripts. |
| `sop/oring-numbered.txt` | The SOP. `sop/paired/` holds a copy per clip, for `--pair-sop`. |
| `data/clips/` | The 12 recordings, made for this example. |
| `reference/` | The verified batch and serving outputs. |
