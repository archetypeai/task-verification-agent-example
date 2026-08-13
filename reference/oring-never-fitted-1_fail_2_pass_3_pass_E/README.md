# What the model claimed, and what the frames show

Three frames from `1_fail_2_pass_3_pass_E.mp4`, taken from the interval TVA itself
reported for step 1 (`timestamp_start: 2.7`, `timestamp_end: 5.5`). Extracted with:

```sh
ffmpeg -ss 2.7 -i data/clips/1_fail_2_pass_3_pass_E.mp4 -frames:v 1 out.jpg
```

TVA's verdict for step 1, under three different SOPs:

| SOP | status | reason |
|---|---|---|
| `oring-numbered` | PASSED | *"...stretching the O-ring, and placing it onto the cap groove."* |
| `oring-focused` | PASSED | *"The worker picks up the O-ring and places it onto the blue cap."* |
| `oring-discriminative` | PASSED | *"...picking up a black O-ring **from the cluster** and placing it onto the blue cap."* |

What the frames show:

| frame | t | what happens |
|---|---|---|
| `t2.7s-01` | 2.7s | the right hand picks up the **blue cap** off the mat |
| `t4.1s-02` | 4.1s | cap in the right hand, manifold in the left; hands meet in mid-air |
| `t5.5s-03` | 5.5s | the cap is being threaded onto the manifold port |

**The spare O-rings sit in a cluster at the top-centre of the cutting mat — four
black, one brown — in pixel-identical positions in all three frames.** The worker's
hand never goes near them. The clip goes straight from picking up the cap to
threading it.

So the reason text is not a misjudgement of a borderline action. The action it
describes does not occur, in an interval where something else demonstrably does,
and the model supplied a confident timestamp for it.

Note the third row especially. `oring-discriminative` was written to ask for a state
change that a later assembled cap cannot imply — one ring leaving the cluster. The
model asserted that too, in the SOP's own vocabulary. Every discriminating detail
added to the SOP became part of the fabrication rather than a test it had to pass.

This also rules out spatial resolution as the cause. At 1080p the rings are ~100 px
across and remain visible as a cluster even squashed to `size: 224` — and it does not
matter, because the ring is never in the hands at all. The failure is not that a
small object was missed; it is that a clearly-visible group of parts was never
touched and the model did not notice.
