# FRONTEND_UI_SUMMARY

> 复核日期：2026-09-09
> 范围：`frontend/`。重点阅读 `frontend/src/styles.css`、`frontend/src/queue.css`、`frontend/package.json`，并对 `frontend/src/main.tsx` 定向搜索应用壳、导航、`PageHeader`、页面组件和主要 `className`。本文件是静态代码审计，不包含运行时截图或真实数据验证。

## 1. 总体结论

当前前端是一个手写 React/Vite 管理台：信息模型已经覆盖“企业—岗位—招聘事件—来源证据—导入/处理队列—求职进度”，但交互层仍以“页面 + 白色卡片 + 小字号辅助信息”为主。最值得优先改善的是导航的信息架构、窄屏可用性、操作层级、焦点/无障碍反馈，以及设置与处理队列的高密度信息呈现。

## 2. 页面结构

### 应用壳

`App` 通过页面状态切换视图，而不是独立路由。`.app-shell` 包含固定 `.sidebar` 和 `.content`；打开企业后，`CompanyDetailShell` 会覆盖当前列表页。`PageHeader` 统一提供 eyebrow、标题、描述和右侧操作区。

### 导航与页面

| 页面 | 当前结构 |
| --- | --- |
| 企业与岗位 | `.metrics` 指标 → `.toolbar` 搜索/行业筛选/排序 → `.company-grid` 企业卡片；管理员可进入企业选择、合并、删除、概览检索和导出。 |
| 企业详情 | 返回按钮 → 概览、公开信息核查、企业资料、专业需求、招聘公共信息、时间轴、岗位；右侧为来源与证据。管理员可通过 `CompanyEditor` 编辑企业资料。 |
| 招聘时间轴 | `TimelinePage` 支持列表/周视图、事件类型筛选、上一周/本周/下一周；`.timeline-event` 支持展开详情。 |
| 求职进度 | `ApplicationsPage` 使用四列 `.kanban`：感兴趣、已投递、面试中、Offer；岗位由 `ApplicationColumn` 渲染。 |
| 导入招聘信息 | `ImportPage` 的 `.import-grid` 包含群聊文本、公开 URL/文件、群聊记录三条入口；群聊记录通过 `TraceMemoGroups` 筛群，再勾选消息导入。 |
| 管理台/待审核 | `AdminPage` 管理邀请和管理员工作流；`ReviewPage` 展示原始消息、处理任务、日志、模型/审核结果和完整载荷。 |
| 处理队列 | `QueuePage` 展示队列控制、状态指标、筛选/批量取消、任务原文、来源引用、子任务、错误和阶段日志。 |
| 系统设置 | `SettingsPage` 的 `.settings-grid` 包含同步隐私、处理器并发、TraceMemo、通用模型 API、AList WebDAV、SMTP、Agent API；`LocalStoragePanel` 负责本地数据和备份。另有账户安全页。 |

普通用户导航包含企业、时间轴、求职进度、账户安全；管理员额外看到导入、管理台、处理队列、系统设置和待审核。当前管理台也提供进入设置/审核的重复入口。

## 3. 主要组件与样式系统

- 应用级：`App`、`NavButton`、`PageHeader`、`Metric`、`AuthFrame`。
- 企业/岗位：`CompaniesPage`、`CompanyCard`、`CompanyDetailShell`、`CompanyViewBase`、`CompanyView`、`CompanyEditor`、`JobRow`、`EvidenceCard`。
- 过程视图：`TimelinePage`/`TimelineEventCard`、`ApplicationsPage`/`ApplicationColumn`、`ImportPage`、`TraceMemoGroups`、`QueuePage`、`ReviewPage`。
- 管理配置：`AdminPage`、`SettingsPage`、`LocalStoragePanel`、`AccountSecurityPage`。
- 核心复用 class：`.detail-card`、`.section-title`、`.primary`、`.secondary`、`.filter`、`.status`、`.empty-state`、`.setting-help`。队列专用样式集中在 `queue.css` 的 `.queue-item`、`.queue-current-step`、`.queue-subtasks`、`.queue-meta`、`.log-line`。

## 4. 当前视觉语言

- 冷色 SaaS 工作台：深蓝渐变侧栏，浅灰蓝背景，靛蓝/紫色为主色。
- 白色圆角卡片、细边框、轻阴影、胶囊状态标签；绿色/橙色/红色表达成功、警告和错误。
- 字体为 Inter/system/微软雅黑；图标是 Unicode 字符，不依赖图标库。
- `package.json` 只有 React 19、ReactDOM、Vite 和 TypeScript；脚本为 `dev`、`typecheck`、`build`，没有组件库或专门的测试脚本。
- 响应式断点为 `1000px`、`680px`：中屏收窄侧栏并改为单列详情，小屏侧栏压缩到 64px，设置/导入变单列。

## 5. 主要 UX 问题

1. **导航层级混杂。** 普通用户入口和管理员运维入口连续排列，没有分组、分隔或权限说明；管理台与设置/审核存在重复入口。
2. **移动端导航不可发现。** `max-width:680px` 时 `.nav-button` 的文字被设为 `font-size:0`，只保留 Unicode 图标；`NavButton` 没有明确的 `aria-label` 或 `title`。
3. **键盘与焦点反馈不足。** 样式中主要只有输入框的 `:focus`，没有统一的 `:focus-visible`；按钮、卡片、导航和可展开内容的键盘状态不够明确。
4. **操作区过载。** 企业页和队列页的 `PageHeader` 同时承载多个按钮；`.header-actions` 只负责排列/换行，没有主次分层或溢出策略。
5. **卡片密度过高且语义相同。** `.detail-card` 同时承担企业事实、证据、设置、队列和审核载荷容器，长页面滚动成本高，关键动作容易被淹没。
6. **设置页风险操作集中。** 一个页面同时包含模型、密钥、备份、SMTP、清除缓存/聊天记录等高影响操作；保存、测试、清除的反馈机制不完全一致，部分错误使用浏览器 `alert`。
7. **窄屏看板仍为两列。** 小屏 `.kanban` 仍保持 `1fr 1fr`，在有效宽度较小时岗位卡片和标题容易拥挤。
8. **反馈分散。** 全局 `.notice`/`.error-banner`、行内 `.setting-help`、`.form-error` 和 `alert` 并存；固定右上角提示也可能遮挡页面头部。
9. **队列信息密集。** `.queue-item` 同时展示状态、当前步骤、原文、来源、子任务、错误、元数据和日志，当前步骤与最重要的下一步操作不够突出。

## 6. 最值得修改的 class / 组件

按优先级建议：

1. `NavButton`、`.sidebar`、`.nav-button`：增加用户/管理员分组、移动端可识别标签、`aria`、键盘焦点和更明确的当前项。
2. `PageHeader`、`.header-actions`、`.primary`/`.secondary`：建立主操作、次操作、危险操作和“更多”层级，统一加载/禁用/成功/失败反馈。
3. `.content`、`.detail-card`、`.settings-grid`、`.kanban`：降低长页卡片密度，重新设计 680px 以下的布局和看板列行为。
4. `CompaniesPage`、`CompanyCard`、`CompanyView`、`JobRow`：强化搜索筛选结果、岗位层级、收藏/关注和证据入口，减少企业详情的信息堆叠。
5. `ImportPage`、`.import-grid`、`.dropzone`、`.import-message-list`：把“选群 → 读消息 → 选消息 → 入队”做成明确的阶段流程，并固定主要提交反馈。
6. `SettingsPage`、`TraceMemoGroups`、`LocalStoragePanel`：按任务拆分设置，隔离密钥与破坏性操作，提供分区保存和明确的影响说明。
7. `QueuePage`、`queue.css`：优先突出状态/当前步骤/下一动作，再折叠原文、来源、子任务和日志；移动端保留可扫描的最小信息。
