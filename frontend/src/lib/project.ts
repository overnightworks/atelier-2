import { PRODUCT_NAME } from "./productName";

/**
 * What this installation's one project is called on screen.
 *
 * The wire serves no name for a run, an agent, or this installation, so this is
 * client-owned wording and never a stored datum. It lives in one place because
 * the board card, the project level and the rail's footer must never disagree
 * about what the operator is looking at. It reads the product's own name (#515
 * owner) because this installation's one project is the product itself — a
 * second literal here said "atelier-2" while every other wall said "atelier"
 * (#654).
 *
 * A workflow revision does carry a name since #146, which is why the picker can
 * offer one. Nothing above a run can, which is why rule 5 stays open at #22.
 *
 * When #133 gives a project a backend identity, this constant is replaced by
 * that name, and every screen that reads it follows.
 */
export const THE_ONE_PROJECT = PRODUCT_NAME;
