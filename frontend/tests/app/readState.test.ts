import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReadState from "../../src/components/ReadState.svelte";
import { readStateCopy, retryLabel } from "../../src/lib/readStateCopy";
import { reportConnectionLost, reportConnectionRestored } from "../../src/lib/connectionState";
import {
  beginRead,
  confirmRead,
  failRead,
  retainedRead,
  type BegunRead,
  type RetainedRead
} from "../../src/lib/readResource";

type ReadStateFailure =
  | { kind: "unavailable"; title: string }
  | { kind: "incomplete"; title: string };

afterEach(() => {
  cleanup();
  reportConnectionRestored();
});

describe("recoverable read state", () => {
  it("suppresses its own failure and Retry while the whole workshop reads unreachable, and shows them again once it does not (#700)", async () => {
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const failed = failRead(
      first.read,
      first.generation,
      { kind: "unavailable", title: "Saved workflows unavailable" }
    );
    render(ReadState, { props: { read: failed, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.getByRole("alert").isConnected).toBe(true);

    reportConnectionLost();
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.queryByRole("button", { name: retryLabel("saved workflows") })).toBeNull();
    });

    reportConnectionRestored();
    await waitFor(() => {
      expect(screen.getByText("Saved workflows unavailable").isConnected).toBe(true);
      expect(screen.getByRole("button", { name: retryLabel("saved workflows") }).isConnected).toBe(true);
    });
  });

  it("offers a Retry control only while the read is failed, and Retry repeats that read", async () => {
    const retry = vi.fn();
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const failed = failRead(
      first.read,
      first.generation,
      { kind: "unavailable", title: "Saved workflows unavailable" }
    );
    render(ReadState, { props: { read: failed, label: "saved workflows", onRetry: retry } });

    const button = screen.getByRole("button", { name: retryLabel("saved workflows") });
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("alert").textContent).toContain("Saved workflows unavailable");
    expect(screen.getByRole("alert").textContent).not.toContain("Failed to fetch");

    await fireEvent.click(button);
    expect(retry).toHaveBeenCalledTimes(1);
  });

  /**
   * The control exists only while the read is failed, so every attempt
   * rebuilds it -- and only a rebuild that carries the keyboard with it keeps
   * a keyboard operator on the one thing a failed read offers (REQ-UIQ-10).
   */
  describe("the keyboard across a rebuilt Retry", () => {
    const label = "saved workflows";
    const unavailable = { kind: "unavailable", title: "Saved workflows unavailable" } as const;
    const retryControl = (): HTMLElement => screen.getByRole("button", { name: retryLabel(label) });

    /**
     * Whether the operator's own tab holds the keyboard. jsdom cannot report a
     * focused document whose focus rests on nothing -- its `hasFocus` means
     * only "some element is focused", and its `<body>` cannot be focused -- so
     * every scenario states the tab's own answer instead of inheriting one.
     */
    function tabHoldsKeyboard(holds: boolean): void {
      vi.spyOn(document, "hasFocus").mockReturnValue(holds);
    }

    beforeEach(() => {
      tabHoldsKeyboard(true);
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    function renderFailedRead() {
      const onRetry = vi.fn();
      const first = beginRead(retainedRead<string, ReadStateFailure>());
      let read = failRead(first.read, first.generation, unavailable);
      const view = render(ReadState, { props: { read, label, onRetry } });

      async function show(next: RetainedRead<string, ReadStateFailure>): Promise<void> {
        read = next;
        await view.rerender({ read, label, onRetry });
      }

      async function beginAttempt(): Promise<BegunRead<string, ReadStateFailure>> {
        const attempt = beginRead(read);
        await show(attempt.read);
        expect(screen.queryByRole("button", { name: retryLabel(label) })).toBeNull();
        return attempt;
      }

      return {
        container: view.container,
        async attemptAgain(): Promise<void> {
          const attempt = await beginAttempt();
          await show(failRead(attempt.read, attempt.generation, unavailable));
        },
        async attemptSucceeds(): Promise<void> {
          const attempt = await beginAttempt();
          await show(confirmRead(attempt.read, attempt.generation, "confirmed truth"));
        }
      };
    }

    it("leaves the keyboard where it stands when an unprompted failure raises the control", () => {
      renderFailedRead();

      expect(document.activeElement).not.toBe(retryControl());
    });

    it("returns the keyboard on every rebuild, not only on the attempt the operator pressed", async () => {
      const { attemptAgain } = renderFailedRead();
      retryControl().focus();
      await fireEvent.click(retryControl());

      await attemptAgain();
      expect(document.activeElement).toBe(retryControl());

      // A background read of the same resource rebuilds the control exactly
      // as the operator's own retry does, and the keyboard belongs on it
      // either way.
      await attemptAgain();
      expect(document.activeElement).toBe(retryControl());
    });

    it("forgets the keyboard once a retry confirms, so the next unprompted failure steals nothing", async () => {
      const { attemptAgain, attemptSucceeds } = renderFailedRead();
      retryControl().focus();
      await fireEvent.click(retryControl());
      await attemptAgain();
      expect(document.activeElement).toBe(retryControl());

      await attemptSucceeds();
      expect(screen.queryByRole("button", { name: retryLabel(label) })).toBeNull();

      await attemptAgain();
      expect(document.activeElement).not.toBe(retryControl());
    });

    it("leaves focus and its memory alone while the operator's tab holds the keyboard elsewhere", async () => {
      const { attemptAgain } = renderFailedRead();
      retryControl().focus();
      await fireEvent.click(retryControl());

      tabHoldsKeyboard(false);
      await attemptAgain();
      expect(document.activeElement).not.toBe(retryControl());

      tabHoldsKeyboard(true);
      await attemptAgain();
      expect(document.activeElement).toBe(retryControl());
    });

    it("leaves the keyboard on whatever the operator moved to instead", async () => {
      const { container, attemptAgain } = renderFailedRead();
      retryControl().focus();
      const otherControl = container.ownerDocument.createElement("button");
      container.append(otherControl);
      otherControl.focus();

      await attemptAgain();

      expect(document.activeElement).toBe(otherControl);
    });
  });

  it("shows no button while looking, refreshing or holding confirmed truth -- a control only ever answers a named failure", () => {
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const looking = first.read;
    render(ReadState, { props: { read: looking, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain(readStateCopy.looking);
    cleanup();

    const confirmed = confirmRead(looking, first.generation, "truth");
    const refreshing = beginRead(confirmed).read;
    render(ReadState, { props: { read: refreshing, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain(readStateCopy.refreshing);
    cleanup();

    const idleConfirmed: RetainedRead<string, ReadStateFailure> = {
      confirmed: "truth",
      generation: confirmed.generation,
      request: { state: "idle" }
    };
    render(ReadState, { props: { read: idleConfirmed, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
