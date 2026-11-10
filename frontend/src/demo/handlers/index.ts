// 体验版假后端入口：导入即注册全部领域的处理函数。
import "./session"
import "./accounts-providers"
import "./materials"
import "./builds"
import "./builds-submissions"
import "./strategies-admin"
import "./ads"
import "./ads-management"
import "./campaign-copies"
import "./automation"
import "./image-composition"
import "./carousel-presets"

// 演示工装与注册副作用共享同一模块图，避免开发热更新产生第二份注册表。
export { DemoReply, resolveDemoRoute } from "../runtime/registry"
