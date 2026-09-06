/**
 * The History result half-sentence, derived from a run's typed output.
 *
 * One mapping owner, next to `historyPageCopy.ts`. Known catalog workflows
 * yield a short derived sentence from their declared schema; an unknown
 * workflow, or a payload that does not match, falls back to a shape
 * statement that carries no content (field count, item count, or "text").
 * Finding text, reasons, bodies, and other payload strings never enter
 * the sentence.
 */
import {
  historyBreakdownOutcome,
  historyCodeReviewOutcome,
  historyFieldsShape,
  historyItemsShape,
  historyPageCopy,
  historyRefineOutcome
} from "./historyPageCopy";

const CATALOG_CODE_REVIEW = "code-review";
const CATALOG_REFINE = "refine";
const CATALOG_DOCUMENTATION_CURATION = "documentation-curation";
const CATALOG_BREAKDOWN = "breakdown";
const CATALOG_PLAN_REVIEW = "plan-review";

const CODE_REVIEW_VERDICTS = {
  approve: historyPageCopy.outcome.approved,
  revise: historyPageCopy.outcome.revise,
  "cannot-judge": historyPageCopy.outcome.cannotJudge
} as const;

const PLAN_REVIEW_VERDICTS = {
  pass: historyPageCopy.outcome.pass,
  revise: historyPageCopy.outcome.revise,
  "cannot-judge": historyPageCopy.outcome.cannotJudge
} as const;

const DOCUMENTATION_CURATION_VERDICTS = {
  approve: historyPageCopy.outcome.approved,
  revise: historyPageCopy.outcome.revise,
  "cannot-judge": historyPageCopy.outcome.cannotJudge
} as const;

const BREAKDOWN_VERDICTS = {
  buildable: historyPageCopy.outcome.buildable,
  needs_decision: historyPageCopy.outcome.needsDecision
} as const;

type FindingSeverity = "high" | "medium" | "low";
const SEVERITY_COPY: Record<FindingSeverity, string> = {
  high: historyPageCopy.outcome.high,
  medium: historyPageCopy.outcome.medium,
  low: historyPageCopy.outcome.low
};
const SEVERITY_RANK: Record<FindingSeverity, number> = {
  high: 3,
  medium: 2,
  low: 1
};

export function historyOutcome(workflowName: string, decodedAnswer: string): string {
  const declared = parseJson(decodedAnswer);
  switch (workflowName) {
    case CATALOG_CODE_REVIEW: {
      const mapped = codeReviewOutcome(declared);
      if (mapped !== null) return mapped;
      break;
    }
    case CATALOG_REFINE: {
      const mapped = refineOutcome(declared);
      if (mapped !== null) return mapped;
      break;
    }
    case CATALOG_DOCUMENTATION_CURATION: {
      const mapped = documentationCurationOutcome(declared);
      if (mapped !== null) return mapped;
      break;
    }
    case CATALOG_BREAKDOWN: {
      const mapped = breakdownOutcome(declared);
      if (mapped !== null) return mapped;
      break;
    }
    case CATALOG_PLAN_REVIEW: {
      const mapped = planReviewOutcome(declared);
      if (mapped !== null) return mapped;
      break;
    }
    default:
      break;
  }
  return shapeStatement(declared);
}

function codeReviewOutcome(declared: unknown): string | null {
  if (!isRecord(declared)) return null;
  const verdict = declared.verdict;
  if (verdict !== "approve" && verdict !== "revise" && verdict !== "cannot-judge") {
    return null;
  }
  const findings = Array.isArray(declared.findings) ? declared.findings : [];
  let highest: FindingSeverity | null = null;
  for (const finding of findings) {
    if (!isRecord(finding)) continue;
    const severity = finding.severity;
    if (severity !== "high" && severity !== "medium" && severity !== "low") continue;
    if (highest === null || SEVERITY_RANK[severity] > SEVERITY_RANK[highest]) {
      highest = severity;
    }
  }
  return historyCodeReviewOutcome(
    CODE_REVIEW_VERDICTS[verdict],
    findings.length,
    highest === null ? null : SEVERITY_COPY[highest]
  );
}

function refineOutcome(declared: unknown): string | null {
  if (!isRecord(declared) || !Array.isArray(declared.expectations)) return null;
  const lenses = new Set<string>();
  for (const line of declared.expectations) {
    if (isRecord(line) && typeof line.lens === "string") lenses.add(line.lens);
  }
  if (Array.isArray(declared.lenses_without_lines)) {
    for (const lens of declared.lenses_without_lines) {
      if (typeof lens === "string") lenses.add(lens);
    }
  }
  return historyRefineOutcome(declared.expectations.length, lenses.size);
}

function documentationCurationOutcome(declared: unknown): string | null {
  if (!isRecord(declared)) return null;
  const verdict = declared.verdict;
  if (verdict !== "approve" && verdict !== "revise" && verdict !== "cannot-judge") {
    return null;
  }
  return DOCUMENTATION_CURATION_VERDICTS[verdict];
}

function breakdownOutcome(declared: unknown): string | null {
  if (!isRecord(declared) || !Array.isArray(declared.slices)) return null;
  const verdict = declared.verdict;
  if (verdict !== "buildable" && verdict !== "needs_decision") return null;
  return historyBreakdownOutcome(declared.slices.length, BREAKDOWN_VERDICTS[verdict]);
}

function planReviewOutcome(declared: unknown): string | null {
  if (!isRecord(declared)) return null;
  const verdict = declared.verdict;
  if (verdict !== "pass" && verdict !== "revise" && verdict !== "cannot-judge") {
    return null;
  }
  return PLAN_REVIEW_VERDICTS[verdict];
}

function shapeStatement(declared: unknown): string {
  if (isRecord(declared)) return historyFieldsShape(Object.keys(declared).length);
  if (Array.isArray(declared)) return historyItemsShape(declared.length);
  return historyPageCopy.outcome.text;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
