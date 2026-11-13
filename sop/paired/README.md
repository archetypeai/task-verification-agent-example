# Paired SOPs — one per clip, named `<clip-stem>.txt`

Every file here is a **byte-identical copy of `../oring-numbered.txt`**. They exist
because of how the pipeline receives inputs.

## Why stem-matched names

A run's inputs arrive as **one flat list of file ids** — there is no field saying
"this text belongs to that video". So when a run carries several videos and several
SOPs, the pipeline pairs them **by matching stems**: `X.mp4` with `X.txt`. That
convention is shared by the console and the API, since both use the same blueprint.

For a **1:1 run** (one video, one SOP) the pairing is unambiguous and a mismatched
name works — every result committed under `reference/` was produced with a single
shared `oring-numbered.txt` against differently-named clips, and the verdicts plainly
reflect the SOP. **Whether the name changes anything for a 1:1 run is untested**;
compare digests with and without `--pair-sop` to find out.

For a **multi-input run** the convention is the only thing that can associate a video
with its procedure, so matching stems is required.

A second benefit: a per-clip name means two concurrent runs cannot overwrite each
other's SOP object. A single shared name allows exactly that, and it destroyed three
runs during this work — see the upload note in the top-level README.

## Using them

```sh
python3 run_tva_example.py --video 1_fail_2_pass_3_fail_A.mp4 --pair-sop
python3 sweep.py --all --pair-sop        # one SOP per clip, stems matched
```

Both prefer the committed file here when its content matches the `--sop` source, and
fall back to generating the same bytes under the right name if it does not.

## Regenerating

These are derived, so do not edit them individually — edit `../oring-numbered.txt`
(or whichever SOP you want paired) and regenerate:

```sh
python3 sop/paired/regenerate.py                       # from ../oring-numbered.txt
python3 sop/paired/regenerate.py --sop sop/oring.txt   # from a different source
```

`regenerate.py` fails loudly if a file here has drifted from its source, so a stale
copy cannot be used by accident.
