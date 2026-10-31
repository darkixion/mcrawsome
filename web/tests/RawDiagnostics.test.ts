import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import RawDiagnostics from "../src/components/RawDiagnostics.svelte";

const histogram = { frame_index: 0, timestamp_ns: "8000000000000001", width: 128, height: 64, cfa: "rggb", bin_edges: Array.from({length:257},(_,i)=>i*4), planes: ["R","G (R row)","G (B row)","B"].map((name,site)=>({ name, site, black_level:64, white_level:1023, samples:2048, minimum:0, maximum:1023, at_or_below_black:4, at_or_above_white:2, bins:Array.from({length:256},(_,i)=>i===0?2046:i===255?2:0) })) };
const timing={ frames:3,intervals:2,median_ms:41.666667,minimum_ms:41.666667,maximum_ms:41.666667,dropped:0,duplicated:0,nonpositive:0,buckets:[{first_frame:1,last_frame:1,intervals:1,minimum_ms:41.666667,maximum_ms:41.666667,mean_ms:41.666667},{first_frame:2,last_frame:2,intervals:1,minimum_ms:41.666667,maximum_ms:41.666667,mean_ms:41.666667}] };
afterEach(()=>{cleanup();vi.unstubAllGlobals();});

it("only measures on request and renders recorded RAW/timing results",async()=>{
 vi.stubGlobal("fetch",vi.fn(async(url)=>Response.json(String(url).endsWith("/timing")?timing:histogram)));
 render(RawDiagnostics,{id:"a",name:"a.mcraw",playing:false});
 expect(fetch).not.toHaveBeenCalled();
 const foldout=screen.getByText("RAW diagnostics").closest("details")!;foldout.open=true;await fireEvent(foldout,new Event("toggle"));
 expect(fetch).not.toHaveBeenCalled();
 await fireEvent.click(screen.getByRole("button",{name:"Read frame timing"}));
 expect(await screen.findByRole("img",{name:"Recorded frame intervals"})).toBeInTheDocument();
 expect(screen.getByText(/256.*groups|every recorded interval/i)).toBeInTheDocument();
 await fireEvent.click(screen.getByRole("button",{name:"Measure first RAW frame"}));
 expect(await screen.findByRole("img",{name:"First-frame RAW histogram"})).toBeInTheDocument();
 expect(screen.getByText(/before.*white balance/i)).toBeInTheDocument();
 expect(fetch).toHaveBeenCalledTimes(2);
 expect(screen.getByRole("button",{name:"Save RAW histogram JSON"})).toBeInTheDocument();
});

it("aborts a pending measurement when playback starts and discards late replies",async()=>{
 let finish:(r:Response)=>void=()=>{};let signal:AbortSignal|undefined;
 vi.stubGlobal("fetch",vi.fn((_url,init)=>{signal=init.signal;return new Promise(resolve=>{finish=resolve;});}));
 const view=render(RawDiagnostics,{id:"a",name:"a.mcraw",playing:false});
 const foldout=screen.getByText("RAW diagnostics").closest("details")!;foldout.open=true;await fireEvent(foldout,new Event("toggle"));
 await fireEvent.click(screen.getByRole("button",{name:"Measure first RAW frame"}));
 await view.rerender({id:"a",name:"a.mcraw",playing:true});
 await waitFor(()=>expect(signal?.aborted).toBe(true));
 expect(screen.getByRole("button",{name:"Measure first RAW frame"})).toBeDisabled();
 finish(Response.json(histogram));
 await new Promise(resolve=>setTimeout(resolve,0));
 expect(screen.queryByRole("img",{name:"First-frame RAW histogram"})).toBeNull();
 expect(screen.getByText(/Pause playback/)).toBeInTheDocument();
});
