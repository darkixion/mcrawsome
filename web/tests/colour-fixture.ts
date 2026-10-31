import type { Schemas } from "../src/lib/api";

export const colourChoices: Schemas["ColourChoices"] = {
  gamuts: [
    { id: "ap1", name: "ACES AP1" }, { id: "awg3", name: "ARRI Wide Gamut 3" },
    { id: "davinci-wg", name: "DaVinci Wide Gamut" }, { id: "camera-native", name: "Camera Native" },
  ],
  transfers: [
    { id: "acescct", name: "ACEScct" }, { id: "logc3", name: "ARRI LogC3" },
    { id: "davinci-intermediate", name: "DaVinci Intermediate" }, { id: "hlg", name: "HLG" },
  ],
  log_transfers: ["acescct", "logc3", "davinci-intermediate"],
  logc3_ei: [400, 800, 1600],
  legacy: { acescct: { gamut: "ap1", transfer: "acescct" }, davinci: { gamut: "davinci-wg", transfer: "davinci-intermediate" } },
};
