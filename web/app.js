/* 三粒度加工与差异分析控制台前端（原生 JS，无框架依赖） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (url, opts) => {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const badge = (text, cls) => `<span class="badge ${cls}">${esc(text)}</span>`;
const fmt = (v) => (typeof v === "number" ? v.toLocaleString("zh-CN", { maximumFractionDigits: 6 }) : esc(v));

let LAST_FULL = null;   // 最近一次运行的完整三路径结果
let LAST_SPEC = null;

// ---------------- 标签切换 ----------------
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === `tab-${t.dataset.tab}`));
  if (t.dataset.tab === "jobs") loadJobs();
}));

// ---------------- ① 粒度登记 ----------------
async function loadRegistry() {
  const [tables, rels, audit, scenarios] = await Promise.all([
    api("/api/meta/tables"), api("/api/meta/relationships"),
    api("/api/meta/audit"), api("/api/scenarios"),
  ]);
  $("#scn-dup").checked = scenarios.duplicate_product;
  $("#scn-weight").checked = scenarios.incomplete_weights;

  $("#table-meta").innerHTML = tables.map((t) => `
    <div class="card">
      <h4>${esc(t.table_name)} <span class="pill">${esc(t.grain_label)}</span></h4>
      <dl class="kv">
        <dt>粒度键</dt><dd>${t.grain_keys.map(esc).join(", ")}</dd>
        <dt>主键</dt><dd>${t.pk_cols.map(esc).join(", ")}</dd>
      </dl>
      <div class="table-wrap" style="max-height:180px;margin-top:8px">
        <table><thead><tr><th>列</th><th>角色</th><th>可加</th></tr></thead><tbody>
        ${t.columns.map((c) => `<tr><td><code>${esc(c.col_name)}</code></td>
          <td>${roleLabel(c.role)}</td>
          <td>${c.additive ? badge("可加", "ok") : badge("不可加", "warn")}</td></tr>`).join("")}
        </tbody></table>
      </div>
    </div>`).join("");

  $("#rel-meta").innerHTML = rels.map((r) => `
    <div class="card">
      <h4>${esc(r.name)} ${r.active ? badge("启用", "ok") : badge("停用", "warn")}</h4>
      <dl class="kv">
        <dt>连接</dt><dd>${esc(r.left_table)}.${r.left_cols.map(esc).join("+")}
          → ${esc(r.right_table)}.${r.right_cols.map(esc).join("+")}</dd>
        <dt>基数</dt><dd>${esc(r.cardinality)}</dd>
        <dt>分摊基准</dt><dd>${r.basis_col ? `${esc(r.basis_table)}.${esc(r.basis_col)}` : "—"}</dd>
        ${r.join_through ? `<dt>桥接</dt><dd>${esc(r.join_through.bridge)}</dd>` : ""}
        <dt>说明</dt><dd>${esc(r.note || "")}</dd>
      </dl>
      <div class="edit-row">
        <label class="muted">基数</label>
        <select id="card-${esc(r.name)}">
          ${["N:1", "1:N", "M:N"].map((c) =>
            `<option ${c === r.cardinality ? "selected" : ""}>${c}</option>`).join("")}
        </select>
        <label class="muted">启用</label>
        <select id="act-${esc(r.name)}">
          <option value="1" ${r.active ? "selected" : ""}>启用</option>
          <option value="0" ${!r.active ? "selected" : ""}>停用</option>
        </select>
        <button class="small primary" onclick="saveRel('${esc(r.name)}')">保存修改</button>
      </div>
    </div>`).join("");

  $("#rel-audit").innerHTML = audit.length
    ? audit.map((a) => `<div class="audit-item">
        <b>${esc(a.changed_at)}</b> · ${esc(a.relationship_name)}
        ${a.reason ? `· 原因：${esc(a.reason)}` : ""}
        <div>前: ${esc(a.before_json)}<br>后: ${esc(a.after_json)}</div></div>`).join("")
    : `<div class="muted">暂无变更记录</div>`;
}
const roleLabel = (r) => ({ key: "键", dim: "维度", measure: "度量" }[r] || r);

window.saveRel = async function (name) {
  const reason = prompt("修改原因（将写入只增审计表）：", "网页修正维度关系");
  if (reason === null) return;
  const changes = {
    cardinality: $(`#card-${name}`).value,
    active: $(`#act-${name}`).value === "1",
    reason,
  };
  try {
    await api(`/api/meta/relationships/${name}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    await loadRegistry();
  } catch (e) { alert(e.message); }
};

$("#scn-dup").addEventListener("change", (e) =>
  api("/api/scenarios/duplicate_product", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked }) }).then(loadRegistry));
$("#scn-weight").addEventListener("change", (e) =>
  api("/api/scenarios/incomplete_weights", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked }) }).then(loadRegistry));

// ---------------- ② 数据浏览 ----------------
const TABLES = ["orders", "order_lines", "allocations", "dim_product", "campaigns", "line_campaign"];
function initDataPicker() {
  $("#data-picker").innerHTML = TABLES.map((t, i) =>
    `<button class="small ${i === 0 ? "primary" : ""}" data-t="${t}">${t}</button>`).join("");
  $("#data-picker").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      $("#data-picker").querySelectorAll("button").forEach((x) => x.classList.remove("primary"));
      b.classList.add("primary");
      loadData(b.dataset.t);
    }));
  loadData(TABLES[0]);
}
async function loadData(table) {
  const data = await api(`/api/data/${table}`);
  const cols = Object.keys(data.rows[0] || {});
  $("#data-table").innerHTML = `<div class="card"><div class="table-wrap">
    <table><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${data.rows.map((r) => `<tr>${cols.map((c) =>
      `<td class="${typeof r[c] === "number" ? "num" : ""}">${fmt(r[c])}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div></div>`;
}

// ---------------- ③ 计算路径对比 ----------------
async function initReports() {
  const reports = await api("/api/reports");
  $("#report-select").innerHTML = reports.map((r) =>
    `<option value="${esc(r.report_name)}">${esc(r.report_name)}</option>`).join("");
  $("#report-desc").textContent = reports[0].description;
  $("#report-select").addEventListener("change", () => {
    const r = reports.find((x) => x.report_name === $("#report-select").value);
    $("#report-desc").textContent = r.description;
  });
}

$("#btn-run").addEventListener("click", () => runAndRender(false));

async function runAndRender(isRebuild, rebuildJob) {
  const report_name = $("#report-select").value;
  try {
    const body = { report_name };
    if (isRebuild) { body.path = rebuildPathEligible(); body.rebuild_from_job = rebuildJob; }
    const data = await api("/api/reports/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    LAST_FULL = data.full; LAST_SPEC = data.full.spec;
    renderPaths(LAST_FULL);
    renderDiagnosis(LAST_FULL, data.job, isRebuild, data.supersession);
    document.querySelector('.tab[data-tab="diagnosis"]').click();
  } catch (e) { alert(e.message); }
}
function rebuildPathEligible() {
  const d = LAST_FULL.diagnosis;
  return d.valid_paths.includes("ATJ") ? "ATJ" : (d.valid_paths[0] || "ATJ");
}

function renderPaths(full) {
  const edgeTxt = full.edges.length
    ? full.edges.map((e) => `<span class="pill">${esc(e.from)}→${esc(e.to)}
        ${e.fanout ? "扇出" : "唯一查找"}${e.factor_col ? ` ×${esc(e.factor_col)}` : ""}</span>`).join(" ")
    : `<span class="pill">无连接（维度在事实表上）</span>`;
  $("#path-summary").innerHTML = `<div class="card"><h4>规划出的连接路径</h4>${edgeTxt}</div>`;

  $("#path-results").innerHTML = `<div class="path-col">
    ${["naive", "atj", "dja"].map((p) => pathCard(full[p], p)).join("")}</div>`;
}

function pathCard(obj, key) {
  const title = { naive: "NAIVE 朴素明细连接（诊断基线）", atj: "ATJ 先聚合再关联", dja: "DJA 明细连接按贡献归并" }[key];
  if (!obj.eligible) {
    return `<div class="path-box rejected">
      <h4>${title} ${badge("拒绝", "reject")}</h4>
      ${obj.reasons.map((r) => `<div class="reason"><b>${esc(r.code)}</b>${r.edge ? ` <span class="pill">${esc(r.edge)}</span>` : ""}<br>${esc(r.detail)}
        ${r.sample ? `<br><code>${esc(JSON.stringify(r.sample))}</code>` : ""}</div>`).join("")}
    </div>`;
  }
  const measureCols = Object.keys(obj.result[0] || {}).filter((k) => k !== "group");
  const totals = obj.naive_totals || obj.allocated_totals || obj.base_totals;
  return `<div class="path-box eligible">
    <h4>${title} ${badge("合法", "ok")}</h4>
    ${totals ? `<div class="kv" style="margin-bottom:8px">${Object.entries(totals)
      .map(([k, v]) => `<dt>${esc(k)} 合计</dt><dd>${fmt(v)}</dd>`).join("")}</div>` : ""}
    <div class="table-wrap" style="max-height:220px">
      <table><thead><tr><th>分组</th>${measureCols.map((c) => `<th class="num">${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${obj.result.map((r) => `<tr><td>${r.group.map(fmt).join(" / ")}</td>
        ${measureCols.map((c) => `<td class="num">${fmt(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>
    </div>
    <div class="stage" style="margin-top:8px">
      ${obj.stage_counts.map((s) => `<div>${esc(s.stage)}：<b>${s.rows}</b> 行</div>`).join("")}
    </div>
  </div>`;
}

// ---------------- ④ 差异诊断 + 重建 ----------------
function renderDiagnosis(full, job, isRebuild, supersession) {
  const d = full.diagnosis;
  const n = full.naive;
  let html = `<div class="card">
    <h4>结论 ${d.rebuildable ? badge("可重建", "ok") : badge("无合法路径", "err")}</h4>
    <p>${esc(d.conclusion)}</p>
    <p class="muted">合法计算路径：${d.valid_paths.length ? d.valid_paths.map((v) => `<span class="pill">${v}</span>`).join(" ") : "无"}</p>
  </div>`;

  html += `<div class="card"><h4>诊断发现</h4>${d.findings.map((f) =>
    `<div class="finding ${f.severity}">${badge(f.severity, f.severity === "OK" ? "ok" :
      f.severity === "ERROR" ? "err" : f.severity === "WARN" ? "warn" : "reject")}
      <code>${esc(f.code)}</code> ${esc(f.summary)}</div>`).join("")}</div>`;

  if (n.eligible) {
    html += `<div class="card"><h4>金额/计数为何变化（两个方向，勿用净额掩盖）</h4>
      <div class="table-wrap"><table><thead><tr><th>度量</th><th class="num">原始合计</th>
      <th class="num">朴素合计</th><th class="num">净差异</th><th class="num">重复放大(+)</th>
      <th class="num">孤儿丢失(-)</th></tr></thead><tbody>
      ${Object.keys(n.base_totals).map((c) => `<tr>
        <td><code>${esc(c)}</code></td><td class="num">${fmt(n.base_totals[c])}</td>
        <td class="num">${fmt(n.naive_totals[c])}</td>
        <td class="num ${n.net_delta[c] >= 0 ? "delta-pos" : "delta-neg"}">${fmt(n.net_delta[c])}</td>
        <td class="num delta-pos">${fmt(n.repeated_extra[c])}</td>
        <td class="num delta-neg">${fmt(n.lost_totals[c])}</td></tr>`).join("")}
      </tbody></table></div></div>`;

    if (n.duplicate_contributions.length) {
      html += `<div class="card"><h4>重复贡献定位（被复制的事实行）</h4>
        <div class="table-wrap"><table><thead><tr><th>事实行</th><th>副本数</th>
        <th>度量</th><th class="num">单值(正确)</th><th class="num">重复累计(错误)</th>
        <th>落到的分组</th></tr></thead><tbody>
        ${n.duplicate_contributions.map((dup) => Object.entries(dup.measures).map(([c, b], i) =>
          `<tr>${i === 0 ? `<td rowspan="${Object.keys(dup.measures).length}"><code>${esc(dup.fact_key.join(","))}</code></td>
            <td rowspan="${Object.keys(dup.measures).length}">${dup.copies}</td>` : ""}
          <td><code>${esc(c)}</code></td><td class="num">${fmt(b.single_value)}</td>
          <td class="num delta-pos">${fmt(b.repeated_total)}</td>
          <td>${b.by_group.map((g) => `${g.group.map(fmt).join("/")}: ${fmt(g.amount)}`).join("；")}</td></tr>`).join("")).join("")}
        </tbody></table></div></div>`;
    }
    if (n.lost_rows.length) {
      html += `<div class="card"><h4>被内连接丢弃的孤儿事实行</h4>
        <div class="table-wrap"><table><thead><tr><th>事实行</th><th>丢失度量</th></tr></thead>
        <tbody>${n.lost_rows.map((l) => `<tr><td><code>${esc(l.fact_key.join(","))}</code></td>
          <td>${esc(JSON.stringify(l.measures))}</td></tr>`).join("")}</tbody></table></div></div>`;
    }
  }

  if (d.group_level_differences.length) {
    html += `<div class="card"><h4>分组级差异（朴素值 vs 正确值）</h4>
      <div class="table-wrap"><table><thead><tr><th>分组</th><th>度量</th>
      <th class="num">朴素值</th><th class="num">正确值</th><th class="num">差值</th><th class="num">放大倍数</th>
      </tr></thead><tbody>${d.group_level_differences.map((g) => `<tr>
        <td>${g.group.map(fmt).join(" / ")}</td><td><code>${esc(g.measure)}</code></td>
        <td class="num">${fmt(g.naive_value)}</td><td class="num">${fmt(g.correct_value)}</td>
        <td class="num delta-pos">${fmt(g.delta)}</td>
        <td class="num">${g.amplification_ratio ? fmt(g.amplification_ratio) + "×" : "—"}</td></tr>`).join("")}
      </tbody></table></div></div>`;
  }

  $("#diagnosis-box").innerHTML = html;

  // 重建区
  const rb = $("#rebuild-box");
  if (d.rebuildable && job && job.status === "OK") {
    rb.innerHTML = `<div class="card">
      <h4>重建受影响报表</h4>
      <p class="muted">当前任务 <code>${esc(job.job_id)}</code> 已是合法路径结果。
        可基于合法路径 ${d.valid_paths.map((v) => `<span class="pill">${v}</span>`).join(" ")} 重建，
        系统将写入新版本任务并把旧任务标记为已取代，旧任务内容不变。</p>
      <button class="primary" onclick="doRebuild('${esc(job.job_id)}')">用合法路径重建（新版本）</button>
      ${supersession ? `<p class="sup-tag" style="margin-top:8px">已建立取代关系：
        ${esc(supersession.old_job_id)} → ${esc(supersession.new_job_id)}</p>` : ""}
    </div>`;
  } else if (!d.rebuildable) {
    rb.innerHTML = `<div class="card"><h4>无法重建</h4>
      <p class="muted">当前登记下无合法计算路径。请先到「粒度登记」修正关系基数或补登分摊基准，
      再重新运行——系统不会对朴素结果做任何事后去重。</p></div>`;
  }
}

window.doRebuild = function (jobId) {
  runAndRender(true, jobId);
};

// ---------------- ⑤ 任务历史（只增）----------------
async function loadJobs() {
  const jobs = await api("/api/jobs");
  if (!jobs.length) { $("#jobs-box").innerHTML = `<p class="muted">尚无任务</p>`; return; }
  const groups = {};
  jobs.forEach((j) => (groups[j.report_name] ||= []).push(j));
  $("#jobs-box").innerHTML = Object.entries(groups).map(([name, list]) => `
    <div class="card">
      <h4>${esc(name)} <span class="pill">${list.length} 个版本</span></h4>
      <div class="table-wrap"><table><thead><tr><th>版本</th><th>任务ID</th><th>路径</th>
      <th>状态</th><th>创建时间</th><th>取代关系</th></tr></thead><tbody>
      ${list.map((j) => `<tr class="${j.new_job_id ? "superseded" : ""}">
        <td class="num">v${j.version}</td><td><code>${esc(j.job_id)}</code></td>
        <td><span class="pill">${esc(j.path)}</span></td>
        <td>${j.status === "OK" ? badge("OK", "ok") : badge(j.status, "reject")}</td>
        <td>${esc(j.created_at)}</td>
        <td>${j.new_job_id ? `<span class="sup-tag">已被 ${esc(j.new_job_id)} v${j.new_version} 取代（记录保留）</span>`
          : badge("当前", "info")}</td></tr>`).join("")}
      </tbody></table></div>
    </div>`).join("");
}

// ---------------- 启动 ----------------
loadRegistry();
initDataPicker();
initReports();
