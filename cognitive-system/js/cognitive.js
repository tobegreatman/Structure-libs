/* ==========================================================================
   CognitionOS · 应用逻辑
   知识图谱交互 / 心智模型筛选 / 卡片捕获与本地持久化 / 滚动动效
   ========================================================================== */

(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var STORE_KEY = "cognitionos_notes_v1";

  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* ------------------------------------------------------------------
   * 一、数据
   * ------------------------------------------------------------------ */

  /* 知识图谱：1 个枢纽 + 6 个子系统 + 12 个概念 */
  var GRAPH_NODES = [
    { id: "cognition", label: "认知系统", x: 500, y: 320, r: 46, tier: "hub", cat: "核心枢纽",
      desc: "整个体系的中央枢纽。感知、记忆、思维、知识、元认知与输出六大子系统在此协同，构成一条完整的信息处理回路。" },
    { id: "metacognition", label: "元认知", x: 500, y: 88, r: 31, tier: "sat", cat: "监控系统",
      desc: "对认知的认知。它监控系统本身的运行状态，决定何时切换策略、如何分配注意力——是智力的方向盘。" },
    { id: "perception", label: "感知", x: 232, y: 168, r: 31, tier: "sat", cat: "输入系统",
      desc: "信息的入口。注意力决定哪些信号进入认知系统，观察的质量决定原始数据的保真度。" },
    { id: "memory", label: "记忆", x: 232, y: 472, r: 31, tier: "sat", cat: "存储系统",
      desc: "信息的仓库与检索系统。工作记忆是狭小的临时缓冲区，长期记忆是近乎无限的知识仓库。" },
    { id: "thinking", label: "思维", x: 768, y: 168, r: 31, tier: "sat", cat: "处理系统",
      desc: "信息的加工车间。推理、批判与直觉在此交织，把原始信息加工为判断与洞见。" },
    { id: "knowledge", label: "知识", x: 768, y: 472, r: 31, tier: "sat", cat: "结构层",
      desc: "被组织与压缩后的信息结构。知识不是信息的堆叠，而是彼此连接的意义网络。" },
    { id: "output", label: "输出", x: 500, y: 552, r: 31, tier: "sat", cat: "输出系统",
      desc: "知识的出口，也是最强的理解检验器。输出倒逼输入，让模糊的理解无所遁形。" },
    { id: "attention", label: "注意", x: 76, y: 92, r: 16, tier: "leaf", cat: "概念",
      desc: "认知的聚光灯。注意力是稀缺资源——你关注什么，什么就进入你的世界。" },
    { id: "observation", label: "观察", x: 68, y: 248, r: 16, tier: "leaf", cat: "概念",
      desc: "不带预设地看。高质量输入，始于对细节与异常的敏感。" },
    { id: "working-memory", label: "工作记忆", x: 72, y: 396, r: 16, tier: "leaf", cat: "概念",
      desc: "意识的临时工作台，一次只能容纳 4±1 个组块。组块化是扩展它的唯一方式。" },
    { id: "long-memory", label: "长期记忆", x: 92, y: 576, r: 16, tier: "leaf", cat: "概念",
      desc: "经由编码与巩固形成。提取练习比重复阅读更能强化记忆痕迹。" },
    { id: "reasoning", label: "推理", x: 924, y: 92, r: 16, tier: "leaf", cat: "概念",
      desc: "从已知走向未知的桥梁：演绎给出必然，归纳给出可能，溯因给出解释。" },
    { id: "critical", label: "批判性思维", x: 932, y: 248, r: 16, tier: "leaf", cat: "概念",
      desc: "对思考本身的审视：这个结论的证据是什么？还有别的解释吗？" },
    { id: "mental-model", label: "心智模型", x: 928, y: 396, r: 16, tier: "leaf", cat: "概念",
      modelsLink: true,
      desc: "跨领域可复用的思维框架，是知识的晶体形态。手中的模型越多，视角越多。" },
    { id: "practice", label: "刻意练习", x: 908, y: 576, r: 16, tier: "leaf", cat: "概念",
      desc: "在能力边缘进行针对性训练并获取即时反馈——通往专长的唯一可靠路径。" },
    { id: "reflection", label: "反思", x: 330, y: 34, r: 16, tier: "leaf", cat: "概念",
      desc: "经验的复盘与再解释。未经审视的经验不是经验，只是经历。" },
    { id: "monitoring", label: "自我监控", x: 670, y: 34, r: 16, tier: "leaf", cat: "概念",
      desc: "实时觉察自己的理解程度与偏差，在错误固化之前发出警报。" },
    { id: "writing", label: "写作", x: 330, y: 612, r: 16, tier: "leaf", cat: "概念",
      desc: "把网状的思想线性地展开。写作即思考本身，而非思考的记录。" },
    { id: "teaching", label: "教授他人", x: 670, y: 612, r: 16, tier: "leaf", cat: "概念",
      desc: "费曼技巧：如果你不能简单地解释它，说明你还没有真正理解它。" }
  ];

  /* [起点, 终点, 类型] —— cross 为跨域虚线连接 */
  var GRAPH_EDGES = [
    ["cognition", "metacognition"], ["cognition", "perception"], ["cognition", "memory"],
    ["cognition", "thinking"], ["cognition", "knowledge"], ["cognition", "output"],
    ["perception", "attention"], ["perception", "observation"],
    ["memory", "working-memory"], ["memory", "long-memory"],
    ["thinking", "reasoning"], ["thinking", "critical"],
    ["knowledge", "mental-model"], ["knowledge", "practice"],
    ["metacognition", "reflection"], ["metacognition", "monitoring"],
    ["output", "writing"], ["output", "teaching"],
    ["metacognition", "thinking", "cross"], ["metacognition", "memory", "cross"],
    ["knowledge", "output", "cross"], ["memory", "output", "cross"],
    ["thinking", "knowledge", "cross"], ["perception", "thinking", "cross"]
  ];

  /* 心智模型库 */
  var MODELS = [
    { name: "第一性原理", cat: "思维方式", desc: "把问题拆解到不可再分的基本事实，再从头向上推理，而不是类比式地模仿。" },
    { name: "二阶思维", cat: "决策", desc: "总是多问一句「然后呢？」——考虑行为引发的连锁反应与长期后果。" },
    { name: "机会成本", cat: "决策", desc: "每一次选择的真实价格，是你为此放弃的最佳替代选项。" },
    { name: "沉没成本", cat: "决策", desc: "已经付出且无法回收的成本，不应影响接下来的任何决策。" },
    { name: "确认偏误", cat: "认知偏差", desc: "我们主动寻找支持自己观点的证据，而对相反的信号视而不见。" },
    { name: "系统思维", cat: "系统论", desc: "关注要素之间的连接与反馈回路，而不是孤立地看待每个事件。" },
    { name: "复利效应", cat: "增长", desc: "微小的持续改进经过时间累积，产生指数级的回报。" },
    { name: "反脆弱", cat: "系统论", desc: "不仅抵御冲击，更要从波动与不确定中受益。" },
    { name: "帕累托法则", cat: "效率", desc: "80% 的结果来自 20% 的关键投入——找到杠杆点，其余交给系统。" },
    { name: "熵增定律", cat: "系统论", desc: "无序是默认趋势。维持秩序需要持续输入能量：整理、复习、重构。" },
    { name: "邓宁-克鲁格效应", cat: "认知偏差", desc: "知道得越少，越容易高估自己；知道得越多，越清楚边界在哪。" },
    { name: "飞轮效应", cat: "增长", desc: "从静止到转动最难；一旦转起来，自势能将驱动增长。" }
  ];

  /* 预置知识卡片 —— 覆盖六大子系统，每张为可独立记忆的原子洞察 */
  var SEED_NOTES = [
    /* 元认知 */
    { id: "seed-16", date: "8月25日", text: "Kahneman 双系统：系统 1 快而省力靠直觉，系统 2 慢而费力靠推理。多数错误，发生在该启动系统 2 时却让系统 1 替你拍板——自我监控，就是在这道关上设一道岗。", tags: ["元认知", "自我监控"] },
    { id: "seed-15", date: "8月25日", text: "反思是把经验重读一遍。未经反思的经验只是经历——它发生过，却不曾真正属于你。", tags: ["元认知", "反思"] },

    /* 感知 */
    { id: "seed-14", date: "8月25日", text: "观察的最高级形式，是看到「本应有人看到却没人看到」的异常——信号不在噪声里，而在我们对噪声的麻木里。注意力决定看到什么。", tags: ["感知", "观察", "注意力"] },
    { id: "seed-4", date: "8月24日", text: "注意力是认知的货币。每一次通知打断，都是一次微小的破产——专注不是状态，是资产。", tags: ["注意力"] },

    /* 记忆 */
    { id: "seed-13", date: "8月24日", text: "组块化是把信息打包的艺术。11 位随机数字记不住，「139-1234-5678」却只需三口——工作记忆的瓶颈，要靠组块去撬动。", tags: ["记忆", "工作记忆"] },
    { id: "seed-2", date: "8月21日", text: "艾宾浩斯遗忘曲线：24 小时内约 70% 的新记忆会被侵蚀——除非你在遗忘之前，主动回忆一次。提取练习比重复阅读更能强化长期记忆。", tags: ["记忆", "长期记忆"] },

    /* 思维 */
    { id: "seed-12", date: "8月24日", text: "推理的三种姿态：演绎给出必然，归纳给出可能，溯因给出解释。选错姿态，结论必错——批判性思维，始于知道自己在做哪种推理。", tags: ["思维", "推理", "批判性思维"] },
    { id: "seed-11", date: "8月23日", text: "批判性思维的两句追问：这个结论的证据是什么？还有别的解释吗？能稳定问出这两句的人，已比多数人接近真相。", tags: ["思维", "批判性思维"] },

    /* 知识 */
    { id: "seed-10", date: "8月23日", text: "心智模型是知识的晶体形态。手中的模型越多，看同一件事的视角就越多——拿锤子的人看什么都像钉子，是因为他工具箱里只有一把锤子。", tags: ["知识", "心智模型"] },
    { id: "seed-9", date: "8月22日", text: "刻意练习不在舒适区，也不在恐慌区，而在能力边缘的拉伸区——针对性训练加即时反馈，是通往专长的唯一可靠路径。", tags: ["知识", "刻意练习"] },

    /* 输出 */
    { id: "seed-8", date: "8月22日", text: "写作不是思考的记录，写作即思考本身。把网状思想线性展开的那一刻，逻辑的缺口才会暴露——模糊的想法，写不出。", tags: ["输出", "写作"] },
    { id: "seed-3", date: "8月21日", text: "教授他人是最诚实的理解测试：如果你不能用简单的语言把它讲给外行听懂，说明理解还有缺口。费曼技巧，本质是逼出你的盲区。", tags: ["输出", "教授他人"] },

    /* 跨系统 · 系统论与增长 */
    { id: "seed-7", date: "8月20日", text: "贝叶斯更新：新证据到来时按比例修正旧信念。既不为新证据翻盘，也不让旧信念僵化——更新是认知的呼吸。", tags: ["决策", "贝叶斯更新"] },
    { id: "seed-6", date: "8月20日", text: "熵增定律提醒：秩序是逆流而上。笔记不复习会失忆，关系不维护会疏远——维持秩序，就是持续输入能量。", tags: ["系统论", "熵增定律"] },
    { id: "seed-5", date: "8月19日", text: "涌现：水分子不湿，水才湿。意识从神经元中涌现，文化从个体中涌现——整体总拥有部分所没有的性质。", tags: ["系统论", "涌现"] },
    { id: "seed-1", date: "8月19日", text: "知识的复利不在于数量，而在于网络密度——每张新卡片的价值，等于它能连接的旧卡片数量。复利效应，在认知网络里同样成立。", tags: ["增长", "复利效应", "网络效应"] }
  ];

  var nodeById = {};
  GRAPH_NODES.forEach(function (n) { nodeById[n.id] = n; });

  var adjacency = {};
  GRAPH_NODES.forEach(function (n) { adjacency[n.id] = []; });
  GRAPH_EDGES.forEach(function (e) {
    adjacency[e[0]].push(e[1]);
    adjacency[e[1]].push(e[0]);
  });

  /* ------------------------------------------------------------------
   * 二、知识图谱
   * ------------------------------------------------------------------ */

  var graphSvg = $("#graphSvg");
  var edgesGroup = $("#graphEdges");
  var nodesGroup = $("#graphNodes");
  var graphPanel = $("#graphPanel");
  var currentSelection = "cognition";

  function el(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, attrs[key]);
      }
    }
    return node;
  }

  function renderGraph() {
    /* 连线 */
    GRAPH_EDGES.forEach(function (e) {
      var a = nodeById[e[0]];
      var b = nodeById[e[1]];
      var line = el("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        "class": "graph-edge" + (e[2] === "cross" ? " graph-edge--cross" : ""),
        "data-a": e[0], "data-b": e[1]
      });
      edgesGroup.appendChild(line);
    });

    /* 节点 */
    GRAPH_NODES.forEach(function (n, i) {
      var group = el("g", {
        "class": "graph-node",
        "data-id": n.id,
        tabindex: "0",
        role: "button",
        "aria-label": "概念：" + n.label
      });
      group.style.setProperty("--fd", (i * 0.55 % 5).toFixed(2) + "s");
      group.style.setProperty("--hd", (i * 0.8 % 4).toFixed(2) + "s");

      var inner = el("g", { "class": "graph-node-inner" });

      inner.appendChild(el("circle", {
        cx: n.x, cy: n.y, r: n.r * 1.45, "class": "node-halo"
      }));

      var circleClass = n.tier === "hub" ? "node-core" : (n.tier === "sat" ? "node-sat" : "node-leaf");
      inner.appendChild(el("circle", { cx: n.x, cy: n.y, r: n.r, "class": circleClass }));

      var labelY, labelClass;
      if (n.tier === "hub") { labelY = n.y + 5; labelClass = "node-label--hub"; }
      else if (n.tier === "sat") { labelY = n.y + 4.5; labelClass = "node-label--sat"; }
      else { labelY = n.y + n.r + 17; labelClass = "node-label--leaf"; }

      inner.appendChild(el("text", {
        x: n.x, y: labelY, "class": "node-label " + labelClass
      })).textContent = n.label;

      group.appendChild(inner);
      nodesGroup.appendChild(group);

      group.addEventListener("click", function (ev) {
        ev.stopPropagation();
        selectNode(n.id);
      });
      group.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectNode(n.id);
        }
      });
    });

    /* 点击空白回到枢纽 */
    graphSvg.addEventListener("click", function () {
      selectNode("cognition");
    });
  }

  function selectNode(id) {
    if (!nodeById[id]) return;
    currentSelection = id;
    var neighbors = adjacency[id] || [];

    $$(".graph-node", nodesGroup).forEach(function (g) {
      var nid = g.getAttribute("data-id");
      g.classList.remove("selected", "dim");
      if (nid !== id && neighbors.indexOf(nid) === -1) g.classList.add("dim");
      if (nid === id) g.classList.add("selected");
    });

    $$(".graph-edge", edgesGroup).forEach(function (line) {
      var a = line.getAttribute("data-a");
      var b = line.getAttribute("data-b");
      line.classList.remove("lit", "dim");
      if (a === id || b === id) line.classList.add("lit");
      else line.classList.add("dim");
    });

    renderPanel(nodeById[id], neighbors);
  }

  function renderPanel(node, neighbors) {
    var chips = neighbors.map(function (nid) {
      return '<button class="chip chip-outline chip--action" data-node="' + nid + '">' +
        escapeHtml(nodeById[nid].label) + "</button>";
    }).join("");

    graphPanel.innerHTML =
      '<div class="gp-head"><h3>' + escapeHtml(node.label) + '</h3>' +
      '<span class="chip chip-blue">' + escapeHtml(node.cat) + "</span></div>" +
      '<p class="gp-desc">' + escapeHtml(node.desc) + "</p>" +
      (neighbors.length
        ? '<div class="gp-links"><span class="gp-links-label">关联概念 · ' + neighbors.length +
          "</span><div class=\"gp-chips\">" + chips + "</div></div>"
        : "") +
      (node.modelsLink
        ? '<a class="gp-more" href="#models">在模型库查看 ' + MODELS.length + " 个模型 →</a>"
        : "") +
      (node.id !== "cognition"
        ? '<button class="gp-reset" data-node="cognition">↺ 返回中央枢纽</button>'
        : "");
  }

  /* 面板内的概念跳转（事件委托） */
  graphPanel.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-node]");
    if (btn) selectNode(btn.getAttribute("data-node"));
  });

  /* ------------------------------------------------------------------
   * 三、心智模型库
   * ------------------------------------------------------------------ */

  var modelsToolbar = $("#modelsToolbar");
  var modelsGrid = $("#modelsGrid");
  var currentFilter = "全部";

  function renderModelsToolbar() {
    var cats = ["全部"];
    MODELS.forEach(function (m) {
      if (cats.indexOf(m.cat) === -1) cats.push(m.cat);
    });
    modelsToolbar.innerHTML = cats.map(function (cat) {
      return '<button class="filter-chip' + (cat === currentFilter ? " active" : "") +
        '" data-filter="' + escapeHtml(cat) + '" role="tab" aria-selected="' + (cat === currentFilter) +
        '">' + escapeHtml(cat) + "</button>";
    }).join("");
  }

  function renderModels() {
    var list = MODELS.filter(function (m) {
      return currentFilter === "全部" || m.cat === currentFilter;
    });
    modelsGrid.innerHTML = list.map(function (m, i) {
      return '<article class="card card-hover model-card" style="animation-delay:' + (i * 0.045).toFixed(3) + 's">' +
        '<span class="model-idx">' + String(i + 1).padStart(2, "0") + "</span>" +
        '<span class="chip">' + escapeHtml(m.cat) + "</span>" +
        "<h3>" + escapeHtml(m.name) + "</h3>" +
        "<p>" + escapeHtml(m.desc) + "</p></article>";
    }).join("");
  }

  modelsToolbar.addEventListener("click", function (ev) {
    var chip = ev.target.closest("[data-filter]");
    if (!chip) return;
    currentFilter = chip.getAttribute("data-filter");
    renderModelsToolbar();
    renderModels();
  });

  /* ------------------------------------------------------------------
   * 四、知识卡片流
   * ------------------------------------------------------------------ */

  var noteGrid = $("#noteGrid");
  var noteInput = $("#noteInput");
  var noteTags = $("#noteTags");
  var noteAdd = $("#noteAdd");

  function loadNotes() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      return [];
    }
  }

  function saveNotes(notes) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(notes)); } catch (err) { /* 忽略写入失败 */ }
  }

  /* 自动识别卡片与图谱概念、心智模型的连接 */
  function detectLinks(text) {
    var links = [];
    GRAPH_NODES.forEach(function (n) {
      if (n.id !== "cognition" && text.indexOf(n.label) !== -1) {
        links.push({ type: "node", label: n.label, target: n.id });
      }
    });
    MODELS.forEach(function (m) {
      if (text.indexOf(m.name) !== -1) {
        links.push({ type: "model", label: m.name, target: m.cat });
      }
    });
    return links;
  }

  function renderNotes() {
    var userNotes = loadNotes();
    var all = userNotes.concat(SEED_NOTES);

    noteGrid.innerHTML = all.map(function (note, i) {
      var links = detectLinks(note.text);
      var tagsHtml = (note.tags || []).map(function (t) {
        return '<span class="note-tag">#' + escapeHtml(t) + "</span>";
      }).join("");
      var linksHtml = links.map(function (l) {
        return '<button class="note-link" data-link-type="' + l.type + '" data-target="' +
          escapeHtml(l.target) + '">' + escapeHtml(l.label) + " ↗</button>";
      }).join("");
      var isUser = String(note.id).indexOf("seed-") !== 0;

      return '<article class="note-card" style="animation-delay:' + (i * 0.06).toFixed(2) + 's">' +
        '<span class="note-date">' + escapeHtml(note.date) + "</span>" +
        (isUser
          ? '<button class="note-del" data-del="' + escapeHtml(note.id) + '" aria-label="删除卡片">' +
            '<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M1 1l10 10M11 1L1 11" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button>'
          : "") +
        '<p class="note-text">' + escapeHtml(note.text) + "</p>" +
        (tagsHtml ? '<div class="note-tags">' + tagsHtml + "</div>" : "") +
        (linksHtml ? '<div class="note-links">' + linksHtml + "</div>" : "") +
        "</article>";
    }).join("");
  }

  function addNote() {
    var text = (noteInput.value || "").trim();
    if (!text) {
      noteInput.focus();
      return;
    }
    var tags = (noteTags.value || "")
      .split(/[,，、\s]+/)
      .map(function (t) { return t.replace(/^#/, "").trim(); })
      .filter(function (t, i, arr) { return t && arr.indexOf(t) === i; })
      .slice(0, 5);

    var note = {
      id: "n" + Date.now().toString(36),
      date: new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric" }),
      text: text,
      tags: tags
    };

    var notes = loadNotes();
    notes.unshift(note);
    saveNotes(notes);

    noteInput.value = "";
    noteTags.value = "";
    noteInput.style.height = "auto";
    renderNotes();
    noteInput.focus();
  }

  noteAdd.addEventListener("click", addNote);

  document.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter" &&
        (ev.target === noteInput || ev.target === noteTags)) {
      addNote();
    }
  });

  /* 输入框自适应高度 */
  noteInput.addEventListener("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 120) + "px";
  });

  /* 卡片操作（事件委托）：删除 / 跳转连接 */
  noteGrid.addEventListener("click", function (ev) {
    var del = ev.target.closest("[data-del]");
    if (del) {
      var notes = loadNotes().filter(function (n) { return n.id !== del.getAttribute("data-del"); });
      saveNotes(notes);
      renderNotes();
      return;
    }

    var link = ev.target.closest("[data-link-type]");
    if (link) {
      if (link.getAttribute("data-link-type") === "node") {
        selectNode(link.getAttribute("data-target"));
        $("#graph").scrollIntoView({ behavior: "smooth" });
      } else {
        currentFilter = link.getAttribute("data-target");
        renderModelsToolbar();
        renderModels();
        $("#models").scrollIntoView({ behavior: "smooth" });
      }
    }
  });

  /* ------------------------------------------------------------------
   * 五、滚动显现 / 计数动画 / 导航高亮
   * ------------------------------------------------------------------ */

  function animateCounter(elm) {
    var target = parseInt(elm.getAttribute("data-counter"), 10) || 0;
    var duration = 1400;
    var start = null;

    function tick(now) {
      if (start === null) start = now;
      var p = Math.min((now - start) / duration, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      elm.textContent = Math.round(target * eased).toLocaleString("zh-CN");
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  var revealObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var elm = entry.target;
      if (elm.classList.contains("reveal")) elm.classList.add("revealed");
      if (elm.hasAttribute && elm.hasAttribute("data-counter")) animateCounter(elm);
      revealObserver.unobserve(elm);
    });
  }, { threshold: 0.18 });

  $$(".reveal").forEach(function (elm) { revealObserver.observe(elm); });
  $$("[data-counter]").forEach(function (elm) { revealObserver.observe(elm); });

  /* 导航高亮（scrollspy） */
  var spySections = ["graph", "models", "cycle", "cards", "paths", "index"];
  var spyObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var id = entry.target.id;
      $$("#navLinks a").forEach(function (a) {
        a.classList.toggle("active", a.getAttribute("data-spy") === id);
      });
    });
  }, { rootMargin: "-38% 0px -55% 0px" });

  spySections.forEach(function (id) {
    var section = document.getElementById(id);
    if (section) spyObserver.observe(section);
  });

  /* ------------------------------------------------------------------
   * 六、初始化
   * ------------------------------------------------------------------ */

  renderGraph();
  selectNode("cognition");
  renderModelsToolbar();
  renderModels();
  renderNotes();
})();
