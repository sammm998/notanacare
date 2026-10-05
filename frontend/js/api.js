import { $ } from "./util.js";

export async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText;
    throw new Error(detail);
  }
  return data;
}

/** Poll a background job until it finishes; shows progress in the header badge. */
export async function runJob(submitPromise, label) {
  const badge = $("#b-job");
  const text = $("#b-job-text");
  badge.classList.remove("hidden");
  text.textContent = label;
  try {
    const { job_id } = await submitPromise;
    for (;;) {
      await new Promise((r) => setTimeout(r, 900));
      const job = await api("GET", `/api/jobs/${job_id}`);
      const last = job.progress && job.progress.length ? ` · ${job.progress[job.progress.length - 1]}` : "";
      text.textContent = `${label} ${job.elapsed_s ? `(${job.elapsed_s}s)` : ""}${last}`.slice(0, 120);
      if (job.status === "done") return job.result;
      if (job.status === "error") throw new Error(job.error);
    }
  } finally {
    badge.classList.add("hidden");
  }
}
