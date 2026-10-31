import { mount } from "svelte";
import App from "./App.svelte";
import { PRODUCT } from "./lib/brand";
import "./lib/theme.css";

document.title = PRODUCT;
const target = document.getElementById("app");
if (target === null) throw new Error("the page has no #app element");
export default mount(App, { target });
