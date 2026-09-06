import { retryLabel } from "../../src/lib/readStateCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import {
  workbenchQuestionAttribute,
  workbenchQuestions
} from "../../src/lib/workbenchQuestions";

export type WorkbenchQuestion = (typeof workbenchQuestions)[keyof typeof workbenchQuestions];

/**
 * Reading the Workbench's rendered controls, for the two test layers that
 * check every control answers a question: the jsdom room test walks real
 * elements, the browser test collects the same facts inside the page. Only
 * the facts half is production's -- an `Element` never reaches the product.
 * `WorkbenchControlFacts` and its two readers below have no production
 * caller either; they are tooling for the e2e gate that proves every
 * rendered Workbench control is inventoried.
 */
export const workbenchStageSelector = ".workbench";
export const workbenchInteractiveSelector = 'a[href], button, [role="button"], [role="link"]';

export type WorkbenchControlFacts = {
  questionId: string | null;
  href: string | null;
  ariaLabel: string | null;
  tag: string;
};

export function workbenchControlFacts(element: Element): WorkbenchControlFacts {
  return {
    questionId: element.getAttribute(workbenchQuestionAttribute),
    href: element.getAttribute("href"),
    ariaLabel: element.getAttribute("aria-label"),
    tag: element.tagName.toLowerCase()
  };
}

export function describeWorkbenchControlFacts(facts: WorkbenchControlFacts): string {
  const name = facts.ariaLabel ?? facts.questionId ?? "";
  return `${facts.tag}${facts.href === null ? "" : `[href="${facts.href}"]`} ${name}`.trim();
}

export function questionForWorkbenchControlFacts(facts: WorkbenchControlFacts): WorkbenchQuestion | null {
  if (facts.questionId !== null) {
    return questionById(facts.questionId);
  }
  if (facts.tag === "a" && facts.href !== null && facts.href.startsWith("/atelier/runs/")) {
    return workbenchQuestions.openRun;
  }
  if (
    facts.tag === "button" &&
    facts.ariaLabel === retryLabel(workbenchPageCopy.runsLabel)
  ) {
    return workbenchQuestions.reloadWorkbenchRuns;
  }
  return null;
}

function questionById(id: string): WorkbenchQuestion | null {
  return Object.values(workbenchQuestions).find((entry) => entry.id === id) ?? null;
}

export function describeWorkbenchControl(element: Element): string {
  return describeWorkbenchControlFacts(workbenchControlFacts(element));
}

export function questionForWorkbenchControl(element: Element): WorkbenchQuestion | null {
  return questionForWorkbenchControlFacts(workbenchControlFacts(element));
}

export function unansweredWorkbenchControls(root: ParentNode): Element[] {
  return [...root.querySelectorAll(workbenchInteractiveSelector)].filter(
    (element) => questionForWorkbenchControl(element) === null
  );
}
