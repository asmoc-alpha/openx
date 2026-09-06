"use strict";

/* OpenX Serve — 静态 mock 数据。
   Phase 1：在 Phase 2 接入真实后端 API 之前，先用 mock 让 UI 看起来活着。
   真实数据从 /api/agents、/api/sessions、/api/usage 等接口拿到后，会覆盖
   下面对应的全局变量（或直接通过 window.OXMock 暴露）。

   所有静态数据采用「设计稿快照」：和《Agent Web App - 三栏布局.pdf》当前
   帧一致，便于一眼对照。 */

const OXMock = {
  agents: [
    {
      name: "研究助手",
      icon: "R",
      color: "#6c7bff",
      badge: "12",
      active: true,
      status: "online",
    },
    {
      name: "代码工程师",
      icon: "</>",
      color: "#3fb950",
      badge: "",
      active: false,
      status: "online",
    },
    {
      name: "写作助手",
      icon: "✎",
      color: "#b58cff",
      badge: "",
      active: false,
      status: "offline",
    },
    {
      name: "数据分析",
      icon: "▤",
      color: "#4dd0e1",
      badge: "",
      active: false,
      status: "offline",
    },
    {
      name: "设计助理",
      icon: "✦",
      color: "#d9a441",
      badge: "",
      active: false,
      status: "offline",
    },
  ],

  sessions: [
    {
      session_id: "current",
      title: "多智能体协作的市场调研框架",
      tag: "研究",
      tag_color: "#6c7bff",
      time: "14:23",
      today: true,
      active: true,
    },
    {
      session_id: "py-pipe",
      title: "Python 数据管道优化与异常监控方案",
      tag: "代码",
      tag_color: "#3fb950",
      time: "10:24",
      today: true,
      active: false,
    },
    {
      session_id: "okr",
      title: "季度产品 OKR 复盘与下阶段规划",
      tag: "写作",
      tag_color: "#b58cff",
      time: "08:15",
      today: true,
      active: false,
    },
    {
      session_id: "interview",
      title: "B 端用户访谈纪要整理与洞察提炼",
      tag: "研究",
      tag_color: "#6c7bff",
      time: "昨天 16:42",
      today: false,
      active: false,
    },
    {
      session_id: "jd",
      title: "招聘 JD 优化与候选人评估矩阵",
      tag: "写作",
      tag_color: "#b58cff",
      time: "昨天 11:00",
      today: false,
      active: false,
    },
  ],

  currentTask: {
    title: "多智能体协作的市场调研框架",
    status: "正在执行",
    statusKey: "running",
    step: "步骤 2 / 4",
    progress: 60,
    progressLabel: "已完成 60%",
    eta: "预计还需 2 分钟",
  },

  workflowSteps: [
    {
      num: 1,
      name: "需求解析 · Coordinator",
      meta: "已完成 · 1.4s · 拆解为 3 个子任务",
      status: "done",
    },
    {
      num: 2,
      name: "并行采集 · 3 个领域 Agent",
      meta: "执行中 · 已耗时 8.2s · 预计 12s",
      status: "running",
    },
    {
      num: 3,
      name: "交叉验证 · 冲突检测",
      meta: "等待中 · 预计 3s",
      status: "pending",
    },
    {
      num: 4,
      name: "共识合成 · 洞察生成",
      meta: "等待中",
      status: "pending",
    },
  ],

  parallelAgents: [
    {
      name: "WebAgent",
      icon: "W",
      color: "#6c7bff",
      role: "网络情报 · 行业研究",
      stats: ["8 tokens", "2.1k", "3.8s"],
      status: "running",
    },
    {
      name: "InterviewAgent",
      icon: "I",
      color: "#b58cff",
      role: "用户访谈 · 痛点提取",
      stats: ["32 tokens", "1.8k", "4.2s"],
      status: "running",
    },
    {
      name: "CompetitorAgent",
      icon: "C",
      color: "#f0616d",
      role: "竞品监控 · 策略对比",
      stats: ["12 tokens", "1.5k", "5.1s"],
      status: "running",
    },
  ],

  user: {
    name: "林子昂",
    initials: "LZ",
    pro: true,
    usage: { used: 8420, total: 10000 },
  },

  chatSample: [
    {
      role: "user",
      content:
        "我想搭建一个多智能体协作的市场调研框架，针对 B 端 SaaS 产品的目标客户分析。" +
        "需要综合使用网络搜索、用户访谈、竞品分析三个工具，并能在执行、调研讨论到完整的协作流程和 Agent 角色分工。",
      meta: "14:23 · 58 tokens",
    },
    {
      role: "assistant",
      agent: "研究助手",
      model: "Atlas-Pro",
      enhance: "推理增强",
      time: "14:23",
      tokens: "58 tokens",
      duration: "耗时 12s",
      title: "针对 B 端 SaaS 市场调研的协作框架",
      body:
        "我已为您设计了一个由 3 个领域智能体 + 1 个协调者组成的多 Agent 协作系统，" +
        "遵循并行采集 → 交叉验证 → 共识合成三阶段流程。整套工作流平均端到端耗时约 " +
        "8-12 分钟，相比单 Agent 串行模式效率提升 3.2 倍。",
      sections: [
        {
          heading: "三阶段协作流程",
          body:
            "阶段一为并行采集，三个领域 Agent 同时启动并产出独立的原始素材包；" +
            "阶段二为交叉验证，协调者对原始素材进行去重与冲突检测；" +
            "阶段三为共识合成，由协调者汇总成结构化洞察报告。",
        },
      ],
      tools: [
        {
          name: "web_search",
          desc: "行业情报采集",
          status: "done",
          output: "2025 年 B 端 SaaS 市场规模、政策与趋势",
        },
        {
          name: "interview_insight",
          desc: "用户调研分析",
          status: "running",
          output: "",
        },
      ],
      attachments: [
        { icon: "📄", name: "市场调研报告.docx" },
        { icon: "🧠", name: "Atlas-Pro · 推理增强" },
      ],
    },
    {
      role: "user",
      content: "基于这个框架，把执行细节展开一下：每个 Agent 的输入契约、输出格式，" +
              "以及冲突检测的具体规则……",
      meta: "14:24 · 24 tokens",
    },
  ],
};