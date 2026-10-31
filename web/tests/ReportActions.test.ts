import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import ReportActions from "../src/components/ReportActions.svelte";

afterEach(()=>{cleanup();vi.unstubAllGlobals();});
it("copies exact JSON and saves through the native file dialog",async()=>{
 const value={coefficients:[.123456789012345,2],timestamp_ns:"8000000000000001",device:"雪"};
 const writeText=vi.fn(async(_text: string)=>{}),save_report=vi.fn(async(_content: string, _name: string)=>true);
 vi.stubGlobal("navigator",{clipboard:{writeText}});vi.stubGlobal("pywebview",{api:{save_report}});
 render(ReportActions,{value,filename:"calibration.json",label:"calibration"});
 await fireEvent.click(screen.getByRole("button",{name:"Copy calibration JSON"}));
 expect(JSON.parse(writeText.mock.calls[0]![0]!)).toEqual(value);
 await fireEvent.click(screen.getByRole("button",{name:"Save calibration JSON"}));
 expect(save_report).toHaveBeenCalledWith(JSON.stringify(value,null,2)+"\n","calibration.json");
 expect(await screen.findByText("JSON saved")).toBeInTheDocument();
});
it("provides selectable JSON when clipboard access is unavailable",async()=>{
 vi.stubGlobal("navigator",{});
 render(ReportActions,{value:{exact:123},filename:"data.json",label:"measurement"});
 await fireEvent.click(screen.getByRole("button",{name:"Copy measurement JSON"}));
 const text=await screen.findByRole("textbox",{name:"measurement JSON"});
 expect(text).toHaveValue('{\n  "exact": 123\n}\n');
 expect(text).toHaveFocus();
});

it("saves clip details with a .meta filename and metadata label", async () => {
 const save_report = vi.fn(async () => true);
 vi.stubGlobal("pywebview", { api: { save_report } });
 render(ReportActions, { value: { details: { notes: "Keep" } }, filename: "clip.meta", label: "clip details" });
 await fireEvent.click(screen.getByRole("button", { name: "Save clip details metadata" }));
 expect(save_report).toHaveBeenCalledWith(expect.any(String), "clip.meta");
});
