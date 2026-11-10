// Talks to the local server (app/server.py).

/** GET (no body) or POST JSON. A server error becomes an exception with the server's message. */
export async function api(url, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(url, options);
  const type = r.headers.get("Content-Type") || "";
  const data = type.includes("json") ? await r.json() : null;
  if (!r.ok) throw new Error((data && data.error) || `error ${r.status}`);
  return data;
}

/** "?v=…&pct=…" without empty values. */
export function query(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") q.set(k, v);
  return "?" + q.toString();
}

/** Copies the file to input/. Uses XMLHttpRequest because fetch does not report upload progress. */
export function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total * 100); };
    xhr.onload = () => {
      let r = {};
      try { r = JSON.parse(xhr.responseText); } catch (_) { /* response without JSON: use the status */ }
      if (xhr.status === 200) resolve(r); else reject(new Error(r.error || `error ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("failed to copy the file"));
    xhr.send(file);
  });
}
