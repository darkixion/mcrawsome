// Board geometry must agree with mcrawsome.camera.target.CORNERS.
export const TARGET = { columns: 10, rows: 7, innerCorners: [9, 6] } as const;
export type CaptureStep = { title: string; instruction: string; seconds: number; cue: string; distance: "near" | "far"; gate?: boolean };

function pass(distance: "near" | "far"): CaptureStep[] {
  const step = (title: string, instruction: string, seconds: number, cue: string): CaptureStep => ({ title, instruction, seconds, cue, distance });
  const centre = () => step("Return to centre", "Slowly aim back at the middle of the screen and level the phone. Keep the lens in the same place.", 6, "⊙");
  const directions = [
    ["up", "↑"], ["down", "↓"], ["left", "←"], ["right", "→"],
  ];
  return [
    step(distance === "near" ? "Line up the whole screen" : "Line up from farther away",
      distance === "near"
        ? "Start recording on your phone. Centre the whole display in the camera frame, with a little space around every edge. Keep the screen and phone level."
        : "Keep recording. Centre the whole display again; it should now look about half to two-thirds as wide. Keep the same focus, lens and camera settings.", 12, "⊙"),
    ...directions.flatMap(([direction, cue]) => [
      step(`Slowly rotate ${direction}`, `Keep the lens in the same place. Smoothly aim ${direction} until the board approaches the edge of the camera frame. Stop just before a checkerboard corner leaves the frame, and hold briefly.`, 7, cue!),
      centre(),
    ]),
    step("Roll gently clockwise", "Keep aiming at the centre. Rotate the phone clockwise by about 15–20°, without changing its distance.", 7, "↻"),
    centre(),
    step("Roll gently anticlockwise", "Keep aiming at the centre. Rotate the phone anticlockwise by about 15–20°.", 7, "↺"),
    centre(),
    step("Add a view from the left", "Move the phone sideways to your left, keeping it at the same height. Turn it right to centre the whole checkerboard again. Keep the phone level and hold briefly. The arrow shows where to move the phone.", 10, "←"),
    step("Add a view from the right", "Return to your starting position, then move the phone sideways to your right at the same height. Turn it left to centre the whole checkerboard. Keep the phone level and hold briefly.", 10, "→"),
    step("Add a view from above", "Return to your starting position, then raise the phone a little. Tilt it down until the whole checkerboard is centred, then hold briefly.", 10, "↑"),
    step("Add a view from below", "Return to your starting position, then lower the phone a little. Tilt it up until the whole checkerboard is centred, then hold briefly.", 10, "↓"),
  ];
}

export const CAPTURE_STEPS: CaptureStep[] = [
  ...pass("near"),
  { title: "Move farther away", instruction: "Keep recording. Move straight back until the screen looks half to two-thirds as wide. Keep focus locked. When ready, start the farther pass.", seconds: 0, cue: "↔", distance: "far", gate: true },
  ...pass("far"),
];

export function remainingSeconds(step: CaptureStep, elapsed: number): number {
  return Math.max(0, Math.ceil(step.seconds - elapsed));
}
