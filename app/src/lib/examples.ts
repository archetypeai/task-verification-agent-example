// Example SOPs for the videos this repo documents. See the repo README.

export type Example = { label: string; video: string; sop: string };

export const EXAMPLES: Example[] = [
	{
		label: 'O-ring cap',
		video: 'data/clips/*.mp4 (30 s each, in the repo)',
		sop: [
			'Step 1: Install the rubber O-ring into the blue block-off cap, ensuring it is properly seated in the groove.',
			'Step 2: Thread the blue cap onto the black manifold by hand until snug, without cross-threading.',
			'Step 3: Using a wrench, tighten the blue cap to the specified torque.'
		].join('\n')
	},
	{
		label: 'Gel electrophoresis',
		video: 'examples/gel-electrophoresis (9 min; download.sh fetches it)',
		sop: [
			'Step 1: Lift the gel tray holding the solidified agarose gel out of the white casting stand.',
			'Step 2: Place the gel tray into the clear electrophoresis chamber.',
			'Step 3: Pour running buffer from the graduated cylinder into the chamber until the gel is covered.',
			'Step 4: Attach a new tip from the tip box to the micropipette.',
			'Step 5: Draw a sample from a tube in the red tube rack into the micropipette tip.',
			'Step 6: Dispense the sample into a well of the gel, with the tip just inside the well.',
			'Step 7: Place the lid with the red and black leads onto the electrophoresis chamber.',
			'Step 8: Plug the leads into the power supply and switch it on.',
			'Step 9: Let the gel run until the dye bands move out of the wells into the gel.'
		].join('\n')
	}
];
