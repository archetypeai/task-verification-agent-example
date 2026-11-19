# Example: agarose gel electrophoresis (9 min)

A longer video to try the app's **Long video** mode on: a lab bench procedure, filmed
by hand, nine SOP steps over 9 min 13 s. The repo's o-ring clips are 30 s each; at that
length one query sees a frame every half second, while over this video one query sees
one frame every 8.6 s.

```sh
sh examples/gel-electrophoresis/download.sh     # 97 MB from Wikimedia Commons, checksum-verified
```

That writes `video/gel-electrophoresis.webm` and, if ffmpeg is installed, an `.mp4`
next to it. `video/` is gitignored: the video is not committed.

## Try it in the app

1. Start the agent (see [`app/README.md`](../../app/README.md)).
2. Drop `video/gel-electrophoresis.mp4` (or the `.webm`) into **Video**.
3. **SOP**: load [`sop.txt`](sop.txt), or click the **Gel electrophoresis** example.
4. Choose **Long video**: chunk length 20 s gives 28 chunks, so about 1.5–2 minutes of
   queries. Then **Verify**, and compare the merge rules on the results.

**Whole video** does not work here: even the smallest re-encode (320×240) of 9 minutes
is over the 2 MiB request, and the app says "no encode fits the 2 MiB request cap"
after about a minute of trying. At 20 s, every chunk fits (the largest is 1.5 MB).

## The steps, and roughly when they happen

Every step is performed in this video. The times were read from frames sampled every
3–5 s, so treat them as ±5 s. They are good for judging a verdict's time range, not
for scoring frames.

| Step | What | When |
|---|---|---|
| 1 | Lift the gel tray out of the white casting stand | 6–28 s |
| 2 | Place the tray into the clear electrophoresis chamber | 28–70 s |
| 3 | Pour running buffer from the graduated cylinder | 75–100 s |
| 4 | Attach a new tip to the micropipette | 130–245 s |
| 5 | Draw a sample from a tube in the red rack | 245–280 s, then repeated |
| 6 | Dispense the sample into a well | 280–375 s, several wells; close-up 340–375 s |
| 7 | Place the lid with the red and black leads | 380–430 s |
| 8 | Plug the leads into the power supply and switch it on | 430–455 s |
| 9 | Let the gel run until the dye bands leave the wells | 470–545 s |

The rest: 0–6 s title, 100–130 s narration over the filled chamber, 545–553 s credits.

**What this video can and cannot show.** All nine steps are done, so a perfect score
here says nothing about catching a skipped step. A `FAILED` or `MISSING` is a false
rejection, and a time range far from the table is a misplaced one. To see an omission,
cut a step out. For example, drop the buffer pour (step 3):

```sh
cd examples/gel-electrophoresis/video
ffmpeg -v error -i gel-electrophoresis.mp4 \
  -vf "select='not(between(t,72,102))',setpts=N/FRAME_RATE/TB" -an gel-no-buffer.mp4
```

Expect the blind spot the repo documents: the buffer is in the chamber for the rest of
the video, so the step may still pass.

## What one run returned (2026-11-18, production, serving, 64 / 224 / 5760)

Long video, 20 s chunks, no overlap: 28 chunks, all answered, 136 s of queries, about
3 minutes in all. With any merge rule (they agree here, since no chunk said `FAILED`):
steps 1–7 `PASSED`, steps 8 and 9 `MISSING`. Steps 8 and 9 are false rejections. The
per-chunk answers (in `app/runs/`) show the passes aren't evidence either:

- **Each chunk is checked as if it were the whole procedure.** The chunk at 220–240 s
  shows only a pipette tip being attached (step 4), yet it passes steps 1–4 in order,
  describing the tray, the buffer and the tip. Step 1 (6–28 s) passed in 20 of 28 chunks.
- **Rows drift off their steps.** The `step` field is the row's position, not a match to
  the SOP. In the same chunk, the row numbered 2 describes pouring buffer (step 3) and
  row 3 describes attaching the tip (step 4). Some chunks return a step 10 for this
  9-step SOP.
- **Some descriptions are invented.** At 437–460 s the leads are being plugged into
  the power supply (step 8); the answer describes pipetting and marks step 8 missing.
  Another chunk describes "putting on gloves", which never happens.

**Why steps 8 and 9 are always missing.** Each chunk's passes are a run starting at step
1, with "The video ends before…" for the rest, so late steps are rarely reached. The
number of chunks passing each step falls with its position: 20, 19, 10, 14, 9, 8, 4, 0, 0.
It's not that the video hides them. At 224×224 the power supply's display and the hand on
its buttons are visible at 437–460 s, but that chunk describes pipetting (the pipette and
tube rack fill the foreground), and no answer in any chunk mentions the power supply
being used. Step 9 is a wait: within a 28 s chunk the dye barely moves, and the answer
says "the video ends before the person lets the gel run" over footage of the running gel.

So on this video the merged verdicts reflect the SOP's order more than the video.
Long-video mode keeps the frames dense, but each chunk's answer needs the same human
review as any other TVA verdict. The app's steps × chunks grid shows the pattern: a
staircase of passes in chunks where only one step happens.

Rerunning doesn't change this. The same setting two hours later, on a new deployment,
returned the same answer from every chunk for every step (252 of 252). Generation is
deterministic for a given video and SOP, as the repo found on the short clips.

### One step per query (2026-11-18, same agent and settings)

252 queries, 507 s of query time (about 2 s each; a one-line SOP gets a short answer),
none failed. **All 9 steps `PASSED`, including 8 and 9**, and each one passed in the
chunks where it really happens: 48 of 50 such chunk answers passed (step 3 missed 1 of 3,
step 6 missed 1 of 6). Step 8 passed in all 3 of its chunks ("plugging the leads into the
power supply… the red light turning on"), and step 9 in all 5 of its own.

The cost is false passes. Every step also passed in 7–17 chunks where it doesn't happen:

```
step  chunks 0 → 27 (P = passed; capitals = chunk overlaps the real time)
  1   PPppppp...pfppppp..ppp.ppfp.
  4   pp....PPPPPPPpppppp.pp.pppp.
  7   pppppppf..p.ppppf.PPPPppppp.
  8   .p..........pp.p....pPPPpp..
  9   .p.f......p...pp...fpppPPPPP
```

So one step per query fixes the late-step misses caused by the list. What remains is the
behaviour the repo documents on the o-ring clips: a step passes wherever its objects are
in view. Every step is done in this video, so 9 of 9 is the right answer, but these false
passes suggest a skipped step would still pass. To test that, run the cut video
(`gel-no-buffer.mp4`, above) with one step per query.

**When each step happened.** The earliest chunk that passed a step pointed at the real
time for only 1 of 9 steps here, so the app times a step by its longest unbroken run of
passing chunks. That run overlapped the real time for all 9, though it can be wide (step
4: 133–374 s for a step at 130–245 s). In all-steps runs, neither rule is reliable.

## Credit

"Electrophoresis: Assembling The Rig & Loading/Running The Gel" by Courtney Harrington,
Community College Consortium for Bioscience Credentials (c3bc), licensed
[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/), via
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Agarose_Gel_Electrophoresis_-_Assembling_the_Rig_and_Loading-Running_the_Gel.webm).
Unmodified, apart from the optional mp4 conversion and the cut above.
