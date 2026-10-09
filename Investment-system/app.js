/* ============================================================
   投资知识体系 · 知识库交互层
   视图切换（hash 路由）+ 侧边导航 + 章末翻页 + 全局搜索
   无 JS 时自动降级为整页滚动（CSS 渐进增强）
   ============================================================ */

(function () {
  "use strict";

  document.documentElement.classList.add("js");

  /* ---------- 视图定义（顺序即阅读顺序） ---------- */
  const VIEWS = [
    { id: "home",     sections: ["top", "map"], title: "首页 · 体系总览" },
    { id: "ch1",      sections: ["ch1"],        title: "01 市场有效性" },
    { id: "ch2",      sections: ["ch2"],        title: "02 行为金融" },
    { id: "ch3",      sections: ["ch3"],        title: "03 资产配置" },
    { id: "ch4",      sections: ["ch4"],        title: "04 风险管理" },
    { id: "ch5",      sections: ["ch5"],        title: "05 估值与决策" },
    { id: "ch6",      sections: ["ch6"],        title: "06 执行纪律" },
    { id: "summary",  sections: ["summary"],    title: "终章 · 十条规定" },
    { id: "appendix", sections: ["appendix"],   title: "附录 · 指标速查" },
  ];
  const ANCHOR_TO_VIEW = { top: "home", map: "home" };

  /* ---------- DOM 工具 ---------- */
  const $ = (id) => document.getElementById(id);
  const sectionEl = {};
  VIEWS.forEach((v) => v.sections.forEach((sid) => {
    sectionEl[sid] = document.getElementById(sid);
  }));

  const state = { view: null };

  /* ============================================================
     视图切换
     ============================================================ */
  function resolveView(hash) {
    const id = (hash || "").replace(/^#/, "");
    if (!id) return "home";
    if (ANCHOR_TO_VIEW[id]) return ANCHOR_TO_VIEW[id];
    return VIEWS.some((v) => v.id === id) ? id : null;
  }

  function showView(viewId, opts) {
    opts = opts || {};
    const view = VIEWS.find((v) => v.id === viewId);
    if (!view) return;
    state.view = viewId;

    VIEWS.forEach((v) => v.sections.forEach((sid) => {
      sectionEl[sid].classList.toggle("active", v.id === viewId);
    }));

    // 侧边栏高亮
    document.querySelectorAll(".snav").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === viewId);
    });

    // 更新 URL（不触发二次滚动）
    if (location.hash !== "#" + viewId && opts.updateHash !== false) {
      history.replaceState(null, "", "#" + viewId);
    }

    // 回到视图顶部
    window.scrollTo({ top: 0, behavior: opts.instant === false ? "smooth" : "auto" });

    closeDrawer();
    if (opts.then) requestAnimationFrame(opts.then);
  }

  /* ============================================================
     章末翻页（上一章 / 下一章）
     ============================================================ */
  function buildPagers() {
    VIEWS.forEach((v, i) => {
      const host = sectionEl[v.sections[v.sections.length - 1]];
      const pager = document.createElement("nav");
      pager.className = "pager";
      pager.setAttribute("aria-label", "章节导航");

      const prev = VIEWS[i - 1];
      const next = VIEWS[i + 1];
      if (prev) {
        pager.appendChild(makePagerBtn(prev, "上一章", "pager__btn pager__btn--prev"));
      }
      if (next) {
        pager.appendChild(makePagerBtn(next, "下一章", "pager__btn pager__btn--next"));
      }
      host.appendChild(pager);
    });
  }

  function makePagerBtn(view, label, cls) {
    const a = document.createElement("a");
    a.href = "#" + view.id;
    a.className = cls;
    a.innerHTML = '<span class="pager__label">' + label + '</span>' +
                  '<span class="pager__title">' + view.title + "</span>";
    return a;
  }

  /* ============================================================
     页内锚点：统一拦截，切换视图
     ============================================================ */
  document.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#"]');
    if (!a) return;
    const id = a.getAttribute("href").slice(1);
    if (!id) return;

    const viewId = resolveView("#" + id);
    if (viewId && viewId !== state.view) {
      e.preventDefault();
      showView(viewId);
    } else if (viewId === state.view) {
      // 同视图内锚点：平滑滚动到目标（如首页内 #map）
      e.preventDefault();
      const el = document.getElementById(id);
      if (el) el.scrollIntoView({ behavior: "smooth" });
    }
  });

  window.addEventListener("hashchange", () => {
    const viewId = resolveView(location.hash);
    if (viewId && viewId !== state.view) showView(viewId, { updateHash: false });
  });

  /* ============================================================
     移动端抽屉
     ============================================================ */
  const sidebar = $("sidebar");
  const scrim = $("layoutScrim");
  const menuBtn = $("menuBtn");

  function openDrawer() {
    sidebar.classList.add("open");
    scrim.hidden = false;
    menuBtn.setAttribute("aria-expanded", "true");
  }
  function closeDrawer() {
    sidebar.classList.remove("open");
    scrim.hidden = true;
    menuBtn.setAttribute("aria-expanded", "false");
  }
  menuBtn.addEventListener("click", () => {
    sidebar.classList.contains("open") ? closeDrawer() : openDrawer();
  });
  scrim.addEventListener("click", closeDrawer);

  sidebar.addEventListener("click", (e) => {
    const btn = e.target.closest(".snav");
    if (!btn) return;
    showView(btn.dataset.view);
  });

  /* ============================================================
     全局搜索
     ============================================================ */
  const searchInput = $("searchInput");
  const searchWrap = $("searchWrap");
  const searchDropdown = $("searchDropdown");
  const clearBtn = $("clearSearch");

  const TYPE_LABEL = {
    chapter: "章节", card: "概念", acc: "规则",
    stat: "数据", quote: "洞察", row: "条目",
  };

  /* 从 DOM 自动构建索引 — 内容更新无需维护数据文件 */
  const INDEX = [];
  function textOf(el, max) {
    const t = (el.textContent || "").replace(/\s+/g, " ").trim();
    return t.length > max ? t.slice(0, max) + "…" : t;
  }
  function viewOfSection(sid) {
    return VIEWS.find((v) => v.sections.indexOf(sid) > -1);
  }
  function push(view, type, title, snippet, el) {
    if (!title) return;
    INDEX.push({
      view, type, el,
      title: textOf(title, 60),
      snippet: textOf(snippet, 110),
    });
  }

  function buildIndex() {
    VIEWS.forEach((v) => {
      const root = v.sections.map((sid) => sectionEl[sid]).filter(Boolean);
      root.forEach((sec) => {
        // 章标题 + 导语
        const head = sec.querySelector(".chapter-head__title");
        if (head) push(v, "chapter", head, sec.querySelector(".chapter-head__thesis"), sec.querySelector(".chapter-head"));
        // Hero 文案（首页）
        const heroTitle = sec.querySelector(".hero__title");
        if (heroTitle) push(v, "chapter", heroTitle, sec.querySelector(".hero__subtitle"), sec);
        // 概念卡
        sec.querySelectorAll(".card").forEach((c) => {
          push(v, "card", c.querySelector(".card__title"), c.querySelector(".card__body"), c);
        });
        // 手风琴（执行要点 / SOP / 清单）
        sec.querySelectorAll("details.acc").forEach((d) => {
          push(v, "acc", d.querySelector("summary"), d.querySelector(".acc__content"), d);
        });
        // 手风琴分组标题（执行要点 / 例行 SOP / 铁律等）
        sec.querySelectorAll(".accordion__title").forEach((t) => {
          const next = t.nextElementSibling;
          const summary = next && next.querySelector("summary");
          push(v, "acc", t, summary || t, t);
        });
        // 数据卡
        sec.querySelectorAll(".stat").forEach((s) => {
          push(v, "stat", s.querySelector(".stat__value"), s.querySelector(".stat__label"), s);
        });
        // 引块
        sec.querySelectorAll("blockquote.quote").forEach((q) => {
          push(v, "quote", q.childNodes[0], q, q);
        });
        // 表格行（偏差 / 风险 / 指标 / 十条规定；跳过卡片内嵌小表避免噪音）
        sec.querySelectorAll("table tbody tr").forEach((tr) => {
          if (tr.closest(".card")) return;
          const cells = tr.querySelectorAll("td");
          if (cells.length >= 2) {
            push(v, "row", cells[0], cells[1], tr);
          }
        });
      });
    });
  }

  /* 查询与渲染 */
  let results = [];
  let selected = -1;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function hl(s) {
    const t = esc(s);
    const q = searchInput.value.trim();
    if (!q) return t;
    const lq = esc(q).toLowerCase();
    const lower = t.toLowerCase();
    let out = "", i = 0;
    for (;;) {
      const idx = lower.indexOf(lq, i);
      if (idx === -1) { out += t.slice(i); break; }
      out += t.slice(i, idx) + "<mark>" + t.slice(idx, idx + lq.length) + "</mark>";
      i = idx + lq.length;
    }
    return out;
  }

  function doSearch() {
    const q = searchInput.value.trim().toLowerCase();
    searchWrap.classList.toggle("has-value", q.length > 0);
    clearBtn.hidden = !q;

    if (!q) { hideDropdown(); return; }
    results = INDEX.filter((it) =>
      it.title.toLowerCase().includes(q) || it.snippet.toLowerCase().includes(q)
    ).sort((a, b) => {
      const at = a.title.toLowerCase().includes(q) ? 0 : 1;
      const bt = b.title.toLowerCase().includes(q) ? 0 : 1;
      return at - bt;
    }).slice(0, 10);
    selected = results.length ? 0 : -1;
    renderDropdown(q);
  }

  function renderDropdown() {
    searchDropdown.innerHTML = "";
    if (!results.length) {
      const empty = document.createElement("div");
      empty.className = "sd-empty";
      empty.textContent = "没有匹配的内容 — 试试「再平衡」「安全边际」「Kelly」";
      searchDropdown.appendChild(empty);
    } else {
      results.forEach((it, i) => {
        const btn = document.createElement("button");
        btn.className = "sd-item" + (i === selected ? " selected" : "");
        btn.innerHTML =
          '<div class="sd-item__chapter">' + esc(it.view.title) + " · " + TYPE_LABEL[it.type] + "</div>" +
          '<div class="sd-item__title">' + hl(it.title) + "</div>" +
          '<div class="sd-item__snippet">' + hl(it.snippet) + "</div>";
        btn.addEventListener("click", () => pickResult(i));
        searchDropdown.appendChild(btn);
      });
    }
    searchDropdown.hidden = false;
  }

  function hideDropdown() {
    searchDropdown.hidden = true;
    results = [];
    selected = -1;
  }

  function pickResult(i) {
    const it = results[i];
    if (!it) return;
    searchInput.value = "";
    doSearch(); // 清空并收起
    searchInput.blur();

    showView(it.view.id, {
      instant: true,
      then: () => {
        // 若目标在手风琴内，先展开
        let d = it.el.closest("details");
        while (d) { d.open = true; d = d.parentElement ? d.parentElement.closest("details") : null; }
        // 等待展开后的布局
        requestAnimationFrame(() => {
          it.el.scrollIntoView({ behavior: "smooth", block: "center" });
          it.el.classList.remove("search-hit");
          void it.el.offsetWidth; // 重启动画
          it.el.classList.add("search-hit");
          setTimeout(() => it.el.classList.remove("search-hit"), 1800);
        });
      },
    });
  }

  searchInput.addEventListener("input", doSearch);
  searchInput.addEventListener("focus", () => {
    if (searchInput.value.trim()) doSearch();
  });
  clearBtn.addEventListener("click", () => {
    searchInput.value = "";
    doSearch();
    searchInput.focus();
  });

  /* 点击页面其他区域收起下拉 */
  document.addEventListener("click", (e) => {
    if (!searchWrap.contains(e.target)) hideDropdown();
  });

  /* 键盘：/ 聚焦、↑↓ 选择、Enter 确认、Esc 关闭 */
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== searchInput) {
      e.preventDefault();
      searchInput.focus();
      searchInput.select();
      return;
    }
    if (document.activeElement !== searchInput) return;

    if (e.key === "Escape") {
      hideDropdown();
      searchInput.blur();
    } else if (e.key === "ArrowDown" && results.length) {
      e.preventDefault();
      selected = (selected + 1) % results.length;
      renderDropdown();
    } else if (e.key === "ArrowUp" && results.length) {
      e.preventDefault();
      selected = (selected - 1 + results.length) % results.length;
      renderDropdown();
    } else if (e.key === "Enter" && selected > -1) {
      e.preventDefault();
      pickResult(selected);
    }
  });

  /* ============================================================
     初始化
     ============================================================ */
  buildIndex();
  buildPagers();
  const initial = resolveView(location.hash) || "home";
  showView(initial, { updateHash: false, instant: true });
})();
