<script lang="ts">
  import { onMount, tick } from "svelte";

  import { createCockpitApi, type CockpitApi, type HealthResource } from "./api/client";
  import { onConnectionRecovered, watchConnectionRecovery } from "./lib/connectionState";
  import { MutationJournal, createRunId as makeRunId } from "./lib/mutationJournal";
  import { PRODUCT_NAME } from "./lib/productName";
  import { cockpitRoute } from "./lib/route";
  import { noteObservedVersion, recordLoadedVersion } from "./lib/versionState";
  import { WORKSHOP_DESTINATION } from "./lib/workshop";
  import ConnectionNotice from "./components/ConnectionNotice.svelte";
  import WorkshopShell from "./components/WorkshopShell.svelte";
  import WorkbenchPage from "./pages/WorkbenchPage.svelte";
  import RunCockpitPage from "./pages/RunCockpitPage.svelte";
  import SettingsPage from "./pages/SettingsPage.svelte";
  import WorkflowDetailPage from "./pages/WorkflowDetailPage.svelte";
  import CatalogPage from "./pages/CatalogPage.svelte";
  import HistoryPage from "./pages/HistoryPage.svelte";

  export let cockpitApi: CockpitApi = createCockpitApi();
  export let mutationJournal: MutationJournal = new MutationJournal(sessionStorage);
  export let createRunId: () => string = makeRunId;

  let route = cockpitRoute(window.location.pathname + window.location.search);
  let workshopShell: WorkshopShell;
  let inAppFromPath: string | null = null;

  function servedVersion(health: HealthResource) {
    return { commit: health.source_commit, deployedAt: health.serve_started_at };
  }

  onMount(() => {
    const readRoute = () => {
      // Origin is for navigate() clicks only; a history-stack run uses its own state.
      inAppFromPath = null;
      route = cockpitRoute(window.location.pathname + window.location.search);
    };
    window.addEventListener("popstate", readRoute);
    // The footer's baseline (#1100): a failed read leaves it quiet rather
    // than breaking the page over decorative provenance.
    void cockpitApi.health().then((health) => recordLoadedVersion(servedVersion(health)), () => {});
    // The bounded recovery probe for whichever page holds no open stream of
    // its own (#700) -- one loop for the whole app, torn down with it.
    const stopWatchingRecovery = watchConnectionRecovery((signal) => cockpitApi.health(signal));
    // A redeploy the operator lived through (#1100): read health once on the
    // same edge every other room already reloads its own data on, and let
    // the footer say so instead of reloading on its own.
    const stopWatchingVersion = onConnectionRecovered(() => {
      void cockpitApi.health().then((health) => noteObservedVersion(servedVersion(health)), () => {});
    });
    return () => {
      window.removeEventListener("popstate", readRoute);
      stopWatchingRecovery();
      stopWatchingVersion();
    };
  });

  async function navigate(path: string): Promise<void> {
    inAppFromPath = window.location.pathname;
    window.history.pushState(null, "", path);
    route = cockpitRoute(path);
    await tick();
    workshopShell?.focusStage();
  }
</script>

<svelte:head><meta name="theme-color" content="#f2efe7" /><title>{PRODUCT_NAME}</title></svelte:head>

<!-- One line above every room, the Workbench included: the room that used to
     speak its own connection state did so through the ear it no longer has
     (#1099), and a terminal says nothing about the workshop's reachability. -->
<ConnectionNotice />
<WorkshopShell bind:this={workshopShell} {route} {navigate}>
  {#if route.page === "workbench"}
    <WorkbenchPage {cockpitApi} {mutationJournal} {navigate} />
  {:else if route.page === "settings"}
    <SettingsPage {cockpitApi} />
  {:else if route.page === "catalog"}
    <CatalogPage {cockpitApi} {navigate} />
  {:else if route.page === "workflow"}
    <WorkflowDetailPage {cockpitApi} {mutationJournal} {navigate} {createRunId} name={route.name} />
  {:else if route.page === "history"}
    <HistoryPage {cockpitApi} {navigate} />
  {:else if route.page === "run"}
    {#key route.publicReference}
    <RunCockpitPage
      {cockpitApi}
      {mutationJournal}
      publicReference={route.publicReference}
      {navigate}
      {inAppFromPath}
    />
    {/key}
  {:else}
    <section class="surface">
      <header class="surface-head">
        <h1>Page not found</h1>
        <p>No page lives at this address. The Workbench holds what is running.</p>
      </header>
      <a
        class="button primary"
        href={WORKSHOP_DESTINATION.workbench.path}
        onclick={(event) => { event.preventDefault(); navigate(WORKSHOP_DESTINATION.workbench.path); }}
      >{WORKSHOP_DESTINATION.workbench.label}</a>
    </section>
  {/if}
</WorkshopShell>
