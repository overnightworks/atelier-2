/**
 * Copy for the Workbench's terminal seat: the frame around the terminal, and
 * the two honest sentences for when there is no terminal to frame.
 *
 * The seat itself says nothing — what is in it belongs to the operator and the
 * agent CLI, and the workshop neither reads nor repeats it. These strings are
 * only what the room says around it: whose seat this is, what it hangs on, what
 * it may do, and where to go while it cannot be reached.
 */
export const seatCopy = {
  regionLabel: "Terminal",
  terminalTitle: "Your terminal in this project",
  /** What the seat hangs on, said plainly because the trust boundary must be visible. */
  attachment: "tmux · ttyd · loopback only",
  connecting: "Connecting to your terminal…",
  /**
   * The way out is a real one the operator already has: the same agent CLI in
   * a terminal of their own, or the workshop's HTTP API. Naming it is what
   * makes this a refusal rather than a dead frame.
   */
  unreachableTitle: "The terminal is not answering",
  unreachableDetail:
    "Talk to the agent CLI in a terminal of your own, or drive the workshop through its HTTP API, until it answers here again.",
  trustBoundary:
    "A terminal is a shell: the same trust boundary as this local serve — one user, no login."
} as const;
