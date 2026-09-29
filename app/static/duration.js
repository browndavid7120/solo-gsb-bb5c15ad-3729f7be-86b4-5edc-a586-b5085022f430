/* 两事件耗时约束判定抽屉 —— 复用 app.js 的 state / overrides / el / esc / toast /
   focusEvent / shortId / fmtNum / colorFor 与 compare.js 的抽屉样式。
   只读：携带当前未保存来源假设，不保存假设、不改写数据；不受页面筛选影响
   （每次都用无筛选参数向 /api/recompute 取全量事件填充选择框）。 */
"use strict";

const durState = {
  events: [],     // 全量事件（未筛选），仅用于选择框
  result: null,
};
const dur$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function durOpen() {
  dur$("#dur-overlay").classList.remove("hidden");
  durFillEvents();
}
function durClose() {
  dur$("#dur-overlay").classList.add("hidden");
}

async function durLoadAllEvents() {
  // 显式不带任何筛选条件：计算不受页面筛选影响
  const res = await fetch("/api/recompute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overrides: state.overrides || {} }),
  });
  if (!res.ok) {
    toast("事件列表加载失败", "error");
    return [];
  }
  const data = await res.json();
  return data.events || [];
}

function durOption(e) {
  const t = (e.est_text || "").slice(11, 23);
  return `<option value="${esc(e.id)}">${esc(t)} · ${esc(shortId(e.id))} · `
    + `${esc(e.source_id)} · ${esc(e.summary || "")}</option>`;
}

async function durFillEvents() {
  const events = await durLoadAllEvents();
  durState.events = events;
  const opts = events.map(durOption).join("");
  const selS = dur$("#dur-start");
  const selE = dur$("#dur-end");
  const prevS = selS.value, prevE = selE.value;
  selS.innerHTML = opts;
  selE.innerHTML = opts;
  const has = (v) => events.some((e) => e.id === v);
  if (prevS && has(prevS)) selS.value = prevS;
  else if (events.length) selS.selectedIndex = 0;
  if (prevE && has(prevE)) selE.value = prevE;
  else if (events.length > 1) selE.selectedIndex = 1;
  else if (events.length) selE.value = selS.value;
}

/* --------------------------------------------------------------- 判定 */

async function durRun() {
  const startEvent = dur$("#dur-start").value;
  const endEvent = dur$("#dur-end").value;
  const raw = dur$("#dur-threshold").value;
  const threshold = raw === "" || raw === null ? NaN : Number(raw);
  const hint = dur$("#dur-hint");
  const box = dur$("#dur-result");

  if (!startEvent || !endEvent) { toast("请先选择起点与终点事件", "error"); return; }
  if (startEvent === endEvent) { toast("起点与终点必须是两个不同事件", "error"); return; }
  if (!isFinite(threshold) || threshold < 0) {
    toast("耗时阈值必须是非负有限数值（秒）", "error"); return;
  }

  hint.textContent = "联立求解中…";
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "在全部可行时间分配中求紧界…"));

  const res = await fetch("/api/duration-bounds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      start_event: startEvent,
      end_event: endEvent,
      threshold_seconds: threshold,
      overrides: state.overrides || {},
    }),
  });
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    durState.result = null;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "判定失败: " + esc(msg)));
    toast("判定失败: " + msg, "error");
    return;
  }
  durState.result = await res.json();
  durRender();
}

/* --------------------------------------------------------------- 渲染 */

const DUR_VERDICT_META = {
  must_exceed: { icon: "⛔", cls: "bad", label: "必超" },
  must_not_exceed: { icon: "✅", cls: "ok", label: "必不超" },
  may_exceed: { icon: "❓", cls: "warn", label: "可能超" },
};

function durRender() {
  const d = durState.result;
  const box = dur$("#dur-result");
  box.innerHTML = "";
  if (!d) return;
  if (!d.feasible) {
    box.appendChild(durInfeasibleBlock(d));
    return;
  }
  box.appendChild(durVerdictBlock(d));
  box.appendChild(durRangeBlock(d));
  box.appendChild(durWitnessBlock(d, "min"));
  box.appendChild(durWitnessBlock(d, "max"));
}

function durEndpointTag(eid, role) {
  const ev = durState.events.find((x) => x.id === eid);
  const text = ev ? `（${esc((ev.est_text || "").slice(11, 23))}）` : "";
  return `<span class="dur-endpoint ${role}">
    <span class="dur-endpoint-role">${role === "start" ? "起点" : "终点"}</span>
    <span class="chain-node" data-eid="${esc(eid)}">${esc(shortId(eid))}</span>${text}</span>`;
}

function durVerdictBlock(d) {
  const meta = DUR_VERDICT_META[d.verdict] || DUR_VERDICT_META.may_exceed;
  const blk = el("div", "cmp-block feas-verdict " + meta.cls);
  const dirty = Object.keys(state.overrides || {}).length > 0;
  const dn = d.duration;
  blk.innerHTML = `
    <div class="feas-verdict-head">
      <span class="feas-verdict-icon">${meta.icon}</span>
      <div>
        <div class="feas-verdict-title">判定：${meta.label}
          <span class="dur-threshold">阈值 ${fmtNum(d.threshold_seconds)}s</span></div>
        <div class="feas-verdict-sub">${esc(d.verdict_explanation)}
          ${dirty ? '<span class="tag amb">含未保存调整</span>' : ""}</div>
      </div>
    </div>
    <div class="dur-endpoints">${durEndpointTag(d.start, "start")}
      <span class="dur-minus">−</span>${durEndpointTag(d.end, "end")}</div>
    <div class="sens-meta">
      <span class="stat">事件 <b>${d.stats.event_count}</b></span>
      <span class="stat">参与约束 <b>${d.stats.constraint_count}</b></span>
      ${d.stats.skipped_constraints
        ? `<span class="stat warn">悬空约束已忽略 <b>${d.stats.skipped_constraints}</b></span>` : ""}
      <span class="stat">强连通分量 <b>${d.stats.component_count}</b></span>
    </div>`;
  return blk;
}

function durRangeBlock(d) {
  const dn = d.duration, r = d.reachability, nv = d.naive_interval_subtraction;
  const blk = el("div", "cmp-block");
  blk.innerHTML = `
    <h3>证据约束下的精确耗时范围
      <span class="cmp-count">t(终点) − t(起点)，在所有满足全部区间与约束的时间分配中取值；可为负（负=终点实际更早）</span>
    </h3>
    <div class="dur-range">
      <div class="dur-bound-val">
        <span class="dur-bound-label">最小耗时</span>
        <b>${esc(dn.min_text)}</b>
        <span class="dur-bound-sub">${fmtNum(dn.min_seconds)} s</span>
      </div>
      <div class="dur-range-bar">
        <span>◄ 全部可行分配的耗时取值区间，宽度 ${fmtNum(dn.span_seconds)}s ─►</span>
      </div>
      <div class="dur-bound-val">
        <span class="dur-bound-label">最大耗时</span>
        <b>${esc(dn.max_text)}</b>
        <span class="dur-bound-sub">${fmtNum(dn.max_seconds)} s</span>
      </div>
    </div>
    <div class="feas-note ok">🔗 ${esc(r.description)}</div>
    <div class="feas-note ${nv.matches ? "ok" : "warn"}">
      📐 朴素"两区间直接相减"为 [${esc(nv.lo_text)}, ${esc(nv.hi_text)}]：${esc(nv.note)}
      ${nv.matches ? "本案例中它恰好与紧界一致。" : "（与上方紧界不一致，请勿据此下结论）"}
    </div>
    <div class="feas-note" style="background:transparent">${esc(d.summary)}</div>`;
  return blk;
}

function durWitnessBlock(d, which) {
  const w = d.witnesses[which];
  const isMin = which === "min";
  const blk = el("div", "cmp-block");
  const title = isMin ? "达到最小耗时的全量时刻见证" : "达到最大耗时的全量时刻见证";
  blk.innerHTML = `<h3>${isMin ? "🔽" : "🔼"} ${title}
    <span class="cmp-count">本安排下 t(终点) − t(起点) = <b>${fmtNum(w.value_seconds)}s</b>；
      每个事件均在自身闭区间内并满足全部约束，点击行在主时间线定位</span></h3>
    <div class="feas-note ok" style="margin-bottom:8px">${esc(w.note)}</div>`;

  const list = el("div", "feas-list");
  const frag = document.createDocumentFragment();
  for (const e of w.events) {
    const isStart = e.id === d.start, isEnd = e.id === d.end;
    const row = el("div", "feas-row dur-wit-row"
      + (isStart ? " is-start" : "") + (isEnd ? " is-end" : "")
      + (e.in_cycle ? " in-cycle" : ""));
    row.innerHTML = `
      <span class="feas-row-time" title="见证取值时刻">${esc(e.time_text.slice(11, 23))}</span>
      <span class="chain-node feas-row-id" data-eid="${esc(e.id)}">${esc(shortId(e.id))}
        ${isStart ? '<span class="dur-role-tag start">起</span>' : ""}
        ${isEnd ? '<span class="dur-role-tag end">终</span>' : ""}</span>
      <span class="feas-row-src">${esc(e.source_id)}</span>
      <span class="feas-row-summary">${esc(e.summary || "")}</span>
      <span class="feas-row-range" title="真实时间闭区间 [lo, hi]；取值距下界/上界余量均 ≥ 0">
        [${esc(e.lo_text.slice(11, 23))} ~ ${esc(e.hi_text.slice(11, 23))}]
        · ↓${fmtNum(e.slack_to_lo)}s ↑${fmtNum(e.slack_to_hi)}s
        ${e.in_cycle ? '<span class="tag amb" title="处于因果环内：环内事件取同一时刻">环</span>' : ""}
      </span>`;
    row.querySelector(".feas-row-id").addEventListener("click", () => {
      durClose();
      focusEvent(e.id);
    });
    frag.appendChild(row);
  }
  list.appendChild(frag);
  blk.appendChild(list);
  return blk;
}

/* ---- 无解：展示现有矛盾路径见证（直接复用全局可行性抽屉的矛盾路径渲染） ---- */

function durInfeasibleBlock(d) {
  const blk = el("div", "cmp-block");
  const head = el("div", "cmp-block feas-verdict bad");
  head.innerHTML = `
    <div class="feas-verdict-head">
      <span class="feas-verdict-icon">✗</span>
      <div>
        <div class="feas-verdict-title">全局时序无解 · 不输出耗时结论</div>
        <div class="feas-verdict-sub">${esc(d.summary)}</div>
      </div>
    </div>`;
  blk.appendChild(head);
  // 矛盾路径与 /api/temporal-feasibility 完全同构，复用其渲染（点击事件回主时间线）
  if (d.witness && typeof feasConflictBlock === "function") {
    blk.appendChild(feasConflictBlock(d.witness));
  }
  return blk;
}

/* --------------------------------------------------------------- 绑定 */

dur$("#btn-duration").addEventListener("click", durOpen);
dur$("#dur-close").addEventListener("click", durClose);
dur$("#dur-rerun").addEventListener("click", async () => {
  await durFillEvents();
  await durRun();
});
dur$("#dur-run").addEventListener("click", durRun);
dur$("#dur-overlay").addEventListener("click", (e) => {
  if (e.target.id === "dur-overlay") durClose();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !dur$("#dur-overlay").classList.contains("hidden")) durClose();
});
