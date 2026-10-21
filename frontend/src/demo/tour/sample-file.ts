// 素材上传演示：体验者不需要准备视频，引导卡片上的按钮生成一个示例文件并交给页面的文件选择框。
// 文件名包含完整剧名（真实规则：搭建时按文件名里的剧名匹配素材）；内容只是占位字节，体验版不会真的传输。
export const SAMPLE_VIDEO_NAME = "Love After the Storm_01.mp4"

export function chooseSampleVideo(inputSelector = "input#material-files") {
  const input = document.querySelector<HTMLInputElement>(inputSelector)
  if (!input) return false
  const bytes = new Uint8Array(3 * 1024 * 1024)
  // MP4 文件头（ftyp），让按扩展名或文件头判断类型的逻辑都识别为视频。
  bytes.set([0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70, 0x6d, 0x70, 0x34, 0x32])
  const file = new File([bytes], SAMPLE_VIDEO_NAME, { type: "video/mp4" })
  const transfer = new DataTransfer()
  transfer.items.add(file)
  input.files = transfer.files
  input.dispatchEvent(new Event("change", { bubbles: true }))
  return true
}
