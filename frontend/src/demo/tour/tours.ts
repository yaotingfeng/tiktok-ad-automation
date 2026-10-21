import { chooseOptions, fillField } from "./actions"
import { button, byText, navItem } from "./find"
import { chooseSampleVideo } from "./sample-file"
import type { Tour, TourContext, TourStep } from "./types"

// 体验版引导内容。主线按真实业务顺序带完整流程；每页讲解覆盖主线没有展开的功能。
// 定位都用页面上可见的文字，生产页面改文案时需要同步这里（端到端测试会发现）。
const page =
  (path = "") =>
  (c: TourContext) =>
    `/tenants/${c.tenantId}${path}?bc_id=${c.bcId}`
const main = "main"
const dialog = '[role="dialog"]'
const inDialog = (selector: string, text: string | RegExp) =>
  byText(selector, text, { within: dialog })
const tab = (label: string) => byText('[role="tab"]', label, { exact: true })
const firstRowCheckbox = () =>
  document.querySelector(
    'main tbody tr [role="checkbox"], main tbody tr input[type="checkbox"]',
  )

/** 页面上可见的表格里出现了某段文字（不看筛选项、隐藏页签等内容）。 */
const tableHas = (text: string | RegExp) => () => {
  const content = Array.from(document.querySelectorAll("main tbody"))
    .filter((el) => (el as HTMLElement).offsetParent !== null)
    .map((el) => el.textContent ?? "")
    .join(" ")
  return typeof text === "string" ? content.includes(text) : text.test(content)
}

const DEMO_DRAMAS_INPUT = [
  "The Billionaire's Secret Bride",
  "Reborn as the Alpha's Mate",
].join("\n")
const DEMO_ACCOUNTS_INPUT = ["7431000000000001001", "7431000000000001002"].join(
  "\n",
)

const mainSteps: TourStep[] = [
  {
    title: "欢迎体验 TK-ADA",
    body: "TK-ADA 是 TikTok 短剧出海的批量投放工具。接下来按日常工作顺序走一遍：\n授权账户 → 版权方与推广链接 → 素材 → 投放策略 → 批量搭建 → 看报表、调广告 → 自动规则。\n\n每一步都会高亮要点的位置，跟着点就行；页面上的数据都是演示数据。",
    path: page("/accounts"),
  },
  {
    title: "第 1 步 · 广告账户",
    body: "通过 TikTok 授权后，BC 下的广告账户自动同步到这里：能否搭建、能否上传素材、平台状态一目了然。\n顶部可以切换租户和 BC，所有页面都按当前 BC 隔离。",
    path: page("/accounts"),
    target: main,
  },
  {
    title: "授权管理",
    body: "点“授权管理”，看看 TikTok 官方 API 与 MCP 两种授权连接。",
    target: tab("授权管理"),
    advance: "click",
  },
  {
    title: "授权连接",
    body: "一次授权即可接入多个 BC。授权、同步、停用都在这里完成；搭建时使用 BC 的默认连接。",
    target: main,
  },
  {
    title: "第 2 步 · 版权方连接",
    body: "接入网眼、嘉书、刚刚好、兑吧等版权方后台。搭建时系统会自动去版权方取推广链接，不用手工复制。\n点左侧“版权方连接”。",
    target: navItem("版权方连接"),
    advance: "click",
  },
  {
    title: "已接入的版权方",
    body: "每个连接都可以查看已发现的推广应用、编辑凭据、重新验证。",
    target: main,
  },
  {
    title: "推广链接记录",
    body: "点“链接记录”，查看系统已经取到的每部剧的推广链接。",
    target: tab("链接记录"),
    advance: "click",
  },
  {
    title: "第 3 步 · 素材库",
    body: "素材统一上传、统一管理。文件名里写上完整剧名，搭建时就能自动匹配到对应的剧。\n点左侧“素材库”。",
    target: navItem("素材库"),
    advance: "click",
  },
  {
    title: "上传素材",
    body: "点“批量上传”，演示一次素材上传。",
    target: button("批量上传", true),
    advance: "click",
    settleMs: 800,
  },
  {
    title: "选择素材文件",
    body: "正式使用时在这里选择本地视频（一次最多 2 万个）。\n体验版不需要准备文件：点下面的按钮，用示例视频“Love After the Storm_01.mp4”。",
    target: dialog,
    action: {
      label: "使用示例视频",
      run: () => {
        chooseSampleVideo()
      },
    },
  },
  {
    title: "开始上传",
    body: "点“开始上传”。",
    target: inDialog("button", /开始上传/),
    advance: "click",
    settleMs: 1200,
  },
  {
    title: "上传进度",
    body: "原文件先临时中转，再由系统自动安排合法的素材账户上传到 TikTok，最后核实入库。等几秒，状态会依次变化，直到“账户素材可用”。",
    target: main,
    readyWhen: tableHas("账户素材可用"),
  },
  {
    title: "第 4 步 · 投放策略",
    body: "把预算、出价、定向、分组方式、命名规则保存成策略，搭建时一键套用，团队口径统一。\n点左侧“投放策略”。",
    target: navItem("投放策略"),
    advance: "click",
  },
  {
    title: "策略列表",
    body: "每次修改都会保存为新版本，历史版本可追溯。可以新建、编辑、复制为新策略。",
    target: main,
  },
  {
    title: "第 5 步 · 批量搭建",
    body: "核心功能：一次把多部剧投到多个账户。\n点左侧“广告搭建”。",
    target: navItem("广告搭建"),
    advance: "click",
  },
  {
    title: "选择连接、应用和策略",
    body: "选择这批剧从哪个版权方取链、用哪个推广应用、套用哪套投放策略。\n点下面的按钮一键选好。",
    target: byText("label", "版权方连接"),
    action: {
      label: "一键选择",
      run: () =>
        chooseOptions([
          ["版权方连接", "网眼"],
          ["推广应用"],
          ["投放策略", "美区 · 目标 ROAS 1.1"],
        ]),
    },
  },
  {
    title: "填写剧目和账户",
    body: "每行一部剧名、每行一个账户 ID。所有剧都会投到所有账户。\n点下面的按钮填入示例：2 部剧 × 2 个账户。",
    target: "#build-drama",
    action: {
      label: "填入示例剧目和账户",
      run: () => {
        fillField("#build-drama", DEMO_DRAMAS_INPUT)
        fillField("#build-account", DEMO_ACCOUNTS_INPUT)
      },
    },
  },
  {
    title: "解析并准备",
    body: "点“解析并准备”：系统核实剧名和账户，自动去版权方取推广链接，并按剧名匹配素材。这一步还不会创建广告。",
    target: button("解析并准备", true),
    advance: "click",
    settleMs: 3500,
  },
  {
    title: "准备结果",
    body: "每部剧的推广链接、匹配到的素材数量都在这里，可以逐个调整链接和素材。",
    target: main,
    readyWhen: () => !!button("选择小程序", true)(),
  },
  {
    title: "选择推广小程序",
    body: "点“选择小程序”，选这批剧实际播放的小程序。",
    target: button("选择小程序", true),
    advance: "click",
    settleMs: 800,
  },
  {
    title: "选一个小程序",
    body: "点第一个“选择并继续”。系统会检查每个目标账户是否可用。",
    target: inDialog("button", "选择并继续"),
    advance: "click",
    settleMs: 2500,
  },
  {
    title: "选择投放身份",
    body: "点“选择投放身份”，选广告展示用的 TikTok 账号。",
    target: button("选择投放身份", true),
    advance: "click",
    settleMs: 800,
  },
  {
    title: "选一个身份",
    body: "点“Velvet Reels”。系统会逐个账户验证这个身份。",
    target: inDialog(
      "button, [role=option], li, div[tabindex]",
      "Velvet Reels",
    ),
    advance: "click",
    settleMs: 2500,
  },
  {
    title: "生成搭建预览",
    body: "定向、排期都已按策略带出，也可以针对这批单独修改。点“生成搭建预览”。",
    target: button("生成搭建预览", true),
    advance: "click",
    settleMs: 4000,
  },
  {
    title: "搭建预览",
    body: "提交前精确列出要创建多少系列、广告组、广告，每个账户的预算合计，以及因为什么原因排除了哪些组合。确认无误再提交。",
    target: main,
    readyWhen: () => {
      const submit = document.querySelector<HTMLButtonElement>(
        'button[aria-label^="创建并立即启用"]',
      )
      return !!submit && !submit.disabled
    },
  },
  {
    title: "提交搭建",
    body: "点“创建并立即启用”。提交后系统直接在 TikTok 创建并启用广告，不需要再手动开启。",
    target: button("创建并立即启用", true),
    advance: "click",
    settleMs: 2500,
  },
  {
    title: "搭建任务",
    body: "后台逐个账户执行，进度、成功和失败数量实时更新；失败的部分可以一键重试或核查。等几秒看看进度变化。",
    target: main,
  },
  {
    title: "第 6 步 · 广告报表",
    body: "所有账户的投放数据汇总在一张报表里。\n点左侧“广告报表”。",
    target: navItem("广告报表"),
    advance: "click",
  },
  {
    title: "核心指标",
    body: "消耗、ROAS、展示、点击率、CPM 等指标按所选日期汇总。下面可以按账户、系列、广告组、广告、素材、剧六个维度分析。",
    target: main,
  },
  {
    title: "按系列查看",
    body: "点“系列”页签。",
    target: tab("系列"),
    advance: "click",
    settleMs: 1500,
  },
  {
    title: "勾选要调整的广告",
    body: "勾选第一行。勾选后可以批量启用、暂停、修改预算、修改目标 ROAS、改名。",
    target: firstRowCheckbox,
    advance: "click",
  },
  {
    title: "批量修改预算",
    body: "点“修改预算”。提交前会先预览每个对象的“原值 → 新值”，确认后才真正修改。",
    target: button("修改预算", true),
    advance: "click",
    settleMs: 1500,
  },
  {
    title: "设置新预算",
    body: "填入新的日预算（例如 250），确认预览无误后点“提交管理任务”。\n也可以点“下一步”跳过。",
    target: dialog,
  },
  {
    title: "操作记录",
    body: "每一次批量修改都有记录：谁、什么时候、改了哪些对象、平台回执是否成功。点“查看明细”可以看到每个对象的平台回执。",
    path: page("/ad-management-tasks"),
    target: main,
  },
  {
    title: "第 7 步 · 自动规则",
    body: "让系统按规则盯盘：ROAS 过低自动暂停、达标自动提预算、夜间降预算……\n点左侧“自动规则”。",
    target: navItem("自动规则"),
    advance: "click",
  },
  {
    title: "规则与执行模式",
    body: "每条规则可以选择“仅通知”“确认后执行”或“自动执行”，并按固定频率运行。新建规则时可以先试运行，看看会命中哪些对象。",
    target: main,
  },
  {
    title: "待确认",
    body: "点“待确认”：需要人工确认的规则命中会在这里排队，可以勾选后确认执行或忽略。",
    target: tab("待确认"),
    advance: "click",
  },
  {
    title: "体验完成",
    body: "主流程就到这里。左上角的“本页怎么用”可以单独查看每个页面的更多功能；点“重置”回到初始数据重新体验。",
  },
]

export const MAIN_TOUR: Tour = {
  id: "main",
  title: "主线引导",
  steps: mainSteps,
}

const pageTour = (id: string, title: string, steps: TourStep[]): Tour => ({
  id,
  title,
  steps,
})

const PAGE_TOURS: { match: RegExp; tour: Tour }[] = [
  {
    match: /\/accounts$/,
    tour: pageTour("accounts", "账户与授权", [
      {
        title: "广告账户目录",
        body: "这里列出当前 BC 下已同步的广告账户，可按名称或 ID、平台状态、可用性筛选。“可搭建”“可上传”表示该账户能否用于建广告、上传素材。",
        target: main,
      },
      {
        title: "账户详情",
        body: "点任意一行的“查看详情”可以看到账户的币种、时区、权限和最近核验时间。",
        target: button("查看详情", true),
      },
      {
        title: "授权管理",
        body: "“授权管理”页签管理 TikTok 授权连接：新增授权、为 BC 设置默认连接、手动同步账户、停用连接。",
        target: tab("授权管理"),
      },
    ]),
  },
  {
    match: /\/providers$/,
    tour: pageTour("providers", "版权方连接", [
      {
        title: "版权方连接",
        body: "每个连接对应一个版权方后台账号。系统会定期验证连接，取链时自动恢复过期会话。",
        target: main,
      },
      {
        title: "新增连接",
        body: "点“新增连接”选择版权方类型并填写账号即可接入。",
        target: button("新增连接", true),
      },
      {
        title: "推广应用",
        body: "“查看已发现应用”列出该连接下的推广应用，可为每个应用设置默认的起播集、卡点集等取链参数。",
        target: button("查看已发现应用", true),
      },
      {
        title: "链接记录",
        body: "“链接记录”页签保存每部剧已取得的推广链接，搭建时会优先复用。",
        target: tab("链接记录"),
      },
    ]),
  },
  {
    match: /\/materials$/,
    tour: pageTour("materials", "素材库", [
      {
        title: "素材列表",
        body: "所有已上传素材：来源 BC、文件信息、平台入库状态、上传账户和可用账户数。可按文件名、状态、日期筛选。",
        target: main,
      },
      {
        title: "批量上传",
        body: "点“批量上传”选择本地视频。体验版可以在引导里使用示例视频。",
        target: button("批量上传", true),
      },
      {
        title: "上传队列",
        body: "“上传队列”页签查看每一批导入的进度，中断的上传可以继续或重选文件。",
        target: tab("上传队列"),
      },
    ]),
  },
  {
    match: /\/strategies(\/new)?$/,
    tour: pageTour("strategies", "投放策略", [
      {
        title: "策略列表",
        body: "每套策略包含预算、出价、广告组与广告的生成规则、定向、命名模板和文案池。",
        target: main,
      },
      {
        title: "新建策略",
        body: "点“新建策略”，右侧会实时演示按当前规则会生成多少系列、广告组和广告。",
        target: button("新建策略"),
      },
    ]),
  },
  {
    match: /\/builds\/new$/,
    tour: pageTour("builds-new", "广告搭建", [
      {
        title: "三步完成批量搭建",
        body: "批量输入 → 准备与调整 → 搭建预览。准备阶段自动取链、匹配素材，预览确认后才会提交。",
        target: main,
      },
      {
        title: "草稿箱",
        body: "没做完的搭建会保存在“草稿箱”，可以随时继续。",
        target: button("草稿箱", true),
      },
    ]),
  },
  {
    match: /\/build-tasks$/,
    tour: pageTour("build-tasks", "搭建任务", [
      {
        title: "搭建任务",
        body: "每次提交都是一个任务，可以按状态筛选：进行中、需处理、已完成。点进任务查看每个剧 × 账户组合的结果。",
        target: main,
      },
    ]),
  },
  {
    match: /\/ads$/,
    tour: pageTour("ads", "广告报表", [
      {
        title: "日期与搜索",
        body: "选择日期范围、按名称/剧名/版权方/ID 搜索，可以把常用筛选保存起来。",
        target: button("保存筛选", true),
      },
      {
        title: "六个维度",
        body: "账户、系列、广告组、广告、素材、剧，同一套指标从不同角度看。",
        target: tab("系列"),
      },
      {
        title: "列设置",
        body: "“列设置”可以选择要看的指标和顺序。",
        target: button("列设置", true),
      },
      {
        title: "刷新与导出",
        body: "“刷新”立即同步最新数据，“导出”生成报表文件下载。",
        target: button("导出", true),
      },
    ]),
  },
  {
    match: /\/ad-management-tasks$/,
    tour: pageTour("ad-management-tasks", "操作记录", [
      {
        title: "操作记录",
        body: "所有批量启停、改预算、改出价、改名的执行记录，包括人工和自动规则触发的。点“查看明细”看每个对象的平台回执。",
        target: main,
      },
    ]),
  },
  {
    match: /\/automation-rules$/,
    tour: pageTour("automation-rules", "自动规则", [
      {
        title: "执行概览",
        body: "顶部统计最近的执行动作、待确认、失败和冲突数量。",
        target: main,
      },
      {
        title: "新建规则",
        body: "可以从模板新建，也可以自定义条件、动作、范围和运行频率；保存前可以试运行。",
        target: button("从模板新建", true),
      },
      {
        title: "待确认与执行记录",
        body: "“待确认”处理需要人工确认的命中，“执行记录”查看每次运行的结果。",
        target: tab("执行记录"),
      },
    ]),
  },
  {
    match: /\/notifications$/,
    tour: pageTour("notifications", "通知设置", [
      {
        title: "飞书通知",
        body: "配置飞书机器人地址后，自动规则的预警和执行结果会推送到群里，可以先发测试消息验证。",
        target: main,
      },
    ]),
  },
  {
    match: /\/members$/,
    tour: pageTour("members", "成员管理", [
      {
        title: "成员与角色",
        body: "租户管理员、投手、只读成员三种角色，权限按租户隔离。可以添加已有用户或新建用户。",
        target: main,
      },
    ]),
  },
  {
    match: /^\/platform\/tenants$/,
    tour: pageTour("platform", "平台管理", [
      {
        title: "多租户",
        body: "平台管理员在这里开通租户、设置默认 BC、停用租户，并可“进入租户”代管操作。",
        target: main,
      },
    ]),
  },
]

export function pageTourFor(pathname: string) {
  return PAGE_TOURS.find(({ match }) => match.test(pathname))?.tour ?? null
}
