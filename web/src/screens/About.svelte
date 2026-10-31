<script lang="ts">
  import { onMount } from "svelte";
  import SystemCheck from "../components/SystemCheck.svelte";
  import SupportDiagnostics from "../components/SupportDiagnostics.svelte";
  import ErrorNote from "../components/ErrorNote.svelte";
  import { api, type ApiError, type Schemas } from "../lib/api";
  import { LOGO, PRODUCT } from "../lib/brand";

  let about = $state<Schemas["About"] | null>(null);
  let problem = $state<ApiError | null>(null);
  // Licence texts come one at a time, when opened: all of them together are several megabytes.
  let texts = $state<Record<string, string | ApiError>>({});

  async function load(file: string, open: boolean): Promise<void> {
    if (!open || file in texts) return;
    texts[file] = "Loading...";
    const result = await api.licence(file);
    texts[file] = result.ok ? result.data.text : result;
  }
  onMount(async () => {
    const result = await api.about();
    if (result.ok) about = result.data;
    else problem = result;
  });
</script>

<section>
  <img class="logo" src={LOGO} alt="" />
  <h1>About {PRODUCT}</h1>
  <p class="independent">
    {PRODUCT} is an independent open-source project for MCRAW recordings. It is not affiliated with or
    endorsed by MotionCam.
  </p>
  <SupportDiagnostics />
  {#if problem}<ErrorNote error={problem} />{/if}
  {#if about}
    <p>Version <span>{about.version}</span></p>
    <SystemCheck checks={about.checks} />
    <h2>Components and licences</h2>
    <p>
      {PRODUCT} uses your system's Python, FFmpeg, WebKitGTK and other libraries; their licences
      are in <code>/usr/share/doc</code>. These are the licences of the parts {PRODUCT} ships itself.
    </p>
    {#each about.licences as file (file)}
      <details ontoggle={(event) => load(file, event.currentTarget.open)}>
        <summary>{file}</summary>
        {#if typeof texts[file] === "string"}
          <pre>{texts[file]}</pre>
        {:else if texts[file]}
          <ErrorNote error={texts[file]} />
        {/if}
      </details>
    {/each}
  {:else if !problem}
    <p>Loading the engine report...</p>
  {/if}
</section>

<style>
  section { padding: 24px; max-width: 960px; }
  .logo { display: block; height: 56px; margin-bottom: 16px; }
  h1 { font-size: 20px; margin: 0 0 8px; }
  .independent { color: var(--muted); margin: 0 0 16px; }
  h2 { font-size: 16px; margin-top: 28px; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
  details { border-top: 1px solid var(--line); padding: 12px 0; }
  summary { cursor: pointer; }
</style>
