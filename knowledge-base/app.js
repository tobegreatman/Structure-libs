/* ============================================================
   认知知识库 · 交互逻辑
   依赖：data.js（KNOWLEDGE / INDEX）
   ============================================================ */

(function () {
  "use strict";

  /* ---------- 状态 ---------- */
  const state = {
    mode: "browse",          // browse | review
    activeDomain: null,      // null = 全部
    activeTopic: null,       // null = 该领域全部主题
    search: "",
    // 温习
    reviewScope: "all",      // all | domainId | topicId | weak
    reviewCount: 10,
    reviewQueue: [],
    reviewIndex: 0,
    reviewKnown: 0,
    reviewFuzzy: 0,
  };

  /* 掌握状态持久化（localStorage） */
  const STORE_KEY = "kb-mastered-v1";
  let mastered = new Set();
  try {
    mastered = new Set(JSON.parse(localStorage.getItem(STORE_KEY) || "[]"));
  } catch (e) { /* 忽略损坏数据 */ }
  function saveMastered() {
    localStorage.setItem(STORE_KEY, JSON.stringify([...mastered]));
  }

  /* ---------- DOM 工具 ---------- */
  const $ = (id) => document.getElementById(id);
  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  /* 转义后为搜索词高亮（<mark>），查询词用同一转义保证对齐 */
  function hl(s) {
    const t = esc(s);
    const q = state.search;
    if (!q) return t;
    const eq = esc(q);
    const lower = t.toLowerCase();
    const lq = eq.toLowerCase();
    let out = "", i = 0;
    for (;;) {
      const idx = lower.indexOf(lq, i);
      if (idx === -1) { out += t.slice(i); break; }
      out += t.slice(i, idx) + "<mark>" + t.slice(idx, idx + eq.length) + "</mark>";
      i = idx + eq.length;
    }
    return out;
  }

  /* 概念是否匹配查询词（小写） */
  function conceptMatches(c, lq) {
    return (
      c.name.toLowerCase().includes(lq) ||
      (c.en || "").toLowerCase().includes(lq) ||
      c.one.toLowerCase().includes(lq) ||
      c.points.join(" ").toLowerCase().includes(lq) ||
      c.trap.toLowerCase().includes(lq) ||
      c.apply.toLowerCase().includes(lq)
    );
  }

  /* ---------- 全部卡片展平 ---------- */
  const ALL = [];
  KNOWLEDGE.domains.forEach(d =>
    d.topics.forEach(t =>
      t.concepts.forEach(c => ALL.push({ domain: d, topic: t, concept: c }))
    )
  );

  /* ============================================================
     侧边导航树
     ============================================================ */
  function buildNav() {
    const tree = $("navTree");
    tree.innerHTML = "";

    // 全部
    const allBtn = el("button", "nav-domain all" + (state.activeDomain == null ? " active" : ""));
    allBtn.innerHTML = `
      <span class="nav-caret-spacer"></span>
      <span class="nav-name">全部概念</span>
      <span class="nav-count">${ALL.length}</span>`;
    allBtn.addEventListener("click", () => {
      state.activeDomain = null;
      state.activeTopic = null;
      updateNavState();
      renderBrowse();
    });
    tree.appendChild(allBtn);

    // 领域 → 主题
    KNOWLEDGE.domains.forEach(d => {
      const dWrap = el("div", "nav-domain-wrap");

      const dBtn = el("button", "nav-domain" + (state.activeDomain === d.id ? " active" : ""));
      dBtn.dataset.domain = d.id;
      const count = d.topics.reduce((s, t) => s + t.concepts.length, 0);
      const expanded = state.activeDomain === d.id;
      dBtn.innerHTML = `
        <span class="nav-caret${expanded ? " open" : ""}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>
        </span>
        <span class="nav-name">${esc(d.name)}</span>
        <span class="nav-count">${count}</span>`;
      dBtn.addEventListener("click", () => {
        if (state.activeDomain === d.id) {
          // 点击已激活领域 → 收起（回到全部）
          state.activeDomain = null;
          state.activeTopic = null;
        } else {
          state.activeDomain = d.id;
          state.activeTopic = null;
        }
        updateNavState();
        renderBrowse();
      });
      dWrap.appendChild(dBtn);

      const tList = el("div", "nav-topics");
      tList.dataset.domain = d.id;
      const tInner = el("div", "nav-topics-inner");
      d.topics.forEach(t => {
        const tBtn = el("button", "nav-topic" + (state.activeTopic === t.id ? " active" : ""));
        tBtn.dataset.topic = t.id;
        tBtn.innerHTML = `<span class="nav-name">${esc(t.name)}</span><span class="nav-count">${t.concepts.length}</span>`;
        tBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          state.activeDomain = d.id;
          state.activeTopic = t.id;
          updateNavState();
          renderBrowse();
        });
        tInner.appendChild(tBtn);
      });
      tList.appendChild(tInner);
      dWrap.appendChild(tList);
      tree.appendChild(dWrap);
    });
  }

  function updateNavState() {
    document.querySelectorAll(".nav-domain").forEach(b => {
      const isDomain = b.dataset.domain;
      const active = isDomain
        ? (state.activeDomain === isDomain || (state.activeTopic && belongsToDomain(isDomain)))
        : state.activeDomain == null;
      b.classList.toggle("active", active);
      const caret = b.querySelector(".nav-caret");
      if (caret) caret.classList.toggle("open", state.activeDomain === isDomain);
    });
    document.querySelectorAll(".nav-topics").forEach(list => {
      const open = state.activeDomain === list.dataset.domain;
      list.classList.toggle("open", open);
    });
    document.querySelectorAll(".nav-topic").forEach(b => {
      b.classList.toggle("active", state.activeTopic === b.dataset.topic);
    });
  }

  function belongsToDomain(domainId) {
    const d = KNOWLEDGE.domains.find(x => x.id === domainId);
    return d && d.topics.some(t => t.id === state.activeTopic);
  }

  /* ============================================================
     浏览模式：卡片渲染
     ============================================================ */
  function currentCards() {
    let list = ALL;
    if (state.activeTopic) {
      list = list.filter(x => x.topic.id === state.activeTopic);
    } else if (state.activeDomain) {
      list = list.filter(x => x.domain.id === state.activeDomain);
    }
    if (state.search) {
      const lq = state.search.toLowerCase();
      list = list.filter(x => conceptMatches(x.concept, lq));
    }
    return list;
  }

  function renderBrowse() {
    const area = $("cardArea");
    area.innerHTML = "";
    const cards = currentCards();

    // 页头
    const title = $("pageTitle"), desc = $("pageDesc");
    if (state.search) {
      title.textContent = `搜索：“${state.search}”`;
      desc.textContent = cards.length ? `找到 ${cards.length} 张卡片` : "没有匹配的概念";
    } else if (state.activeTopic) {
      const hit = ALL.find(x => x.topic.id === state.activeTopic);
      title.textContent = hit.topic.name;
      desc.textContent = `${hit.domain.name} · ${cards.length} 张卡片`;
    } else if (state.activeDomain) {
      const d = KNOWLEDGE.domains.find(x => x.id === state.activeDomain);
      title.textContent = d.name;
      desc.textContent = d.tagline;
    } else {
      title.textContent = "全部概念";
      desc.textContent = "所有领域 · 按主题分组";
    }

    if (!cards.length) {
      const box = el("div", "empty-state",
        `<p>没有找到匹配的概念</p>
         <p class="empty-sub">试试其他关键词，或清除搜索</p>
         <div class="empty-suggest">
           ${["复利", "偏差", "风险", "估值", "周期", "决策"].map(s =>
             `<button class="suggest-chip" data-s="${s}">${s}</button>`).join("")}
         </div>`);
      box.querySelectorAll(".suggest-chip").forEach(b => {
        b.addEventListener("click", () => {
          searchInput.value = b.dataset.s;
          searchInput.dispatchEvent(new Event("input"));
        });
      });
      area.appendChild(box);
      return;
    }

    // 分组（保持领域→主题顺序）
    const groups = new Map();
    cards.forEach(x => {
      const key = x.domain.id + "/" + x.topic.id;
      if (!groups.has(key)) groups.set(key, { domain: x.domain, topic: x.topic, items: [] });
      groups.get(key).items.push(x);
    });

    const frag = document.createDocumentFragment();
    groups.forEach(g => {
      const groupHead = el("div", "group-head",
        `<span class="group-topic">${esc(g.topic.name)}</span>
         <span class="group-domain">${esc(g.domain.name)}</span>
         <span class="group-count">${g.items.length}</span>`);
      frag.appendChild(groupHead);

      const grid = el("div", "card-grid");
      g.items.forEach(x => grid.appendChild(buildCard(x)));
      frag.appendChild(grid);
    });
    area.appendChild(frag);
  }

  /* ============================================================
     内嵌计算工具：让“应用”可以当场动手算
     ============================================================ */
  function fmtMoney(x) {
    if (!isFinite(x)) return "—";
    if (x >= 1e8) return (x / 1e8).toFixed(2) + " 亿";
    if (x >= 1e4) return (x / 1e4).toFixed(1) + " 万";
    return Math.round(x).toLocaleString("zh-CN");
  }

  const TOOLS = {
    compound: {
      title: "动手算 · 收益宣称过数学关",
      hint: "输入宣称的年化与年限，看终值是否离谱",
      inputs: [
        { key: "p", label: "初始资金", unit: "元", def: 100000, step: 1000, min: 0 },
        { key: "r", label: "年化收益", unit: "%", def: 15, step: 0.5, min: -99, max: 95 },
        { key: "n", label: "年限", unit: "年", def: 10, step: 1, min: 1, max: 50 }
      ],
      calc(v) {
        const g = 1 + v.r / 100;
        if (g <= 0) return "年化 -100% 意味着本金归零——没有终值。";
        const end = v.p * Math.pow(g, v.n);
        const mult = Math.pow(g, v.n);
        const base = Math.pow(1.2, v.n);
        let out = `终值 <b>${fmtMoney(end)}</b>（${mult.toFixed(1)} 倍）`;
        if (v.p > 0) out += ` · 对照 20% 顶级年化同期为 ${fmtMoney(v.p * base)}（${base.toFixed(1)} 倍）`;
        if (v.r > 25) out += ` · <span class="tool-warn">长期年化 &gt; 25%，历史上几乎无人持续做到</span>`;
        return out;
      }
    },
    position: {
      title: "动手算 · 仓位反推",
      hint: "先定最多亏多少，再倒推最多买多少",
      inputs: [
        { key: "cap", label: "总资金", unit: "元", def: 100000, step: 1000, min: 1 },
        { key: "loss", label: "单笔最大亏损", unit: "% 总资金", def: 1, step: 0.5, min: 0.1, max: 100 },
        { key: "drop", label: "预计最大跌幅", unit: "%", def: 20, step: 1, min: 1, max: 100 }
      ],
      calc(v) {
        const riskAmt = v.cap * v.loss / 100;
        const pos = riskAmt / (v.drop / 100);
        const pct = pos / v.cap * 100;
        let out = `最多亏 <b>${fmtMoney(riskAmt)}</b> → 建议仓位 <b>${fmtMoney(pos)}</b>（占总资金 ${pct.toFixed(1)}%）`;
        if (pct > 100) out += ` · <span class="tool-warn">超出总资金：该跌幅假设下放不下这笔风险，需降低仓位或放弃</span>`;
        return out;
      }
    },
    kelly: {
      title: "动手算 · 凯利仓位",
      hint: "胜率与赔率 → 理论最优下注比例",
      inputs: [
        { key: "p", label: "胜率", unit: "%", def: 55, step: 1, min: 1, max: 99 },
        { key: "b", label: "盈亏比", unit: "赢:输", def: 2, step: 0.1, min: 0.1 }
      ],
      calc(v) {
        const f = (v.p / 100 * (v.b + 1) - 1) / v.b;
        if (f <= 0) return `凯利比例 ≤ 0：<b>负期望</b>——数学上不值得下注，无论故事多动听。`;
        return `凯利比例 <b>${(f * 100).toFixed(1)}%</b> · 实践中常用半凯利 <b>${(f * 50).toFixed(1)}%</b>（牺牲部分收益换波动可控）`;
      }
    }
  };

  function renderToolHTML(type) {
    const t = TOOLS[type];
    return `
        <div class="card-section tool">
          <h4>${t.title}</h4>
          <p class="tool-hint">${t.hint}</p>
          <div class="tool-inputs">
            ${t.inputs.map(i => `
            <label class="tool-field">
              <span class="tool-label">${i.label}<em>${i.unit}</em></span>
              <input class="tool-input" type="number" data-key="${i.key}" value="${i.def}" step="${i.step}"${i.min != null ? ` min="${i.min}"` : ""}${i.max != null ? ` max="${i.max}"` : ""} />
            </label>`).join("")}
          </div>
          <p class="tool-result" data-tool-result></p>
        </div>`;
  }

  function initTool(card, concept) {
    const t = TOOLS[concept.tool];
    const inputs = card.querySelectorAll(".tool-input");
    const out = card.querySelector("[data-tool-result]");
    const read = () => {
      const v = {};
      inputs.forEach(inp => { v[inp.dataset.key] = parseFloat(inp.value) || 0; });
      out.innerHTML = t.calc(v);
    };
    inputs.forEach(inp => inp.addEventListener("input", read));
    read();
  }

  function buildCard({ domain, topic, concept }) {
    const isMastered = mastered.has(concept.id);
    const card = el("article", "card" + (isMastered ? " mastered" : ""));
    card.dataset.id = concept.id;

    card.innerHTML = `
      <header class="card-head" tabindex="0" role="button" aria-expanded="false">
        <div class="card-title">
          <h3>${hl(concept.name)}</h3>
          ${concept.en ? `<span class="card-en">${hl(concept.en)}</span>` : ""}
        </div>
        <div class="card-side">
          ${isMastered ? '<span class="mastered-dot" title="已掌握"></span>' : ""}
          <svg class="card-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
        </div>
      </header>
      <p class="card-one">${hl(concept.one)}</p>
      <div class="card-body">
        <div class="card-section">
          <h4>核心要点</h4>
          <ul>${concept.points.map(p => `<li>${hl(p)}</li>`).join("")}</ul>
        </div>
        ${concept.diagram ? `
        <div class="card-section diagram">
          <h4>图解</h4>
          <div class="diagram-panel">${concept.diagram}</div>
        </div>` : ""}
        <div class="card-section trap">
          <h4>常见误区</h4>
          <p>${hl(concept.trap)}</p>
        </div>
        <div class="card-section apply">
          <h4>应用</h4>
          <p>${hl(concept.apply)}</p>
        </div>
        ${concept.tool && TOOLS[concept.tool] ? renderToolHTML(concept.tool) : ""}
        ${concept.links && concept.links.length ? `
        <div class="card-section links">
          <h4>关联</h4>
          <div class="link-chips">
            ${concept.links.map(l => `<button class="link-chip" data-link="${esc(l)}">${esc(l)}</button>`).join("")}
          </div>
        </div>` : ""}
        <div class="card-tools">
          <button class="tool-toggle mastered-toggle" data-id="${esc(concept.id)}">
            ${isMastered ? "✓ 已掌握（点击取消）" : "标记为已掌握"}
          </button>
          <span class="card-topic-tag">${esc(topic.name)}</span>
        </div>
      </div>`;

    // 展开 / 收起
    const head = card.querySelector(".card-head");
    const toggle = () => {
      card.classList.toggle("expanded");
      head.setAttribute("aria-expanded", card.classList.contains("expanded"));
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });

    // 掌握标记
    card.querySelector(".mastered-toggle").addEventListener("click", (e) => {
      e.stopPropagation();
      if (mastered.has(concept.id)) mastered.delete(concept.id);
      else mastered.add(concept.id);
      saveMastered();
      refreshCardStates();
      updateStats();
    });

    // 关联跳转
    card.querySelectorAll(".link-chip").forEach(chip => {
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        jumpToConcept(chip.dataset.link);
      });
    });

    // 内嵌计算工具
    if (concept.tool && TOOLS[concept.tool]) initTool(card, concept);

    return card;
  }

  function jumpToConcept(name) {
    const hit = INDEX[name];
    if (!hit) return;
    closeDropdown();
    state.search = "";
    $("searchInput").value = "";
    toggleClearBtn();
    state.activeDomain = hit.domain.id;
    state.activeTopic = hit.topic.id;
    updateNavState();
    renderBrowse();
    // 展开目标卡片
    const card = document.querySelector(`.card[data-id="${hit.concept.id}"]`);
    if (card) {
      card.classList.add("expanded", "flash");
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => card.classList.remove("flash"), 1600);
    }
  }

  function refreshCardStates() {
    document.querySelectorAll(".card").forEach(card => {
      const id = card.dataset.id;
      const isM = mastered.has(id);
      card.classList.toggle("mastered", isM);
      const dot = card.querySelector(".card-side");
      let d = card.querySelector(".mastered-dot");
      if (isM && !d) {
        d = el("span", "mastered-dot");
        d.title = "已掌握";
        dot.insertBefore(d, dot.firstChild);
      } else if (!isM && d) {
        d.remove();
      }
      const btn = card.querySelector(".mastered-toggle");
      if (btn) btn.textContent = isM ? "✓ 已掌握（点击取消）" : "标记为已掌握";
    });
  }

  /* ---------- 统计 ---------- */
  function updateStats() {
    $("statTotal").textContent = ALL.length;
    $("statMastered").textContent = mastered.size;
    const pct = ALL.length ? Math.round((mastered.size / ALL.length) * 100) : 0;
    $("progressFill").style.width = pct + "%";
    const weak = ALL.filter(x => !mastered.has(x.concept.id)).length;
    const note = $("reviewNote");
    if (note) {
      note.textContent = weak === ALL.length
        ? "还没有标记过“已掌握”，温习将随机抽取全部卡片"
        : `还有 ${weak} 张卡片未标记掌握 · 温习优先抽取它们`;
    }
  }

  /* ============================================================
     搜索（高亮 + 快速跳转下拉 + 键盘导航）
     ============================================================ */
  const searchInput = $("searchInput");
  let ddItems = [];   // 下拉候选
  let ddIndex = -1;   // 当前选中项

  function closeDropdown() {
    const dd = $("searchDropdown");
    dd.hidden = true;
    dd.innerHTML = "";
    ddIndex = -1;
  }

  function renderDropdown() {
    const dd = $("searchDropdown");
    if (!state.search || state.mode !== "browse" || !ddItems.length) {
      dd.hidden = true;
      return;
    }
    dd.hidden = false;
    dd.innerHTML = ddItems.map((x, i) => `
      <button class="dd-item${i === ddIndex ? " active" : ""}" data-i="${i}">
        <span class="dd-name">${hl(x.concept.name)}</span>
        <span class="dd-topic">${esc(x.topic.name)} · ${esc(x.domain.name)}</span>
      </button>`).join("") +
      `<div class="dd-foot"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 跳转</span><span><kbd>Esc</kbd> 关闭</span></div>`;
    dd.querySelectorAll(".dd-item").forEach(btn => {
      // mousedown + preventDefault：抢在 input blur 前触发跳转
      btn.addEventListener("mousedown", (e) => {
        e.preventDefault();
        jumpFromDropdown(ddItems[+btn.dataset.i]);
      });
    });
  }

  function jumpFromDropdown(item) {
    closeDropdown();
    searchInput.blur();
    jumpToConcept(item.concept.name);
  }

  searchInput.addEventListener("input", () => {
    state.search = searchInput.value.trim();
    toggleClearBtn();
    if (state.mode === "review") { closeDropdown(); return; }
    const lq = state.search.toLowerCase();
    ddItems = lq ? ALL.filter(x => conceptMatches(x.concept, lq)).slice(0, 8) : [];
    ddIndex = ddItems.length ? 0 : -1;
    renderDropdown();
    renderBrowse();
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) closeDropdown();
  });

  document.addEventListener("keydown", (e) => {
    const inSearch = document.activeElement === searchInput;

    // --- 搜索框内：下拉键盘导航 ---
    if (inSearch) {
      const ddOpen = !$("searchDropdown").hidden;
      if (e.key === "ArrowDown" && ddOpen) {
        e.preventDefault();
        ddIndex = Math.min(ddIndex + 1, ddItems.length - 1);
        renderDropdown();
      } else if (e.key === "ArrowUp" && ddOpen) {
        e.preventDefault();
        ddIndex = Math.max(ddIndex - 1, 0);
        renderDropdown();
      } else if (e.key === "Enter" && ddOpen && ddItems[ddIndex]) {
        e.preventDefault();
        jumpFromDropdown(ddItems[ddIndex]);
      } else if (e.key === "Escape") {
        closeDropdown();
      }
      return;
    }

    // --- 温习模式：全键盘操作 ---
    if (state.mode === "review" && !$("reviewSession").hidden) {
      handleReviewKeys(e);
      return;
    }

    if (e.key === "/") {
      e.preventDefault();
      searchInput.focus();
    }
  });
  function toggleClearBtn() {
    $("clearSearch").hidden = !searchInput.value;
  }
  $("clearSearch").addEventListener("click", () => {
    searchInput.value = "";
    state.search = "";
    toggleClearBtn();
    renderBrowse();
    searchInput.focus();
  });

  /* ============================================================
     模式切换
     ============================================================ */
  document.querySelectorAll(".mode-btn").forEach(btn => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
  function setMode(mode) {
    if (state.mode === mode) return;
    state.mode = mode;
    closeDropdown();
    document.querySelectorAll(".mode-btn").forEach(b =>
      b.classList.toggle("active", b.dataset.mode === mode));
    $("browseView").hidden = mode !== "browse";
    $("reviewView").hidden = mode !== "review";
    if (mode === "review") resetReviewUI();
  }

  /* ============================================================
     温习模式
     ============================================================ */
  function resetReviewUI() {
    $("reviewSetup").hidden = false;
    $("reviewSession").hidden = true;
    $("reviewSummary").hidden = true;
    buildScopeChips();
    updateStats();
  }

  function buildScopeChips() {
    const box = $("scopeChips");
    box.innerHTML = "";
    const scopes = [
      { id: "all", label: "全部" },
      { id: "weak", label: "未掌握优先" },
      ...KNOWLEDGE.domains.map(d => ({ id: d.id, label: d.name }))
    ];
    scopes.forEach(s => {
      const chip = el("button", "chip" + (state.reviewScope === s.id ? " active" : ""));
      chip.textContent = s.label;
      chip.dataset.scope = s.id;
      chip.addEventListener("click", () => {
        state.reviewScope = s.id;
        box.querySelectorAll(".chip").forEach(c => c.classList.toggle("active", c.dataset.scope === s.id));
      });
      box.appendChild(chip);
    });
  }
  $("countChips").querySelectorAll(".chip").forEach(chip => {
    chip.addEventListener("click", () => {
      state.reviewCount = parseInt(chip.dataset.count, 10);
      $("countChips").querySelectorAll(".chip").forEach(c =>
        c.classList.toggle("active", c === chip));
    });
  });

  $("startReview").addEventListener("click", startReview);

  function pickReviewCards() {
    let pool = ALL.slice();
    const scope = state.reviewScope;
    if (scope === "weak") {
      // 未掌握优先，不足时用已掌握补齐
      const weak = ALL.filter(x => !mastered.has(x.concept.id));
      const known = ALL.filter(x => mastered.has(x.concept.id));
      shuffle(known);
      pool = weak.concat(known);
    } else if (scope !== "all") {
      const d = KNOWLEDGE.domains.find(x => x.id === scope);
      pool = d ? ALL.filter(x => x.domain.id === scope) : ALL;
    } else {
      shuffle(pool);
    }
    if (scope === "weak") shuffle(pool.filter(x => !mastered.has(x.concept.id))); // 未掌握部分也随机
    if (state.reviewCount > 0) pool = pool.slice(0, state.reviewCount);
    return pool;
  }

  function shuffle(a) {
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  /* 本轮内已重排过的模糊卡（每张最多重现一次） */
  const requeued = new Set();

  function startReview() {
    const cards = pickReviewCards();
    if (!cards.length) {
      $("reviewNote").textContent = "没有可温习的卡片（该范围内全部已掌握 ✓）";
      return;
    }
    state.reviewQueue = cards;
    state.reviewIndex = 0;
    state.reviewKnown = 0;
    state.reviewFuzzy = 0;
    requeued.clear();
    $("reviewSetup").hidden = true;
    $("reviewSession").hidden = false;
    $("reviewSummary").hidden = true;
    $("reviewTotal").textContent = cards.length;
    // 移走按钮焦点：避免残留焦点让空格键再次触发“开始温习”
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    showReviewCard();
  }

  function showReviewCard() {
    const item = state.reviewQueue[state.reviewIndex];
    const c = item.concept;
    $("reviewPos").textContent = state.reviewIndex + 1;
    $("reviewFill").style.width = ((state.reviewIndex) / state.reviewQueue.length * 100) + "%";

    // 正面
    $("reviewTopicBadge").textContent = item.topic.name;
    $("reviewQuestion").textContent = c.name;

    // 背面内容
    $("reviewAnswerBadge").textContent = item.topic.name;
    $("reviewAnswerTitle").textContent = c.name + (c.en ? " · " + c.en : "");
    $("reviewOne").textContent = c.one;
    $("reviewPoints").innerHTML = c.points.map(p => `<li>${esc(p)}</li>`).join("");
    $("reviewTrap").textContent = c.trap;

    // 复位翻转状态
    const card = $("reviewCard");
    card.classList.remove("revealed", "leaving");
    // 触发进场动画
    card.classList.remove("enter");
    void card.offsetWidth;
    card.classList.add("enter");
  }

  $("revealBtn").addEventListener("click", () => {
    $("reviewCard").classList.add("revealed");
  });

  $("markKnown").addEventListener("click", () => markCard(true));
  $("markFuzzy").addEventListener("click", () => markCard(false));

  /* 温习快捷键：空格揭晓 · 1/← 模糊 · 2/→ 记住 · 完成后 Enter 再来一轮 */
  function handleReviewKeys(e) {
    if (!$("reviewSummary").hidden) {
      if (e.key === "Enter") { e.preventDefault(); $("againBtn").click(); }
      return;
    }
    if ($("reviewCard").hidden) return;
    const revealed = $("reviewCard").classList.contains("revealed");
    if ((e.key === " " || e.key === "Enter") && !revealed) {
      e.preventDefault();
      $("revealBtn").click();
    } else if (revealed && (e.key === "1" || e.key === "ArrowLeft")) {
      e.preventDefault();
      $("markFuzzy").click();
    } else if (revealed && (e.key === "2" || e.key === "ArrowRight")) {
      e.preventDefault();
      $("markKnown").click();
    }
  }

  function markCard(known) {
    // 标记按钮点击后立即失焦：防止动画期间空格键误触发同一按钮跳卡
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    const item = state.reviewQueue[state.reviewIndex];
    if (known) {
      mastered.add(item.concept.id);
      state.reviewKnown++;
    } else {
      state.reviewFuzzy++;
      // 模糊的卡片追加到本轮末尾再考一次（每张一次），强化即时巩固
      if (!requeued.has(item.concept.id)) {
        requeued.add(item.concept.id);
        state.reviewQueue.push(item);
        $("reviewTotal").textContent = state.reviewQueue.length;
      }
    }
    saveMastered();
    updateStats();

    const card = $("reviewCard");
    card.classList.add("leaving");
    setTimeout(() => {
      state.reviewIndex++;
      if (state.reviewIndex >= state.reviewQueue.length) {
        finishReview();
      } else {
        showReviewCard();
      }
    }, 320);
  }

  function finishReview() {
    $("reviewFill").style.width = "100%";
    $("sumKnown").textContent = state.reviewKnown;
    $("sumFuzzy").textContent = state.reviewFuzzy;
    const card = $("reviewCard");
    card.hidden = true;
    $("reviewSummary").hidden = false;
  }

  $("againBtn").addEventListener("click", () => {
    $("reviewCard").hidden = false;
    startReview();
  });
  $("exitReview").addEventListener("click", () => setMode("browse"));

  /* ============================================================
     初始化
     ============================================================ */
  $("footerDate").textContent = "更新于 " + new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "long" });
  buildNav();
  renderBrowse();
  updateStats();
})();
