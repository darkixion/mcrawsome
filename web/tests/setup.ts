import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { forgetClipDetails } from "../src/lib/clip-details";
import { openSections } from "../src/lib/sections";

// Each test stubs its own engine answers: one test's shared clip details must not answer the next.
afterEach(() => forgetClipDetails());
// Folded sections start from their defaults in every test.
afterEach(() => openSections.set({}));
