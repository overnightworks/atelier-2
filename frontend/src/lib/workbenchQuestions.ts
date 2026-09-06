/**
 * Inventory of interactive Workbench controls: each rendered control has an
 * entry, and each entry is shaped as a question. This is the Workbench map
 * only — not a workshop-wide registry, and not a judgement that the question
 * is the right one.
 */
export const workbenchQuestions = {
  emptyStart: {
    id: "empty-start",
    question: "What is the one next action when nothing has happened yet?"
  },
  emptyOpenSettings: {
    id: "empty-open-settings",
    question: "Can I go fix the conductor's binding or model in Settings?"
  },
  openRun: {
    id: "open-run",
    question: "Can I open a run to see it or answer what it needs?"
  },
  reloadWorkbenchRuns: {
    id: "reload-workbench-runs",
    question: "Can I read the workbench runs again?"
  },
  saySomething: {
    id: "say-something",
    question: "Can I tell the workshop what I want?"
  },
  resendConductorMessage: {
    id: "resend-conductor-message",
    question: "Can I send this exact message again after it failed to send?"
  },
  answerDecision: {
    id: "answer-decision",
    question: "Can I answer, or send again, a decision that waits on me?"
  },
  discardPoisonedJournalDoor: {
    id: "discard-poisoned-journal-door",
    question: "Can I open the one door out of a poisoned remembered-sendings journal?"
  },
  discardPoisonedJournalConfirm: {
    id: "discard-poisoned-journal-confirm",
    question: "Can I confirm forgetting the poisoned remembered-sendings journal?"
  },
  discardPoisonedJournalCancel: {
    id: "discard-poisoned-journal-cancel",
    question: "Can I cancel and leave the poisoned remembered-sendings journal as it was?"
  }
} as const;

export const workbenchQuestionAttribute = "data-workbench-question";
