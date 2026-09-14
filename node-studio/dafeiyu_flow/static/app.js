const $ = (selector) => document.querySelector(selector);
const SVG_NS = "http://www.w3.org/2000/svg";
const TERMINAL_RUN_STATES = new Set(["success", "succeeded", "completed", "failed", "error", "cancelled", "canceled"]);

let graph = null;
let descriptors = [];
let selectedId = null;
let connecting = null;
let graphLoadGeneration = 0;
let runGeneration = 0;
let currentRunId = null;
let nodeSerial = 1;

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function setStatus(message) {
  $("#status").textContent = message;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  const text = await response.text();
  let payload = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { error: text };
    }
  }
  if (!response.ok) {
    const error = new Error(payload.error || payload.message || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function descriptorFor(type) {
  return descriptors.find((item) => item.type === type);
}

function nodeFor(id) {
  return graph?.nodes.find((node) => node.id === id);
}

function graphNodeElement(id) {
  return [...document.querySelectorAll(".node")].find((element) => element.dataset.id === id);
}

function cloneDefault(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

function defaultConfig(descriptor) {
  const config = {};
  for (const [name, schema] of Object.entries(descriptor.config || {})) {
    if (Object.prototype.hasOwnProperty.call(schema, "default")) {
      config[name] = cloneDefault(schema.default);
    } else if (schema.kind === "boolean") {
      config[name] = false;
    } else if (schema.kind === "array") {
      config[name] = [];
    } else if (schema.kind === "object") {
      config[name] = {};
    } else if (schema.kind === "integer" || schema.kind === "number") {
      config[name] = 0;
    } else {
      config[name] = "";
    }
  }
  return config;
}

function makeLocalId(type) {
  const stem = type.replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "node";
  let id;
  do {
    id = `${stem}_local_${nodeSerial++}`;
  } while (nodeFor(id));
  return id;
}

function addNode(descriptor) {
  const viewport = $("#viewport");
  const offset = graph.nodes.length % 8;
  const node = {
    id: makeLocalId(descriptor.type),
    type: descriptor.type,
    position: {
      x: Math.max(30, viewport.scrollLeft + 70 + offset * 22),
      y: Math.max(30, viewport.scrollTop + 50 + offset * 22),
    },
    config: defaultConfig(descriptor),
  };
  graph.nodes.push(node);
  selectedId = node.id;
  connecting = null;
  render();
  inspectSelected();
  setStatus(`已添加：${descriptor.title}`);
}

function portCenter(nodeId, portName, side) {
  const node = nodeFor(nodeId);
  const descriptor = node && descriptorFor(node.type);
  const ports = side === "out" ? descriptor?.outputs || [] : descriptor?.inputs || [];
  const index = Math.max(0, ports.findIndex((port) => port.name === portName));
  return {
    x: node.position.x + (side === "out" ? 190 : 0),
    y: node.position.y + 55 + index * 21,
  };
}

function edgePath(from, to) {
  const bend = Math.max(55, Math.abs(to.x - from.x) * 0.45);
  return `M${from.x},${from.y} C${from.x + bend},${from.y} ${to.x - bend},${to.y} ${to.x},${to.y}`;
}

function drawEdges() {
  const svg = $("#edges");
  svg.textContent = "";
  for (const edge of graph.edges) {
    if (!nodeFor(edge.from.node) || !nodeFor(edge.to.node)) continue;
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("class", "edge");
    path.setAttribute("d", edgePath(
      portCenter(edge.from.node, edge.from.port, "out"),
      portCenter(edge.to.node, edge.to.port, "in"),
    ));
    svg.append(path);
  }
}

function portType(nodeId, portName, side) {
  const node = nodeFor(nodeId);
  const descriptor = node && descriptorFor(node.type);
  const ports = side === "out" ? descriptor?.outputs : descriptor?.inputs;
  return ports?.find((port) => port.name === portName);
}

function portsCompatible(output, input) {
  return Boolean(output && input && output.dataType === input.dataType);
}

function beginConnection(nodeId, port) {
  connecting = { nodeId, port: port.name, dataType: port.dataType };
  render();
  inspectSelected();
  setStatus(`连线中：请选择 ${port.dataType} 类型的输入端口`);
}

function uniqueEdgeId() {
  const existing = new Set(graph.edges.map((edge) => edge.id));
  let id;
  do {
    id = `edge-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  } while (existing.has(id));
  return id;
}

function finishConnection(targetNodeId, input) {
  if (!connecting) return;
  const output = portType(connecting.nodeId, connecting.port, "out");
  if (!portsCompatible(output, input)) {
    setStatus(`类型不兼容：${output?.dataType || "未知"} → ${input.dataType}`);
    return;
  }
  if (connecting.nodeId === targetNodeId) {
    setStatus("不能把节点连接到自身");
    return;
  }
  const duplicate = graph.edges.some((edge) =>
    edge.from.node === connecting.nodeId && edge.from.port === connecting.port &&
    edge.to.node === targetNodeId && edge.to.port === input.name
  );
  if (!duplicate) {
    if (!input.many) {
      graph.edges = graph.edges.filter((edge) => !(edge.to.node === targetNodeId && edge.to.port === input.name));
    }
    graph.edges.push({
      id: uniqueEdgeId(),
      from: { node: connecting.nodeId, port: connecting.port },
      to: { node: targetNodeId, port: input.name },
    });
  }
  connecting = null;
  render();
  inspectSelected();
  setStatus(duplicate ? "该连线已存在" : "连线已创建");
}

function createPort(node, port, side) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `port ${side}`;
  element.title = `${port.name} · ${port.dataType}`;
  const dot = document.createElement("i");
  dot.className = "dot";
  if (side === "in") {
    element.append(dot, document.createTextNode(port.name));
  } else {
    element.append(document.createTextNode(port.name), dot);
  }
  if (connecting) {
    if (side === "out" && connecting.nodeId === node.id && connecting.port === port.name) {
      element.classList.add("connecting");
    } else if (side === "in") {
      element.classList.add(connecting.dataType === port.dataType ? "compatible" : "incompatible");
    }
  }
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    selectedId = node.id;
    if (side === "out") beginConnection(node.id, port);
    else if (connecting) finishConnection(node.id, port);
    else {
      render();
      inspectSelected();
      setStatus("请先点击一个输出端口");
    }
  });
  return element;
}

function enableDrag(handle, element, node) {
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startY = event.clientY;
    const originX = node.position.x;
    const originY = node.position.y;
    const move = (next) => {
      node.position.x = Math.max(0, originX + next.clientX - startX);
      node.position.y = Math.max(0, originY + next.clientY - startY);
      element.style.left = `${node.position.x}px`;
      element.style.top = `${node.position.y}px`;
      drawEdges();
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
}

function render() {
  const layer = $("#nodes");
  layer.textContent = "";
  for (const node of graph.nodes) {
    const descriptor = descriptorFor(node.type);
    const element = document.createElement("div");
    element.className = `node${selectedId === node.id ? " selected" : ""}`;
    element.dataset.id = node.id;
    element.style.left = `${node.position.x}px`;
    element.style.top = `${node.position.y}px`;

    const title = document.createElement("div");
    title.className = "node-title";
    title.append(document.createTextNode(descriptor?.title || node.type));
    const id = document.createElement("span");
    id.className = "node-id";
    id.textContent = node.id;
    title.append(id);

    const ports = document.createElement("div");
    ports.className = "ports";
    const inputs = document.createElement("div");
    const outputs = document.createElement("div");
    for (const port of descriptor?.inputs || []) inputs.append(createPort(node, port, "in"));
    for (const port of descriptor?.outputs || []) outputs.append(createPort(node, port, "out"));
    ports.append(inputs, outputs);
    element.append(title, ports);
    element.addEventListener("click", () => {
      selectedId = node.id;
      render();
      inspectSelected();
    });
    enableDrag(title, element, node);
    layer.append(element);
  }
  drawEdges();
  $("#delete-node").disabled = !selectedId;
}

function fieldInput(name, schema, value, onValue) {
  const label = document.createElement("label");
  label.className = "field";
  const caption = document.createElement("span");
  caption.textContent = schema.label || name;
  label.append(caption);

  let input;
  if (schema.kind === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    label.insertBefore(input, caption);
    input.addEventListener("change", () => onValue(input.checked));
  } else if (schema.kind === "array" || schema.kind === "object") {
    input = document.createElement("textarea");
    input.value = pretty(value ?? (schema.kind === "array" ? [] : {}));
    input.spellcheck = false;
    input.addEventListener("change", () => {
      try {
        const parsed = JSON.parse(input.value);
        const validShape = schema.kind === "array" ? Array.isArray(parsed) : parsed && typeof parsed === "object" && !Array.isArray(parsed);
        if (!validShape) throw new Error(`需要 ${schema.kind === "array" ? "JSON 数组" : "JSON 对象"}`);
        input.setCustomValidity("");
        onValue(parsed);
        setStatus(`已更新配置：${schema.label || name}`);
      } catch (error) {
        input.setCustomValidity(error.message);
        input.reportValidity();
      }
    });
  } else {
    input = document.createElement("input");
    input.type = schema.kind === "integer" || schema.kind === "number" ? "number" : "text";
    input.value = value ?? "";
    if (schema.kind === "integer") input.step = "1";
    if (schema.min !== undefined) input.min = String(schema.min);
    if (schema.max !== undefined) input.max = String(schema.max);
    if (schema.maxLength !== undefined) input.maxLength = schema.maxLength;
    input.addEventListener("input", () => {
      if (!input.checkValidity()) return;
      const nextValue = input.type === "number" ? Number(input.value) : input.value;
      onValue(nextValue);
    });
  }
  label.append(input);
  if (schema.kind === "array" || schema.kind === "object") {
    const help = document.createElement("span");
    help.className = "field-help";
    help.textContent = schema.kind === "array" ? "请输入有效 JSON 数组" : "请输入有效 JSON 对象";
    label.append(help);
  }
  return label;
}

function inspectSelected() {
  const box = $("#inspector");
  box.textContent = "";
  const node = nodeFor(selectedId);
  if (!node) {
    box.className = "hint";
    box.textContent = "点击节点查看并编辑配置。";
    $("#delete-node").disabled = true;
    return;
  }
  box.className = "";
  const descriptor = descriptorFor(node.type);
  const meta = document.createElement("ul");
  meta.className = "meta-list";
  for (const value of [`ID：${node.id}`, `类型：${node.type}`]) {
    const item = document.createElement("li");
    item.textContent = value;
    meta.append(item);
  }
  box.append(meta);
  if (connecting) {
    const hint = document.createElement("div");
    hint.className = "connect-hint";
    hint.textContent = `等待输入端口：${connecting.dataType}（点击空白或按 Esc 取消）`;
    box.append(hint);
  }
  const schemaEntries = Object.entries(descriptor?.config || {});
  if (!schemaEntries.length) {
    const empty = document.createElement("div");
    empty.className = "empty-config hint";
    empty.textContent = "此节点没有可编辑配置。";
    box.append(empty);
  }
  node.config ||= {};
  for (const [name, schema] of schemaEntries) {
    box.append(fieldInput(name, schema, node.config[name], (value) => {
      node.config[name] = value;
    }));
  }
  $("#delete-node").disabled = false;
}

function deleteSelected() {
  if (!selectedId || !nodeFor(selectedId)) return;
  const deletedId = selectedId;
  graph.nodes = graph.nodes.filter((node) => node.id !== deletedId);
  graph.edges = graph.edges.filter((edge) => edge.from.node !== deletedId && edge.to.node !== deletedId);
  if (connecting?.nodeId === deletedId) connecting = null;
  selectedId = null;
  render();
  inspectSelected();
  setStatus(`已删除节点及关联连线：${deletedId}`);
}

function markTrace(trace = []) {
  document.querySelectorAll(".node").forEach((node) => node.classList.remove("success", "error"));
  const list = $("#trace");
  list.textContent = "";
  for (const item of trace) {
    const row = document.createElement("li");
    const status = String(item.status || "");
    row.className = `trace-item ${status === "success" ? "success" : status === "error" ? "error" : ""}`;
    const duration = Number(item.duration_ms ?? item.durationMs);
    const durationText = Number.isFinite(duration) ? ` · ${duration.toFixed(2)}ms` : "";
    row.textContent = `${item.sequence ?? "-"}. ${item.node_id ?? item.nodeId ?? "未知节点"} · ${status || "未知"}${durationText}`;
    row.addEventListener("click", () => {
      $("#detail").textContent = pretty(item);
      selectedId = item.node_id ?? item.nodeId ?? null;
      render();
      inspectSelected();
      graphNodeElement(selectedId)?.classList.add(status === "success" ? "success" : "error");
    });
    list.append(row);
    const nodeId = item.node_id ?? item.nodeId;
    if (nodeId) graphNodeElement(nodeId)?.classList.add(status === "success" ? "success" : "error");
  }
}

function runIdFrom(payload) {
  return payload.id ?? payload.runId ?? payload.run_id ?? payload.run?.id ?? null;
}

function runStatusFrom(payload) {
  return String(payload.status ?? payload.run?.status ?? "").toLowerCase();
}

function showRun(payload) {
  const run = payload.run || payload;
  const result = run.result ?? run.output ?? payload.result;
  const trace = run.trace ?? payload.trace ?? [];
  if (result !== undefined) $("#result").textContent = pretty(result);
  markTrace(Array.isArray(trace) ? trace : []);
  $("#detail").textContent = pretty(run);
  const status = runStatusFrom(payload);
  setStatus(status === "success" || status === "succeeded" || status === "completed" ? "运行完成" : `运行结束：${status || "未知状态"}`);
}

function csrfTokenFrom(session) {
  return session.csrfToken ?? session.csrf_token ?? session.csrf ?? session.session?.csrfToken ?? session.session?.csrf_token;
}

async function mutationApi(path, body) {
  const session = await api("/api/session");
  const token = csrfTokenFrom(session);
  if (!token) throw new Error("会话响应中缺少 CSRF token");
  return api(path, {
    method: "POST",
    headers: { "X-Node-Studio-CSRF": token },
    body: JSON.stringify(body),
  });
}

async function createRun(graphSnapshot) {
  const created = await mutationApi("/api/runs", { graph: graphSnapshot });
  const runId = runIdFrom(created);
  if (!runId) throw new Error("创建运行成功，但响应中缺少运行 ID");
  return runId;
}

function wait(delay) {
  return new Promise((resolve) => window.setTimeout(resolve, delay));
}

async function pollRun(runId) {
  let payload = await api(`/api/runs/${encodeURIComponent(runId)}`);
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const status = runStatusFrom(payload);
    if (TERMINAL_RUN_STATES.has(status)) return payload;
    setStatus(`运行 ${runId}：${status || "等待中"}`);
    await wait(500);
    payload = await api(`/api/runs/${encodeURIComponent(runId)}`);
  }
  throw new Error("运行轮询超时，请稍后重试");
}

async function runGraph() {
  const generation = ++runGeneration;
  const graphSnapshot = JSON.parse(JSON.stringify(graph));
  $("#run").disabled = true;
  try {
    setStatus("正在建立安全会话…");
    const runId = await createRun(graphSnapshot);
    if (generation !== runGeneration) return;
    currentRunId = runId;
    $("#replay").disabled = false;
    setStatus(`运行已创建：${runId}`);
    const completed = await pollRun(runId);
    if (generation === runGeneration) showRun(completed);
  } catch (error) {
    if (error.status === 404) setStatus("新版运行 API 尚未可用（需要 /api/session 与 /api/runs）");
    else setStatus(error.message);
  } finally {
    $("#run").disabled = false;
  }
}

async function replayRun() {
  if (!currentRunId) return;
  $("#replay").disabled = true;
  try {
    setStatus(`正在创建录制重放：${currentRunId}`);
    const replay = await mutationApi(`/api/runs/${encodeURIComponent(currentRunId)}/replay`, { mode: "recorded" });
    const replayId = runIdFrom(replay) || currentRunId;
    currentRunId = replayId;
    if (replay.trace || replay.result !== undefined) showRun(replay);
    else showRun(await pollRun(replayId));
  } catch (error) {
    if (error.status === 404) setStatus("录制重放 API 尚未可用");
    else setStatus(error.message);
  } finally {
    $("#replay").disabled = !currentRunId;
  }
}

async function loadGraph(name) {
  const generation = ++graphLoadGeneration;
  const loaded = await api(`/api/graphs/${encodeURIComponent(name)}`);
  if (generation !== graphLoadGeneration) return;
  runGeneration += 1;
  graph = loaded;
  graph.nodes ||= [];
  graph.edges ||= [];
  for (const node of graph.nodes) {
    node.config ||= {};
    node.position ||= { x: 40, y: 40 };
  }
  selectedId = null;
  connecting = null;
  currentRunId = null;
  $("#replay").disabled = true;
  $("#result").textContent = graph.description || "尚未运行";
  $("#trace").textContent = "";
  $("#detail").textContent = "点击轨迹查看输入输出";
  render();
  inspectSelected();
}

function buildPalette() {
  const palette = $("#palette");
  palette.textContent = "";
  const groups = {};
  for (const descriptor of descriptors) (groups[descriptor.category] ||= []).push(descriptor);
  for (const [category, items] of Object.entries(groups)) {
    const heading = document.createElement("h3");
    heading.textContent = category;
    palette.append(heading);
    for (const descriptor of items) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "palette-item";
      button.append(document.createTextNode(descriptor.title));
      const type = document.createElement("small");
      type.textContent = descriptor.type;
      button.append(type);
      button.addEventListener("click", () => addNode(descriptor));
      palette.append(button);
    }
  }
}

async function boot() {
  descriptors = (await api("/api/node-types")).nodeTypes;
  buildPalette();
  const names = (await api("/api/graphs")).graphs;
  for (const item of names) {
    const option = document.createElement("option");
    option.value = item.file;
    option.textContent = item.name;
    $("#graph").append(option);
  }
  $("#graph").addEventListener("change", (event) => loadGraph(event.target.value));
  $("#validate").addEventListener("click", async () => {
    try {
      const result = await mutationApi("/api/validate", { graph });
      setStatus(`图有效：${result.executionOrder.length} 个节点`);
    } catch (error) {
      setStatus(error.message);
    }
  });
  $("#run").addEventListener("click", runGraph);
  $("#replay").addEventListener("click", replayRun);
  $("#delete-node").addEventListener("click", deleteSelected);
  $("#viewport").addEventListener("click", (event) => {
    if (event.target === $("#viewport") || event.target === $("#nodes")) {
      connecting = null;
      render();
      inspectSelected();
    }
  });
  document.addEventListener("keydown", (event) => {
    const editing = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement;
    if (event.key === "Escape" && connecting) {
      connecting = null;
      render();
      inspectSelected();
      setStatus("已取消连线");
    } else if ((event.key === "Delete" || event.key === "Backspace") && !editing) {
      deleteSelected();
    }
  });
  if (!names.length) throw new Error("没有可加载的示例图");
  await loadGraph(names[0].file);
}

boot().catch((error) => setStatus(error.message));
