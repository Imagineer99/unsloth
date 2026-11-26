// Observe the same SSE bytes the UI reads. Set identical generation parameters on both sides.
(() => {
  const observed = window.pr19738 = {text:"", finish:"", frames:[], prefillBeforeText:false, decode:false};
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const path = new URL(typeof input === "string" ? input : input.url || input, location.href).pathname;
    if (init?.body && /\/(?:chat-runs|chat\/completions)$/.test(path)) {
      const req = JSON.parse(init.body);
      const params = req.requestPayload || req.request_payload || req;
      Object.assign(params, {temperature:0, max_tokens:96, seed:47, enable_tools:false});
      init = {...init, body:JSON.stringify(req)};
    }
    const response = await nativeFetch(input, init);
    if (!/chat-runs|chat\/completions/.test(path) || !response.headers.get("content-type")?.includes("event-stream")) return response;
    const decoder = new TextDecoder();
    let buffer = "";
    function record(line) {
      if (!line.startsWith("data:")) return;
      const raw = line.slice(5).trim();
      if (!raw || raw==="[DONE]") return;
      const frame = JSON.parse(raw);
      const chunk = frame.payload || frame.event?.payload || frame;
      if (chunk.type === "generation_progress") {
        observed.frames.push(chunk);
        if (chunk.phase==="prefill" && chunk.processed > (chunk.cached || 0) && chunk.processed < chunk.total && !observed.text) observed.prefillBeforeText=true;
        if (chunk.phase==="decode" && chunk.tokens>0 && chunk.tok_per_s>0) observed.decode=true;
      }
      for (const c of chunk.choices || []) {
        observed.text += (c.delta?.reasoning_content || "")+(c.delta?.content || "");
        if (c.finish_reason) observed.finish=c.finish_reason;
      }
    }
    const stream = response.body.pipeThrough(new TransformStream({
      transform(bytes, ctl) {
        buffer += decoder.decode(bytes, {stream:true});
        const lines=buffer.split(/\r?\n/);
        buffer=lines.pop();
        for (const line of lines) record(line);
        ctl.enqueue(bytes);
      },
      flush() {record(buffer+decoder.decode());}
    }));
    return new Response(stream,{headers:response.headers,status:response.status,statusText:response.statusText});
  };
})();
