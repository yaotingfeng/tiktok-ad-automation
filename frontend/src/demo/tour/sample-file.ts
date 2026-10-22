// 素材上传演示：体验版里不需要真实视频。页面上任何“选择视频文件”的地方（选择本地素材、
// 重新选择文件等）都不再弹出系统文件框，直接放入示例视频。
// 文件名包含完整剧名（真实规则：搭建时按文件名里的剧名匹配素材），选的是演示数据里还没有
// 素材的剧，上传后能看到它们入库；内容只是占位字节，体验版不会真的传输。
export const SAMPLE_VIDEO_NAMES = [
  "Love After the Storm_01.mp4",
  "Love After the Storm_02.mp4",
  "The Doctor's Hidden Identity_01.mp4",
]

function sampleFiles() {
  const transfer = new DataTransfer()
  SAMPLE_VIDEO_NAMES.forEach((name, index) => {
    const bytes = new Uint8Array((2 + index) * 1024 * 1024)
    // MP4 文件头（ftyp），让按扩展名或文件头判断类型的逻辑都识别为视频。
    bytes.set([0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70, 0x6d, 0x70, 0x34, 0x32])
    transfer.items.add(new File([bytes], name, { type: "video/mp4" }))
  })
  return transfer.files
}

/** 把示例视频交给文件选择框，并触发页面的 change 处理。 */
export function giveSampleVideos(input: HTMLInputElement) {
  input.files = sampleFiles()
  input.dispatchEvent(new Event("change", { bubbles: true }))
}

const wantsVideo = (input: HTMLInputElement) =>
  input.type === "file" &&
  (input.accept.includes("video") || input.id === "material-files")

/** 体验版启动时调用：视频文件选择框被打开时改为直接放入示例视频。 */
export function installSampleVideoPicker() {
  const click = HTMLInputElement.prototype.click
  HTMLInputElement.prototype.click = function sampleClick(
    this: HTMLInputElement,
  ) {
    if (wantsVideo(this)) giveSampleVideos(this)
    else click.call(this)
  }
  const showPicker = HTMLInputElement.prototype.showPicker
  if (showPicker)
    HTMLInputElement.prototype.showPicker = function samplePicker(
      this: HTMLInputElement,
    ) {
      if (wantsVideo(this)) giveSampleVideos(this)
      else showPicker.call(this)
    }
}
