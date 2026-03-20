/**
 * 临时存储待上传的文件和需求
 * 用于首页点击启动引擎后立即跳转，在Process页面再进行API调用
 */
import { reactive } from 'vue'

const state = reactive({
  files: [],
  simulationRequirement: '',
  simulationMode: 'social',
  isPending: false
})

export function setPendingUpload(files, requirement, simulationMode = 'social') {
  state.files = files
  state.simulationRequirement = requirement
  state.simulationMode = simulationMode
  state.isPending = true
}

export function getPendingUpload() {
  return {
    files: state.files,
    simulationRequirement: state.simulationRequirement,
    simulationMode: state.simulationMode,
    isPending: state.isPending
  }
}

export function clearPendingUpload() {
  state.files = []
  state.simulationRequirement = ''
  state.simulationMode = 'social'
  state.isPending = false
}

export default state
