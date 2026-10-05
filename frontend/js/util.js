export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function hhmm(m) {
  if (m === null || m === undefined) return "--:--";
  const v = Math.round(m);
  return `${String(Math.floor(v / 60)).padStart(2, "0")}:${String(v % 60).padStart(2, "0")}`;
}

export function parseHHMM(s) {
  const [h, m] = String(s).split(":").map(Number);
  return h * 60 + (m || 0);
}

let toastTimer;
export function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

export function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return Number(v).toLocaleString("sv-SE", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function pct(v) {
  return v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`;
}
