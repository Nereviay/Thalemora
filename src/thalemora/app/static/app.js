"use strict";
const $ = (s) => document.querySelector(s);
const titles = {
  history: "历史与原话",
  memory: "Memory · 长期记忆",
  water: "Water · 水系",
  self_model: "Self Model · 自我认识",
  emotion: "Emotion · 情绪",
  diary: "Diary · 日记",
  dream: "Dream · 梦境",
  whisper: "Whisper · 低语",
  reflection: "Reflection · 反思",
  continuity: "Continuity · 连续性",
  persona: "Persona · 人格配置",
  sources: "来源授权",
  models: "模型与用量",
  backup: "备份与恢复",
};
const inner = [
  "emotion",
  "diary",
  "dream",
  "whisper",
  "reflection",
  "continuity",
  "persona",
];
let token = "",
  view = "history",
  cursor = 0,
  cursors = [],
  rows = [],
  query = "",
  memoryScope = "source-scoped",
  agentOffset = 0,
  loading = false,
  revision = 0;
const limit = 20;
function notice(text, error = false) {
  $("#notice").textContent = text;
  $("#notice").classList.toggle("error", error);
}
function node(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}
function action(text, fn) {
  const b = node("button", text, "quiet");
  b.type = "button";
  b.onclick = () => guard(fn);
  return b;
}
async function guard(fn) {
  try {
    await fn();
  } catch (e) {
    notice(e.message, true);
  }
}
async function api(operation, args = {}, post = false) {
  const suffix = post ? "" : "?" + new URLSearchParams(args);
  const response = await fetch("/api/" + operation + suffix, {
    method: post ? "POST" : "GET",
    headers: {
      Authorization: "Bearer " + token,
      ...(post ? { "Content-Type": "application/json" } : {}),
    },
    ...(post ? { body: JSON.stringify(args) } : {}),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "请求失败");
  return data.result;
}
function scope() {
  return {
    agent_id: $("#agent").value,
    app_id: $("#appId").value,
    channel: $("#channel").value,
  };
}
function details(value) {
  $("#detailBody").textContent = JSON.stringify(value, null, 2);
  $("#detail").showModal();
}
function download(value, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function reset() {
  revision++;
  cursor = 0;
  cursors = [];
  query = "";
  memoryScope = "source-scoped";
}
async function agents() {
  const previous = $("#agent").value;
  const list = await api("list_agents", { limit, offset: agentOffset });
  $("#agent").replaceChildren(
    ...list.map((a) => {
      const option = node("option", a.display_name);
      option.value = a.agent_id;
      return option;
    }),
  );
  if (list.some((a) => a.agent_id === previous)) $("#agent").value = previous;
  $("#agentPrev").disabled = agentOffset === 0;
  $("#agentNext").disabled = list.length < limit;
}
function record(item, title, text) {
  const row = node("article", undefined, "record");
  row.append(node("h2", title), node("p", text || "—"));
  const meta = item.source_event_id
    ? `${item.role || "未知角色"} · ${item.occurred_at || "无来源时间"} · v${item.version}`
    : item.recorded_at || item.happened_at || "";
  if (meta) row.append(node("small", meta));
  const actions = node("div", undefined, "actions");
  actions.append(action("记录详情", () => details(item)));
  row.append(actions);
  $("#content").append(row);
  return actions;
}
function form(fields, button, submit) {
  const f = node("form");
  const elements = {};
  for (const [key, label, type = "text", value = "", options = []] of fields) {
    const l = node("label", label);
    const el = node(type === "textarea" || type === "select" ? type : "input");
    if (type === "select") {
      for (const [value, label] of options) {
        const option = node("option", label);
        option.value = value;
        el.append(option);
      }
    } else if (type !== "textarea") el.type = type;
    el.value = value;
    if (type !== "checkbox") el.required = true;
    l.append(el);
    f.append(l);
    elements[key] = el;
  }
  const b = node("button", button);
  b.type = "submit";
  f.append(b);
  f.onsubmit = (e) => {
    e.preventDefault();
    guard(async () => {
      b.disabled = true;
      try {
        await submit(
          Object.fromEntries(
            Object.entries(elements).map(([k, el]) => [
              k,
              el.type === "checkbox" ? el.checked : el.value,
            ]),
          ),
        );
      } finally {
        b.disabled = false;
      }
    });
  };
  return f;
}
function addImport() {
  const l = node("label", "导入合成/授权历史 JSON（每文件 1..100 条）");
  const input = node("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = () =>
    guard(async () => {
      const file = input.files[0];
      if (!file) return;
      if (file.size > 1024 * 1024)
        throw Error("单批文件最多 1 MiB；大历史请使用 Python 分批导入");
      const events = JSON.parse(await file.text());
      await api("import_history", { ...scope(), events }, true);
      await load();
      notice("历史已导入");
    });
  l.append(input);
  $("#tools").append(l);
}
async function renderModels() {
  const caps = await api("capabilities");
  rows = caps.models;
  for (const config of rows) {
    record(
      config,
      config.module + " · " + config.state,
      `调用尝试 ${config.calls} 次 · 返回 token ${config.actual_tokens ?? "未知"} · 费用未提供\n此入口仅做显式调用；不自动执行领域提炼。`,
    );
  }
  for (const module of ["embedding", "rerank"]) {
    const config = rows.find((r) => r.module === module).config;
    const d = node("details");
    d.append(node("summary", module === "embedding" ? "Embedding · 召回向量" : "Rerank · 候选重排"));
    d.append(node("p", "本地使用宿主已提供的模型或已存在的模型目录，不自动下载。API 会发送查询和记忆候选到所配服务，按预算计数；Rerank 使用独立 /rerank 端点。切换 Embedding 模型后，下次召回自动重建向量索引。"));
    const f = form([
      ["mode", "模型方式", "select", config.mode || (config.enabled ? "api" : "disabled"), [["disabled", "关闭"], ["local", "本地模型"], ["api", "API"]]],
      ["model", "本地模型标识或目录 / API 模型名", "text", config.model || ""],
      ["base_url", "API base URL（自动追加 /embeddings 或 /rerank）", "text", config.base_url || ""],
      ["key_env", "服务端 Key 环境变量名", "text", config.key_env || ""],
      ["call_budget", "累计调用预算（硬上限）", "number", config.call_budget || 0],
      ["token_budget", "累计 token 阈值（可能超出）", "number", config.token_budget || 0],
    ], "保存 " + module, async (v) => {
      v.module = module;
      v.call_budget = Number(v.call_budget);
      v.token_budget = Number(v.token_budget);
      await api("configure_provider", v, true);
      await load();
      notice("配置已保存");
    });
    for (const input of f.querySelectorAll("input")) input.required = false;
    d.append(f);
    $("#tools").append(d);
  }
  const d = node("details");
  d.append(node("summary", "其他用途 Provider"));
  d.append(
    form(
      [
        ["module", "用途：writer / gatekeeper / reflection"],
        ["base_url", "兼容 API base URL"],
        ["model", "模型名"],
        ["key_env", "服务端 Key 环境变量名"],
        ["capability", "能力：chat / embedding"],
        ["call_budget", "累计调用预算（硬上限）", "number", "0"],
        ["token_budget", "累计 token 阈值（可能超出）", "number", "0"],
        ["enabled", "启用调用", "checkbox"],
      ],
      "保存配置",
      async (v) => {
        v.call_budget = Number(v.call_budget);
        v.token_budget = Number(v.token_budget);
        await api("configure_provider", v, true);
        await load();
        notice("配置已保存");
      },
    ),
  );
  $("#tools").append(d);
  $("#tools").append(
    form(
      [
        ["module", "调用用途"],
        ["text", "显式发送的测试文本", "textarea"],
      ],
      "调用已启用 Provider",
      async (v) => details(await api("call_provider", v, true)),
    ),
  );
  const audit = await api("usage", { limit, offset: cursor });
  for (const call of audit)
    record(
      call,
      "调用 #" + call.id + " · " + call.status,
      call.module + " · " + (call.tokens ?? "未知") + " token",
    );
  rows = audit;
}
async function load() {
  if (loading) {
    revision++;
    return;
  }
  loading = true;
  const currentRevision = revision;
  let membershipsCount = 0;
  $("#refresh").disabled = true;
  $("#content").replaceChildren(node("p", "正在读取当前页…", "empty"));
  $("#tools").replaceChildren();
  $("#title").textContent = titles[view];
  $("#count").textContent = "";
  $("#scopeLabel").textContent = $("#channel").value;
  document
    .querySelectorAll("nav button")
    .forEach((b) =>
      b.setAttribute(
        "aria-current",
        b.dataset.view === view ? "page" : "false",
      ),
    );
  try {
    if (!scope().agent_id && !["models", "backup"].includes(view)) {
      $("#content").replaceChildren(
        node("p", "先在左侧注册一个 Agent，再授权来源应用与通道。", "empty"),
      );
      rows = [];
      return;
    }
    $("#content").replaceChildren();
    const s = scope(),
      page = { limit, offset: cursor };
    if (view === "models") {
      await renderModels();
    } else if (view === "backup") {
      rows = [];
      const block = node("article", undefined, "record");
      block.append(
        node("h2", "停写后封存，恢复到新空目录"),
        node(
          "p",
          "先停止本实例所有 HTTP、MCP 和 Python 写入，再在本机运行。快照包含所有 Agent 的私有数据，请按原数据权限保存。管理 token 不包含在快照中。",
        ),
        node(
          "pre",
          "thalemora backup ./state ./snapshot\nthalemora restore ./snapshot ./restored\nTHALEMORA_DATA=./restored thalemora serve",
        ),
        node(
          "p",
          "恢复后沿用 Agent ID 与授权，另设管理 token。历史全量导出使用 export-history 命令逐页流式写出；当前历史页也可单独导出。",
        ),
      );
      $("#content").append(block);
    } else if (view === "sources") {
      rows = await api("sources", { agent_id: s.agent_id, ...page });
      for (const r of rows) {
        const a = record(r, r.app_id, r.channel);
        a.append(
          action("切换此来源", async () => {
            $("#appId").value = r.app_id;
            $("#channel").value = r.channel;
            view = "history";
            reset();
            await load();
          }),
        );
      }
    } else if (view === "history") {
      rows = await api(query ? "search" : "history", {
        ...s,
        ...page,
        ...(query ? { query } : { include_deleted: true }),
      });
      for (const r of rows) {
        const a = record(
          r,
          r.source_event_id,
          r.deleted ? "已删除（正文不可用）" : r.text,
        );
        if (!r.deleted)
          a.append(
            action("原文存为人工记忆", async () => {
              details(
                await api(
                  "remember",
                  { ...s, source_event_id: r.source_event_id },
                  true,
                ),
              );
              notice("已创建含来源证据的人工记忆");
            }),
          );
      }
      $("#tools").append(
        form(
          [["query", "历史原文检索（1–200 字符）", "text", query]],
          "检索",
          async (v) => {
            query = v.query;
            cursor = 0;
            cursors = [];
            await load();
          },
        ),
      );
      $("#tools").append(
        action("显示全部当前历史", async () => {
          query = "";
          reset();
          await load();
        }),
        action("导出当前页", () => download(rows, "history-page.json")),
      );
      addImport();
    } else if (view === "memory") {
      const memoryArgs = { ...s, scope: memoryScope };
      $("#tools").append(form(
        [["scope", "记忆查看范围", "select", memoryScope, [
          ["source-scoped", "当前应用来源（默认）"],
          ["owner-global", "管理员：该 Agent 当前通道的全部来源"],
        ]]], "切换查看范围", async (v) => {
          reset();
          memoryScope = v.scope;
          await load();
        },
      ));
      $("#tools").append(node("p", memoryScope === "owner-global"
        ? "管理员视图：该 Agent 当前通道的全部来源；未知或冲突归属仅供管理员处理。"
        : "仅查看当前应用来源的记忆；来源更新或删除不抹除历史归属。", "muted"));
      rows = await api("memory", { ...memoryArgs, ...page });
      for (const r of rows) {
        const a = record(r, r.title || "记忆 #" + r.id, r.content);
        if (r.ownership) a.append(node("small", "归属：" + (r.ownership.app || r.ownership.state)));
        a.append(
          action("查看来源证据", async () =>
            details(
              await api("evidence", { ...memoryArgs, card_id: r.id, limit, offset: 0 }),
            ),
          ),
        );
      }
    } else if (view === "water") {
      const ownerArgs = { agent_id: s.agent_id, channel: s.channel, scope: "owner-global" };
      $("#tools").append(node("p", "管理员水系：该 Agent 当前通道的全部来源；不按应用分树。", "muted"));
      rows = await api("water", { ...ownerArgs, ...page });
      for (const r of rows)
        record(
          r,
          r.title,
          `${r.node_type} · ${r.node_id} · 上级 ${r.parent_node_id || "海"}`,
        );
      const memberships = await api("water_memberships", { ...ownerArgs, ...page });
      membershipsCount = memberships.length;
      for (const r of memberships)
        record(r, "记忆 #" + r.card_id + " 的归属", r.container_node_id);
      const d = node("details");
      d.append(node("summary", "添加湖 / 溪流 / 支流"));
      d.append(
        form(
          [
            ["node_id", "节点 ID"],
            ["node_type", "类型：lake / stream / branch"],
            ["title", "标题"],
            ["parent_node_id", "上级 ID（根湖填 -）"],
          ],
          "创建节点",
          async (v) => {
            if (v.parent_node_id === "-") v.parent_node_id = null;
            await api("water_node", { ...ownerArgs, app_id: null, ...v }, true);
            await load();
          },
        ),
      );
      d.append(
        form(
          [
            ["card_id", "Memory ID", "number"],
            ["to_node_id", "目标溪流/支流 ID（海填 -）"],
            ["reason", "调整理由"],
          ],
          "调整归属",
          async (v) => {
            v.card_id = Number(v.card_id);
            if (v.to_node_id === "-") v.to_node_id = null;
            await api("water_move", { ...ownerArgs, app_id: null, ...v }, true);
            await load();
          },
        ),
      );
      $("#tools").append(d);
    } else {
      rows = await api("inner_state", { ...s, ...page, kind: view });
      for (const r of rows)
        record(
          r,
          r.item_id || r.claim_id || "Persona v" + r.version,
          r.payload?.text ||
            r.statement ||
            r.content ||
            JSON.stringify(r.payload || r),
        );
      if (
        [
          "emotion",
          "diary",
          "dream",
          "whisper",
          "self_model",
          "continuity",
        ].includes(view)
      )
        $("#tools").append(
          node(
            "p",
            "自主创作、自我修订与 epoch 候选需连接可信 Agent 宿主；本页浏览已保存状态。",
            "muted",
          ),
        );
      if (view === "reflection")
        $("#tools").append(
          form(
            [
              ["text", "反思建议", "textarea"],
              ["target_kind", "目标领域"],
              ["target_id", "目标 ID"],
              ["target_version", "目标版本", "number", "1"],
            ],
            "保存人工建议",
            async (v) => {
              v.target_version = Number(v.target_version);
              v.recorded_at = new Date().toISOString();
              await api("reflect", { ...s, ...v }, true);
              await load();
            },
          ),
        );
      if (view === "persona" && s.channel === "main") {
        $("#tools").append(
          form(
            [
              ["content", "本地管理的人格配置", "textarea"],
              [
                "expected_version",
                "当前版本（首次 0）",
                "number",
                String(rows.length ? rows[rows.length - 1].version : 0),
              ],
              ["reason", "配置理由"],
            ],
            "保存 managed Persona",
            async (v) => {
              v.expected_version = Number(v.expected_version);
              await api(
                "configure_persona",
                {
                  agent_id: s.agent_id,
                  app_id: s.app_id,
                  ...v,
                  authority: "managed",
                  recorded_at: new Date().toISOString(),
                },
                true,
              );
              await load();
            },
          ),
        );
      }
    }
    if (!$("#content").childElementCount)
      $("#content").append(node("p", "当前页暂无记录。", "empty"));
    $("#count").textContent = view === "water"
      ? rows.length + " 个节点 · " + membershipsCount + " 条归属 / 当前页"
      : rows.length + " 条 / 当前页";
    $("#pageLabel").textContent = "每页至多 " + limit + " 条";
    $("#prev").disabled = !cursors.length;
    $("#next").disabled = (view === "water"
      ? Math.max(rows.length, membershipsCount) : rows.length) < limit || view === "backup";
  } catch (e) {
    rows = [];
    $("#content").replaceChildren(node("p", "读取失败：" + e.message, "empty"));
    $("#next").disabled = true;
    throw e;
  } finally {
    loading = false;
    $("#refresh").disabled = false;
    if (currentRevision !== revision) await load();
  }
}
for (const [key, title] of Object.entries(titles)) {
  const b = action(title, async () => {
    view = key;
    reset();
    await load();
  });
  b.setAttribute("aria-label", title);
  b.replaceChildren(
    node("span", title, "nav-full"),
    node(
      "span",
      title.includes(" · ") ? title.split(" · ")[1] : title,
      "nav-short",
    ),
  );
  b.dataset.view = key;
  $("#nav").append(b);
}
$("#loginForm").onsubmit = (e) => {
  e.preventDefault();
  guard(async () => {
    token = $("#token").value;
    await agents();
    $("#token").value = "";
    $("#login").hidden = true;
    $("#workspace").hidden = false;
    notice("已连接本地实例");
    await load();
  });
};
$("#agentForm").onsubmit = (e) => {
  e.preventDefault();
  guard(async () => {
    await api("register_agent", { display_name: $("#agentName").value }, true);
    $("#agentName").value = "";
    await agents();
    reset();
    view = "sources";
    await load();
    notice("Agent 已注册，请显式授权应用与通道");
  });
};
$("#grant").onclick = () =>
  guard(async () => {
    const s = scope();
    await api("grant_source", s, true);
    notice("已授权 " + s.app_id + " / " + s.channel);
    reset();
    await load();
  });
$("#refresh").onclick = () => guard(load);
$("#agent").onchange = () =>
  guard(async () => {
    reset();
    await load();
  });
$("#channel").onchange = $("#agent").onchange;
$("#appId").onchange = $("#agent").onchange;
$("#next").onclick = () =>
  guard(async () => {
    cursors.push(cursor);
    cursor = inner.includes(view)
      ? view === "persona"
        ? rows[rows.length - 1].version
        : rows[rows.length - 1].seq
      : cursor + limit;
    await load();
  });
$("#prev").onclick = () =>
  guard(async () => {
    cursor = cursors.pop() ?? 0;
    await load();
  });
$("#agentNext").onclick = () =>
  guard(async () => {
    agentOffset += limit;
    await agents();
    reset();
    await load();
  });
$("#agentPrev").onclick = () =>
  guard(async () => {
    agentOffset = Math.max(0, agentOffset - limit);
    await agents();
    reset();
    await load();
  });
$("#closeDetail").onclick = () => $("#detail").close();
$("#logout").onclick = () => {
  token = "";
  $("#workspace").hidden = true;
  $("#login").hidden = false;
  notice("连接已断开");
};
