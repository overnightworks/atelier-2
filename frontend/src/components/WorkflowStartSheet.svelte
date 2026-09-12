<script lang="ts">
  import { onMount, tick } from "svelte";
  import { SvelteSet } from "svelte/reactivity";

  import {
    MAXIMUM_ARTIFACT_BYTES,
    type AgentConfigurationRevisionListItem,
    type AuthProfileRevision,
    type CockpitApi,
    type ModelRegistryRevision,
    type ObservedQueueItem,
    type ProjectModelResolution,
    type WorkflowRevisionDetail
  } from "../api/client";
  import {
    observedSourceHeading,
    observedWorkItemLabel,
    pinnedModelLine,
    projectDefaultLine,
    startAccountSuffix,
    startConfigurationLabel,
    startNotStartableReason,
    startOrderByteCount,
    startOrderGroup,
    workItemFor,
    workflowStartCopy
  } from "../lib/catalogPageCopy";
  import { problemCode } from "../lib/catalogName";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import {
    createRunId as makeRunId,
    startMutation,
    type MutationJournal,
    type StartOrder
  } from "../lib/mutationJournal";
  import {
    classifyStartOrderSchema,
    summarizeOrderSchema,
    typeLabel,
    type OrderSchemaField,
    type OrderSchemaResource,
    type StartOrderSchemaShape
  } from "../lib/orderSchema";
  import { readRawOrderJson } from "../lib/rawOrderJson";
  import { readEveryAgentConfiguration, readEveryAuthProfile } from "../lib/runPages";
  import { agentRolesOf } from "../lib/savedWorkflows";
  import { matchesSearchTerm } from "../lib/searchTerm";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let revision: WorkflowRevisionDetail;
  export let workflowName: string;
  export let navigate: (path: string) => void;
  export let createRunId: () => string = makeRunId;
  export let onClose: () => void;

  interface OrderDraft {
    readonly name: string;
    readonly schemaRevision: string;
    resource: OrderSchemaResource | null;
    shape: StartOrderSchemaShape | null;
    values: Record<string, string>;
    /** A string-schema order's exact text, typed or read from a file (#438 Scheibe 1b). */
    stringValue: string;
    /**
     * An object-schema order's optional whole-instance override: when filled and
     * valid JSON, it is published instead of the fields composed above.
     */
    rawJson: string;
  }

  type RoleResolution = ProjectModelResolution["resolutions"][number];
  type ModelOverride = { role: string; agent_configuration_revision_hash: string };

  interface RegisteredConfiguration {
    configuration: AgentConfigurationRevisionListItem;
    modelId: string;
    accountId: string;
  }

  type DialogElement = HTMLElement & {
    open: boolean;
    showModal?: () => void;
    close?: () => void;
  };

  let configurations: readonly AgentConfigurationRevisionListItem[] = [];
  let registeredConfigurations: readonly RegisteredConfiguration[] = [];
  let observedQueueItems: readonly ObservedQueueItem[] = [];
  let projectReference: string | null = null;
  let resolutions: Record<string, RoleResolution> = {};
  let manualOverrides: Record<string, string> = {};
  let resolutionGeneration = 0;
  let orders: OrderDraft[] = [];
  let loading = true;
  let resolving = false;
  let starting = false;
  let failure: string | null = null;
  let startFailure: string | null = null;
  let dialogElement: DialogElement;
  let closeButton: HTMLButtonElement;
  let opener: HTMLElement | null = null;
  let openWorkItemOrder: string | null = null;
  let activeWorkItemId: string | null = null;
  let workItemFilter = "";
  let workItemFilterElement: HTMLInputElement | null = null;
  let suppressDialogCancel = false;

  $: roles = agentRolesOf(revision.graph);
  $: canStart =
    !loading &&
    !resolving &&
    !starting &&
    roles.every(roleCanStart) &&
    orders.every(orderCanStart);
  $: offerableQueueItems = observedQueueItems.filter((item) => item.retired_at === null);
  $: filteredItemsBySource = visibleItemsBySource(workItemFilter, offerableQueueItems);
  $: disabledStartReason = startDisabledReason(
    loading,
    resolving,
    orders,
    roles,
    resolutions,
    registeredConfigurations
  );

  onMount(() => {
    const activeElement = globalThis.document.activeElement;
    opener = activeElement instanceof globalThis.HTMLElement ? activeElement : null;
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    closeButton.focus();
    void load();
  });

  async function load(): Promise<void> {
    loading = true;
    failure = null;
    startFailure = null;
    try {
      const [projects, configurationsPage, profilesPage, loadedOrders] = await Promise.all([
        cockpitApi.listProjects(),
        readEveryAgentConfiguration((after) => cockpitApi.listAgentConfigurationRevisions(after)),
        readEveryAuthProfile((after) => cockpitApi.listAuthProfileRevisions(after)),
        Promise.all(declaredOrders().map(loadOrder))
      ]);
      if (!configurationsPage.complete) throw new Error(workflowStartCopy.configurationsIncomplete);
      if (!profilesPage.complete) throw new Error(workflowStartCopy.accountsIncomplete);
      projectReference = projects.items[0]?.public_project_reference ?? null;
      if (projectReference === null && roles.length > 0) {
        throw new Error(workflowStartCopy.servedProjectMissing);
      }
      configurations = configurationsPage.configurations;
      registeredConfigurations = await registeredConfigurationsOf(
        configurations,
        profilesPage.profiles
      );
      orders = loadedOrders;
      if (loadedOrders.some((order) => order.shape?.kind === "work_item")) {
        observedQueueItems = await readEveryObservedQueueItem();
      }
      if (roles.length > 0) await resolveModels(false);
    } catch (error) {
      failure = humanErrorMessage(error, workflowStartCopy.sheetUnavailable);
    } finally {
      loading = false;
    }
  }

  async function registeredConfigurationsOf(
    available: readonly AgentConfigurationRevisionListItem[],
    profiles: readonly AuthProfileRevision[]
  ): Promise<RegisteredConfiguration[]> {
    const providers = [...new Set(available.map((configuration) => configuration.provider_id))]
      .sort();
    const registries = (await Promise.all(providers.map(async (providerId) => {
      try {
        return await cockpitApi.getModelRegistry(providerId);
      } catch (error) {
        if (problemCode(error) === "model-registry-missing") return null;
        throw error;
      }
    }))).filter((registry): registry is ModelRegistryRevision => registry !== null);
    const registered: RegisteredConfiguration[] = [];
    const included = new SvelteSet<string>();
    for (const registry of registries) {
      for (const entry of registry.entries) {
        if (entry.provider_check !== "checked") continue;
        const configuration = available.find((candidate) =>
          candidate.agent_configuration_revision_hash === entry.agent_configuration_revision_hash &&
          candidate.provider_id === registry.provider_id
        );
        const profile = configuration === undefined
          ? undefined
          : profiles.find((candidate) =>
              candidate.auth_profile_revision_hash === configuration.auth_profile_revision_hash &&
              candidate.provider_id === configuration.provider_id
            );
        if (
          configuration === undefined ||
          profile === undefined ||
          included.has(configuration.agent_configuration_revision_hash)
        ) continue;
        included.add(configuration.agent_configuration_revision_hash);
        registered.push({ configuration, modelId: entry.model_id, accountId: profile.profile_id });
      }
    }
    return registered;
  }

  function modelOverrides(): ModelOverride[] {
    return roles.flatMap((role) => {
      const configurationHash = manualOverrides[role];
      return configurationHash === undefined || configurationHash.length === 0
        ? []
        : [{ role, agent_configuration_revision_hash: configurationHash }];
    });
  }

  function exactResolutions(
    response: ProjectModelResolution
  ): Record<string, RoleResolution> | null {
    if (
      response.workflow_revision_hash !== revision.workflow_revision_hash ||
      response.public_project_reference !== projectReference ||
      response.resolutions.length !== roles.length
    ) return null;
    const byRole: Record<string, RoleResolution> = {};
    for (const resolution of response.resolutions) {
      if (!roles.includes(resolution.role) || byRole[resolution.role] !== undefined) return null;
      byRole[resolution.role] = resolution;
    }
    return roles.every((role) => byRole[role] !== undefined) ? byRole : null;
  }

  async function resolveModels(forStart: boolean): Promise<Record<string, RoleResolution> | null> {
    if (roles.length === 0) return {};
    const reference = projectReference;
    if (reference === null) throw new Error(workflowStartCopy.servedProjectMissing);
    const generation = ++resolutionGeneration;
    resolving = true;
    try {
      const response = await cockpitApi.resolveProjectModels(
        reference,
        revision.workflow_revision_hash,
        modelOverrides()
      );
      const exact = exactResolutions(response);
      if (exact === null) throw new Error(workflowStartCopy.rolesUnresolved);
      if (generation !== resolutionGeneration) return null;
      resolutions = exact;
      return exact;
    } catch (error) {
      if (generation !== resolutionGeneration) return null;
      if (forStart) startFailure = humanErrorMessage(error, workflowStartCopy.startUnavailable);
      else throw error;
      return null;
    } finally {
      if (generation === resolutionGeneration) resolving = false;
    }
  }

  function registeredConfiguration(configurationHash: string | null): RegisteredConfiguration | undefined {
    if (configurationHash === null) return undefined;
    return registeredConfigurations.find(({ configuration }) =>
      configuration.agent_configuration_revision_hash === configurationHash
    );
  }

  /**
   * The listed configuration behind a hash, straight from the exhaustive
   * fetch (`configurations`) rather than the registry-matched subset
   * (`registeredConfigurations`): the host's `not_startable_reason` for a
   * resolved-but-unavailable role lives here even when the model registry
   * never matched that configuration into `registeredConfigurations` at all.
   */
  function configurationByHash(hash: string): AgentConfigurationRevisionListItem | undefined {
    return configurations.find((candidate) => candidate.agent_configuration_revision_hash === hash);
  }

  function roleCanStart(role: string): boolean {
    const resolution = resolutions[role];
    if (resolution?.agent_configuration_revision_hash === null) return false;
    return registeredConfiguration(resolution?.agent_configuration_revision_hash ?? null)
      ?.configuration.startable === true;
  }

  function configurationLabel(registered: RegisteredConfiguration): string {
    return startConfigurationLabel(
      registered.configuration.provider_id,
      registered.modelId,
      registered.accountId
    );
  }

  function resolvedConfigurationLabel(
    resolution: RoleResolution,
    registered: RegisteredConfiguration | undefined
  ): string {
    const model = resolution.model_id ?? workflowStartCopy.unavailable;
    const account = registered === undefined ? "" : startAccountSuffix(registered.accountId);
    if (resolution.source === "pinned-in-workflow") {
      return pinnedModelLine(model, account);
    }
    if (resolution.source === "from-project") {
      return projectDefaultLine(
        resolution.declared_difficulty,
        model,
        resolution.default_difficulty !== resolution.declared_difficulty,
        account
      );
    }
    return registered === undefined ? model : configurationLabel(registered);
  }

  async function chooseConfiguration(role: string, configurationHash: string): Promise<void> {
    manualOverrides = { ...manualOverrides };
    if (configurationHash.length === 0) delete manualOverrides[role];
    else manualOverrides[role] = configurationHash;
    startFailure = null;
    await resolveModels(false).catch((error: unknown) => {
      failure = humanErrorMessage(error, workflowStartCopy.sheetUnavailable);
    });
  }

  async function readEveryObservedQueueItem(): Promise<readonly ObservedQueueItem[]> {
    const items: ObservedQueueItem[] = [];
    const seenCursors: string[] = [];
    let after: string | undefined;
    let nextAfter: string | null = "";
    while (nextAfter !== null) {
      const page = await cockpitApi.listObservedQueueItems(after);
      items.push(...page.items);
      nextAfter = page.next_after;
      if (nextAfter === null) continue;
      if (seenCursors.includes(nextAfter)) throw new Error(workflowStartCopy.observedQueueIncomplete);
      seenCursors.push(nextAfter);
      after = nextAfter;
    }
    return items;
  }

  function declaredOrders(): OrderDraft[] {
    return revision.graph.orders.map((order) => ({
      name: order.name,
      schemaRevision: order.schema.revision,
      resource: null,
      shape: null,
      values: {},
      stringValue: "",
      rawJson: ""
    }));
  }

  async function loadOrder(order: OrderDraft): Promise<OrderDraft> {
    const document = await cockpitApi.getSchemaRevision(order.schemaRevision);
    return {
      ...order,
      resource: { document, summary: summarizeOrderSchema(document) },
      shape: classifyStartOrderSchema(document, order.schemaRevision)
    };
  }

  /**
   * The way out a Raw JSON syntax refusal names for `order`: an
   * `inline_object` order keeps its per-field form beside Raw JSON, so it
   * can send a person back there; a `raw_object` order has no such form
   * (that is why it fell back to Raw JSON alone), so it names a different
   * one (#1130 finding 2).
   */
  function rawJsonWayOut(order: OrderDraft): string {
    return order.shape?.kind === "raw_object"
      ? workflowStartCopy.rawJsonWayOutAlone
      : workflowStartCopy.rawJsonWayOutBesideForm;
  }

  function orderCanStart(order: OrderDraft): boolean {
    if (order.resource === null || order.shape === null || order.shape.kind === "unsupported") {
      return false;
    }
    if (order.shape.kind === "work_item") return (order.values.work_item?.length ?? 0) > 0;
    if (order.shape.kind === "string") return order.stringValue.length > 0;
    if (order.shape.kind === "raw_object") return readRawOrderJson(order.rawJson, rawJsonWayOut(order)).ok;
    if (order.rawJson.trim().length > 0) return readRawOrderJson(order.rawJson, rawJsonWayOut(order)).ok;
    return requiredFieldsFilled(order);
  }

  function orderGroupLabel(order: OrderDraft): string {
    return order.shape?.kind === "work_item" ? workflowStartCopy.workItem : startOrderGroup(order.name);
  }

  function startDisabledReason(
    isLoading: boolean,
    isResolving: boolean,
    currentOrders: readonly OrderDraft[],
    currentRoles: readonly string[],
    currentResolutions: Readonly<Record<string, RoleResolution>>,
    currentConfigurations: readonly RegisteredConfiguration[]
  ): string | null {
    if (isLoading || isResolving) return workflowStartCopy.startPreparing;
    const incompleteOrder = currentOrders.find((order) => !orderCanStart(order));
    if (incompleteOrder !== undefined) {
      if (incompleteOrder.shape?.kind === "work_item") {
        return observedQueueItems.length === 0
          ? workflowStartCopy.startNeedsWorkItemSource
          : workflowStartCopy.startNeedsWorkItem;
      }
      return workflowStartCopy.startNeedsOrder;
    }
    const unresolvedRole = currentRoles.find((role) => {
      const resolution = currentResolutions[role];
      const configurationHash = resolution?.agent_configuration_revision_hash ?? null;
      return currentConfigurations.find(({ configuration }) =>
        configuration.agent_configuration_revision_hash === configurationHash
      )?.configuration.startable !== true;
    });
    return unresolvedRole === undefined
      ? null
      : workflowStartCopy.startNeedsConfiguration(unresolvedRole);
  }

  function requiredFieldsFilled(order: OrderDraft): boolean {
    return (order.resource?.summary.fields ?? [])
      .filter((field) => field.required)
      .every((field) => (order.values[field.name] ?? "").trim().length > 0);
  }

  function setOrderValue(orderName: string, field: string, value: string): void {
    orders = orders.map((order) =>
      order.name === orderName ? { ...order, values: { ...order.values, [field]: value } } : order
    );
  }

  function setOrderStringValue(orderName: string, value: string): void {
    orders = orders.map((order) => (order.name === orderName ? { ...order, stringValue: value } : order));
  }

  function setOrderRawJson(orderName: string, value: string): void {
    orders = orders.map((order) => (order.name === orderName ? { ...order, rawJson: value } : order));
  }

  async function setOrderStringFromFile(orderName: string, file: File): Promise<void> {
    setOrderStringValue(orderName, await file.text());
  }

  function orderByteLength(value: string): number {
    return new TextEncoder().encode(value).length;
  }

  /** The exact bytes an object order's structured form composes, absent a Raw JSON override. */
  function structuredObjectOrderJson(order: OrderDraft): string {
    const fields = order.resource?.summary.fields ?? [];
    const value = Object.fromEntries(
      fields.flatMap((field) => {
        const typed = order.values[field.name] ?? "";
        return typed.length === 0 ? [] : [[field.name, typedValue(field, typed)]];
      })
    );
    return JSON.stringify(value);
  }

  /**
   * The exact bytes one order publishes as its artifact (#438 Scheibe 1b): a
   * string order's typed or file text as-is, an object order's Raw JSON text
   * as the operator wrote it when present, or its structured form otherwise.
   * A work item stays its own third door -- the start reads it, so it never
   * publishes anything here.
   */
  function orderPublicationText(order: OrderDraft): string {
    if (order.shape?.kind === "string") return order.stringValue;
    return order.rawJson.trim().length > 0 ? order.rawJson : structuredObjectOrderJson(order);
  }

  async function publishedOrder(order: OrderDraft): Promise<StartOrder> {
    if (order.shape?.kind === "work_item") {
      return { name: order.name, work_item: order.values.work_item ?? "" };
    }
    const published = await cockpitApi.publishArtifact(orderPublicationText(order));
    return { name: order.name, artifact_hash: published.value.artifact_hash };
  }

  function inputType(field: OrderSchemaField): "checkbox" | "number" | "text" {
    if (field.types?.includes("boolean")) return "checkbox";
    if (field.types?.includes("number") || field.types?.includes("integer")) return "number";
    return "text";
  }

  function typedValue(field: OrderSchemaField, value: string): boolean | number | string {
    if (field.types?.includes("boolean")) return value === "true";
    if (field.types?.includes("number") || field.types?.includes("integer")) return Number(value);
    return value;
  }

  function adapterGrammarLabel(reference: string): string {
    if (reference.startsWith("gh:")) return `#${reference.slice(3)}`;
    if (reference.startsWith("gl:")) return `!${reference.slice(3)}`;
    return reference;
  }

  function platformOf(reference: string): string {
    if (reference.startsWith("gh:")) return workflowStartCopy.github;
    if (reference.startsWith("gl:")) return workflowStartCopy.gitlab;
    return workflowStartCopy.unknownSource;
  }

  function observedGroupHeading(item: ObservedQueueItem): string {
    return observedSourceHeading(item.project_id, platformOf(item.tracker_item_reference));
  }

  function groupObservedItemsBySource(
    items: readonly ObservedQueueItem[]
  ): ReadonlyArray<readonly [string, readonly ObservedQueueItem[]]> {
    const grouped: Array<[string, ObservedQueueItem[]]> = [];
    for (const item of items) {
      const heading = observedGroupHeading(item);
      const group = grouped.find(([candidate]) => candidate === heading);
      if (group === undefined) grouped.push([heading, [item]]);
      else group[1].push(item);
    }
    return grouped;
  }

  /**
   * The picker's one filter owner (#962 ruling: narrows by a tap on number
   * or title). Matches the adapter-grammar reference (`#45`), the raw
   * reference (`45`), and the title, substring and case-insensitive, so
   * either spelling of the number finds the item.
   */
  function filterObservedItems(
    items: readonly ObservedQueueItem[],
    query: string
  ): readonly ObservedQueueItem[] {
    return items.filter((item) =>
      matchesSearchTerm(
        [item.tracker_item_reference, adapterGrammarLabel(item.tracker_item_reference), item.title],
        query
      )
    );
  }

  /**
   * The one grouping-and-filtering pipeline behind both the reactive render
   * value and the imperative reads (`flattenedObservedItems`) that need the
   * result for a `workItemFilter` that has not reached the reactive
   * statement yet.
   */
  function visibleItemsBySource(
    query: string,
    items: readonly ObservedQueueItem[]
  ): ReadonlyArray<readonly [string, readonly ObservedQueueItem[]]> {
    return groupObservedItemsBySource(filterObservedItems(items, query));
  }

  function selectedWorkItemLabel(order: OrderDraft): string {
    const reference = order.values.work_item ?? "";
    if (reference.length === 0) return workflowStartCopy.choose;
    const item = observedQueueItems.find(
      (candidate) => candidate.tracker_item_reference === reference
    );
    return item === undefined ? adapterGrammarLabel(reference) : workItemLabel(item);
  }

  function workItemLabel(item: ObservedQueueItem): string {
    return observedWorkItemLabel(adapterGrammarLabel(item.tracker_item_reference), item.title);
  }

  function workItemListName(orderName: string): string {
    return workItemFor(orderName);
  }

  function workItemOptionId(orderName: string, itemId: string): string {
    return `work-item-option-${orderName}-${itemId}`;
  }

  function workItemComboboxId(orderName: string): string {
    return `work-item-combobox-${orderName}`;
  }

  function flattenedObservedItems(): readonly ObservedQueueItem[] {
    return visibleItemsBySource(workItemFilter, offerableQueueItems).flatMap(([, items]) => items);
  }

  function closeWorkItemPicker(): void {
    const closedOrder = openWorkItemOrder;
    openWorkItemOrder = null;
    activeWorkItemId = null;
    workItemFilter = "";
    if (closedOrder === null) return;
    // The filter field the closing picker owned is about to unmount; hand
    // focus back to the trigger instead of letting it fall to the document.
    globalThis.document.getElementById(workItemComboboxId(closedOrder))?.focus();
  }

  function openWorkItemPicker(orderName: string, preferEnd = false): void {
    workItemFilter = "";
    // Reachable only from the combobox button, which this template renders
    // solely once offerableQueueItems is non-empty, so the freshly-cleared
    // filter always yields at least one item here.
    const items = flattenedObservedItems();
    const selected = orders.find((order) => order.name === orderName)?.values.work_item ?? "";
    const selectedItem = items.find((item) => item.tracker_item_reference === selected);
    openWorkItemOrder = orderName;
    activeWorkItemId = selectedItem?.item_id
      ?? (preferEnd ? items.at(-1)?.item_id : items[0]?.item_id)
      ?? null;
    // A mouse or keyboard open both land the caret in the filter field, the
    // one place ArrowUp/ArrowDown/Enter/Escape now work (REQ-UIQ-05).
    void tick().then(() => workItemFilterElement?.focus());
  }

  function toggleWorkItemPicker(orderName: string): void {
    if (openWorkItemOrder === orderName) closeWorkItemPicker();
    else openWorkItemPicker(orderName);
  }

  /** Live narrowing (#962 ruling): typing filters without Enter, keeping the
   * active option under the caret when it is still visible, else moving to
   * the first visible one. */
  function setWorkItemFilter(query: string): void {
    workItemFilter = query;
    const items = flattenedObservedItems();
    activeWorkItemId = items.some((item) => item.item_id === activeWorkItemId)
      ? activeWorkItemId
      : items[0]?.item_id ?? null;
  }

  function moveActiveWorkItem(orderName: string, delta: number): void {
    const items = flattenedObservedItems();
    if (items.length === 0) return;
    if (openWorkItemOrder !== orderName) {
      openWorkItemPicker(orderName, delta < 0);
      return;
    }
    const current = items.findIndex((item) => item.item_id === activeWorkItemId);
    const start = current === -1 ? (delta > 0 ? -1 : items.length) : current;
    activeWorkItemId = items[Math.max(0, Math.min(items.length - 1, start + delta))]!.item_id;
  }

  function chooseWorkItem(orderName: string, reference: string): void {
    setOrderValue(orderName, "work_item", reference);
    closeWorkItemPicker();
  }

  function chooseActiveWorkItem(orderName: string): void {
    const item = flattenedObservedItems().find((candidate) => candidate.item_id === activeWorkItemId);
    if (item === undefined) return;
    chooseWorkItem(orderName, item.tracker_item_reference);
  }

  /**
   * The closed combobox's own keys: open it (ArrowUp/ArrowDown/Enter/Space)
   * or move within it once it is already open by some other route. Once the
   * picker opens, focus leaves this button for the filter field below, so
   * Tab is left to its native action — the filter field is the very next
   * focusable element, not a target this handler intercepts (REQ-UIQ-05).
   */
  function handleWorkItemPickerKey(orderName: string, event: KeyboardEvent): void {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActiveWorkItem(orderName, 1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveActiveWorkItem(orderName, -1);
        return;
      case "Enter":
      case " ":
        event.preventDefault();
        if (openWorkItemOrder === orderName) chooseActiveWorkItem(orderName);
        else openWorkItemPicker(orderName);
        return;
      default:
        return;
    }
  }

  /**
   * The filter field's own keys, once real keyboard focus reaches it
   * (REQ-UIQ-05). Deliberately not `handleWorkItemPickerKey`: that
   * handler's " " case would swallow a space typed into a title filter.
   */
  function handleWorkItemFilterKey(orderName: string, event: KeyboardEvent): void {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActiveWorkItem(orderName, 1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveActiveWorkItem(orderName, -1);
        return;
      case "Enter":
        event.preventDefault();
        chooseActiveWorkItem(orderName);
        return;
      case "Escape":
        // Close only the picker, not the whole sheet: stop the dialog's own
        // Escape handling (containDialogFocus) from seeing an already-closed
        // picker and dismissing the sheet underneath it.
        event.preventDefault();
        event.stopPropagation();
        closeWorkItemPicker();
        return;
      default:
        return;
    }
  }

  function dismiss(): void {
    if (dialogElement.open && typeof dialogElement.close === "function") dialogElement.close();
    else dialogElement.open = false;
    onClose();
    globalThis.queueMicrotask(() => opener?.focus());
  }

  function handleDialogCancel(event: Event): void {
    if (openWorkItemOrder !== null || suppressDialogCancel) {
      event.preventDefault();
      suppressDialogCancel = false;
      closeWorkItemPicker();
      return;
    }
    dismiss();
  }

  function containDialogFocus(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      event.preventDefault();
      if (openWorkItemOrder !== null || suppressDialogCancel) {
        suppressDialogCancel = true;
        closeWorkItemPicker();
        return;
      }
      dismiss();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      dialogElement.querySelectorAll<HTMLElement>(
        'button:not([disabled]):not([role="option"]), input:not([disabled]), select:not([disabled]), a[href]'
      )
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && globalThis.document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && globalThis.document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function start(): Promise<void> {
    if (!canStart) return;
    starting = true;
    startFailure = null;
    try {
      const refreshed = await resolveModels(true);
      if (refreshed === null || !roles.every((role) => {
        const hash = refreshed[role]?.agent_configuration_revision_hash ?? null;
        return hash !== null && registeredConfiguration(hash)?.configuration.startable === true;
      })) return;
      const expectedBindings = roles.map((role) => ({
        role,
        agent_configuration_revision_hash: refreshed[role]!.agent_configuration_revision_hash!
      }));
      const mutation = startMutation(
        createRunId(),
        revision.workflow_revision_hash,
        modelOverrides(),
        await Promise.all(orders.map(publishedOrder))
      );
      await mutationJournal.prepare(mutation);
      const result = await cockpitApi.start(mutation);
      const returned = "workflow_format_version" in result.value ? result.value.agent_bindings : null;
      if (
        returned === null ||
        returned.length !== expectedBindings.length ||
        new SvelteSet(returned.map((binding) => binding.role)).size !== returned.length ||
        expectedBindings.some(
          (binding) =>
            returned.find((candidate) => candidate.role === binding.role)
              ?.agent_configuration_revision_hash !== binding.agent_configuration_revision_hash
        )
      ) {
        throw new Error(workflowStartCopy.startResponseChangedRoles);
      }
      const resolved = await mutationJournal.resolve(mutation.mutation_id, {
        type: "start_response",
        status: result.status,
        target: mutation.target,
        request_body_base64: mutation.body_base64,
        run_id: result.value.run_id,
        public_run_reference: result.value.public_run_reference,
        workflow_revision_hash: result.value.workflow_revision_hash
      });
      if (!resolved) throw new Error(workflowStartCopy.startResponseUnproven);
      navigate(`/atelier/runs/${result.value.public_run_reference}`);
    } catch (error) {
      startFailure = humanErrorMessage(error, workflowStartCopy.startUnavailable);
    } finally {
      starting = false;
    }
  }
</script>

<div class="sheet-positioner">
  <dialog
    bind:this={dialogElement}
    class="sheet"
    aria-labelledby="start-sheet-title"
    onkeydown={containDialogFocus}
    oncancel={handleDialogCancel}
  >
    <header>
      <h2 id="start-sheet-title">{workflowStartCopy.startTitle(workflowName)}</h2>
    </header>
    {#if loading}
      <p>{workflowStartCopy.preparing}</p>
    {:else if failure !== null}
      <p class="failure" role="alert">{failure}</p>
      <button type="button" onclick={() => { void load(); }}>{workflowStartCopy.retry}</button>
    {:else}
      {#snippet rawJsonField(order: OrderDraft)}
        <label>
          {workflowStartCopy.rawJsonFor(order.name)}
          <textarea
            value={order.rawJson}
            disabled={starting}
            oninput={(event) => setOrderRawJson(order.name, event.currentTarget.value)}
          ></textarea>
        </label>
      {/snippet}
      {#snippet rawJsonRefusal(order: OrderDraft)}
        {#if order.rawJson.trim().length > 0}
          {@const rawJsonVerdict = readRawOrderJson(order.rawJson, rawJsonWayOut(order))}
          {#if !rawJsonVerdict.ok}
            <p class="failure" role="alert">{rawJsonVerdict.reason}</p>
          {/if}
        {/if}
      {/snippet}
      {#each orders as order (order.name)}
        <fieldset aria-label={orderGroupLabel(order)}>
          {#if order.shape?.kind !== "work_item"}
            <legend>{order.name}</legend>
          {/if}
          {#if order.shape?.kind === "work_item"}
            <div class="work-item">
              <span>{workflowStartCopy.workItem}</span>
              {#if observedQueueItems.length === 0}
                <div class="degraded with-action">
                  <span>{workflowStartCopy.noSource}</span>
                  <button
                    type="button"
                    class="link"
                    onclick={() => navigate("/atelier/settings")}
                  >
                    {workflowStartCopy.connectSource}
                  </button>
                </div>
              {:else if offerableQueueItems.length === 0}
                <div class="degraded">
                  <span>{workflowStartCopy.allRetired}</span>
                </div>
              {:else}
                <button
                  type="button"
                  id={workItemComboboxId(order.name)}
                  class="picker-field"
                  role="combobox"
                  aria-haspopup="listbox"
                  aria-expanded={openWorkItemOrder === order.name}
                  aria-controls={`work-item-list-${order.name}`}
                  aria-label={workItemListName(order.name)}
                  disabled={starting}
                  onclick={() => toggleWorkItemPicker(order.name)}
                  onkeydown={(event) => handleWorkItemPickerKey(order.name, event)}
                >
                  <span>{selectedWorkItemLabel(order)}</span>
                  <span class="picker-caret" aria-hidden="true">{openWorkItemOrder === order.name ? "▴" : "▾"}</span>
                </button>
                {#if openWorkItemOrder === order.name}
                  <div class="picker-menu">
                    <input
                      type="search"
                      class="picker-filter"
                      role="combobox"
                      aria-expanded="true"
                      aria-controls={`work-item-list-${order.name}`}
                      aria-activedescendant={activeWorkItemId !== null
                        ? workItemOptionId(order.name, activeWorkItemId)
                        : undefined}
                      aria-autocomplete="list"
                      placeholder={workflowStartCopy.filterWorkItemsPlaceholder}
                      aria-label={workflowStartCopy.filterWorkItemsLabel}
                      value={workItemFilter}
                      bind:this={workItemFilterElement}
                      oninput={(event) => setWorkItemFilter(event.currentTarget.value)}
                      onkeydown={(event) => handleWorkItemFilterKey(order.name, event)}
                    />
                    <div
                      class="picker-options"
                      id={`work-item-list-${order.name}`}
                      role="listbox"
                      aria-label={workItemListName(order.name)}
                    >
                      {#if filteredItemsBySource.length === 0}
                        <div class="picker-none">{workflowStartCopy.noWorkItemMatch(workItemFilter)}</div>
                      {:else}
                        {#each filteredItemsBySource as [heading, items] (heading)}
                          <div class="picker-group">{heading}</div>
                          {#each items as item (item.item_id)}
                            <button
                              type="button"
                              class="picker-option"
                              class:selected={(order.values.work_item ?? "") === item.tracker_item_reference}
                              class:active={activeWorkItemId === item.item_id}
                              id={workItemOptionId(order.name, item.item_id)}
                              tabindex="-1"
                              role="option"
                              aria-selected={(order.values.work_item ?? "") === item.tracker_item_reference}
                              onmousedown={(event) => event.preventDefault()}
                              onclick={() => chooseWorkItem(order.name, item.tracker_item_reference)}
                            >
                              {workItemLabel(item)}
                            </button>
                          {/each}
                        {/each}
                      {/if}
                    </div>
                  </div>
                {/if}
              {/if}
            </div>
          {:else if order.shape?.kind === "string"}
            <label>
              {workflowStartCopy.orderText}
              <textarea
                value={order.stringValue}
                disabled={starting}
                oninput={(event) => setOrderStringValue(order.name, event.currentTarget.value)}
              ></textarea>
            </label>
            <label>
              {workflowStartCopy.publishFromFile(order.name)}
              <input
                type="file"
                disabled={starting}
                onchange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  if (file !== undefined) void setOrderStringFromFile(order.name, file);
                }}
              />
            </label>
            <span class="pill">
              {startOrderByteCount(orderByteLength(order.stringValue), MAXIMUM_ARTIFACT_BYTES)}
            </span>
          {:else if order.shape?.kind === "inline_object"}
            {#each order.resource?.summary.fields ?? [] as field (field.name)}
              <label>
                {field.name} ({typeLabel(field.types)}){field.required ? " *" : ""}
                {#if inputType(field) === "checkbox"}
                  <select
                    value={order.values[field.name] ?? ""}
                    disabled={starting}
                    onchange={(event) => setOrderValue(order.name, field.name, event.currentTarget.value)}
                  >
                    <option value="">{workflowStartCopy.choose}</option>
                    <option value="true">{workflowStartCopy.trueLabel}</option>
                    <option value="false">{workflowStartCopy.falseLabel}</option>
                  </select>
                {:else}
                  <input
                    type={inputType(field)}
                    value={order.values[field.name] ?? ""}
                    required={field.required}
                    disabled={starting}
                    oninput={(event) => setOrderValue(order.name, field.name, event.currentTarget.value)}
                  />
                {/if}
              </label>
            {/each}
            <details>
              <summary>{workflowStartCopy.rawJson}</summary>
              {@render rawJsonField(order)}
            </details>
            {@render rawJsonRefusal(order)}
          {:else if order.shape?.kind === "raw_object"}
            {@render rawJsonField(order)}
            {@render rawJsonRefusal(order)}
          {:else}
            <div class="degraded" role="status">
              <span>{order.shape?.reason ?? workflowStartCopy.orderUnavailable}</span>
            </div>
          {/if}
        </fieldset>
      {/each}
      {#if roles.length > 0}
        <fieldset class="role-configurations" aria-label={workflowStartCopy.roles}>
          <legend>{workflowStartCopy.roles}</legend>
          {#each roles as role (role)}
            {@const resolution = resolutions[role]}
            {@const resolvedHash = resolution?.agent_configuration_revision_hash ?? null}
            {@const resolvedConfiguration = registeredConfiguration(resolvedHash)}
            {@const roleUnavailable = resolvedHash !== null && !roleCanStart(role)}
            <div class="role-row">
              <label>
                {role}
                <span class="role-control">
                  <select
                    class:needs-choice={resolvedHash === null}
                    aria-invalid={resolvedHash === null}
                    aria-label={workflowStartCopy.configurationFor(role)}
                    disabled={starting || resolving}
                    onchange={(event) => { void chooseConfiguration(role, event.currentTarget.value); }}
                  >
                    <option value="" selected={resolvedHash === null}>{workflowStartCopy.choose}</option>
                    {#if resolvedHash !== null && resolvedConfiguration === undefined}
                      <option value={resolvedHash} selected disabled>
                        {resolvedConfigurationLabel(resolution!, undefined)}
                      </option>
                    {/if}
                    {#each registeredConfigurations as registered (registered.configuration.agent_configuration_revision_hash)}
                      <option
                        value={registered.configuration.agent_configuration_revision_hash}
                        selected={resolvedHash === registered.configuration.agent_configuration_revision_hash}
                        disabled={!registered.configuration.startable}
                      >{resolvedHash === registered.configuration.agent_configuration_revision_hash
                          ? resolvedConfigurationLabel(resolution!, registered)
                          : configurationLabel(registered)}</option>
                    {/each}
                  </select>
                </span>
              </label>
              {#if resolvedHash !== null && (resolution?.source === "chosen-now" || roleUnavailable)}
                <p class="role-source">
                  {#if roleUnavailable}
                    <span class="unavailable">◇ {startNotStartableReason(configurationByHash(resolvedHash)?.not_startable_reason ?? null)}</span>
                    {#if resolution?.source === "chosen-now"} · {/if}
                  {/if}
                  {#if resolution?.source === "chosen-now"}{workflowStartCopy.chosenNow}{/if}
                </p>
              {/if}
            </div>
          {/each}
        </fieldset>
      {/if}
      {#if startFailure !== null}
        <p class="failure" role="alert">{startFailure}</p>
      {/if}
    {/if}
    <footer>
      {#if !loading && failure === null}
        <button
          type="button"
          class="primary"
          disabled={!canStart}
          title={!canStart && !starting ? disabledStartReason ?? undefined : undefined}
          onclick={start}
        >
          {startFailure === null ? workflowStartCopy.startRun : workflowStartCopy.tryAgain}
        </button>
      {/if}
      <button bind:this={closeButton} type="button" class="quiet" disabled={starting} onclick={dismiss}>{workflowStartCopy.cancel}</button>
    </footer>
  </dialog>
</div>

<style>
  .sheet-positioner { position: fixed; inset: 0; z-index: 10; pointer-events: none; }
  .sheet { box-sizing: border-box; position: fixed; inset: var(--space-5) var(--space-5) auto auto; width: min(var(--dialog-width), calc(100% - (var(--space-5) * 2))); max-height: calc(100vh - (var(--space-5) * 2)); margin: 0; overflow-y: auto; padding: var(--space-5); background: var(--panel2); border: var(--edge) solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--shadow-lift); pointer-events: auto; }
  .sheet::backdrop { background: color-mix(in srgb, var(--ground) 80%, transparent); }
  header, footer { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  h2 { font-family: var(--serif); }
  fieldset, label { display: grid; gap: var(--space-1); margin: var(--space-4) 0; }
  input, select, textarea { min-height: var(--tap); font: inherit; }
  textarea { resize: vertical; min-height: calc(var(--tap) * 2); font-family: inherit; }
  .pill { display: inline-block; justify-self: start; white-space: nowrap; border: var(--edge) solid var(--line); border-radius: var(--r-pill); padding: 0 var(--space-2); color: var(--ink-dim); background: var(--chip); font-size: var(--text-2xs); line-height: 1.65; }
  .failure { color: var(--signal-failure); }
  .degraded { display: flex; align-items: center; gap: var(--space-2); border: 1px dashed var(--signal-attention); padding: var(--space-2); color: var(--signal-attention); }
  .degraded.with-action { justify-content: space-between; }
  .link { min-height: var(--tap); border: 0; background: transparent; color: var(--ink); font: inherit; font-weight: var(--weight-strong); text-decoration: underline; }
  .work-item { display: grid; gap: var(--space-1); }
  .picker-field { display: flex; width: 100%; justify-content: space-between; gap: var(--space-2); background: var(--panel2); border-color: var(--line); font-weight: var(--weight-medium); text-align: left; }
  .picker-caret { color: var(--ink-dim); }
  .picker-menu { border: var(--edge) solid var(--line); border-radius: var(--r); background: var(--panel2); padding: var(--space-1) 0; }
  .picker-filter { box-sizing: border-box; display: block; width: 100%; min-height: var(--tap); margin: 0 0 var(--space-1); border: var(--edge) solid var(--line); border-radius: var(--r); padding: var(--space-2) var(--space-3); color: var(--ink); background: var(--panel2); font-size: var(--text-xs); }
  .picker-group { padding: var(--space-1) var(--space-3); color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .picker-none { padding: var(--space-2) var(--space-3) var(--space-2) var(--space-5); color: var(--ink-dim); }
  .picker-option { display: flex; width: 100%; justify-content: flex-start; border: 0; border-radius: 0; background: transparent; font-weight: var(--weight-medium); padding: var(--space-2) var(--space-3) var(--space-2) var(--space-5); text-align: left; }
  .picker-option.selected { background: var(--chip); }
  .picker-option.active { outline: var(--edge) solid var(--ink); outline-offset: calc(-1 * var(--edge)); }
  .role-configurations { border-color: var(--ink); margin: var(--space-5) 0; padding: var(--space-3); }
  .role-configurations legend { color: var(--ink); font-weight: var(--weight-strong); padding: 0 var(--space-1); }
  .role-row { margin: var(--space-4) 0; }
  .role-row label { margin: 0; }
  .role-control { display: grid; min-width: 0; }
  .needs-choice { border-color: var(--signal-attention); color: var(--signal-attention); }
  .role-source { color: var(--ink); font-size: var(--text-2xs); font-weight: var(--weight-strong); margin: var(--space-1) 0 0; }
  .unavailable { color: var(--ink-dim); }
  /*
   * A `showModal()`-opened dialog stays capped by Chromium's own UA rule
   * (`max-width: calc((100% - 6px) - 2em)`) unless a rule here overrides
   * `max-width` too; `width: 100%` alone cannot beat a narrower
   * `max-width`.
   */
  @media (max-width: 480px) { .sheet { inset: auto 0 0 0; width: 100%; max-width: 100%; height: auto; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
